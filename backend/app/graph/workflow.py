"""The agent state machine.

    NEW -> UNDERSTAND -> ROUTE -> PLAN -> RETRIEVE -> VERIFY POLICY
        -> CALL TOOLS -> VERIFY RESULT -> RESPOND -> END

Escalation parks the workflow at AWAIT_APPROVAL rather than ending it, via
`interrupt_before=["execute_approved"]`: the graph writes a `pending_actions`
row and stops at the checkpoint. An approval resumes the same graph, so what
gets executed is the exact tool call a human saw.
"""

from __future__ import annotations

import json
import logging
import uuid

from langgraph.graph import END, START, StateGraph

from app.agents import planner, router, specialists
from app.db import models as m
from app.graph.checkpointer import get_checkpointer
from app.graph.state import (
    AWAITING_APPROVAL,
    DONE,
    RUNNING,
    WorkflowState,
    current_session,
)
from app.policy.engine import Decision
from app.rag import store as rag_store
from app.security import claims
from app.services import memory as memory_service
from app.tools import registry

log = logging.getLogger(__name__)

# Overridden by the max_refund_retries policy rule.
DEFAULT_MAX_ATTEMPTS = 2

# Batches of tool calls per turn. Each round costs one model call.
MAX_TOOL_ROUNDS = 3

# Used when a reply cannot be trusted and escalating is not an option because
# the turn has already been through review once. Written here, not by the model.
UNVERIFIED_FALLBACK = (
    "Let me correct myself before we go any further: I have not been able to "
    "complete that, and nothing on your order or your payment method has been "
    "changed. Tell me how you'd like to proceed and I'll put it through "
    "properly."
)

_graph = None


# ----------------------------------------------------------------------
# nodes
# ----------------------------------------------------------------------


def understand(state: WorkflowState) -> dict:
    """Load who we are talking to and what was said before."""
    session = current_session()
    mem = memory_service.load(session, state["customer_id"])
    history = memory_service.conversation_history(session, state["conversation_id"])
    # This turn's message is already persisted; drop it so it isn't seen twice.
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    return {
        "memory_block": mem.render(),
        "history_block": memory_service.render_history(history),
        "status": RUNNING,
    }


def route_node(state: WorkflowState) -> dict:
    """Intent detection."""
    session = current_session()
    result = router.route(
        session,
        state["message"],
        history=state.get("history_block", ""),
        conversation_id=state["conversation_id"],
    )

    agent = specialists.agent_for(result.intent)
    update = {
        "intent": result.intent,
        "confidence": result.confidence,
        "entities": result.entities,
        "sentiment": result.sentiment,
        "agent": agent,
        "injection": result.injection.as_dict() if result.injection else {},
    }

    reason = _escalation_trigger(result)
    if reason:
        update["escalation_reason"] = reason
    return update


def plan_node(state: WorkflowState) -> dict:
    """Build the execution plan."""
    session = current_session()
    plan = planner.build(
        session,
        agent=state["agent"],
        message=state["message"],
        intent=state["intent"],
        entities=state.get("entities", {}),
        memory_block=state.get("memory_block", ""),
        policy_block=state.get("policy_block", "(not retrieved yet)"),
        conversation_id=state["conversation_id"],
    )
    return {"plan": plan.as_dict()}


def retrieve(state: WorkflowState) -> dict:
    """Similarity search over the knowledge corpus."""
    session = current_session()
    query = state["message"]
    if state.get("intent"):
        # Bias retrieval towards the routed branch.
        query = f"{state['intent'].replace('_', ' ').lower()}: {query}"

    hits = rag_store.search(session, query, k=4)
    return {
        "retrieved": [hit.as_dict() for hit in hits],
        "policy_block": _render_policy(hits),
    }


def decide(state: WorkflowState) -> dict:
    """Ask the specialist what to do next."""
    session = current_session()
    decision = specialists.decide(
        session,
        agent=state["agent"],
        message=state["message"],
        plan_block=_render_plan(state.get("plan")),
        memory_block=state.get("memory_block", ""),
        policy_block=state.get("policy_block", ""),
        history_block=state.get("history_block", ""),
        tool_results=state.get("tool_results", []),
        conversation_id=state["conversation_id"],
    )
    return {
        "proposed_calls": decision.tool_calls,
        "plan": {**state.get("plan", {}), "agent_reasoning": decision.reasoning,
                 "clarifying_question": decision.clarifying_question,
                 "draft_answer": decision.draft_answer},
    }


def verify_policy(state: WorkflowState) -> dict:
    """Evaluate every proposed write before anything runs.

    Decides the shape of the turn — escalate, drop a call, or proceed. The
    executor runs the gate again; that check is the one that cannot be bypassed.
    """
    session = current_session()
    approved, verdicts = [], []
    escalate_for = None

    for call in state.get("proposed_calls", []):
        verdict = registry.evaluate_gate(session, call["tool"], call.get("args", {}))
        if verdict is None:
            approved.append(call)
            continue

        verdicts.append(verdict.to_dict())
        if verdict.decision is Decision.ALLOW:
            approved.append(call)
        elif verdict.decision is Decision.REQUIRES_APPROVAL and escalate_for is None:
            escalate_for = call

    update: dict = {"proposed_calls": approved, "verdicts": verdicts}
    if escalate_for is not None:
        update["escalation_reason"] = "POLICY_REQUIRES_APPROVAL"
        update["proposed_calls"] = [escalate_for]
    return update


def call_tools(state: WorkflowState) -> dict:
    """Execute through the tool layer. The gate runs again in there."""
    session = current_session()
    results, verdicts = [], []

    for call in state.get("proposed_calls", []):
        outcome = registry.execute(
            session,
            call["tool"],
            call.get("args", {}),
            conversation_id=state["conversation_id"],
            idempotency_key=f"{state['turn_id']}:{call['tool']}",
        )
        payload = outcome.as_dict()
        payload["malformed"] = _is_malformed(outcome)
        results.append(payload)
        if outcome.verdict:
            verdicts.append(outcome.verdict)

    return {
        "tool_results": results,
        "verdicts": verdicts,
        "attempts": state.get("attempts", 0) + 1,
        "tool_rounds": state.get("tool_rounds", 0) + 1,
        "proposed_calls": [],
    }


def verify_result(state: WorkflowState) -> dict:
    """Decide whether what came back is usable.

    An ok:true payload missing its line items counts as a failure.
    """
    failures = [
        r for r in state.get("tool_results", [])
        if r.get("status") == "FAILED" or r.get("malformed")
    ]
    if not failures:
        return {}

    max_attempts = _max_attempts()
    if state.get("attempts", 0) < max_attempts:
        # Hand the failure back to the agent to retry or reroute.
        return {"proposed_calls": [], "escalation_reason": ""}

    return {"escalation_reason": "REPEATED_FAILURES"}


def escalate(state: WorkflowState) -> dict:
    """Park the workflow and queue the action for /admin."""
    session = current_session()
    call = (state.get("proposed_calls") or [{}])[0]
    reason = state.get("escalation_reason") or "UNSPECIFIED"

    action = m.PendingAction(
        id=f"PA-{uuid.uuid4().hex[:10].upper()}",
        conversation_id=state["conversation_id"],
        # The checkpoint thread this action resumes. One thread per turn, so an
        # approval reopens exactly the workflow a human looked at.
        workflow_id=thread_id(state["conversation_id"], state["turn_id"]),
        customer_id=state["customer_id"],
        order_id=(call.get("args") or {}).get("order_id") or state.get("entities", {}).get("order_id"),
        tool_name=call.get("tool") or "None",
        tool_args=call.get("args") or {},
        amount_cents=_amount_of(call),
        status="PENDING",
        priority=_priority(state, reason),
        policy_reasons=_blocking_reasons(state.get("verdicts", [])),
        escalation_package=build_escalation_package(session, state, reason),
    )
    session.add(action)
    session.add(
        m.AuditLog(
            event="escalated",
            actor="agent",
            conversation_id=state["conversation_id"],
            subject_id=action.id,
            detail={"reason": reason, "tool": action.tool_name,
                    "amount_cents": action.amount_cents},
        )
    )
    session.flush()

    return {
        "pending_action_id": action.id,
        "status": AWAITING_APPROVAL,
        "reply": _await_message(reason, action),
        "resolved": False,
    }


def execute_approved(state: WorkflowState) -> dict:
    """Resume point. Runs only after a human acted on the pending action.

    `skip_gate` is set because a person accepted the verdict, but a DENY is
    re-checked and refused anyway.
    """
    session = current_session()
    action_id = state.get("pending_action_id")
    action = session.get(m.PendingAction, action_id) if action_id else None

    if action is None:
        return {"status": RUNNING,
                "tool_results": [{"status": "FAILED", "tool": "approval",
                                  "args": {}, "result": {
                                      "ok": False, "error_code": "action_missing",
                                      "message": "The pending action disappeared."}}]}

    if not state.get("approval_granted"):
        session.add(m.AuditLog(
            event="approval_rejected", actor="admin",
            conversation_id=state["conversation_id"], subject_id=action.id,
            detail={"tool": action.tool_name},
        ))
        session.flush()
        return {"status": RUNNING, "resolved": False,
                "tool_results": [{"status": "BLOCKED", "tool": action.tool_name,
                                  "args": action.tool_args,
                                  "result": {"ok": False, "error_code": "rejected_by_admin",
                                             "message": "A reviewer declined this action."}}]}

    if action.status == "EXECUTED":
        # Replay guard, on top of the tool layer's own.
        return {"status": RUNNING,
                "tool_results": [{"status": "EXECUTED", "tool": action.tool_name,
                                  "args": action.tool_args,
                                  "result": action.execution_result or {"ok": True, "replayed": True}}]}

    verdict = registry.evaluate_gate(session, action.tool_name, action.tool_args)
    if verdict is not None and verdict.decision is Decision.DENY:
        action.status = "REJECTED"
        action.execution_result = {"ok": False, "error_code": "policy_denied",
                                   "message": "Denied on re-check at execution time."}
        session.flush()
        return {"status": RUNNING, "verdicts": [verdict.to_dict()],
                "tool_results": [{"status": "BLOCKED", "tool": action.tool_name,
                                  "args": action.tool_args,
                                  "result": action.execution_result}]}

    outcome = registry.execute(
        session, action.tool_name, action.tool_args,
        conversation_id=state["conversation_id"],
        idempotency_key=f"approval:{action.id}",
        skip_gate=True,
    )
    action.status = "EXECUTED"
    action.executed_at = m.utcnow()
    action.execution_result = outcome.result
    session.add(m.AuditLog(
        event="approved_action_executed", actor="admin",
        conversation_id=state["conversation_id"], subject_id=action.id,
        detail={"tool": action.tool_name, "ok": outcome.ok},
    ))
    session.flush()

    return {"status": RUNNING, "tool_results": [outcome.as_dict()]}


def respond(state: WorkflowState) -> dict:
    """Turn everything that happened into one message, then check it is true."""
    session = current_session()

    plan = state.get("plan") or {}
    if plan.get("clarifying_question") and not state.get("tool_results"):
        # Low confidence or missing information: ask, do not guess.
        return {"reply": plan["clarifying_question"], "status": DONE,
                "resolved": False, "cited_policies": []}

    extra = ""
    if state.get("approval_seen"):
        extra = (
            "This message continues a conversation that was paused for human "
            "review. The customer already knows it was under review — tell them "
            "the outcome directly, do not re-introduce yourself."
        )

    results = state.get("tool_results", [])
    reply = _generate_reply(state, session, extra)
    unbacked = claims.unbacked(reply.message, results)

    if unbacked:
        _log_unbacked(session, state, reply.message, unbacked, retried=False)
        reply = _generate_reply(
            state, session,
            f"{extra}\n\n{claims.correction_instruction(unbacked)}".strip(),
        )
        unbacked = claims.unbacked(reply.message, results)

    if not unbacked:
        return {
            "reply": reply.message,
            "cited_policies": reply.cited_policies,
            "resolved": reply.resolved,
            "status": DONE,
        }

    # Twice in a row. Nothing the model writes goes out.
    _log_unbacked(session, state, reply.message, unbacked, retried=True)
    update = {
        "unbacked_claims": [c.as_dict() for c in unbacked],
        "cited_policies": [],
        "resolved": False,
        "status": DONE,
    }
    if state.get("approval_seen"):
        update["reply"] = UNVERIFIED_FALLBACK
        return update

    update["reply"] = reply.message  # overwritten by `escalate`
    update["escalation_reason"] = "UNVERIFIED_ACTION_CLAIM"
    return update


def _generate_reply(state: WorkflowState, session, extra_instruction: str):
    return specialists.respond(
        session,
        agent=state.get("agent", "QA"),
        message=state["message"],
        memory_block=state.get("memory_block", ""),
        policy_block=state.get("policy_block", ""),
        history_block=state.get("history_block", ""),
        tool_results=state.get("tool_results", []),
        verdicts=state.get("verdicts", []),
        conversation_id=state["conversation_id"],
        extra_instruction=extra_instruction,
    )


def _log_unbacked(
    session, state: WorkflowState, draft: str, unbacked: list, *, retried: bool
) -> None:
    """Every rejected draft is recorded — the text, not just the fact."""
    log.warning(
        "reply claimed unperformed %s on conversation %s (retried=%s)",
        [c.category for c in unbacked], state["conversation_id"], retried,
    )
    session.add(m.AuditLog(
        event="reply_claim_unbacked",
        actor="system",
        conversation_id=state["conversation_id"],
        subject_id=state.get("turn_id"),
        detail={
            "claims": [c.as_dict() for c in unbacked],
            "after_correction": retried,
            "draft": draft,
            "executed_tools": sorted(
                item.get("tool") for item in state.get("tool_results", [])
                if item.get("status") == "EXECUTED"
            ),
        },
    ))
    session.flush()


# ----------------------------------------------------------------------
# edges
# ----------------------------------------------------------------------


def after_route(state: WorkflowState) -> str:
    if state.get("escalation_reason"):
        return "escalate"
    return "plan"


def after_decide(state: WorkflowState) -> str:
    if state.get("proposed_calls"):
        return "verify_policy"
    return "respond"


def after_verify_policy(state: WorkflowState) -> str:
    if state.get("escalation_reason") == "POLICY_REQUIRES_APPROVAL":
        return "escalate"
    if state.get("proposed_calls"):
        return "call_tools"
    return "respond"


def after_verify_result(state: WorkflowState) -> str:
    if state.get("escalation_reason") == "REPEATED_FAILURES":
        return "escalate"
    results = state.get("tool_results", [])
    failures = [
        r for r in results
        if r.get("status") == "FAILED" or r.get("malformed")
    ]
    if failures and state.get("attempts", 0) < _max_attempts():
        return "decide"   # retry, with the failure now in context

    # A round that only read gets another one, so an agent can look up an order
    # before acting on it. `decide` returns no tool calls once it has enough.
    if not failures and _read_only(results) and state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS:
        return "decide"
    return "respond"


def after_respond(state: WorkflowState) -> str:
    """The only way out of `respond` other than END: the reply was not true."""
    if state.get("escalation_reason") == "UNVERIFIED_ACTION_CLAIM":
        return "escalate"
    return "end"


def _read_only(results: list) -> bool:
    """True while nothing in this turn has written or moved money yet."""
    return not any(
        r.get("status") == "EXECUTED" and r.get("tool") in registry.WRITE_TOOLS
        for r in results
    )


# ----------------------------------------------------------------------


def thread_id(conversation_id: str, turn_id: str) -> str:
    return f"{conversation_id}::{turn_id}"


def build_graph():
    graph = StateGraph(WorkflowState)

    graph.add_node("understand", understand)
    graph.add_node("route", route_node)
    # Not "plan" — LangGraph rejects node names that collide with state keys.
    graph.add_node("make_plan", plan_node)
    graph.add_node("retrieve", retrieve)
    graph.add_node("decide", decide)
    graph.add_node("verify_policy", verify_policy)
    graph.add_node("call_tools", call_tools)
    graph.add_node("verify_result", verify_result)
    graph.add_node("escalate", escalate)
    graph.add_node("execute_approved", execute_approved)
    graph.add_node("respond", respond)

    graph.add_edge(START, "understand")
    graph.add_edge("understand", "route")
    graph.add_conditional_edges("route", after_route,
                                {"plan": "make_plan", "escalate": "escalate"})
    graph.add_edge("make_plan", "retrieve")
    graph.add_edge("retrieve", "decide")
    graph.add_conditional_edges("decide", after_decide,
                                {"verify_policy": "verify_policy", "respond": "respond"})
    graph.add_conditional_edges("verify_policy", after_verify_policy,
                                {"call_tools": "call_tools", "escalate": "escalate",
                                 "respond": "respond"})
    graph.add_edge("call_tools", "verify_result")
    graph.add_conditional_edges("verify_result", after_verify_result,
                                {"decide": "decide", "escalate": "escalate",
                                 "respond": "respond"})
    # The pause: LangGraph stops before this node and persists the checkpoint.
    graph.add_edge("escalate", "execute_approved")
    graph.add_edge("execute_approved", "respond")
    # A reply that claims an action no tool performed does not reach the
    # customer; it goes back through `escalate` and parks for review instead.
    graph.add_conditional_edges("respond", after_respond,
                                {"escalate": "escalate", "end": END})

    return graph.compile(
        checkpointer=get_checkpointer(),
        interrupt_before=["execute_approved"],
    )


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def reset_graph() -> None:
    global _graph
    _graph = None


# ----------------------------------------------------------------------
# escalation package
# ----------------------------------------------------------------------


def build_escalation_package(session, state: WorkflowState, reason: str) -> dict:
    mem = memory_service.load(session, state["customer_id"])
    history = memory_service.conversation_history(session, state["conversation_id"], limit=20)
    call = (state.get("proposed_calls") or [{}])[0]

    return {
        "summary": _summarise(state, reason, call),
        "reason": reason,
        "conversation": history,
        "customer_profile": mem.customer,
        "order_history": mem.recent_orders,
        "suggested_action": {
            "tool": call.get("tool"),
            "args": call.get("args", {}),
            "amount_cents": _amount_of(call),
        },
        "agent_reasoning": (state.get("plan") or {}).get("agent_reasoning", ""),
        "plan": (state.get("plan") or {}).get("steps", []),
        "relevant_policy": [
            {"policy_id": hit.get("policy_id"), "heading": hit.get("heading"),
             "content": hit.get("content")}
            for hit in (state.get("retrieved") or [])[:3]
        ],
        "policy_verdicts": state.get("verdicts", []),
        "injection_assessment": state.get("injection", {}),
        "intent": state.get("intent"),
        "confidence": state.get("confidence"),
        "sentiment": state.get("sentiment"),
    }


# ----------------------------------------------------------------------


def _escalation_trigger(result: router.Route) -> str:
    """Escalation triggers known before any tool runs."""
    if result.injection and result.injection.severity == "HIGH":
        return "PROMPT_INJECTION"
    if result.intent == "HUMAN":
        return "CUSTOMER_REQUESTED_HUMAN"
    if result.sentiment == "ANGRY":
        return "HIGH_SENTIMENT"
    if result.low_confidence and result.intent != "GENERAL_QA":
        return "LOW_CONFIDENCE"
    return ""


def _priority(state: WorkflowState, reason: str) -> str:
    if reason in {"HIGH_SENTIMENT", "PROMPT_INJECTION", "CUSTOMER_REQUESTED_HUMAN",
                  "UNVERIFIED_ACTION_CLAIM"}:
        return "HIGH"
    if (state.get("injection") or {}).get("flagged"):
        return "HIGH"
    return "NORMAL"


def _amount_of(call: dict) -> int | None:
    args = call.get("args") or {}
    value = args.get("amount_cents")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _blocking_reasons(verdicts: list) -> list:
    out = []
    for verdict in verdicts:
        out += [r for r in verdict.get("reasons", []) if r.get("decision") != "ALLOW"]
    return out


def _summarise(state: WorkflowState, reason: str, call: dict) -> str:
    parts = [f"{state.get('intent', 'UNKNOWN')} request from the customer"]
    if call.get("tool"):
        amount = _amount_of(call)
        money = f" for ${amount / 100:.2f}" if amount else ""
        parts.append(f"agent proposes {call['tool']}{money}")
    parts.append(f"held for review: {reason.replace('_', ' ').lower()}")
    return "; ".join(parts) + "."


def _await_message(reason: str, action: m.PendingAction) -> str:
    """The immediate reply when a workflow parks.

    Written in code, not by the model, so it still goes out when the LLM is what
    failed — and it must never imply the action was taken.
    """
    if reason == "CUSTOMER_REQUESTED_HUMAN":
        return ("I've passed this to a member of the team — they'll pick it up "
                "from here and you'll see their reply in this chat.")
    if reason == "HIGH_SENTIMENT":
        return ("I'm sorry this has been frustrating. I've flagged it for a "
                "colleague to look at as a priority, and their answer will "
                "appear right here.")
    if reason == "REPEATED_FAILURES":
        return ("Something went wrong on our side while I was doing that, and I "
                "don't want to keep retrying blindly. A colleague will take a "
                "look and I'll come back to you here.")
    if reason == "PROMPT_INJECTION":
        return ("I can only help with orders, returns and product questions on "
                "this account. I've asked a colleague to review this "
                "conversation.")
    if reason == "UNVERIFIED_ACTION_CLAIM":
        # The draft said the action was done. It was not, and the customer must
        # hear that first — before anything about review.
        return ("I'm not going to tell you that's done when I can't confirm it. "
                "Nothing has been refunded, returned or changed on your order. "
                "I've handed this to a colleague to complete properly, and "
                "you'll see their reply in this chat.")
    amount = f" of ${action.amount_cents / 100:.2f}" if action.amount_cents else ""
    what = _PLAIN_ENGLISH.get(action.tool_name, "action")
    return (f"This one needs a quick review before I can complete it — a "
            f"{what}{amount} on this account requires sign-off. I've sent it "
            "over and I'll update you here as soon as it's approved.")


# Tool names are for logs; customers read these instead.
_PLAIN_ENGLISH = {
    "Stripe.RefundPayment": "refund",
    "Stripe.CreateAdjustment": "partial refund",
    "Shopify.CreateReturn": "return",
    "Shopify.CreateExchange": "exchange",
    "Shopify.CancelOrder": "cancellation",
}


def _render_plan(plan: dict | None) -> str:
    """Rebuild a Plan from the keys it owns — the dict picks up agent
    annotations after `decide`."""
    if not plan:
        return "(no plan)"
    return planner.Plan(
        steps=plan.get("steps", []),
        missing_information=plan.get("missing_information", []),
        summary=plan.get("summary", ""),
    ).render()


def _render_policy(hits) -> str:
    if not hits:
        return "(no policy retrieved)"
    return "\n\n".join(
        f"[{hit.policy_id} — {hit.heading or hit.title} "
        f"({hit.authority}, retrieved by {hit.mode})]\n{hit.content}"
        for hit in hits
    )


def _is_malformed(outcome: registry.Outcome) -> bool:
    """Shape validation for order payloads."""
    if outcome.tool != "Shopify.GetOrder" or not outcome.ok:
        return False
    order = outcome.result.get("order") or {}
    return (
        "line_items" not in order
        or not isinstance(order.get("total_cents"), int)
    )


def _max_attempts() -> int:
    try:
        session = current_session()
        rule = session.get(m.PolicyRule, "max_refund_retries")
        return rule.value_int if rule and rule.value_int else DEFAULT_MAX_ATTEMPTS
    except Exception:  # noqa: BLE001
        return DEFAULT_MAX_ATTEMPTS


def state_as_json(state: dict) -> str:
    return json.dumps(state, default=str, indent=2)
