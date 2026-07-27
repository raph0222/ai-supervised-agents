"""Fake Shopify.

Reads and writes `shopify_orders`, `shopify_returns`, `shopify_exchanges` and
`shopify_inventory`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m
from app.integrations.base import ToolResult, failure, simulated_call, success
from app.seed import simulate


@simulated_call("shopify", "GetOrder")
def get_order(session: Session, *, order_id: str) -> ToolResult:
    order = session.get(m.Order, order_id)
    if order is None:
        return failure("order_not_found", f"No order {order_id}.")

    # Rigged malformed response: line_items dropped and total sent as
    # a string. The caller is expected to validate the shape rather than trust it.
    if simulate.has(order_id, simulate.MALFORMED_PAYLOAD):
        payload = _serialise_order(session, order)
        payload.pop("line_items", None)
        payload["total_cents"] = str(payload["total_cents"])
        return success(order=payload)

    return success(order=_serialise_order(session, order))


@simulated_call("shopify", "GetCustomer")
def get_customer(session: Session, *, customer_id: str) -> ToolResult:
    customer = session.get(m.Customer, customer_id)
    if customer is None:
        return failure("customer_not_found", f"No customer {customer_id}.")
    return success(
        customer={
            "id": customer.id,
            "name": customer.full_name,
            "email": customer.email,
            "loyalty_tier": customer.loyalty_tier,
            "lifetime_spend_cents": customer.lifetime_spend_cents,
            "order_count": customer.order_count,
            "risk_score": customer.risk_score,
            "vip": customer.vip,
            "preferences": customer.preferences,
        }
    )


@simulated_call("shopify", "InventoryLookup")
def inventory_lookup(session: Session, *, variant_id: str) -> ToolResult:
    variant = session.get(m.InventoryVariant, variant_id)
    if variant is None:
        return failure("variant_not_found", f"No variant {variant_id}.")

    permanently_out = simulate.has(variant_id, simulate.PERMANENT_OUT_OF_STOCK)
    return success(
        variant_id=variant.variant_id,
        sku=variant.sku,
        size=variant.size,
        color=variant.color,
        in_stock=variant.stock > 0,
        stock=variant.stock,
        restock_date=(
            None if permanently_out or variant.restock_date is None
            else variant.restock_date.isoformat()
        ),
    )


@simulated_call("shopify", "FulfillmentStatus")
def fulfillment_status(session: Session, *, order_id: str) -> ToolResult:
    order = session.get(m.Order, order_id)
    if order is None:
        return failure("order_not_found", f"No order {order_id}.")
    return success(
        order_id=order.id,
        status=order.status,
        carrier=order.carrier,
        tracking_number=order.tracking_number,
        tracking_status=order.tracking_status,
        last_scan_location=order.last_scan_location,
        last_scan_at=order.last_scan_at.isoformat() if order.last_scan_at else None,
        shipped_at=order.shipped_at.isoformat() if order.shipped_at else None,
        delivered_at=order.delivered_at.isoformat() if order.delivered_at else None,
        estimated_delivery_at=(
            order.estimated_delivery_at.isoformat()
            if order.estimated_delivery_at
            else None
        ),
    )


@simulated_call("shopify", "CreateReturn")
def create_return(
    session: Session,
    *,
    order_id: str,
    reason: str,
    line_items: list[dict] | None = None,
    customer_note: str | None = None,
    idempotency_key: str | None = None,
) -> ToolResult:
    order = session.get(m.Order, order_id)
    if order is None:
        return failure("order_not_found", f"No order {order_id}.")

    if idempotency_key:
        existing = session.scalar(
            select(m.Return).where(m.Return.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return success(return_id=existing.id, status=existing.status, replayed=True)

    items = line_items or [
        {"sku": li.sku, "variant_id": li.variant_id, "title": li.title,
         "size": li.size, "quantity": li.quantity}
        for li in order.line_items
    ]

    # High-value returns ship to inspection, not the standard warehouse (POL-RET-001)
    is_high_value = any(
        (p := session.get(m.Product, i["sku"])) and p.product_class == "HIGH_VALUE"
        for i in items
    )
    customer = session.get(m.Customer, order.customer_id)
    fee = 0 if customer.loyalty_tier in ("GOLD", "PLATINUM") else 795

    ret = m.Return(
        id=f"RET-{uuid.uuid4().hex[:8].upper()}",
        order_id=order_id,
        customer_id=order.customer_id,
        status="LABEL_ISSUED",
        reason=reason,
        customer_note=customer_note,
        line_items=items,
        return_shipping_fee_cents=fee,
        fee_waived_reason=(
            f"{customer.loyalty_tier}_TIER" if fee == 0 else None
        ),
        destination="INSPECTION" if is_high_value else "WAREHOUSE",
        idempotency_key=idempotency_key,
    )
    session.add(ret)
    if order.status == "DELIVERED":
        order.status = "RETURN_IN_PROGRESS"
    session.flush()

    return success(
        return_id=ret.id,
        status=ret.status,
        order_status=order.status,
        destination=ret.destination,
        return_shipping_fee_cents=fee,
        label_url=f"https://labels.northbridge.test/{ret.id}.pdf",
    )


@simulated_call("shopify", "CreateExchange")
def create_exchange(
    session: Session,
    *,
    order_id: str,
    from_variant_id: str,
    to_variant_id: str,
    idempotency_key: str | None = None,
) -> ToolResult:
    order = session.get(m.Order, order_id)
    if order is None:
        return failure("order_not_found", f"No order {order_id}.")

    target = session.get(m.InventoryVariant, to_variant_id)
    if target is None:
        return failure("variant_not_found", f"No variant {to_variant_id}.")

    # Never place an exchange against zero inventory (POL-EXC-001). The agent is
    # expected to check first; this is the backstop if it does not.
    if target.stock <= 0:
        permanently_out = simulate.has(to_variant_id, simulate.PERMANENT_OUT_OF_STOCK)
        return failure(
            "out_of_stock",
            f"Variant {to_variant_id} is out of stock.",
            variant_id=to_variant_id,
            restock_date=(
                None if permanently_out or target.restock_date is None
                else target.restock_date.isoformat()
            ),
            backorder_available=not permanently_out and target.restock_date is not None,
        )

    if idempotency_key:
        existing = session.scalar(
            select(m.Exchange).where(m.Exchange.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return success(exchange_id=existing.id, status=existing.status, replayed=True)

    exchange = m.Exchange(
        id=f"EXC-{uuid.uuid4().hex[:8].upper()}",
        order_id=order_id,
        customer_id=order.customer_id,
        from_variant_id=from_variant_id,
        to_variant_id=to_variant_id,
        idempotency_key=idempotency_key,
    )
    session.add(exchange)
    target.stock -= 1  # reserve it
    session.flush()

    return success(
        exchange_id=exchange.id,
        status=exchange.status,
        to_variant_id=to_variant_id,
        remaining_stock=target.stock,
        label_url=f"https://labels.northbridge.test/{exchange.id}.pdf",
    )


@simulated_call("shopify", "CancelOrder")
def cancel_order(session: Session, *, order_id: str, reason: str = "") -> ToolResult:
    order = session.get(m.Order, order_id)
    if order is None:
        return failure("order_not_found", f"No order {order_id}.")
    # Once a tracking number exists the parcel is with the carrier (POL-SHP-001)
    if order.tracking_number:
        return failure(
            "already_shipped",
            "Order has shipped and cannot be cancelled; it must be returned.",
            tracking_number=order.tracking_number,
        )
    order.status = "CANCELLED"
    session.flush()
    return success(order_id=order_id, status=order.status)


def _serialise_order(session: Session, order: m.Order) -> dict:
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "status": order.status,
        "placed_at": order.placed_at.isoformat() if order.placed_at else None,
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "carrier": order.carrier,
        "tracking_number": order.tracking_number,
        "tracking_status": order.tracking_status,
        "subtotal_cents": order.subtotal_cents,
        "shipping_cents": order.shipping_cents,
        "tax_cents": order.tax_cents,
        "total_cents": order.total_cents,
        "line_items": [
            {
                "sku": li.sku,
                "variant_id": li.variant_id,
                "title": li.title,
                "size": li.size,
                "quantity": li.quantity,
                "unit_price_cents": li.unit_price_cents,
                "product_class": (
                    p.product_class if (p := session.get(m.Product, li.sku)) else "STANDARD"
                ),
                "final_sale": bool(p and p.final_sale),
            }
            for li in order.line_items
        ],
    }
