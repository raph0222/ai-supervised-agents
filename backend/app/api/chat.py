"""Chat channel.

Single channel, single hardcoded user, no login. The customer is resolved from
`DEFAULT_CUSTOMER_ID`, never from a header or token.

The POST is synchronous. FastAPI runs `def` endpoints in a threadpool, so a slow
turn does not block the event loop or the SSE stream — live updates ride the
stream, which is how an approval made in another tab lands in the chat.
"""

from __future__ import annotations

import asyncio
import logging
import queue
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models as m
from app.db.session import get_session
from app.policy.engine import PolicyEngine
from app.services import conversation as convo
from app.services import events

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])

HEARTBEAT_SECONDS = 15


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


@router.post("/chat")
def post_chat(payload: ChatRequest, session: Session = Depends(get_session)) -> dict:
    return convo.send(session, payload.message.strip(), payload.conversation_id)


@router.get("/conversations")
def list_conversations(session: Session = Depends(get_session)) -> dict:
    rows = session.scalars(
        select(m.Conversation).order_by(m.Conversation.updated_at.desc()).limit(50)
    ).all()
    return {
        "conversations": [
            {
                "id": r.id,
                "status": r.status,
                "current_agent": r.current_agent,
                "current_intent": r.current_intent,
                "order_id": r.order_id,
                "workflow_state": r.workflow_state,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    }


@router.get("/conversations/current")
def current_conversation(session: Session = Depends(get_session)) -> dict:
    """The chat page's entry point: resume the last conversation or start one."""
    existing = convo.latest(session)
    conversation = existing or convo.get_or_create(session)
    if existing is None:
        session.commit()
    return {
        "conversation_id": conversation.id,
        "messages": convo.history(session, conversation.id),
        "vertex_configured": get_settings().vertex_configured,
        "missing_vertex_vars": get_settings().missing_vertex_vars(),
    }


@router.get("/me")
def me(session: Session = Depends(get_session)) -> dict:
    """The account panel on the chat page: who I am and what I bought.

    Customer-facing, so narrower than `/api/admin/orders` — no fraud flag, no
    risk score. It does carry each order's return verdict, from the same
    `PolicyEngine` the executor calls, so the panel cannot drift from reality.
    """
    settings = get_settings()
    customer = session.get(m.Customer, settings.default_customer_id)
    if customer is None:
        raise HTTPException(
            status_code=404, detail=f"no customer {settings.default_customer_id}"
        )

    now = datetime.now(timezone.utc)
    engine = PolicyEngine(session)
    rules = {r.key: r.value_int for r in session.scalars(select(m.PolicyRule)).all()}
    orders = session.scalars(
        select(m.Order)
        .where(m.Order.customer_id == customer.id)
        .order_by(m.Order.placed_at.desc())
    ).all()

    return {
        "customer": {
            "id": customer.id,
            "name": customer.full_name,
            "email": customer.email,
            "loyalty_tier": customer.loyalty_tier,
            "lifetime_spend_cents": customer.lifetime_spend_cents,
            "order_count": customer.order_count,
            "vip": customer.vip,
            "preferences": customer.preferences or {},
        },
        # The two thresholds worth knowing before you type: they explain most
        # of the verdicts below, and /admin can change either one live.
        "policy": {
            "return_window_days": rules.get("return_window_days") or 30,
            "refund_auto_approve_under_cents": (
                rules.get("refund_auto_approve_under_cents") or 5000
            ),
        },
        "orders": [_customer_order(session, engine, order, now) for order in orders],
    }


def _customer_order(
    session: Session, engine: PolicyEngine, order: m.Order, now: datetime
) -> dict:
    payment = session.scalar(select(m.Payment).where(m.Payment.order_id == order.id))
    returns = session.scalars(
        select(m.Return).where(m.Return.order_id == order.id)
    ).all()

    delivered = order.delivered_at
    if delivered is not None and delivered.tzinfo is None:
        delivered = delivered.replace(tzinfo=timezone.utc)

    return {
        "id": order.id,
        "status": order.status,
        "total_cents": order.total_cents,
        "placed_at": order.placed_at.isoformat() if order.placed_at else None,
        "delivered_at": delivered.isoformat() if delivered else None,
        "days_since_delivery": (now - delivered).days if delivered else None,
        "carrier": order.carrier,
        "tracking_number": order.tracking_number,
        "tracking_status": order.tracking_status,
        "line_items": [
            {
                "sku": li.sku,
                "title": li.title,
                "size": li.size,
                "quantity": li.quantity,
                "unit_price_cents": li.unit_price_cents,
                "line_total_cents": li.line_total_cents,
                "product_class": (
                    p.product_class if (p := session.get(m.Product, li.sku)) else "STANDARD"
                ),
                "final_sale": bool(p and p.final_sale),
            }
            for li in order.line_items
        ],
        "payment": None if payment is None else {
            "amount_cents": payment.amount_cents,
            "refunded_cents": payment.refunded_cents,
            "refundable_cents": payment.refundable_cents,
            "status": payment.status,
        },
        "returns": [
            {"id": r.id, "status": r.status, "reason": r.reason} for r in returns
        ],
        "return_policy": engine.evaluate_return(order.id, now=now).to_dict(),
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, session: Session = Depends(get_session)) -> dict:
    conversation = session.get(m.Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"no conversation {conversation_id}")
    return {
        "conversation_id": conversation.id,
        "status": conversation.status,
        "workflow_state": conversation.workflow_state,
        "messages": convo.history(session, conversation_id),
    }


@router.post("/conversations/new")
def new_conversation(session: Session = Depends(get_session)) -> dict:
    conversation = convo.get_or_create(session)
    session.commit()
    return {"conversation_id": conversation.id, "messages": []}


@router.get("/stream")
async def stream(request: Request, conversation_id: str = "*") -> StreamingResponse:
    """Server-sent events for one conversation, or `*` for everything.

    The admin page subscribes to `*` so its approval queue and activity feed
    update while a conversation runs in another tab.
    """
    subscriber = events.broker.subscribe(conversation_id)

    async def generator():
        idle = 0.0
        try:
            yield events.Event("connected", conversation_id).sse()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = subscriber.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(events.POLL_INTERVAL_SECONDS)
                    idle += events.POLL_INTERVAL_SECONDS
                    if idle >= HEARTBEAT_SECONDS:
                        idle = 0.0
                        # Proxies and browsers drop a silent stream; a comment
                        # frame keeps it open without looking like an event.
                        yield ": keepalive\n\n"
                    continue
                idle = 0.0
                yield event.sse()
        finally:
            events.broker.unsubscribe(conversation_id, subscriber)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
