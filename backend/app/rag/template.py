"""Resolve `{{policy_rule}}` placeholders in the knowledge corpus.

The documents in `seed/knowledge/` own the *sentences* — the conditions, the
exceptions, the tone the agent borrows when it explains itself. They do not own
the numbers. Every threshold in the prose is written as a `{{key}}` placeholder
naming a `policy_rules` row and resolved on the way out of retrieval, so editing
the $50 gate in /admin moves the enforced threshold and the sentence the agent
reads in a single write. Before this, the engine read the row and the prose said
"$50" forever: change the rule to $100 and the agent would auto-approve $80
while telling the customer it needed approval.

Substituting at read time rather than at load time is deliberate — the stored
chunk keeps the template, so a rule change needs no re-chunk and no reseed. Only
the embedding has to be refreshed, because vectors are computed from rendered
text (a vector of `{{refund_auto_approve_under_cents|money}}` retrieves
nothing); `store.invalidate_for_rule` handles that.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m

log = logging.getLogger(__name__)

# `{{key}}` or `{{key|format}}`. Keys are policy_rules primary keys, so the
# character class is exactly what those keys are allowed to contain.
PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*(?:\|\s*([a-z0-9_]+)\s*)?\}\}")

DEFAULT_FORMAT = "int"


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _days(value: int) -> str:
    return f"{value} day" if value == 1 else f"{value} days"


def _times(value: int) -> str:
    """Retry counts read as prose in the policy documents."""
    return {1: "once", 2: "twice"}.get(value, f"{value} times")


FORMATTERS: dict[str, Callable[[int], str]] = {
    "int": str,
    "money": _money,
    "days": _days,
    "times": _times,
}


def values(session: Session) -> dict[str, int]:
    """Every numeric policy rule, keyed the way the placeholders name it."""
    return {
        row.key: row.value_int
        for row in session.scalars(select(m.PolicyRule)).all()
        if row.value_int is not None
    }


def render(text: str, rules: dict[str, int]) -> str:
    """Substitute every resolvable placeholder; leave the rest visible.

    An unresolvable placeholder stays as written rather than collapsing to a
    plausible-looking default. A prompt containing `{{typo_key}}` is a bug you
    can see; a prompt containing a silently wrong threshold is not.
    """

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        fmt = match.group(2) or DEFAULT_FORMAT
        value = rules.get(key)
        formatter = FORMATTERS.get(fmt)
        if value is None or formatter is None:
            log.warning("unresolved knowledge placeholder %s", match.group(0))
            return match.group(0)
        return formatter(value)

    return PLACEHOLDER.sub(_sub, text)


def keys_in(text: str) -> set[str]:
    """Policy rule keys this text depends on."""
    return {match.group(1) for match in PLACEHOLDER.finditer(text)}


def unresolved(text: str, rules: dict[str, int]) -> list[str]:
    """Placeholders `render` would leave untouched — a corpus/DB mismatch.

    Surfaced by `store.stats` so a typo in a policy document shows up on /admin
    instead of inside a customer-facing answer.
    """
    return [
        match.group(0)
        for match in PLACEHOLDER.finditer(text)
        if rules.get(match.group(1)) is None
        or FORMATTERS.get(match.group(2) or DEFAULT_FORMAT) is None
    ]
