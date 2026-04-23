"""Durable SQLite-backed queue for events pending upload to the backend.

All events hit this queue. The uploader thread pulls batches, POSTs them,
and acks on 2xx. Rows survive tracker restarts so offline periods don't
lose data.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .events import Event


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_events_received_idx
    ON pending_events (received_at);
"""


class UploadQueue:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        # check_same_thread=False because background threads call it;
        # we serialize with _lock so SQLite sees one caller at a time.
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.executescript(_SCHEMA)

    def append(self, event: Event) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO pending_events (payload, received_at) VALUES (?, ?)",
                (json.dumps(asdict(event)), event.timestamp),
            )

    def next_batch(self, limit: int = 100) -> list[tuple[int, dict]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, payload FROM pending_events ORDER BY id LIMIT ?",
                (limit,),
            )
            return [(row[0], json.loads(row[1])) for row in cur.fetchall()]

    def ack(self, ids: list[int]) -> int:
        if not ids:
            return 0
        with self._lock:
            placeholders = ",".join("?" * len(ids))
            cur = self._conn.execute(
                f"DELETE FROM pending_events WHERE id IN ({placeholders})",
                ids,
            )
            return cur.rowcount

    def depth(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM pending_events")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
