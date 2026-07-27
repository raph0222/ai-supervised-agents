"""Tool declarations and the executor.

The LLM emits a tool name and arguments; this module decides whether the call may
happen before handing it to the integration layer. Every tool that writes or
moves money declares the policy evaluation that guards it, and `execute()` runs
that evaluation against the database rows before dispatching.

Reads are ungated — the agent needs them to explain a denial.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.db import models as m
from app.integrations import crm_sim, email_sim, shopify_sim, stripe_sim
from app.policy.engine import Decision, PolicyEngine, Verdict
from app.rag import store as rag_store

log = logging.getLogger(__name__)


class Status(str, Enum):
    EXECUTED = "EXECUTED"
    BLOCKED = "BLOCKED"       # the policy engine said DENY or REQUIRES_APPROVAL
    FAILED = "FAILED"         # the simulator returned ok: false
    UNKNOWN_TOOL = "UNKNOWN_TOOL"


@dataclass
class Outcome:
    status: Status
    tool: str
    args: dict
    result: dict = field(default_factory=dict)
    verdict: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status is Status.EXECUTED

    @property
    def needs_approval(self) -> bool:
        return (
            self.status is Status.BLOCKED
            and (self.verdict or {}).get("decision") == Decision.REQUIRES_APPROVAL.value
        )

    @property
    def denied(self) -> bool:
        return (
            self.status is Status.BLOCKED
            and (self.verdict or {}).get("decision") == Decision.DENY.value
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "tool": self.tool,
            "args": self.args,
            "result": self.result,
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]
    writes: bool = False
    # Which policy evaluation guards this tool. None means it is a read.
    gate: str | None = None


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


STR = {"type": "string"}
INT = {"type": "integer"}


# ----------------------------------------------------------------------
# declarations — names and arguments mirror the real APIs
# ----------------------------------------------------------------------

TOOLS: dict[str, ToolSpec] = {
    # --- Shopify ---------------------------------------------------------
    "Shopify.GetOrder": ToolSpec(
        "Shopify.GetOrder",
        "Fetch one order with its line items, delivery dates and tracking.",
        _schema({"order_id": STR}, ["order_id"]),
        shopify_sim.get_order,
    ),
    "Shopify.GetCustomer": ToolSpec(
        "Shopify.GetCustomer",
        "Fetch the customer profile, loyalty tier and risk score.",
        _schema({"customer_id": STR}, ["customer_id"]),
        shopify_sim.get_customer,
    ),
    "Shopify.InventoryLookup": ToolSpec(
        "Shopify.InventoryLookup",
        "Check stock and restock date for one product variant.",
        _schema({"variant_id": STR}, ["variant_id"]),
        shopify_sim.inventory_lookup,
    ),
    "Shopify.FulfillmentStatus": ToolSpec(
        "Shopify.FulfillmentStatus",
        "Carrier, tracking status, last scan and delivery estimate for an order.",
        _schema({"order_id": STR}, ["order_id"]),
        shopify_sim.fulfillment_status,
    ),
    "Shopify.CreateReturn": ToolSpec(
        "Shopify.CreateReturn",
        "Create a return and issue a prepaid label. Requires an eligible order.",
        _schema(
            {"order_id": STR, "reason": STR, "customer_note": STR},
            ["order_id", "reason"],
        ),
        shopify_sim.create_return,
        writes=True,
        gate="return",
    ),
    "Shopify.CreateExchange": ToolSpec(
        "Shopify.CreateExchange",
        "Exchange one variant for another. The target variant must be in stock.",
        _schema(
            {"order_id": STR, "from_variant_id": STR, "to_variant_id": STR},
            ["order_id", "from_variant_id", "to_variant_id"],
        ),
        shopify_sim.create_exchange,
        writes=True,
        gate="exchange",
    ),
    "Shopify.CancelOrder": ToolSpec(
        "Shopify.CancelOrder",
        "Cancel an order that has not shipped yet.",
        _schema({"order_id": STR, "reason": STR}, ["order_id"]),
        shopify_sim.cancel_order,
        writes=True,
    ),
    # --- Stripe ----------------------------------------------------------
    "Stripe.LookupPayment": ToolSpec(
        "Stripe.LookupPayment",
        "Fetch a payment: captured amount, refunded amount, remaining refundable.",
        _schema({"payment_id": STR}, ["payment_id"]),
        stripe_sim.lookup_payment,
    ),
    "Stripe.VerifyPayment": ToolSpec(
        "Stripe.VerifyPayment",
        "Confirm an order has a captured payment and how much is refundable.",
        _schema({"order_id": STR}, ["order_id"]),
        stripe_sim.verify_payment,
    ),
    "Stripe.RefundPayment": ToolSpec(
        "Stripe.RefundPayment",
        "Refund a payment, in minor units (cents). Never call this without "
        "having verified the payment first.",
        _schema(
            {"payment_id": STR, "amount_cents": INT, "reason": STR},
            ["payment_id", "amount_cents"],
        ),
        stripe_sim.refund_payment,
        writes=True,
        gate="refund",
    ),
    "Stripe.CreateAdjustment": ToolSpec(
        "Stripe.CreateAdjustment",
        "Partial refund without a physical return (goodwill, minor damage).",
        _schema(
            {"payment_id": STR, "amount_cents": INT, "note": STR},
            ["payment_id", "amount_cents"],
        ),
        stripe_sim.create_adjustment,
        writes=True,
        gate="refund",
    ),
    # --- CRM -------------------------------------------------------------
    "CRM.CreateTicket": ToolSpec(
        "CRM.CreateTicket",
        "Open a support ticket recording this interaction.",
        _schema(
            {"customer_id": STR, "category": STR, "subject": STR,
             "order_id": STR, "priority": STR, "notes": STR},
            ["customer_id", "category", "subject"],
        ),
        crm_sim.create_ticket,
        writes=True,
    ),
    "CRM.UpdateTicket": ToolSpec(
        "CRM.UpdateTicket",
        "Update a ticket's status, resolution or notes.",
        _schema({"ticket_id": STR, "status": STR, "resolution": STR, "notes": STR},
                ["ticket_id"]),
        crm_sim.update_ticket,
        writes=True,
    ),
    "CRM.GetCustomer": ToolSpec(
        "CRM.GetCustomer",
        "Customer profile with full ticket and return history.",
        _schema({"customer_id": STR}, ["customer_id"]),
        crm_sim.get_customer,
    ),
    "CRM.LogConversation": ToolSpec(
        "CRM.LogConversation",
        "Attach a summary of this conversation to the customer record.",
        _schema({"conversation_id": STR, "summary": STR, "customer_id": STR},
                ["conversation_id", "summary", "customer_id"]),
        crm_sim.log_conversation,
        writes=True,
    ),
    "CRM.EscalateTicket": ToolSpec(
        "CRM.EscalateTicket",
        "Flag a ticket for human review on /admin.",
        _schema({"ticket_id": STR, "reason": STR}, ["ticket_id", "reason"]),
        crm_sim.escalate_ticket,
        writes=True,
    ),
    # --- Email -----------------------------------------------------------
    "Email.SendConfirmation": ToolSpec(
        "Email.SendConfirmation",
        "Email the customer a confirmation of what was done.",
        _schema({"customer_id": STR, "subject": STR, "body": STR, "order_id": STR},
                ["customer_id", "subject", "body"]),
        email_sim.send_confirmation,
        writes=True,
    ),
    "Email.SendReturnLabel": ToolSpec(
        "Email.SendReturnLabel",
        "Email a prepaid return label for an existing return.",
        _schema({"customer_id": STR, "return_id": STR, "label_url": STR, "order_id": STR},
                ["customer_id", "return_id", "label_url"]),
        email_sim.send_return_label,
        writes=True,
    ),
    "Email.NotifySupport": ToolSpec(
        "Email.NotifySupport",
        "Notify the support inbox about something needing attention.",
        _schema({"customer_id": STR, "subject": STR, "body": STR},
                ["customer_id", "subject", "body"]),
        email_sim.notify_support,
        writes=True,
    ),
}


# Which tools each agent may reach. QA has none — it answers from RAG only.
AGENT_TOOLS: dict[str, list[str]] = {
    "RETURN": [
        "Shopify.GetOrder", "Shopify.FulfillmentStatus", "Stripe.VerifyPayment",
        "Shopify.CreateReturn", "Email.SendReturnLabel", "CRM.CreateTicket",
    ],
    "REFUND": [
        "Shopify.GetOrder", "Stripe.VerifyPayment", "Stripe.LookupPayment",
        "Stripe.RefundPayment", "Stripe.CreateAdjustment",
        "Email.SendConfirmation", "CRM.CreateTicket",
    ],
    "EXCHANGE": [
        "Shopify.GetOrder", "Shopify.InventoryLookup", "Shopify.CreateExchange",
        "Shopify.CreateReturn", "Email.SendConfirmation", "CRM.CreateTicket",
    ],
    "SHIPPING": [
        "Shopify.GetOrder", "Shopify.FulfillmentStatus", "Shopify.CancelOrder",
        "CRM.CreateTicket",
    ],
    "WARRANTY": [
        "Shopify.GetOrder", "Shopify.CreateReturn", "Stripe.CreateAdjustment",
        "CRM.CreateTicket", "Email.SendConfirmation",
    ],
    "DAMAGE": [
        "Shopify.GetOrder", "Shopify.FulfillmentStatus", "Stripe.CreateAdjustment",
        "Shopify.CreateReturn", "CRM.CreateTicket", "Email.SendConfirmation",
    ],
    "QA": [],
}

WRITE_TOOLS = {name for name, spec in TOOLS.items() if spec.writes}
GATED_TOOLS = {name for name, spec in TOOLS.items() if spec.gate}


def tools_for(agent: str) -> list[ToolSpec]:
    return [TOOLS[name] for name in AGENT_TOOLS.get(agent, []) if name in TOOLS]


def describe_for_prompt(agent: str) -> str:
    """The tool list as the agent sees it in its system prompt."""
    specs = tools_for(agent)
    if not specs:
        return "(none — you answer from retrieved policy only, you cannot act)"
    lines = []
    for spec in specs:
        args = ", ".join(spec.parameters["properties"])
        gate = "  [policy-gated]" if spec.gate else ""
        lines.append(f"- {spec.name}({args}) — {spec.description}{gate}")
    return "\n".join(lines)


def gemini_declarations(agent: str) -> list[dict]:
    """Function declarations in the shape Vertex function calling expects."""
    return [
        {
            "name": spec.name.replace(".", "_"),  # Vertex rejects dots in names
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in tools_for(agent)
    ]


def canonical_name(name: str) -> str:
    """Map a model-emitted name back to the registry key."""
    if name in TOOLS:
        return name
    underscored = {k.replace(".", "_"): k for k in TOOLS}
    return underscored.get(name, name)


# ----------------------------------------------------------------------
# execution
# ----------------------------------------------------------------------


def evaluate_gate(
    session: Session, tool: str, args: dict
) -> Verdict | None:
    """Run the policy evaluation that guards `tool`, or None if it is a read."""
    spec = TOOLS.get(canonical_name(tool))
    if spec is None or spec.gate is None:
        return None

    engine = PolicyEngine(session)
    if spec.gate == "return":
        return engine.evaluate_return(args.get("order_id", ""))
    if spec.gate == "exchange":
        return engine.evaluate_exchange(
            args.get("order_id", ""), args.get("to_variant_id", "")
        )
    if spec.gate == "refund":
        order_id = args.get("order_id") or _order_for_payment(session, args.get("payment_id"))
        return engine.evaluate_refund(order_id or "", int(args.get("amount_cents") or 0))
    raise ValueError(f"unknown gate {spec.gate!r} on {tool}")


def execute(
    session: Session,
    tool: str,
    args: dict,
    *,
    conversation_id: str | None = None,
    idempotency_key: str | None = None,
    skip_gate: bool = False,
) -> Outcome:
    """Run one tool call.

    `skip_gate` is only for the approval resume path, where a human has already
    accepted the verdict. That path re-checks DENY separately.
    """
    name = canonical_name(tool)
    spec = TOOLS.get(name)
    if spec is None:
        log.warning("agent asked for unknown tool %r", tool)
        return Outcome(Status.UNKNOWN_TOOL, tool, args,
                       {"ok": False, "error_code": "unknown_tool",
                        "message": f"No tool named {tool}."})

    args = _clean(spec, args)

    verdict = None
    if not skip_gate:
        verdict = evaluate_gate(session, name, args)
        if verdict is not None and verdict.decision is not Decision.ALLOW:
            session.add(
                m.AuditLog(
                    event="tool_blocked_by_policy",
                    actor="system",
                    conversation_id=conversation_id,
                    subject_id=args.get("order_id"),
                    detail={"tool": name, "args": args, "verdict": verdict.to_dict()},
                )
            )
            session.flush()
            return Outcome(Status.BLOCKED, name, args,
                           {"ok": False, "error_code": "policy_blocked",
                            "message": _explain(verdict)},
                           verdict.to_dict())

    call_args = dict(args)
    if idempotency_key and _accepts_idempotency(spec):
        call_args["idempotency_key"] = idempotency_key

    result = spec.handler(session, conversation_id=conversation_id, **call_args)
    status = Status.EXECUTED if result.get("ok") else Status.FAILED
    return Outcome(status, name, args, dict(result),
                   verdict.to_dict() if verdict else None)


def policy_search(session: Session, query: str, *, k: int = 4, category: str | None = None) -> list[dict]:
    """Retrieval, not an API call — so it stays out of the registry and ungated."""
    return [hit.as_dict() for hit in rag_store.search(session, query, k=k, category=category)]


# ----------------------------------------------------------------------


def _explain(verdict: Verdict) -> str:
    reasons = "; ".join(
        f"{r.rule} ({r.policy_id}): {r.detail}" for r in verdict.blocking_reasons
    )
    return f"Blocked by policy — {verdict.decision.value}. {reasons}"


def _clean(spec: ToolSpec, args: dict) -> dict:
    """Drop arguments the tool does not declare and coerce the integers.

    Models improvise argument names; passing them through would TypeError
    inside the simulator.
    """
    allowed = spec.parameters["properties"]
    cleaned = {}
    for key, value in (args or {}).items():
        if key not in allowed or value is None:
            continue
        if allowed[key] is INT or allowed[key].get("type") == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        cleaned[key] = value
    return cleaned


def _accepts_idempotency(spec: ToolSpec) -> bool:
    return spec.name in {
        "Shopify.CreateReturn", "Shopify.CreateExchange", "Stripe.RefundPayment",
    }


def _order_for_payment(session: Session, payment_id: str | None) -> str | None:
    if not payment_id:
        return None
    payment = session.get(m.Payment, payment_id)
    return payment.order_id if payment else None
