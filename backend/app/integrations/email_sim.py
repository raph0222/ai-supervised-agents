"""Fake email.

Nothing is sent. Every message lands in `email_outbox`, which /admin renders —
so "did the customer get their label" is answerable by looking at a table.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import models as m
from app.integrations.base import ToolResult, failure, simulated_call, success


def _deliver(
    session: Session,
    *,
    customer_id: str,
    template: str,
    subject: str,
    body: str,
    order_id: str | None = None,
    attachments: list[dict] | None = None,
) -> ToolResult:
    customer = session.get(m.Customer, customer_id)
    if customer is None:
        return failure("customer_not_found", f"No customer {customer_id}.")
    message = m.EmailOutbox(
        to_email=customer.email,
        subject=subject,
        body=body,
        template=template,
        order_id=order_id,
        attachments=attachments or [],
    )
    session.add(message)
    session.flush()
    return success(message_id=message.id, to=customer.email, template=template)


@simulated_call("email", "SendConfirmation")
def send_confirmation(
    session: Session, *, customer_id: str, subject: str, body: str,
    order_id: str | None = None,
) -> ToolResult:
    return _deliver(
        session,
        customer_id=customer_id,
        template="confirmation",
        subject=subject,
        body=body,
        order_id=order_id,
    )


@simulated_call("email", "SendReturnLabel")
def send_return_label(
    session: Session, *, customer_id: str, return_id: str, label_url: str,
    order_id: str | None = None,
) -> ToolResult:
    return _deliver(
        session,
        customer_id=customer_id,
        template="return_label",
        subject=f"Your return label ({return_id})",
        body=(
            f"Your return {return_id} is confirmed. The prepaid label is attached.\n"
            "You have 14 days to ship the item back before the label expires."
        ),
        order_id=order_id,
        attachments=[{"filename": f"{return_id}.pdf", "url": label_url}],
    )


@simulated_call("email", "NotifySupport")
def notify_support(
    session: Session, *, customer_id: str, subject: str, body: str
) -> ToolResult:
    return _deliver(
        session,
        customer_id=customer_id,
        template="support_notice",
        subject=subject,
        body=body,
    )
