"""Fake CRM. Reads and writes `crm_tickets`."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m
from app.db.models import utcnow
from app.integrations.base import ToolResult, failure, simulated_call, success


@simulated_call("crm", "CreateTicket")
def create_ticket(
    session: Session,
    *,
    customer_id: str,
    category: str,
    subject: str,
    order_id: str | None = None,
    priority: str = "NORMAL",
    notes: str | None = None,
) -> ToolResult:
    if session.get(m.Customer, customer_id) is None:
        return failure("customer_not_found", f"No customer {customer_id}.")
    ticket = m.CrmTicket(
        id=f"TKT-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer_id,
        order_id=order_id,
        category=category,
        subject=subject,
        priority=priority,
        notes=notes,
        status="OPEN",
    )
    session.add(ticket)
    session.flush()
    return success(ticket_id=ticket.id, status=ticket.status)


@simulated_call("crm", "UpdateTicket")
def update_ticket(
    session: Session,
    *,
    ticket_id: str,
    status: str | None = None,
    resolution: str | None = None,
    notes: str | None = None,
) -> ToolResult:
    ticket = session.get(m.CrmTicket, ticket_id)
    if ticket is None:
        return failure("ticket_not_found", f"No ticket {ticket_id}.")
    if status:
        ticket.status = status
        if status == "CLOSED":
            ticket.closed_at = utcnow()
    if resolution:
        ticket.resolution = resolution
    if notes:
        ticket.notes = notes
    session.flush()
    return success(ticket_id=ticket.id, status=ticket.status)


@simulated_call("crm", "GetCustomer")
def get_customer(session: Session, *, customer_id: str) -> ToolResult:
    """Customer plus support history — the long-term memory read."""
    customer = session.get(m.Customer, customer_id)
    if customer is None:
        return failure("customer_not_found", f"No customer {customer_id}.")

    tickets = session.scalars(
        select(m.CrmTicket)
        .where(m.CrmTicket.customer_id == customer_id)
        .order_by(m.CrmTicket.created_at.desc())
    ).all()
    returns = session.scalars(
        select(m.Return).where(m.Return.customer_id == customer_id)
    ).all()

    return success(
        customer={
            "id": customer.id,
            "name": customer.full_name,
            "email": customer.email,
            "loyalty_tier": customer.loyalty_tier,
            "risk_score": customer.risk_score,
            "vip": customer.vip,
            "preferences": customer.preferences,
        },
        ticket_history=[
            {
                "id": t.id,
                "category": t.category,
                "subject": t.subject,
                "status": t.status,
                "resolution": t.resolution,
                "csat_score": t.csat_score,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tickets
        ],
        return_history=[
            {
                "id": r.id,
                "order_id": r.order_id,
                "status": r.status,
                "reason": r.reason,
                "resolution_note": r.resolution_note,
            }
            for r in returns
        ],
    )


@simulated_call("crm", "LogConversation")
def log_conversation(
    session: Session, *, conversation_id: str, summary: str, customer_id: str
) -> ToolResult:
    session.add(
        m.AuditLog(
            event="conversation_logged",
            actor="agent",
            conversation_id=conversation_id,
            subject_id=customer_id,
            detail={"summary": summary},
        )
    )
    session.flush()
    return success(conversation_id=conversation_id)


@simulated_call("crm", "EscalateTicket")
def escalate_ticket(session: Session, *, ticket_id: str, reason: str) -> ToolResult:
    """Flag for /admin. Replaces AssignAgent — there is no human agent roster."""
    ticket = session.get(m.CrmTicket, ticket_id)
    if ticket is None:
        return failure("ticket_not_found", f"No ticket {ticket_id}.")
    ticket.escalated = True
    ticket.priority = "HIGH"
    ticket.notes = f"{ticket.notes or ''}\nEscalated: {reason}".strip()
    session.flush()
    return success(ticket_id=ticket.id, escalated=True)
