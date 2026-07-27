"""Workflow state and the session it runs against.

Everything in `WorkflowState` must survive a round trip through the Postgres
checkpointer, so it is primitives, dicts and lists only. The database session is
not serialisable and so travels in a context variable instead, set by whoever
drives the graph.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any, TypedDict

from sqlalchemy.orm import Session


def _replace(_old, new):
    """Last write wins. Nodes own their keys; none of them accumulate."""
    return new


def _extend(old: list, new: list) -> list:
    return (old or []) + (new or [])


class WorkflowState(TypedDict, total=False):
    # identity
    conversation_id: str
    customer_id: str
    message: str
    turn_id: str

    # UNDERSTAND
    memory_block: str
    history_block: str
    injection: dict

    # ROUTE
    intent: str
    confidence: float
    entities: dict
    sentiment: str
    agent: str

    # PLAN / RETRIEVE
    plan: dict
    retrieved: list
    policy_block: str

    # DECIDE / VERIFY POLICY / CALL TOOLS
    proposed_calls: list
    tool_results: Annotated[list, _extend]
    verdicts: Annotated[list, _extend]
    attempts: int
    # Rounds of tool calls in this turn. Separate from `attempts`, which counts
    # retries after a failure: a second round is normally progress, not a retry.
    tool_rounds: int

    # escalation and resume
    status: str                # RUNNING | AWAITING_APPROVAL | DONE
    escalation_reason: str
    pending_action_id: str
    approval_granted: bool
    approval_seen: bool

    # RESPOND
    reply: str
    cited_policies: list
    resolved: bool
    unbacked_claims: list


# Terminal-ish statuses the API cares about.
RUNNING = "RUNNING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
DONE = "DONE"

_session: ContextVar[Session | None] = ContextVar("graph_session", default=None)


@contextmanager
def use_session(session: Session):
    token = _session.set(session)
    try:
        yield session
    finally:
        _session.reset(token)


def current_session() -> Session:
    session = _session.get()
    if session is None:
        raise RuntimeError(
            "no database session bound to the workflow; wrap the run in "
            "graph.state.use_session(session)"
        )
    return session


def new_state(conversation_id: str, customer_id: str, message: str, turn_id: str) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "customer_id": customer_id,
        "message": message,
        "turn_id": turn_id,
        "status": RUNNING,
        "attempts": 0,
        "tool_rounds": 0,
        "tool_results": [],
        "verdicts": [],
    }
