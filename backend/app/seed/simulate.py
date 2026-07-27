"""Registry of the deterministic rigged failures.

Seed rows carry a `_simulate` key — metadata for the fake integration layer,
never a column. The seeder strips it and registers it here, and the simulators
consult this registry before doing real work.

No randomness: the same entity always fails the same way.
"""

from __future__ import annotations

# Recognised behaviours. Anything else in a _simulate string is a seed-data typo
# and the seeder raises rather than silently ignoring it.
REFUND_ALWAYS_DECLINES = "REFUND_ALWAYS_DECLINES"
PERMANENT_OUT_OF_STOCK = "PERMANENT_OUT_OF_STOCK"
MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"

KNOWN_BEHAVIOURS = frozenset(
    {REFUND_ALWAYS_DECLINES, PERMANENT_OUT_OF_STOCK, MALFORMED_PAYLOAD}
)

# entity id -> behaviour
_registry: dict[str, str] = {}


def parse(raw: str) -> str:
    """Extract the behaviour from a `_simulate` value.

    Seed values read like "REFUND_ALWAYS_DECLINES — Stripe.RefundPayment ...";
    the leading token is the behaviour and the rest is documentation.
    """
    behaviour = raw.split("—")[0].split("--")[0].strip()
    if behaviour not in KNOWN_BEHAVIOURS:
        raise ValueError(
            f"unknown _simulate behaviour {behaviour!r}; "
            f"expected one of {sorted(KNOWN_BEHAVIOURS)}"
        )
    return behaviour


def register(entity_id: str, raw: str) -> str:
    behaviour = parse(raw)
    _registry[entity_id] = behaviour
    return behaviour


def behaviour_for(entity_id: str) -> str | None:
    return _registry.get(entity_id)


def has(entity_id: str, behaviour: str) -> bool:
    return _registry.get(entity_id) == behaviour


def all_registered() -> dict[str, str]:
    return dict(_registry)


def clear() -> None:
    _registry.clear()
