"""Deterministic policy engine.

Reasons over database rows, never over model output — the amount comes from
`stripe_payments`, the delivery date from `shopify_orders`, the product class
from `products`. Returns a structured verdict for the agent to explain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import models as m


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"


# When several rules fire, the worst outcome wins.
_SEVERITY = {Decision.ALLOW: 0, Decision.REQUIRES_APPROVAL: 1, Decision.DENY: 2}


@dataclass(frozen=True)
class Reason:
    rule: str
    policy_id: str
    detail: str
    decision: Decision


@dataclass
class Verdict:
    decision: Decision
    reasons: list[Reason] = field(default_factory=list)
    computed: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def blocking_reasons(self) -> list[Reason]:
        return [r for r in self.reasons if r.decision is not Decision.ALLOW]

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reasons": [
                {
                    "rule": r.rule,
                    "policy_id": r.policy_id,
                    "detail": r.detail,
                    "decision": r.decision.value,
                }
                for r in self.reasons
            ],
            "computed": self.computed,
        }


class PolicyEngine:
    """Evaluates hard gates. Thresholds come from `policy_rules`, so /admin can
    change one without a redeploy."""

    def __init__(self, session: Session):
        self.session = session
        self._rules = {
            r.key: r for r in session.scalars(select(m.PolicyRule)).all()
        }

    def _int(self, key: str, default: int) -> int:
        rule = self._rules.get(key)
        return default if rule is None or rule.value_int is None else rule.value_int

    def _policy_id(self, key: str, default: str) -> str:
        rule = self._rules.get(key)
        return rule.policy_id if rule else default

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def evaluate_return(self, order_id: str, *, now: datetime | None = None) -> Verdict:
        now = now or datetime.now(timezone.utc)
        order = self.session.get(m.Order, order_id)
        if order is None:
            return _verdict(Decision.DENY, [
                Reason("ORDER_NOT_FOUND", "POL-RET-001",
                       f"No order {order_id}.", Decision.DENY)
            ])

        reasons: list[Reason] = []
        computed: dict = {"order_id": order_id}

        reasons += self._check_window(order, now, computed)
        reasons += self._check_final_sale(order, computed)
        reasons += self._check_high_value(order, computed)
        reasons += self._check_risk(order, computed)

        return _verdict(_worst(reasons), reasons, computed)

    def evaluate_refund(
        self,
        order_id: str,
        amount_cents: int,
        *,
        now: datetime | None = None,
        include_pending: bool = True,
    ) -> Verdict:
        """Evaluate a refund of `amount_cents` against `order_id`.

        The threshold is applied to the *aggregate* exposure for the order:
        this request, plus refunds already issued, plus refunds sitting
        unapproved in the queue. That is what makes splitting a large refund
        into sub-threshold slices pointless.
        """
        now = now or datetime.now(timezone.utc)
        order = self.session.get(m.Order, order_id)
        if order is None:
            return _verdict(Decision.DENY, [
                Reason("ORDER_NOT_FOUND", "POL-REF-001",
                       f"No order {order_id}.", Decision.DENY)
            ])

        reasons: list[Reason] = []
        computed: dict = {"order_id": order_id, "requested_cents": amount_cents}

        if amount_cents <= 0:
            reasons.append(Reason(
                "INVALID_AMOUNT", "POL-REF-001",
                "Refund amount must be positive.", Decision.DENY,
            ))

        payment = self.session.scalar(
            select(m.Payment).where(m.Payment.order_id == order_id)
        )
        if payment is None:
            reasons.append(Reason(
                "PAYMENT_NOT_FOUND", "POL-REF-001",
                f"No payment recorded for order {order_id}.", Decision.DENY,
            ))
        else:
            reasons += self._check_over_refund(payment, amount_cents, computed)
            reasons += self._check_threshold(
                order, payment, amount_cents, computed, include_pending
            )

        reasons += self._check_high_value(order, computed)
        reasons += self._check_risk(order, computed)

        return _verdict(_worst(reasons), reasons, computed)

    def evaluate_exchange(
        self, order_id: str, to_variant_id: str, *, now: datetime | None = None
    ) -> Verdict:
        now = now or datetime.now(timezone.utc)
        order = self.session.get(m.Order, order_id)
        if order is None:
            return _verdict(Decision.DENY, [
                Reason("ORDER_NOT_FOUND", "POL-EXC-001",
                       f"No order {order_id}.", Decision.DENY)
            ])

        reasons: list[Reason] = []
        computed: dict = {"order_id": order_id, "to_variant_id": to_variant_id}

        # Exchanges inherit the return window and the final-sale bar (POL-EXC-001)
        reasons += self._check_window(order, now, computed, policy_id="POL-EXC-001")
        reasons += self._check_final_sale(order, computed, policy_id="POL-EXC-001")
        reasons += self._check_risk(order, computed)

        variant = self.session.get(m.InventoryVariant, to_variant_id)
        if variant is None:
            reasons.append(Reason(
                "VARIANT_NOT_FOUND", "POL-EXC-001",
                f"No variant {to_variant_id}.", Decision.DENY,
            ))
        else:
            computed["target_stock"] = variant.stock
            if variant.stock <= 0:
                # Not a policy denial — the agent should fall back to backorder
                # or refund (POL-EXC-001). Surfaced as a reason so it can say why.
                reasons.append(Reason(
                    "TARGET_OUT_OF_STOCK", "POL-EXC-001",
                    f"Variant {to_variant_id} has no stock.", Decision.DENY,
                ))

        return _verdict(_worst(reasons), reasons, computed)

    def evaluate_warranty(self, order_id: str, *, now: datetime | None = None) -> Verdict:
        """Warranty runs on product class coverage, not the return window."""
        now = now or datetime.now(timezone.utc)
        order = self.session.get(m.Order, order_id)
        if order is None:
            return _verdict(Decision.DENY, [
                Reason("ORDER_NOT_FOUND", "POL-WAR-001",
                       f"No order {order_id}.", Decision.DENY)
            ])
        if order.delivered_at is None:
            return _verdict(Decision.DENY, [
                Reason("NOT_DELIVERED", "POL-WAR-001",
                       "Order has not been delivered.", Decision.DENY)
            ])

        days = (now - _aware(order.delivered_at)).days
        months_covered = max(
            (p.warranty_months for li in order.line_items
             if (p := self.session.get(m.Product, li.sku))),
            default=0,
        )
        computed = {
            "order_id": order_id,
            "days_since_delivery": days,
            "warranty_months": months_covered,
        }
        reasons: list[Reason] = []
        if days > months_covered * 30:
            reasons.append(Reason(
                "WARRANTY_EXPIRED", "POL-WAR-001",
                f"Delivered {days} days ago; coverage is {months_covered} months.",
                Decision.DENY,
            ))
        reasons += self._check_high_value(order, computed)
        return _verdict(_worst(reasons), reasons, computed)

    # ------------------------------------------------------------------
    # gates
    # ------------------------------------------------------------------

    def _check_window(
        self, order: m.Order, now: datetime, computed: dict,
        *, policy_id: str = "POL-RET-001",
    ) -> list[Reason]:
        window = self._int("return_window_days", 30)
        computed["return_window_days"] = window

        if order.delivered_at is None:
            computed["days_since_delivery"] = None
            return [Reason(
                "NOT_DELIVERED", policy_id,
                "Order has no delivery date; the return window has not started.",
                Decision.DENY,
            )]

        days = (now - _aware(order.delivered_at)).days
        computed["days_since_delivery"] = days
        if days > window:
            return [Reason(
                "OUTSIDE_RETURN_WINDOW", policy_id,
                f"Delivered {days} days ago; the window is {window} days.",
                Decision.DENY,
            )]
        return []

    def _check_final_sale(
        self, order: m.Order, computed: dict, *, policy_id: str = "POL-RET-001"
    ) -> list[Reason]:
        final = [
            li.title for li in order.line_items
            if (p := self.session.get(m.Product, li.sku)) and p.final_sale
        ]
        computed["final_sale_items"] = final
        if final:
            return [Reason(
                "FINAL_SALE", policy_id,
                f"Final sale and not returnable: {', '.join(final)}.",
                Decision.DENY,
            )]
        return []

    def _check_high_value(self, order: m.Order, computed: dict) -> list[Reason]:
        if not self._int("high_value_always_requires_approval", 1):
            return []
        high_value = [
            li.title for li in order.line_items
            if (p := self.session.get(m.Product, li.sku)) and p.product_class == "HIGH_VALUE"
        ]
        computed["high_value_items"] = high_value
        if high_value:
            return [Reason(
                "HIGH_VALUE_ITEM", "POL-RET-001",
                f"High-value item requires inspection and approval: {', '.join(high_value)}.",
                Decision.REQUIRES_APPROVAL,
            )]
        return []

    def _check_risk(self, order: m.Order, computed: dict) -> list[Reason]:
        reasons: list[Reason] = []
        threshold = self._int("risk_score_approval_threshold", 70)
        customer = self.session.get(m.Customer, order.customer_id)
        if customer is not None:
            computed["risk_score"] = customer.risk_score
            if customer.risk_score > threshold:
                reasons.append(Reason(
                    "HIGH_RISK_CUSTOMER", "POL-REF-001",
                    f"Risk score {customer.risk_score} exceeds {threshold}.",
                    Decision.REQUIRES_APPROVAL,
                ))
        if order.fraud_flagged:
            reasons.append(Reason(
                "FRAUD_FLAGGED", "POL-REF-001",
                "Order is flagged for fraud review.",
                Decision.REQUIRES_APPROVAL,
            ))
        return reasons

    def _check_over_refund(
        self, payment: m.Payment, amount_cents: int, computed: dict
    ) -> list[Reason]:
        computed["already_refunded_cents"] = payment.refunded_cents
        computed["refundable_cents"] = payment.refundable_cents

        if payment.refundable_cents <= 0:
            return [Reason(
                "ALREADY_REFUNDED", "POL-REF-001",
                f"Payment {payment.id} is fully refunded.", Decision.DENY,
            )]
        if amount_cents > payment.refundable_cents:
            return [Reason(
                "EXCEEDS_REFUNDABLE", "POL-REF-001",
                f"Refund of {amount_cents} exceeds refundable "
                f"{payment.refundable_cents}.",
                Decision.DENY,
            )]
        return []

    def _check_threshold(
        self,
        order: m.Order,
        payment: m.Payment,
        amount_cents: int,
        computed: dict,
        include_pending: bool,
    ) -> list[Reason]:
        """The $50 gate, applied to aggregate exposure rather than this call.

        POL-REF-001 forbids splitting a refund to stay under the threshold. Prose
        cannot enforce that, so the arithmetic does: already-refunded and
        awaiting-approval amounts are added to the request before comparing.
        """
        threshold = self._int("refund_auto_approve_under_cents", 5000)
        policy_id = self._policy_id("refund_auto_approve_under_cents", "POL-REF-001")

        pending_cents = 0
        if include_pending:
            pending_cents = self.session.scalar(
                select(func.coalesce(func.sum(m.PendingAction.amount_cents), 0))
                .where(
                    m.PendingAction.order_id == order.id,
                    m.PendingAction.status == "PENDING",
                )
            ) or 0

        aggregate = amount_cents + payment.refunded_cents + pending_cents

        computed.update({
            "refund_threshold_cents": threshold,
            "pending_approval_cents": pending_cents,
            "aggregate_refund_cents": aggregate,
        })

        if aggregate >= threshold:
            detail = (
                f"Aggregate refund exposure {aggregate} reaches the {threshold} "
                f"approval threshold"
            )
            if payment.refunded_cents or pending_cents:
                detail += (
                    f" (this request {amount_cents}"
                    f" + already refunded {payment.refunded_cents}"
                    f" + awaiting approval {pending_cents})"
                )
            return [Reason(
                "REFUND_THRESHOLD", policy_id, detail + ".",
                Decision.REQUIRES_APPROVAL,
            )]
        return []


# ----------------------------------------------------------------------


def _worst(reasons: list[Reason]) -> Decision:
    if not reasons:
        return Decision.ALLOW
    return max((r.decision for r in reasons), key=lambda d: _SEVERITY[d])


def _verdict(
    decision: Decision, reasons: list[Reason], computed: dict | None = None
) -> Verdict:
    return Verdict(decision=decision, reasons=reasons, computed=computed or {})


def _aware(value: datetime) -> datetime:
    """Postgres hands back tz-aware values; be defensive for naive inputs."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
