"""Memory.

Short-term is the message list. Long-term is derived from rows that already
exist — past returns, tickets, stored preferences — rather than from a
summarisation pass, so it cannot invent a preference the customer never stated.

Everything renders into a compact text block, since prompts are the consumer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m

# How much conversation the agents see. Long enough to resolve "the other one",
# short enough that an old order id does not keep resurfacing.
SHORT_TERM_TURNS = 12


@dataclass
class Memory:
    customer: dict
    preferences: dict
    recent_orders: list[dict] = field(default_factory=list)
    previous_returns: list[dict] = field(default_factory=list)
    past_tickets: list[dict] = field(default_factory=list)
    communication_style: str = "neutral"

    def as_dict(self) -> dict:
        return {
            "customer": self.customer,
            "preferences": self.preferences,
            "recent_orders": self.recent_orders,
            "previous_returns": self.previous_returns,
            "past_tickets": self.past_tickets,
            "communication_style": self.communication_style,
        }

    def render(self) -> str:
        lines = [
            f"Customer: {self.customer['name']} ({self.customer['id']})",
            f"Loyalty tier: {self.customer['loyalty_tier']}  "
            f"Lifetime spend: ${self.customer['lifetime_spend_cents'] / 100:.2f}  "
            f"Orders: {self.customer['order_count']}",
            f"Risk score: {self.customer['risk_score']}"
            + ("  VIP" if self.customer.get("vip") else ""),
        ]
        if self.preferences:
            pref = ", ".join(f"{k}={v}" for k, v in self.preferences.items())
            lines.append(f"Preferences: {pref}")
        lines.append(f"Preferred tone: {self.communication_style}")

        if self.recent_orders:
            lines.append("\nRecent orders:")
            lines += [
                f"  {o['id']}  {o['status']:<12} ${o['total_cents'] / 100:>8.2f}  "
                f"{o['summary']}" + (f"  delivered {o['days_since_delivery']}d ago"
                                     if o["days_since_delivery"] is not None else "")
                for o in self.recent_orders
            ]
        if self.previous_returns:
            lines.append("\nPrevious returns:")
            lines += [
                f"  {r['id']} on {r['order_id']}: {r['status']} ({r['reason']})"
                for r in self.previous_returns
            ]
        if self.past_tickets:
            lines.append("\nPast support tickets:")
            lines += [
                f"  {t['id']} {t['category']}: {t['subject']} -> {t['status']}"
                + (f" (CSAT {t['csat_score']})" if t["csat_score"] else "")
                for t in self.past_tickets
            ]
        return "\n".join(lines)


def load(session: Session, customer_id: str, *, now=None) -> Memory:
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    customer = session.get(m.Customer, customer_id)
    if customer is None:
        raise ValueError(f"unknown customer {customer_id}")

    orders = session.scalars(
        select(m.Order)
        .where(m.Order.customer_id == customer_id)
        .order_by(m.Order.placed_at.desc())
    ).all()

    recent_orders = []
    for order in orders:
        delivered = order.delivered_at
        days = None
        if delivered is not None:
            aware = delivered if delivered.tzinfo else delivered.replace(tzinfo=timezone.utc)
            days = (now - aware).days
        recent_orders.append({
            "id": order.id,
            "status": order.status,
            "total_cents": order.total_cents,
            "days_since_delivery": days,
            "summary": ", ".join(
                f"{li.title}{f' ({li.size})' if li.size else ''}" for li in order.line_items
            ) or "(no line items)",
        })

    returns = session.scalars(
        select(m.Return)
        .where(m.Return.customer_id == customer_id)
        .order_by(m.Return.created_at.desc())
    ).all()
    tickets = session.scalars(
        select(m.CrmTicket)
        .where(m.CrmTicket.customer_id == customer_id)
        .order_by(m.CrmTicket.created_at.desc())
    ).all()

    preferences = dict(customer.preferences or {})
    style = str(preferences.pop("communication_style", None) or _infer_style(tickets))

    return Memory(
        customer={
            "id": customer.id,
            "name": customer.full_name,
            "email": customer.email,
            "loyalty_tier": customer.loyalty_tier,
            "lifetime_spend_cents": customer.lifetime_spend_cents,
            "order_count": customer.order_count,
            "risk_score": customer.risk_score,
            "vip": customer.vip,
        },
        preferences=preferences,
        recent_orders=recent_orders,
        previous_returns=[
            {"id": r.id, "order_id": r.order_id, "status": r.status,
             "reason": r.reason or "unstated"}
            for r in returns
        ],
        past_tickets=[
            {"id": t.id, "category": t.category, "subject": t.subject,
             "status": t.status, "csat_score": t.csat_score}
            for t in tickets
        ],
        communication_style=style,
    )


def conversation_history(session: Session, conversation_id: str, *, limit: int = SHORT_TERM_TURNS) -> list[dict]:
    rows = session.scalars(
        select(m.Message)
        .where(m.Message.conversation_id == conversation_id)
        .order_by(m.Message.created_at.desc(), m.Message.id.desc())
        .limit(limit)
    ).all()
    return [
        {"role": r.role, "content": r.content, "agent": r.agent}
        for r in reversed(rows)
    ]


def render_history(history: list[dict]) -> str:
    if not history:
        return "(no previous messages in this conversation)"
    return "\n".join(
        f"{turn['role'].upper()}: {turn['content']}" for turn in history
    )


def _infer_style(tickets) -> str:
    """Fall back to the tone the customer's history suggests.

    Explicit preference wins; this only fills the gap. A customer whose tickets
    all closed with high CSAT and short notes gets the concise treatment.
    """
    if not tickets:
        return "neutral"
    scored = [t.csat_score for t in tickets if t.csat_score]
    if scored and sum(scored) / len(scored) >= 4.5:
        return "warm, concise"
    return "neutral"
