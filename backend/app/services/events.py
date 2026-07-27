"""In-process event bus for the chat stream.

An approval made on /admin has to appear in the chat without a reload, and there
is no Redis. One process, one user: a dict of thread-safe queues is the whole
mechanism.

Subscribers poll their queue rather than being pushed to, which costs a fixed
250ms but avoids marshalling callbacks out of the graph's worker thread.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.25
# A slow or abandoned browser tab must not grow without bound.
MAX_QUEUED_EVENTS = 200


@dataclass
class Event:
    type: str
    conversation_id: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def sse(self) -> str:
        payload = json.dumps(
            {"type": self.type, "conversation_id": self.conversation_id,
             "created_at": self.created_at, **self.data},
            default=str,
        )
        return f"event: {self.type}\ndata: {payload}\n\n"


class Broker:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, conversation_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=MAX_QUEUED_EVENTS)
        with self._lock:
            self._subscribers.setdefault(conversation_id, []).append(q)
        return q

    def unsubscribe(self, conversation_id: str, q: queue.Queue) -> None:
        with self._lock:
            subscribers = self._subscribers.get(conversation_id, [])
            if q in subscribers:
                subscribers.remove(q)
            if not subscribers:
                self._subscribers.pop(conversation_id, None)

    def publish(self, event: Event) -> None:
        with self._lock:
            # A conversation-scoped subscriber, plus anyone watching everything
            # (the admin activity feed).
            targets = list(self._subscribers.get(event.conversation_id, []))
            targets += list(self._subscribers.get("*", []))
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                log.warning("dropping event for a subscriber that stopped reading")

    def subscriber_count(self, conversation_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(conversation_id, []))


broker = Broker()


def publish(event_type: str, conversation_id: str, **data: Any) -> None:
    broker.publish(Event(event_type, conversation_id, data))
