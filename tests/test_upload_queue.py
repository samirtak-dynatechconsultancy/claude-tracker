"""Tests for the SQLite upload queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracker.events import Event
from tracker.upload_queue import UploadQueue


def _mk(n: int) -> Event:
    return Event(
        user="u", hostname="h", source="code", event_type="message",
        timestamp=float(n), message_id=f"msg-{n}", output_tokens=n,
    )


def test_append_and_drain(tmp_path: Path):
    q = UploadQueue(tmp_path / "q.db")
    for i in range(5):
        q.append(_mk(i))
    assert q.depth() == 5

    batch = q.next_batch(limit=3)
    assert len(batch) == 3
    ids = [bid for bid, _ in batch]
    payloads = [p for _, p in batch]
    assert [p["message_id"] for p in payloads] == ["msg-0", "msg-1", "msg-2"]
    assert q.ack(ids) == 3
    assert q.depth() == 2

    rest = q.next_batch(limit=100)
    assert [p["message_id"] for _, p in rest] == ["msg-3", "msg-4"]


def test_survives_close_and_reopen(tmp_path: Path):
    path = tmp_path / "q.db"
    q = UploadQueue(path)
    q.append(_mk(1))
    q.append(_mk(2))
    q.close()

    q2 = UploadQueue(path)
    assert q2.depth() == 2
    batch = q2.next_batch()
    assert [p["message_id"] for _, p in batch] == ["msg-1", "msg-2"]


def test_ack_empty_is_noop(tmp_path: Path):
    q = UploadQueue(tmp_path / "q.db")
    assert q.ack([]) == 0
