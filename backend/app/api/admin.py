"""Admin API.

Unprotected by design — this version has no auth anywhere. Nothing here is
production-hardened.

Everything the escalation flow needs: the approval queue with its full escalation
package, the two decisions a reviewer can make, and read-only views of every
simulated call.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models as m
from app.db.session import get_session
from app.rag import embeddings
from app.rag import store as rag_store
from app.services import conversation as convo
from app.services import metrics as metrics_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


class Decision(BaseModel):
    note: str = ""


# ----------------------------------------------------------------------
# approval queue
# ----------------------------------------------------------------------


@router.get("/pending")
def pending_actions(status: str | None = None, session: Session = Depends(get_session)) -> dict:
    stmt = select(m.PendingAction).order_by(m.PendingAction.created_at.desc())
    if status:
        stmt = stmt.where(m.PendingAction.status == status.upper())
    rows = session.scalars(stmt.limit(100)).all()
    return {"actions": [_action_summary(r) for r in rows]}


@router.get("/pending/{action_id}")
def pending_action(action_id: str, session: Session = Depends(get_session)) -> dict:
    action = session.get(m.PendingAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"no pending action {action_id}")
    return {
        **_action_summary(action),
        # Summary, conversation, customer profile, suggested action, order
        # history, agent reasoning and relevant policy.
        "escalation_package": action.escalation_package,
        "execution_result": action.execution_result,
    }


@router.post("/pending/{action_id}/approve")
def approve(action_id: str, decision: Decision, session: Session = Depends(get_session)) -> dict:
    return _resolve(session, action_id, approved=True, note=decision.note)


@router.post("/pending/{action_id}/reject")
def reject(action_id: str, decision: Decision, session: Session = Depends(get_session)) -> dict:
    return _resolve(session, action_id, approved=False, note=decision.note)


def _resolve(session: Session, action_id: str, *, approved: bool, note: str) -> dict:
    try:
        return convo.resume(session, action_id, approved=approved, note=note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to resolve pending action %s", action_id)
        raise HTTPException(
            status_code=500, detail=f"failed to resume the workflow: {exc}"
        ) from exc


# ----------------------------------------------------------------------
# inspection
# ----------------------------------------------------------------------


@router.get("/metrics")
def metrics(session: Session = Depends(get_session)) -> dict:
    return metrics_service.collect(session)


@router.get("/activity")
def activity(limit: int = 25, session: Session = Depends(get_session)) -> dict:
    return {"events": metrics_service.recent_activity(session, limit)}


@router.get("/api-logs")
def api_logs(limit: int = 100, system: str | None = None,
             session: Session = Depends(get_session)) -> dict:
    stmt = select(m.ApiLog).order_by(m.ApiLog.created_at.desc(), m.ApiLog.id.desc())
    if system:
        stmt = stmt.where(m.ApiLog.system == system)
    rows = session.scalars(stmt.limit(min(limit, 500))).all()
    return {
        "logs": [
            {
                "id": r.id, "system": r.system, "operation": r.operation,
                "request": r.request, "response": r.response, "ok": r.ok,
                "error_code": r.error_code, "latency_ms": r.latency_ms,
                "conversation_id": r.conversation_id, "simulated": r.simulated,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/audit-logs")
def audit_logs(limit: int = 100, session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(
        select(m.AuditLog).order_by(m.AuditLog.created_at.desc(), m.AuditLog.id.desc())
        .limit(min(limit, 500))
    ).all()
    return {
        "logs": [
            {"id": r.id, "event": r.event, "actor": r.actor,
             "conversation_id": r.conversation_id, "subject_id": r.subject_id,
             "detail": r.detail,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]
    }


@router.get("/llm-calls")
def llm_calls(limit: int = 100, session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(
        select(m.LlmCall).order_by(m.LlmCall.created_at.desc(), m.LlmCall.id.desc())
        .limit(min(limit, 500))
    ).all()
    return {
        "calls": [
            {"id": r.id, "agent": r.agent, "model": r.model,
             "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
             "cost_usd": round(r.cost_micros / 1_000_000, 6),
             "latency_ms": r.latency_ms,
             "time_to_first_token_ms": r.time_to_first_token_ms,
             "ok": r.ok, "error": r.error,
             "conversation_id": r.conversation_id,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]
    }


@router.get("/emails")
def emails(limit: int = 50, session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(
        select(m.EmailOutbox).order_by(m.EmailOutbox.created_at.desc()).limit(limit)
    ).all()
    return {
        "emails": [
            {"id": r.id, "to": r.to_email, "subject": r.subject, "body": r.body,
             "template": r.template, "order_id": r.order_id,
             "attachments": r.attachments,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]
    }


@router.get("/tickets")
def tickets(session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(
        select(m.CrmTicket).order_by(m.CrmTicket.created_at.desc()).limit(100)
    ).all()
    return {
        "tickets": [
            {"id": r.id, "category": r.category, "subject": r.subject,
             "status": r.status, "priority": r.priority, "escalated": r.escalated,
             "order_id": r.order_id, "resolution": r.resolution,
             "csat_score": r.csat_score,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]
    }


@router.get("/orders")
def orders(session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(select(m.Order).order_by(m.Order.id)).all()
    return {"orders": [_order_summary(session, r) for r in rows]}


@router.get("/orders/{order_id}")
def order_detail(order_id: str, session: Session = Depends(get_session)) -> dict:
    order = session.get(m.Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"no order {order_id}")
    # The summary already carries the returns and the refunded totals; the
    # detail adds the full payment record on top of them.
    payment = session.scalar(select(m.Payment).where(m.Payment.order_id == order_id))
    return {
        **_order_summary(session, order),
        "payment": None if payment is None else {
            "id": payment.id, "amount_cents": payment.amount_cents,
            "refunded_cents": payment.refunded_cents,
            "refundable_cents": payment.refundable_cents, "status": payment.status,
        },
    }


@router.get("/customer")
def customer(session: Session = Depends(get_session)) -> dict:
    from app.services import memory as memory_service

    mem = memory_service.load(session, get_settings().default_customer_id)
    return mem.as_dict()


@router.get("/knowledge")
def knowledge(session: Session = Depends(get_session)) -> dict:
    return rag_store.stats(session)


@router.get("/knowledge/search")
def knowledge_search(q: str, k: int = 5, session: Session = Depends(get_session)) -> dict:
    return {"hits": [hit.as_dict() for hit in rag_store.search(session, q, k=k)]}


# ----------------------------------------------------------------------
# policy parameters — editable from /admin
# ----------------------------------------------------------------------


class RuleUpdate(BaseModel):
    value_int: int


@router.get("/policy-rules")
def policy_rules(session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(select(m.PolicyRule).order_by(m.PolicyRule.key)).all()
    return {
        "rules": [
            {"key": r.key, "value_int": r.value_int, "value_text": r.value_text,
             "policy_id": r.policy_id, "description": r.description,
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in rows
        ]
    }


@router.patch("/policy-rules/{key}")
def update_policy_rule(key: str, payload: RuleUpdate,
                       session: Session = Depends(get_session)) -> dict:
    rule = session.get(m.PolicyRule, key)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"no policy rule {key}")
    before = rule.value_int
    rule.value_int = payload.value_int
    session.add(m.AuditLog(
        event="policy_rule_changed", actor="admin", subject_id=key,
        detail={"key": key, "from": before, "to": payload.value_int},
    ))

    session.commit()

    # The prose quoting this rule is rendered at retrieval, so it is already
    # correct. Its vectors are not — they encode the old number — so rebuild
    # them. Invalidate and re-embed in one transaction: a stale vector still
    # retrieves a chunk whose text is rendered fresh, while a NULL one drops it
    # out of vector search entirely, so a failed re-embed has to leave the old
    # vector in place. Without credentials the corpus is on keyword fallback,
    # which reads rendered text, and there is nothing to rebuild.
    reindexed = embedded = 0
    if embeddings.is_configured():
        try:
            reindexed = rag_store.invalidate_for_rule(session, key)
            embedded = rag_store.embed_missing(session)
            session.commit()
        except Exception:  # noqa: BLE001 - a stale index must not fail the edit
            session.rollback()
            reindexed = embedded = 0
            log.exception("re-embedding after the %s change failed", key)

    return {
        "key": rule.key, "value_int": rule.value_int, "previous": before,
        "chunks_reindexed": reindexed, "chunks_embedded": embedded,
    }


# ----------------------------------------------------------------------


def _action_summary(action: m.PendingAction) -> dict:
    package = action.escalation_package or {}
    return {
        "id": action.id,
        "conversation_id": action.conversation_id,
        "workflow_id": action.workflow_id,
        "order_id": action.order_id,
        "tool_name": action.tool_name,
        "tool_args": action.tool_args,
        "amount_cents": action.amount_cents,
        "status": action.status,
        "priority": action.priority,
        "policy_reasons": action.policy_reasons,
        "summary": package.get("summary", ""),
        "reason": package.get("reason", ""),
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "resolved_at": action.resolved_at.isoformat() if action.resolved_at else None,
        "executed_at": action.executed_at.isoformat() if action.executed_at else None,
    }


def _order_summary(session: Session, order: m.Order) -> dict:
    """Fulfilment, money and returns — the three axes, on every row.
    """
    payment = session.scalar(select(m.Payment).where(m.Payment.order_id == order.id))
    returns = session.scalars(
        select(m.Return).where(m.Return.order_id == order.id).order_by(m.Return.created_at)
    ).all()

    return {
        "id": order.id,
        "status": order.status,
        "total_cents": order.total_cents,
        "placed_at": order.placed_at.isoformat() if order.placed_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "carrier": order.carrier,
        "tracking_number": order.tracking_number,
        "tracking_status": order.tracking_status,
        "fraud_flagged": order.fraud_flagged,
        "payment_id": payment.id if payment else None,
        "refunded_cents": (payment.refunded_cents or None) if payment else None,
        "refundable_cents": payment.refundable_cents if payment else None,
        "return_state": _return_state(returns),
        "returns": [
            {"id": r.id, "status": r.status, "reason": r.reason,
             "destination": r.destination,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in returns
        ],
        "line_items": [
            {
                "sku": li.sku, "title": li.title, "size": li.size,
                "quantity": li.quantity, "unit_price_cents": li.unit_price_cents,
                "product_class": (
                    p.product_class if (p := session.get(m.Product, li.sku)) else "STANDARD"
                ),
                "final_sale": bool(p and p.final_sale),
            }
            for li in order.line_items
        ],
    }


def _return_state(returns: list[m.Return]) -> str | None:
    """The latest return's status, or None when the order has never had one."""
    if not returns:
        return None
    latest = returns[-1]
    return f"{latest.id} {latest.status}" if len(returns) == 1 else (
        f"{latest.id} {latest.status} (+{len(returns) - 1} earlier)"
    )
