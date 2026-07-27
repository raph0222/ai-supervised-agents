"""Shared plumbing for the fake integration layer.

Every simulated call returns the same envelope and lands in `api_logs`. The
envelope is what the agents and the tool executor code against, so swapping a
simulator body for a real SDK changes nothing upstream.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db import models as m

log = logging.getLogger(__name__)


class ToolResult(dict):
    """Envelope returned by every simulated call.

    A dict subclass rather than a model: it is handed straight to the LLM as a
    tool result, so it has to be trivially JSON-serialisable.
    """

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))

    @property
    def error_code(self) -> str | None:
        return self.get("error_code")

    def __bool__(self) -> bool:  # `if result:` means "did it succeed"
        return self.ok


def success(**payload: Any) -> ToolResult:
    return ToolResult(ok=True, **payload)


def failure(error_code: str, message: str, **payload: Any) -> ToolResult:
    return ToolResult(ok=False, error_code=error_code, message=message, **payload)


def simulated_call(system: str, operation: str) -> Callable:
    """Wrap a simulator so its call is timed, logged, and never raises upward.

    The decorated function takes (session, **kwargs) and returns a ToolResult.
    An unexpected exception becomes an `internal_error` envelope: a tool that
    raises would crash the agent loop, and the loop's job is to handle failure.
    """

    def decorator(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        @functools.wraps(fn)
        def wrapper(session: Session, *args: Any, **kwargs: Any) -> ToolResult:
            started = time.perf_counter()
            conversation_id = kwargs.pop("conversation_id", None)
            try:
                result = fn(session, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - deliberate boundary
                log.exception("%s.%s raised", system, operation)
                result = failure("internal_error", str(exc))
            latency_ms = int((time.perf_counter() - started) * 1000)

            session.add(
                m.ApiLog(
                    system=system,
                    operation=operation,
                    request=_scrub(kwargs),
                    response=dict(result),
                    ok=result.ok,
                    error_code=result.error_code,
                    latency_ms=latency_ms,
                    conversation_id=conversation_id,
                    simulated=True,
                )
            )
            session.flush()
            return result

        return wrapper

    return decorator


_SENSITIVE = {"card_number", "cvv", "token", "secret"}


def _scrub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ("***" if k in _SENSITIVE else v)
        for k, v in payload.items()
        if _jsonable(v)
    }


def _jsonable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, list, dict, type(None)))
