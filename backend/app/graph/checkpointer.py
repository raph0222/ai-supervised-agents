"""LangGraph checkpointing on the same Postgres.

State is written down when the workflow parks at AWAIT_APPROVAL, so an approval
hours later resumes exactly there.

Uses a dedicated psycopg connection rather than the SQLAlchemy engine: the saver
wants autocommit, and sharing the ORM's connection would tie checkpoint writes to
application transactions that may still roll back.
"""

from __future__ import annotations

import logging
import threading

from app.config import get_settings

log = logging.getLogger(__name__)

_saver = None
_lock = threading.Lock()


def psycopg_dsn() -> str:
    """SQLAlchemy URL -> libpq DSN."""
    url = get_settings().database_url
    return url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def get_checkpointer():
    """The process-wide saver. Falls back to memory if Postgres is unreachable.

    The fallback is deliberate — answering without resumable approvals beats not
    answering — and logs exactly what was lost.
    """
    global _saver
    if _saver is not None:
        return _saver

    with _lock:
        if _saver is not None:
            return _saver
        try:
            import psycopg  # noqa: PLC0415
            from langgraph.checkpoint.postgres import PostgresSaver  # noqa: PLC0415

            conn = psycopg.connect(psycopg_dsn(), autocommit=True)
            saver = PostgresSaver(conn)
            saver.setup()
            _saver = saver
            log.info("langgraph checkpointer: postgres")
        except Exception:  # noqa: BLE001
            from langgraph.checkpoint.memory import MemorySaver  # noqa: PLC0415

            log.exception(
                "postgres checkpointer unavailable; falling back to in-memory. "
                "Approvals will not survive a restart."
            )
            _saver = MemorySaver()
        return _saver


def reset() -> None:
    global _saver
    _saver = None
