"""Planner agent.

Decides HOW to solve the request. It never calls an API — it emits an ordered
list of steps that the graph executes through the tool layer, which also means
/admin can show a reviewer what the agent intended to do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.llm import client as llm
from app.tools import registry

log = logging.getLogger(__name__)

SYSTEM = """You are the planning agent for Northbridge Components customer support.

You do not talk to the customer and you do not call tools. You produce an
ordered execution plan for the specialist agent that will.

Rules:
- Every plan starts by establishing facts (look up the order or payment) before
  anything that writes. Never plan a write against an order you have not read.
- Only use tools from the provided list. If a step needs a tool that is not
  listed, describe it as a "verify" or "explain" step instead.
- Plan the happy path. Do not plan the denial branch — the policy engine decides
  that at execution time and the agent explains it afterwards.
- Keep it to at most 6 steps. Fewer is better.
- If the request cannot proceed without information the customer has not given
  (which order, which variant), the first step is to ask.

Output exactly:
{"steps": [{"n": 1, "action": "short imperative", "tool": "Tool.Name or null",
            "why": "one clause"}],
 "missing_information": ["what you still need from the customer"],
 "summary": "one sentence describing the approach"}"""


@dataclass
class Plan:
    steps: list[dict] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def blocked_on_customer(self) -> bool:
        return bool(self.missing_information)

    def render(self) -> str:
        if not self.steps:
            return "(no plan)"
        return "\n".join(
            f"{s.get('n', i)}. {s.get('action', '')}"
            + (f"  [{s['tool']}]" if s.get("tool") else "")
            + (f"  — {s['why']}" if s.get("why") else "")
            for i, s in enumerate(self.steps, 1)
        )

    def as_dict(self) -> dict:
        return {
            "steps": self.steps,
            "missing_information": self.missing_information,
            "summary": self.summary,
        }


def build(
    session: Session,
    *,
    agent: str,
    message: str,
    intent: str,
    entities: dict,
    memory_block: str,
    policy_block: str,
    conversation_id: str | None = None,
) -> Plan:
    prompt = f"""Intent: {intent}
Known entities: {entities or '(none)'}

Customer said:
{message}

What we know about this customer:
{memory_block}

Relevant policy:
{policy_block}

Tools available to the {agent} agent:
{registry.describe_for_prompt(agent)}

Produce the execution plan."""

    response = llm.generate_json(
        prompt, system=SYSTEM, agent="planner",
        session=session, conversation_id=conversation_id, max_output_tokens=3000,
    )
    payload = response.raw_json or {}

    steps = [s for s in (payload.get("steps") or []) if isinstance(s, dict)]
    for step in steps:
        # A plan naming a tool the agent cannot reach would fail at execution and
        # look like the agent's fault. Strip it here and keep the intent.
        tool = step.get("tool")
        if tool and registry.canonical_name(tool) not in registry.AGENT_TOOLS.get(agent, []):
            log.info("planner named unavailable tool %r for %s", tool, agent)
            step["tool"] = None
            step["unavailable_tool"] = tool

    return Plan(
        steps=steps[:6],
        missing_information=[
            str(x) for x in (payload.get("missing_information") or []) if x
        ],
        summary=str(payload.get("summary", "")),
    )
