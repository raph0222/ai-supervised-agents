"""Fake Stripe.

No network calls. Reads and writes `stripe_payments` / `stripe_refunds`.

Two invariants are enforced here rather than upstream: a refund can never exceed
the captured amount, and an approved action can never be replayed into a second
refund.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m
from app.integrations.base import ToolResult, failure, simulated_call, success
from app.seed import simulate


def _payment(session: Session, payment_id: str) -> m.Payment | None:
    return session.get(m.Payment, payment_id)


@simulated_call("stripe", "LookupPayment")
def lookup_payment(session: Session, *, payment_id: str) -> ToolResult:
    payment = _payment(session, payment_id)
    if payment is None:
        return failure("payment_not_found", f"No payment {payment_id}.")
    return success(payment=_serialise(payment))


@simulated_call("stripe", "VerifyPayment")
def verify_payment(session: Session, *, order_id: str) -> ToolResult:
    payment = session.scalar(select(m.Payment).where(m.Payment.order_id == order_id))
    if payment is None:
        return failure("payment_not_found", f"No payment for order {order_id}.")
    return success(
        payment_id=payment.id,
        status=payment.status,
        amount_cents=payment.amount_cents,
        refundable_cents=payment.refundable_cents,
    )


@simulated_call("stripe", "RefundPayment")
def refund_payment(
    session: Session,
    *,
    payment_id: str,
    amount_cents: int,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResult:
    payment = _payment(session, payment_id)
    if payment is None:
        return failure("payment_not_found", f"No payment {payment_id}.")

    # Replay guard first: if this key already produced a refund, return that same
    # refund rather than issuing another. An admin double-click must be a no-op.
    if idempotency_key:
        existing = session.scalar(
            select(m.Refund).where(m.Refund.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return success(
                refund_id=existing.id,
                status=existing.status,
                amount_cents=existing.amount_cents,
                replayed=True,
            )

    if amount_cents <= 0:
        return failure("invalid_amount", "Refund amount must be positive.")

    if amount_cents > payment.refundable_cents:
        return failure(
            "amount_too_large",
            f"Refund of {amount_cents} exceeds refundable {payment.refundable_cents}.",
            refundable_cents=payment.refundable_cents,
        )

    # Rigged decline. Checked after validation so the ordering matches
    # a real processor: we only reach the issuer once the request is well-formed.
    if simulate.has(payment_id, simulate.REFUND_ALWAYS_DECLINES):
        return failure(
            "card_declined",
            "The issuer declined the refund.",
            payment_id=payment_id,
        )

    refund = m.Refund(
        id=f"RFD-{uuid.uuid4().hex[:10].upper()}",
        payment_id=payment_id,
        amount_cents=amount_cents,
        status="succeeded",
        reason=reason,
        idempotency_key=idempotency_key,
    )
    session.add(refund)

    payment.refunded_cents += amount_cents
    if payment.refunded_cents >= payment.amount_cents:
        payment.status = "refunded"
    else:
        payment.status = "partially_refunded"
    session.flush()

    return success(
        refund_id=refund.id,
        status=refund.status,
        amount_cents=amount_cents,
        remaining_refundable_cents=payment.refundable_cents,
    )


@simulated_call("stripe", "CreateAdjustment")
def create_adjustment(
    session: Session, *, payment_id: str, amount_cents: int, note: str = ""
) -> ToolResult:
    """Partial refund without a return (POL-REF-001)."""
    return refund_payment.__wrapped__(
        session,
        payment_id=payment_id,
        amount_cents=amount_cents,
        reason=f"adjustment: {note}" if note else "adjustment",
    )


def _serialise(payment: m.Payment) -> dict:
    return {
        "id": payment.id,
        "order_id": payment.order_id,
        "amount_cents": payment.amount_cents,
        "currency": payment.currency,
        "status": payment.status,
        "refunded_cents": payment.refunded_cents,
        "refundable_cents": payment.refundable_cents,
        "captured_at": payment.captured_at.isoformat() if payment.captured_at else None,
        "payment_method": payment.payment_method,
    }
