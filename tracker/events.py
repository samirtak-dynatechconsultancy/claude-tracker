"""Thread-safe in-memory event store shared by parser, detector, API.

Step 2 keeps everything in RAM. Step 3 will add a SQLite-backed queue
for offline reliability + backend upload, but the read API here
(add/all/summary) should remain stable across that swap.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Callable, Literal, Optional

EventSource = Literal["ai_web", "code", "desktop"]
EventType = Literal["message", "session_start", "session_end"]


@dataclass
class Event:
    user: str
    hostname: str
    source: EventSource
    event_type: EventType
    timestamp: float  # unix seconds
    # message events:
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # session events:
    session_id: Optional[str] = None  # for desktop sessions
    # arbitrary extras:
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EventStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._last_code_activity_wall: float = 0.0
        self._listeners: list[Callable[[Event], None]] = []

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        """Register a side-effect for every added event (e.g. upload queue)."""
        with self._lock:
            self._listeners.append(listener)

    def add(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)
            if event.source == "code" and event.event_type == "message":
                self._last_code_activity_wall = time.time()
            listeners = list(self._listeners)
        # Fire listeners outside the lock so they can't deadlock us.
        for fn in listeners:
            try:
                fn(event)
            except Exception:  # pragma: no cover - best-effort
                pass

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def summary(self, now: Optional[float] = None) -> dict:
        """Aggregate totals: today, this week, all-time, per-source, per-entrypoint."""
        now = now if now is not None else time.time()
        day_ago = now - 86400
        week_ago = now - 7 * 86400

        totals = {
            "today": _zero_bucket(),
            "week": _zero_bucket(),
            "all": _zero_bucket(),
            "by_source": defaultdict(_zero_bucket),
            "by_entrypoint": defaultdict(_zero_bucket),
        }

        with self._lock:
            events = list(self._events)

        for e in events:
            _accumulate(totals["all"], e)
            _accumulate(totals["by_source"][e.source], e)
            ep = (e.extras or {}).get("entrypoint") or "unknown"
            if e.source == "code":
                _accumulate(totals["by_entrypoint"][ep], e)
            if e.timestamp >= week_ago:
                _accumulate(totals["week"], e)
            if e.timestamp >= day_ago:
                _accumulate(totals["today"], e)

        totals["by_source"] = dict(totals["by_source"])
        totals["by_entrypoint"] = dict(totals["by_entrypoint"])
        return totals

    def has_code_activity_since(self, wall_since: float) -> bool:
        """True if a code-source message was added to the store at/after `wall_since`
        (wall-clock time, not event timestamp — so historical re-ingestion doesn't
        mislead the desktop detector)."""
        with self._lock:
            return self._last_code_activity_wall >= wall_since


def _zero_bucket() -> dict:
    return {
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "session_starts": 0,
        "session_ends": 0,
    }


def _accumulate(bucket: dict, e: Event) -> None:
    if e.event_type == "message":
        bucket["messages"] += 1
        bucket["input_tokens"] += e.input_tokens
        bucket["output_tokens"] += e.output_tokens
        bucket["cache_creation_tokens"] += e.cache_creation_tokens
        bucket["cache_read_tokens"] += e.cache_read_tokens
    elif e.event_type == "session_start":
        bucket["session_starts"] += 1
    elif e.event_type == "session_end":
        bucket["session_ends"] += 1
