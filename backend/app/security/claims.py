"""Checks a reply against the tools that actually ran.

Catches "I've processed a full refund of $73.53" on a turn where no refund tool
was called. Only completed-action claims count — "I'll issue a refund" and "a
refund will be issued once we receive it" promise nothing that must have
happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Claim:
    category: str
    evidence: str

    def as_dict(self) -> dict:
        return {"category": self.category, "evidence": self.evidence}


# Which executed tools make a claim of each category true.
CLAIM_TOOLS: dict[str, set[str]] = {
    "REFUND": {"Stripe.RefundPayment", "Stripe.CreateAdjustment"},
    "RETURN": {"Shopify.CreateReturn"},
    "EXCHANGE": {"Shopify.CreateExchange"},
    "CANCELLATION": {"Shopify.CancelOrder"},
    "EMAIL": {"Email.SendConfirmation", "Email.SendReturnLabel", "Email.NotifySupport"},
    "TICKET": {"CRM.CreateTicket", "CRM.EscalateTicket"},
}

# No `will`, `can`, `could` or `would` — that omission is the whole filter.
_AUX = r"(?:'ve|'s|have|has|had|was|were|is|are|been)"
_ADVERBS = r"(?:also |just |already |now |since |then |in fact |\w+ly )*"

# category -> (subject, verbs that complete it). Verbs are per category so that
# "a refund … I have sent it over" reads as a handover, not a payment.
_PATTERNS: dict[str, tuple[str, str]] = {
    "REFUND": (
        r"\brefunds?\b|\brefunded\b|money back|reimburse(?:d|ment)?",
        r"processed|issued|refunded|credited|applied|completed|put through|actioned",
    ),
    "RETURN": (
        r"\breturns?\b|\brma\b|return label|prepaid label",
        r"created|opened|arranged|set up|issued|generated|logged|started|"
        r"submitted|initiated|registered",
    ),
    "EXCHANGE": (
        r"\bexchanges?\b|\bexchanged\b|\bswapped\b",
        r"created|arranged|set up|processed|initiated|submitted|booked",
    ),
    "CANCELLATION": (
        r"\bcancell?ations?\b|\bcancell?ed\b|\border\b",
        r"cancell?ed",
    ),
    "EMAIL": (
        r"\be-?mails?\b|\be-?mailed\b|\bconfirmation\b",
        r"sent|e-?mailed|delivered|dispatched",
    ),
    "TICKET": (
        r"\btickets?\b|support case|case number",
        r"opened|created|logged|raised|submitted|escalated",
    ),
}

_COMPILED = {
    category: (
        re.compile(noun, re.I),
        re.compile(rf"\b{_AUX}(?:\s+(?:been|being))?\s+{_ADVERBS}(?:{verbs})\b", re.I),
    )
    for category, (noun, verbs) in _PATTERNS.items()
}

# How far from the verb the subject may sit: a clause, not a sentence.
_WINDOW = 60


def find(text: str) -> list[Claim]:
    """Every completed-action assertion in `text`, one per category."""
    text = text or ""
    found: list[Claim] = []

    for category, (noun_re, done_re) in _COMPILED.items():
        for match in done_re.finditer(text):
            window = text[max(0, match.start() - _WINDOW):match.end() + _WINDOW]
            if noun_re.search(window):
                found.append(Claim(category, " ".join(window.split())))
                break

    return found


def executed_categories(tool_results: list[dict]) -> set[str]:
    """The categories this turn is entitled to talk about in the past tense."""
    ran = {
        item.get("tool")
        for item in tool_results or []
        if item.get("status") == "EXECUTED" and (item.get("result") or {}).get("ok", True)
    }
    return {c for c, tools in CLAIM_TOOLS.items() if tools & ran}


def unbacked(text: str, tool_results: list[dict]) -> list[Claim]:
    """Claims in `text` that no executed tool supports."""
    backed = executed_categories(tool_results)
    return [claim for claim in find(text) if claim.category not in backed]


def correction_instruction(claims: list[Claim]) -> str:
    """The retry prompt: name the lie, then restate the rule."""
    said = ", ".join(sorted({c.category.lower() for c in claims}))
    return (
        "STOP — your previous draft was rejected. It told the customer that a "
        f"{said} had already been carried out. No such action ran this turn, so "
        "that statement is false and would have reached a real person.\n"
        "Rewrite the reply. State only what the completed-actions list above "
        "shows. If the customer asked for something that did not happen, say "
        "plainly that it has not been done yet and what happens next. Do not use "
        "the past tense for anything outside that list."
    )
