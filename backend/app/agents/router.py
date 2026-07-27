"""Router agent.

Says what the customer wants. It never answers and never calls a tool. A regex
pre-pass extracts order ids, leaving the model only intent and emotion.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.llm import client as llm
from app.security import injection

log = logging.getLogger(__name__)

INTENTS = [
    "RETURN", "REFUND", "EXCHANGE", "TRACK_ORDER",
    "PRODUCT_QUESTION", "WARRANTY", "DAMAGE", "GENERAL_QA", "HUMAN",
]

# Below this the agent asks a clarifying question instead of acting.
LOW_CONFIDENCE = 0.6

ORDER_ID = re.compile(r"\b(ORD[-_ ]?\d{3,6})\b", re.I)
VARIANT_ID = re.compile(r"\b(NB-[A-Z]+-\d+(?:-[A-Z0-9]+)*)\b", re.I)

SYSTEM = """You are the routing agent for Northbridge Components customer support.

Your job is ONLY to determine user intent. Never answer the customer. Never
offer help. Never mention policy. Output JSON and nothing else.

Possible intents:
  RETURN            wants to send an item back
  REFUND            wants money back
  EXCHANGE          wants a different variant — form factor, capacity or colour
  TRACK_ORDER       where is my order (WISMO), delivery timing
  PRODUCT_QUESTION  compatibility, specifications, clearance, availability
  WARRANTY          a defect on an item they have owned for a while
  DAMAGE            arrived damaged, wrong item, or never arrived
  GENERAL_QA        anything else answerable from policy or FAQ
  HUMAN             explicitly asks for a person, or is angry enough to need one

Rules:
- REFUND when they want money back with no mention of shipping the item back.
  RETURN when the item is going back. If both, choose RETURN.
- DAMAGE covers missing packages and wrong items, not late ones. A late but
  moving parcel is TRACK_ORDER.
- WARRANTY when the item has been used for a while and something failed.
- confidence is your genuine certainty from 0 to 1. Do not inflate it.
- sentiment is the customer's emotional state, one of: NEUTRAL, FRUSTRATED, ANGRY.

Output exactly:
{"intent": "...", "confidence": 0.0, "entities": {"order_id": "...", "variant_id": "...", "size": "..."},
 "sentiment": "NEUTRAL", "reasoning": "one short sentence"}"""


@dataclass
class Route:
    intent: str
    confidence: float
    entities: dict = field(default_factory=dict)
    sentiment: str = "NEUTRAL"
    reasoning: str = ""
    injection: injection.Assessment | None = None

    @property
    def low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE

    @property
    def needs_human(self) -> bool:
        """Angry escalates; merely frustrated does not."""
        return self.intent == "HUMAN" or self.sentiment == "ANGRY"

    def as_dict(self) -> dict:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "entities": self.entities,
            "sentiment": self.sentiment,
            "reasoning": self.reasoning,
            "injection": self.injection.as_dict() if self.injection else None,
        }


def extract_entities(text: str) -> dict:
    """Deterministic entity extraction. Runs whether or not Vertex is up."""
    entities: dict = {}
    if (order := ORDER_ID.search(text)) is not None:
        entities["order_id"] = order.group(1).upper().replace("_", "-").replace(" ", "-")
    if (variant := VARIANT_ID.search(text)) is not None:
        entities["variant_id"] = variant.group(1).upper()
    if (size := re.search(r"\bsize\s+([A-Za-z0-9.]{1,4})\b", text, re.I)) is not None:
        entities["size"] = size.group(1).upper()
    return entities


def route(
    session: Session,
    message: str,
    *,
    history: str = "",
    conversation_id: str | None = None,
) -> Route:
    assessment = injection.assess(message)
    if conversation_id:
        injection.record(session, conversation_id, message, assessment)

    deterministic = extract_entities(message)

    prompt = (
        f"Conversation so far:\n{history or '(none)'}\n\n"
        f"Latest customer message:\n{injection.wrap_untrusted(message)}\n\n"
        "Classify the intent of the latest message."
    )
    response = llm.generate_json(
        prompt, system=SYSTEM, agent="router",
        session=session, conversation_id=conversation_id,
    )
    payload = response.raw_json or {}

    intent = str(payload.get("intent", "")).upper()
    if intent not in INTENTS:
        log.warning("router returned unusable intent %r; falling back", intent)
        intent, confidence = "GENERAL_QA", 0.0
    else:
        confidence = _confidence(payload.get("confidence"))

    entities = {**(payload.get("entities") or {}), **deterministic}
    entities = {k: v for k, v in entities.items() if v}

    return Route(
        intent=intent,
        confidence=confidence,
        entities=entities,
        sentiment=str(payload.get("sentiment", "NEUTRAL")).upper(),
        reasoning=str(payload.get("reasoning", "")),
        injection=assessment,
    )


def _confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
