"""Metrics for the /admin dashboard.

Computed from the tables that already record the truth — `llm_calls`, `api_logs`,
`audit_logs`, `pending_actions` — rather than from counters kept alongside them,
so the numbers cannot drift from the rows they describe.

Targets are goals, not gates: each measurement is reported next to its target.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import models as m

# Kept next to the measurement so the frontend doesn't hardcode them.
TARGETS = {
    "automation_rate_pct": {"target": 70, "direction": "min"},
    "escalation_rate_pct": {"target": 30, "direction": "max"},
    "avg_response_ms": {"target": 1000, "direction": "max"},
    "p95_response_ms": {"target": 2500, "direction": "max"},
    "csat": {"target": 4.5, "direction": "min"},
}


@dataclass
class Metrics:
    data: dict

    def as_dict(self) -> dict:
        return self.data


def collect(session: Session) -> dict:
    conversations = session.scalar(select(func.count()).select_from(m.Conversation)) or 0
    assistant_turns = (
        session.scalar(
            select(func.count()).select_from(m.Message).where(m.Message.role == "assistant")
        )
        or 0
    )
    escalations = session.scalar(select(func.count()).select_from(m.PendingAction)) or 0
    pending = (
        session.scalar(
            select(func.count()).select_from(m.PendingAction)
            .where(m.PendingAction.status == "PENDING")
        )
        or 0
    )

    # LLM spend and latency
    llm_rows = session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(m.LlmCall.input_tokens), 0),
            func.coalesce(func.sum(m.LlmCall.output_tokens), 0),
            func.coalesce(func.sum(m.LlmCall.cost_micros), 0),
            func.coalesce(func.avg(m.LlmCall.latency_ms), 0),
            func.coalesce(func.avg(m.LlmCall.time_to_first_token_ms), 0),
        )
    ).one()
    llm_calls, in_tok, out_tok, cost_micros, avg_llm_ms, avg_ttft = llm_rows
    llm_failures = (
        session.scalar(
            select(func.count()).select_from(m.LlmCall).where(m.LlmCall.ok.is_(False))
        )
        or 0
    )

    # Per-turn latency: a turn is several model calls, and the customer waits for
    # all of them. Summing by turn is the number that matches their experience.
    per_turn = [
        row[0] for row in session.execute(
            select(func.sum(m.LlmCall.latency_ms))
            .where(m.LlmCall.conversation_id.is_not(None))
            .group_by(m.LlmCall.conversation_id, m.LlmCall.created_at)
        ).all()
        if row[0]
    ]

    tool_calls = session.scalar(select(func.count()).select_from(m.ApiLog)) or 0
    tool_failures = (
        session.scalar(
            select(func.count()).select_from(m.ApiLog).where(m.ApiLog.ok.is_(False))
        )
        or 0
    )
    avg_tool_ms = session.scalar(select(func.coalesce(func.avg(m.ApiLog.latency_ms), 0))) or 0

    policy_blocks = (
        session.scalar(
            select(func.count()).select_from(m.AuditLog)
            .where(m.AuditLog.event == "tool_blocked_by_policy")
        )
        or 0
    )
    injections = (
        session.scalar(
            select(func.count()).select_from(m.AuditLog)
            .where(m.AuditLog.event == "prompt_injection_detected")
        )
        or 0
    )

    csat_avg = session.scalar(
        select(func.avg(m.CrmTicket.csat_score)).where(m.CrmTicket.csat_score.is_not(None))
    )

    chunks = session.scalar(select(func.count()).select_from(m.KnowledgeChunk)) or 0
    embedded = (
        session.scalar(
            select(func.count()).select_from(m.KnowledgeChunk)
            .where(m.KnowledgeChunk.embedding.is_not(None))
        )
        or 0
    )

    automated = max(assistant_turns - escalations, 0)
    data = {
        "conversations": conversations,
        "assistant_turns": assistant_turns,
        "escalations": escalations,
        "pending_approvals": pending,
        "automation_rate_pct": _pct(automated, assistant_turns),
        "escalation_rate_pct": _pct(escalations, assistant_turns),
        "success_rate_pct": _pct(tool_calls - tool_failures, tool_calls),

        "llm_calls": llm_calls,
        "llm_failures": llm_failures,
        "input_tokens": int(in_tok),
        "output_tokens": int(out_tok),
        "cost_usd": round(int(cost_micros) / 1_000_000, 4),
        "avg_llm_call_ms": int(avg_llm_ms or 0),
        "avg_time_to_first_token_ms": int(avg_ttft or 0),
        "avg_response_ms": int(sum(per_turn) / len(per_turn)) if per_turn else 0,
        "p95_response_ms": _p95(per_turn),

        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "avg_tool_call_ms": int(avg_tool_ms or 0),
        "avg_tools_per_turn": round(tool_calls / assistant_turns, 2) if assistant_turns else 0,

        "policy_blocks": policy_blocks,
        "prompt_injections_detected": injections,
        "csat": round(float(csat_avg), 2) if csat_avg is not None else None,

        "knowledge_chunks": chunks,
        "knowledge_embedded": embedded,
        "retrieval_mode": "vector" if embedded else "keyword-fallback",

        "targets": TARGETS,
    }
    return data


def recent_activity(session: Session, limit: int = 25) -> list[dict]:
    rows = session.scalars(
        select(m.AuditLog).order_by(m.AuditLog.created_at.desc(), m.AuditLog.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "event": r.event,
            "actor": r.actor,
            "conversation_id": r.conversation_id,
            "subject_id": r.subject_id,
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, int(round(0.95 * len(ordered))) - 1)
    return int(ordered[index])
