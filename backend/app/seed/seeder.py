"""Load seed/ into the database.

Seed rows carry `*_days_ago` / `*_days_from_now` offsets resolved against now()
at seed time, so the scenarios don't rot out of the return window.

Everything here works with an empty environment; embedding the knowledge corpus
is a separate optional step (see embed_knowledge).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models as m
from app.seed import simulate

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _offset(payload: dict[str, Any], key: str, base: datetime) -> datetime | None:
    """Resolve `<key>_days_ago` / `<key>_days_from_now` into a timestamp."""
    ago = payload.get(f"{key}_days_ago")
    if ago is not None:
        return base - timedelta(days=ago)
    ahead = payload.get(f"{key}_days_from_now")
    if ahead is not None:
        return base + timedelta(days=ahead)
    return None


def _load(seed_dir: Path, name: str) -> dict[str, Any]:
    with open(seed_dir / "data" / name, encoding="utf-8") as fh:
        return json.load(fh)


def _take_simulate(payload: dict[str, Any], entity_id: str) -> None:
    raw = payload.get("_simulate")
    if raw:
        behaviour = simulate.register(entity_id, raw)
        log.info("simulate: %s -> %s", entity_id, behaviour)


# --------------------------------------------------------------------------


def is_empty(session: Session) -> bool:
    return session.scalar(select(func.count()).select_from(m.Customer)) == 0


def seed_all(session: Session, seed_dir: Path | None = None, *, base: datetime | None = None) -> dict[str, int]:
    """Load every seed file. Assumes empty tables — call reseed() to replace."""
    settings = get_settings()
    seed_dir = Path(seed_dir or settings.seed_dir)
    base = base or _now()

    counts = {
        "customers": _seed_customer(session, seed_dir),
        "products": 0,
        "variants": 0,
        "orders": 0,
        "payments": 0,
        "returns": 0,
        "tickets": 0,
        "policy_rules": _seed_policy_rules(session, settings),
    }
    counts["products"], counts["variants"] = _seed_products(session, seed_dir, base)
    counts["orders"] = _seed_orders(session, seed_dir, base)
    counts["payments"] = _seed_payments(session, seed_dir, base)
    counts["returns"] = _seed_returns(session, seed_dir, base)
    counts["tickets"] = _seed_tickets(session, seed_dir, base)

    session.flush()
    log.info("seeded: %s", counts)
    return counts


def reseed(session: Session, seed_dir: Path | None = None) -> dict[str, int]:
    """Truncate everything the seeder owns, then reload. Idempotent.

    Deletes in FK-dependency order. Conversations and messages go too, rather
    than being left pointing at regenerated orders.
    """
    simulate.clear()
    for model in (
        m.Message,
        # Pending actions reference the conversation, so they go first.
        m.PendingAction,
        m.Conversation,
        m.ApiLog,
        m.AuditLog,
        m.LlmCall,
        m.EmailOutbox,
        m.CrmTicket,
        m.Refund,
        m.Payment,
        m.Exchange,
        m.Return,
        m.OrderLineItem,
        m.Order,
        m.InventoryVariant,
        m.Product,
        m.Customer,
        m.PolicyRule,
    ):
        session.query(model).delete()
    session.flush()
    return seed_all(session, seed_dir)


# --------------------------------------------------------------------------


def _seed_customer(session: Session, seed_dir: Path) -> int:
    payload = _load(seed_dir, "customer.json")["customer"]
    base = _now()
    session.add(
        m.Customer(
            id=payload["id"],
            shopify_customer_id=payload.get("shopify_customer_id"),
            stripe_customer_id=payload.get("stripe_customer_id"),
            first_name=payload["first_name"],
            last_name=payload["last_name"],
            email=payload["email"],
            phone=payload.get("phone"),
            loyalty_tier=payload.get("loyalty_tier", "STANDARD"),
            lifetime_spend_cents=payload.get("lifetime_spend_cents", 0),
            order_count=payload.get("order_count", 0),
            risk_score=payload.get("risk_score", 0),
            vip=payload.get("vip", False),
            default_address=payload.get("default_address", {}),
            preferences=payload.get("preferences", {}),
            created_at=_offset(payload, "created", base) or base,
        )
    )
    return 1


def _seed_products(session: Session, seed_dir: Path, base: datetime) -> tuple[int, int]:
    products = _load(seed_dir, "products.json")["products"]
    variant_count = 0
    for p in products:
        session.add(
            m.Product(
                sku=p["sku"],
                title=p["title"],
                category=p["category"],
                product_class=p.get("product_class", "STANDARD"),
                price_cents=p["price_cents"],
                final_sale=p.get("final_sale", False),
                warranty_months=p.get("warranty_months", 12),
                description=p.get("description", ""),
            )
        )
        for v in p["variants"]:
            _take_simulate(v, v["variant_id"])
            restock = None
            if v.get("restock_days_from_now") is not None:
                restock = base + timedelta(days=v["restock_days_from_now"])
            session.add(
                m.InventoryVariant(
                    variant_id=v["variant_id"],
                    sku=p["sku"],
                    size=v["size"],
                    color=v["color"],
                    stock=v.get("stock", 0),
                    restock_date=restock,
                )
            )
            variant_count += 1
    session.flush()
    return len(products), variant_count


def _seed_orders(session: Session, seed_dir: Path, base: datetime) -> int:
    orders = _load(seed_dir, "orders.json")["orders"]
    for o in orders:
        _take_simulate(o, o["id"])
        session.add(
            m.Order(
                id=o["id"],
                customer_id=o["customer_id"],
                status=o["status"],
                placed_at=_offset(o, "placed", base),
                shipped_at=_offset(o, "shipped", base),
                delivered_at=_offset(o, "delivered", base),
                estimated_delivery_at=_offset(o, "estimated_delivery", base),
                shipping_method=o.get("shipping_method", "STANDARD"),
                carrier=o.get("carrier"),
                tracking_number=o.get("tracking_number"),
                tracking_status=o.get("tracking_status"),
                last_scan_location=o.get("last_scan_location"),
                last_scan_at=_offset(o, "last_scan", base),
                subtotal_cents=o["subtotal_cents"],
                shipping_cents=o.get("shipping_cents", 0),
                tax_cents=o.get("tax_cents", 0),
                total_cents=o["total_cents"],
                fraud_flagged=o.get("fraud_flagged", False),
            )
        )
        for li in o["line_items"]:
            session.add(
                m.OrderLineItem(
                    order_id=o["id"],
                    sku=li["sku"],
                    variant_id=li["variant_id"],
                    title=li["title"],
                    size=li.get("size", ""),
                    quantity=li.get("quantity", 1),
                    unit_price_cents=li["unit_price_cents"],
                )
            )
    session.flush()
    return len(orders)


def _seed_payments(session: Session, seed_dir: Path, base: datetime) -> int:
    payments = _load(seed_dir, "payments.json")["payments"]
    for p in payments:
        _take_simulate(p, p["id"])
        session.add(
            m.Payment(
                id=p["id"],
                stripe_payment_intent_id=p["stripe_payment_intent_id"],
                order_id=p["order_id"],
                customer_id=p["customer_id"],
                amount_cents=p["amount_cents"],
                currency=p.get("currency", "usd"),
                status=p.get("status", "succeeded"),
                captured_at=_offset(p, "captured", base),
                refunded_cents=p.get("refunded_cents", 0),
                payment_method=p.get("payment_method", {}),
            )
        )
        # A historical refund needs a matching stripe_refunds row, or the
        # over-refund guard would let this payment be refunded a second time.
        if p.get("refunded_cents"):
            session.add(
                m.Refund(
                    id=f"RFD-{p['id'].split('-')[-1]}",
                    payment_id=p["id"],
                    amount_cents=p["refunded_cents"],
                    status="succeeded",
                    reason="historical",
                    created_at=_offset(p, "refunded", base) or base,
                )
            )
    session.flush()
    return len(payments)


def _seed_returns(session: Session, seed_dir: Path, base: datetime) -> int:
    returns = _load(seed_dir, "returns.json")["returns"]
    for r in returns:
        session.add(
            m.Return(
                id=r["id"],
                order_id=r["order_id"],
                customer_id=r["customer_id"],
                status=r["status"],
                reason=r.get("reason"),
                customer_note=r.get("customer_note"),
                created_at=_offset(r, "created", base) or base,
                label_issued_at=_offset(r, "label_issued", base),
                received_at=_offset(r, "received", base),
                inspection_result=r.get("inspection_result"),
                refund_amount_cents=r.get("refund_amount_cents"),
                refunded_at=_offset(r, "refunded", base),
                return_shipping_fee_cents=r.get("return_shipping_fee_cents", 0),
                fee_waived_reason=r.get("fee_waived_reason"),
                resolution_note=r.get("resolution_note"),
                line_items=r.get("line_items", []),
            )
        )
    session.flush()
    return len(returns)


def _seed_tickets(session: Session, seed_dir: Path, base: datetime) -> int:
    tickets = _load(seed_dir, "crm_tickets.json")["tickets"]
    for t in tickets:
        session.add(
            m.CrmTicket(
                id=t["id"],
                customer_id=t["customer_id"],
                order_id=t.get("order_id"),
                channel=t.get("channel", "web_chat"),
                category=t["category"],
                subject=t["subject"],
                status=t.get("status", "OPEN"),
                priority=t.get("priority", "NORMAL"),
                created_at=_offset(t, "created", base) or base,
                closed_at=_offset(t, "closed", base),
                resolution=t.get("resolution"),
                csat_score=t.get("csat_score"),
                notes=t.get("notes"),
            )
        )
    session.flush()
    return len(tickets)


def _seed_policy_rules(session: Session, settings) -> int:
    """Bootstrap the deterministic gates' parameters.

    The engine reads thresholds from these rows, not from settings, so /admin can
    change one without a redeploy.
    """
    rules = [
        m.PolicyRule(
            key="refund_auto_approve_under_cents",
            value_int=settings.refund_auto_approve_under_cents,
            policy_id="POL-REF-001",
            description="Refunds strictly below this aggregate amount auto-approve.",
        ),
        m.PolicyRule(
            key="return_window_days",
            value_int=settings.return_window_days,
            policy_id="POL-RET-001",
            description="Days from delivery within which a return is allowed.",
        ),
        m.PolicyRule(
            key="risk_score_approval_threshold",
            value_int=settings.risk_score_approval_threshold,
            policy_id="POL-REF-001",
            description="Customer risk score above which every action needs approval.",
        ),
        m.PolicyRule(
            key="high_value_always_requires_approval",
            value_int=1,
            policy_id="POL-RET-001",
            description="Any HIGH_VALUE line item requires approval regardless of amount.",
        ),
        m.PolicyRule(
            key="max_refund_retries",
            value_int=2,
            policy_id="POL-REF-001",
            description="Refund decline retries before escalating.",
        ),
        m.PolicyRule(
            key="exchange_restock_window_days",
            value_int=14,
            policy_id="POL-EXC-001",
            description="Backorder an exchange only if restock is within this many days.",
        ),
    ]
    session.add_all(rules)
    session.flush()
    return len(rules)


# --------------------------------------------------------------------------


def seed_if_empty(session: Session, seed_dir: Path | None = None) -> dict[str, int] | None:
    if not is_empty(session):
        # Rigged-failure registry is in-process, so it must be rebuilt on every
        # boot even when the tables are already populated.
        _rebuild_simulate_registry(seed_dir)
        return None
    return seed_all(session, seed_dir)


def seed_knowledge(session: Session, seed_dir: Path | None = None) -> dict[str, Any]:
    """Chunk seed/knowledge/ into pgvector, embedding only if Vertex is set up.

    Chunking is free and always runs; embedding needs credentials the app does
    not require. Without them the chunks land with NULL vectors.
    """
    from app.rag import embeddings, store  # local import: keeps seeding LLM-free

    result = store.load_corpus(session, seed_dir).as_dict()
    result["embedded"] = 0
    result["embeddings_skipped"] = not embeddings.is_configured()
    if embeddings.is_configured():
        result["embedded"] = store.embed_missing(session)
    else:
        log.warning(
            "knowledge chunks loaded without embeddings (%s unset); "
            "re-run seeding with credentials to populate the vector corpus",
            ", ".join(get_settings().missing_vertex_vars()),
        )
    return result


def _rebuild_simulate_registry(seed_dir: Path | None = None) -> None:
    seed_dir = Path(seed_dir or get_settings().seed_dir)
    simulate.clear()
    for filename, key, id_field in (
        ("orders.json", "orders", "id"),
        ("payments.json", "payments", "id"),
    ):
        for row in _load(seed_dir, filename)[key]:
            _take_simulate(row, row[id_field])
    for product in _load(seed_dir, "products.json")["products"]:
        for variant in product["variants"]:
            _take_simulate(variant, variant["variant_id"])
