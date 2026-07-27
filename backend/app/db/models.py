"""Database schema.

Three groups of tables:

  core        the app's own domain — customers, orders, conversations, audit
  fake        stand-ins for the external systems, named for the system they
              impersonate; the simulators read and write these
  vector      the RAG corpus

Money is stored in integer minor units everywhere. No floats touch an amount.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


# --------------------------------------------------------------------------
# core
# --------------------------------------------------------------------------


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    shopify_customer_id: Mapped[str | None] = mapped_column(String(128))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128))
    first_name: Mapped[str] = mapped_column(String(64))
    last_name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    loyalty_tier: Mapped[str] = mapped_column(String(16), default="STANDARD")
    lifetime_spend_cents: Mapped[int] = mapped_column(Integer, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    vip: Mapped[bool] = mapped_column(Boolean, default=False)
    default_address: Mapped[dict] = mapped_column(JSON, default=dict)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Product(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64))
    # STANDARD | HIGH_VALUE — read directly by the policy engine's high-value gate
    product_class: Mapped[str] = mapped_column(String(16), default="STANDARD")
    price_cents: Mapped[int] = mapped_column(Integer)
    final_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    warranty_months: Mapped[int] = mapped_column(Integer, default=12)
    description: Mapped[str] = mapped_column(Text, default="")

    variants: Mapped[list["InventoryVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class InventoryVariant(Base):
    """Fake Shopify inventory. Written by exchange reservations."""

    __tablename__ = "shopify_inventory"

    variant_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    size: Mapped[str] = mapped_column(String(32))
    color: Mapped[str] = mapped_column(String(32))
    stock: Mapped[int] = mapped_column(Integer, default=0)
    restock_date: Mapped[datetime | None] = mapped_column(TS)

    product: Mapped[Product] = relationship(back_populates="variants")


class Order(Base):
    __tablename__ = "shopify_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(32))
    placed_at: Mapped[datetime] = mapped_column(TS)
    shipped_at: Mapped[datetime | None] = mapped_column(TS)
    # The policy engine's return window is measured from here and nowhere else.
    delivered_at: Mapped[datetime | None] = mapped_column(TS)
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(TS)

    shipping_method: Mapped[str] = mapped_column(String(16), default="STANDARD")
    carrier: Mapped[str | None] = mapped_column(String(16))
    tracking_number: Mapped[str | None] = mapped_column(String(64))
    tracking_status: Mapped[str | None] = mapped_column(String(64))
    last_scan_location: Mapped[str | None] = mapped_column(String(128))
    last_scan_at: Mapped[datetime | None] = mapped_column(TS)

    subtotal_cents: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer)

    fraud_flagged: Mapped[bool] = mapped_column(Boolean, default=False)

    line_items: Mapped[list["OrderLineItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLineItem(Base):
    __tablename__ = "shopify_order_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("shopify_orders.id"))
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    variant_id: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(128))
    size: Mapped[str] = mapped_column(String(32), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_cents: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="line_items")
    product: Mapped[Product] = relationship()

    @property
    def line_total_cents(self) -> int:
        return self.unit_price_cents * self.quantity


# --------------------------------------------------------------------------
# fake external systems
# --------------------------------------------------------------------------


class Payment(Base):
    """Fake Stripe payment intent."""

    __tablename__ = "stripe_payments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    stripe_payment_intent_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(ForeignKey("shopify_orders.id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    captured_at: Mapped[datetime] = mapped_column(TS)
    # Running total. The refund simulator's over-refund guard reads this.
    refunded_cents: Mapped[int] = mapped_column(Integer, default=0)
    payment_method: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def refundable_cents(self) -> int:
        return self.amount_cents - self.refunded_cents


class Refund(Base):
    __tablename__ = "stripe_refunds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("stripe_payments.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    # Set when the refund came from an approved pending action, so an approval
    # can never be replayed into a second refund.
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_refund_idempotency"),
    )


class Return(Base):
    __tablename__ = "shopify_returns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("shopify_orders.id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(24), default="REQUESTED")
    reason: Mapped[str | None] = mapped_column(String(64))
    customer_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    label_issued_at: Mapped[datetime | None] = mapped_column(TS)
    received_at: Mapped[datetime | None] = mapped_column(TS)
    inspection_result: Mapped[str | None] = mapped_column(String(24))
    refund_amount_cents: Mapped[int | None] = mapped_column(Integer)
    refunded_at: Mapped[datetime | None] = mapped_column(TS)
    return_shipping_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    fee_waived_reason: Mapped[str | None] = mapped_column(String(32))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    line_items: Mapped[list] = mapped_column(JSON, default=list)
    # High-value returns route to inspection, not the standard warehouse (POL-RET-001)
    destination: Mapped[str] = mapped_column(String(24), default="WAREHOUSE")
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_return_idempotency"),
    )


class Exchange(Base):
    __tablename__ = "shopify_exchanges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("shopify_orders.id"))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    from_variant_id: Mapped[str] = mapped_column(String(48))
    to_variant_id: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(24), default="RESERVED")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_exchange_idempotency"),
    )


class CrmTicket(Base):
    __tablename__ = "crm_tickets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    order_id: Mapped[str | None] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(24), default="web_chat")
    category: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    resolution: Mapped[str | None] = mapped_column(Text)
    csat_score: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)


class EmailOutbox(Base):
    """Fake email. Nothing is sent; /admin renders this table."""

    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    to_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    template: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str | None] = mapped_column(String(32))
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


# --------------------------------------------------------------------------
# conversation, approval, observability
# --------------------------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    current_agent: Mapped[str | None] = mapped_column(String(32))
    current_intent: Mapped[str | None] = mapped_column(String(32))
    order_id: Mapped[str | None] = mapped_column(String(32))
    workflow_state: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    agent: Mapped[str | None] = mapped_column(String(32))
    # Reasoning, tool calls, policy verdicts — surfaced in /admin, not to the user
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class PendingAction(Base):
    """The /admin approval queue.

    A row here means a workflow is parked at AWAIT_APPROVAL with its LangGraph
    checkpoint intact. Status moves PENDING -> APPROVED/REJECTED once, then
    -> EXECUTED. The single-transition rule is what stops an approval being
    replayed into a second refund.
    """

    __tablename__ = "pending_actions"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    workflow_id: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    order_id: Mapped[str | None] = mapped_column(String(32))

    tool_name: Mapped[str] = mapped_column(String(64))
    tool_args: Mapped[dict] = mapped_column(JSON, default=dict)
    amount_cents: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL")

    # Why the policy engine stopped this — rule ids and policy ids, not prose
    policy_reasons: Mapped[list] = mapped_column(JSON, default=list)
    # Everything a reviewer needs to judge the action.
    escalation_package: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    executed_at: Mapped[datetime | None] = mapped_column(TS)
    execution_result: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_pending_actions_status", "status"),)


class ApiLog(Base):
    """Every simulated external call. Inspectable from /admin."""

    __tablename__ = "api_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system: Mapped[str] = mapped_column(String(24))  # shopify | stripe | crm | email
    operation: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(48))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    conversation_id: Mapped[str | None] = mapped_column(String(48))
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    __table_args__ = (Index("ix_api_logs_created", "created_at"),)


class LlmCall(Base):
    """One model invocation.

    Kept separate from `api_logs`: those are simulated external systems, these
    are real spend. Tokens and cost are recorded per call so /admin can show
    automation rate against what it actually cost to get there.
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str | None] = mapped_column(String(48))
    agent: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Micro-dollars: cost per call is far below a cent and floats would round it
    # to zero. Same reasoning as money in minor units everywhere else.
    cost_micros: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    __table_args__ = (Index("ix_llm_calls_created", "created_at"),)


class AuditLog(Base):
    """Decisions, not calls: policy verdicts, approvals, rejections."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(32))  # agent | admin | system
    conversation_id: Mapped[str | None] = mapped_column(String(48))
    subject_id: Mapped[str | None] = mapped_column(String(48))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class PolicyRule(Base):
    """Runtime parameters for the deterministic gates.

    The engine reads thresholds from here rather than from constants, so /admin
    can change a threshold without a redeploy — and so a test can change one
    without monkeypatching.
    """

    __tablename__ = "policy_rules"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_int: Mapped[int | None] = mapped_column(Integer)
    value_text: Mapped[str | None] = mapped_column(String(255))
    policy_id: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------
# vector
# --------------------------------------------------------------------------

EMBEDDING_DIM = 768  # Vertex text-embedding-005


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(32))
    # binding | informational — a binding chunk outranks an informational one
    authority: Mapped[str] = mapped_column(String(24), default="informational")
    applies_to: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[str] = mapped_column(String(16), default="")
    source_file: Mapped[str] = mapped_column(String(128))
    heading: Mapped[str | None] = mapped_column(String(255))
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    # Null until seeding runs with Vertex credentials
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    __table_args__ = (
        UniqueConstraint("source_file", "chunk_index", name="uq_chunk_source_index"),
    )
