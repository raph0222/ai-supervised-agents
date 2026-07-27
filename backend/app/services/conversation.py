"""Conversation manager — the layer that drives the graph.

One workflow thread per turn, not per conversation. Continuity comes from the
`messages` table, which every turn re-reads; the checkpoint only has to survive
long enough for an approval to resume the turn that raised it. One thread per
conversation would let a turn parked at AWAIT_APPROVAL block every later message.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models as m
from app.graph import workflow
from app.graph.state import AWAITING_APPROVAL, DONE, new_state, use_session
from app.llm import client as llm
from app.services import events

log = logging.getLogger(__name__)


def default_customer_id() -> str:
    return get_settings().default_customer_id


def get_or_create(session: Session, conversation_id: str | None = None) -> m.Conversation:
    """Resolve the conversation. The customer is always the hardcoded one."""
    customer_id = default_customer_id()

    if conversation_id:
        existing = session.get(m.Conversation, conversation_id)
        if existing is not None:
            return existing

    conversation = m.Conversation(
        id=conversation_id or f"CONV-{uuid.uuid4().hex[:12].upper()}",
        customer_id=customer_id,
        status="ACTIVE",
    )
    session.add(conversation)
    session.flush()
    return conversation


def latest(session: Session) -> m.Conversation | None:
    return session.scalar(
        select(m.Conversation)
        .where(m.Conversation.customer_id == default_customer_id())
        .order_by(m.Conversation.updated_at.desc())
    )


def add_message(
    session: Session,
    conversation_id: str,
    role: str,
    content: str,
    *,
    agent: str | None = None,
    meta: dict | None = None,
    publish: bool = True,
) -> m.Message:
    message = m.Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        agent=agent,
        meta=meta or {},
    )
    session.add(message)
    session.flush()

    if publish:
        events.publish(
            "message", conversation_id,
            id=message.id, role=role, content=content, agent=agent,
            meta=message.meta,
        )
    return message


def history(session: Session, conversation_id: str) -> list[dict]:
    rows = session.scalars(
        select(m.Message)
        .where(m.Message.conversation_id == conversation_id)
        .order_by(m.Message.created_at, m.Message.id)
    ).all()
    return [
        {
            "id": r.id,
            "role": r.role,
            "content": r.content,
            "agent": r.agent,
            "meta": r.meta,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ----------------------------------------------------------------------


def send(session: Session, text: str, conversation_id: str | None = None) -> dict:
    """Run one turn.

    Returns the assistant's reply plus enough workflow detail for /admin. With
    Vertex unconfigured the configuration error comes back as a chat message
    rather than a 500.
    """
    conversation = get_or_create(session, conversation_id)
    add_message(session, conversation.id, "user", text)
    # Commit the question before running the turn. If the workflow then fails we
    # roll back its writes, and the customer's message must not go with them.
    session.commit()

    if not llm.is_configured():
        return _llm_unavailable(session, conversation)

    turn_id = uuid.uuid4().hex[:12]
    config = {"configurable": {"thread_id": workflow.thread_id(conversation.id, turn_id)}}
    state = new_state(conversation.id, conversation.customer_id, text, turn_id)

    events.publish("thinking", conversation.id, turn_id=turn_id)

    try:
        with use_session(session):
            result = workflow.get_graph().invoke(state, config)
    except llm.LlmUnavailable:
        return _llm_unavailable(session, conversation)
    except Exception:  # noqa: BLE001 - one bad turn must not kill the session
        log.exception("workflow failed for conversation %s", conversation.id)
        session.rollback()
        message = (
            "Something went wrong on my side and I couldn't finish that. "
            "Nothing was changed on your account. Please try again."
        )
        add_message(session, conversation.id, "assistant", message,
                    agent="system", meta={"error": True})
        session.commit()
        return {"conversation_id": conversation.id, "reply": message,
                "status": "ERROR", "pending_action_id": None}

    return _finish_turn(session, conversation, result, turn_id)


def resume(session: Session, action_id: str, *, approved: bool, note: str = "") -> dict:
    """Resume a parked workflow after an /admin decision."""
    action = session.get(m.PendingAction, action_id)
    if action is None:
        raise LookupError(f"no pending action {action_id}")
    if action.status != "PENDING":
        # Approving twice must be a no-op, not a second refund.
        return {
            "action_id": action.id,
            "status": action.status,
            "replayed": True,
            "conversation_id": action.conversation_id,
            "reply": None,
        }

    action.status = "APPROVED" if approved else "REJECTED"
    action.resolved_at = m.utcnow()
    session.add(m.AuditLog(
        event="approval_decision", actor="admin",
        conversation_id=action.conversation_id, subject_id=action.id,
        detail={"approved": approved, "note": note, "tool": action.tool_name},
    ))
    session.flush()

    conversation = session.get(m.Conversation, action.conversation_id)
    config = {"configurable": {"thread_id": action.workflow_id}}
    graph = workflow.get_graph()

    try:
        with use_session(session):
            graph.update_state(
                config, {"approval_granted": approved, "approval_seen": True}
            )
            result = graph.invoke(None, config)
    except llm.LlmUnavailable:
        session.commit()
        return {"action_id": action.id, "status": action.status,
                "conversation_id": action.conversation_id,
                "reply": None, "error": "vertex_not_configured"}
    except Exception:  # noqa: BLE001
        log.exception("resume failed for pending action %s", action.id)
        session.rollback()
        action = session.get(m.PendingAction, action_id)
        action.status = "PENDING"  # leave it actionable rather than lost
        session.commit()
        raise

    reply = result.get("reply") or ""
    if reply:
        add_message(
            session, action.conversation_id, "assistant", reply,
            agent=result.get("agent"),
            meta={"resumed_from": action.id, "approved": approved,
                  "tool_results": result.get("tool_results", [])},
        )
    if conversation is not None:
        conversation.workflow_state = DONE
        conversation.updated_at = m.utcnow()

    session.commit()
    events.publish(
        "approval_resolved", action.conversation_id,
        action_id=action.id, approved=approved, status=action.status,
    )
    return {
        "action_id": action.id,
        "status": action.status,
        "conversation_id": action.conversation_id,
        "reply": reply,
        "tool_results": result.get("tool_results", []),
    }


# ----------------------------------------------------------------------


def _finish_turn(session: Session, conversation: m.Conversation, result: dict, turn_id: str) -> dict:
    reply = result.get("reply") or (
        "I wasn't able to put together an answer for that one. Could you rephrase it?"
    )
    status = result.get("status") or DONE

    conversation.current_intent = result.get("intent")
    conversation.current_agent = result.get("agent")
    conversation.order_id = (result.get("entities") or {}).get("order_id") or conversation.order_id
    conversation.workflow_state = status
    conversation.updated_at = m.utcnow()

    add_message(
        session, conversation.id, "assistant", reply,
        agent=result.get("agent"),
        meta={
            "turn_id": turn_id,
            "intent": result.get("intent"),
            "confidence": result.get("confidence"),
            "sentiment": result.get("sentiment"),
            "plan": result.get("plan"),
            "tool_results": result.get("tool_results", []),
            "verdicts": result.get("verdicts", []),
            "cited_policies": result.get("cited_policies", []),
            "retrieved": [
                {"policy_id": h.get("policy_id"), "heading": h.get("heading"),
                 "score": h.get("score"), "mode": h.get("mode")}
                for h in (result.get("retrieved") or [])
            ],
            "status": status,
            "pending_action_id": result.get("pending_action_id"),
            "unbacked_claims": result.get("unbacked_claims", []),
        },
    )
    session.commit()

    if status == AWAITING_APPROVAL:
        events.publish(
            "awaiting_approval", conversation.id,
            action_id=result.get("pending_action_id"),
            reason=result.get("escalation_reason"),
        )
    events.publish("turn_complete", conversation.id, status=status)

    return {
        "conversation_id": conversation.id,
        "reply": reply,
        "status": status,
        "agent": result.get("agent"),
        "intent": result.get("intent"),
        "confidence": result.get("confidence"),
        "pending_action_id": result.get("pending_action_id"),
        "tool_results": result.get("tool_results", []),
        "verdicts": result.get("verdicts", []),
        "cited_policies": result.get("cited_policies", []),
        "unbacked_claims": result.get("unbacked_claims", []),
    }


def _llm_unavailable(session: Session, conversation: m.Conversation) -> dict:
    settings = get_settings()
    message = (
        f"The chat agents need Vertex AI, and {' / '.join(settings.missing_vertex_vars())} "
        "is not set in .env. Everything else still works — seed data, orders, "
        "policies, logs and the admin page are all live."
    )
    add_message(session, conversation.id, "assistant", message,
                agent="system", meta={"config_error": True,
                                      "missing": settings.missing_vertex_vars()})
    session.commit()
    return {
        "conversation_id": conversation.id,
        "reply": message,
        "status": "CONFIG_ERROR",
        "missing_vertex_vars": settings.missing_vertex_vars(),
        "pending_action_id": None,
    }
