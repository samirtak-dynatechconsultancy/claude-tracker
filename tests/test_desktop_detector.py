"""Tests for tracker.desktop_detector — focus on session classification.

The process-polling side (is_claude_running) is platform-dependent and
covered by integration runs; here we unit-test the state machine by
driving run_poller with a scripted sequence of is_claude_running
results.
"""

from __future__ import annotations

import threading
import time

import pytest

from tracker import desktop_detector
from tracker.events import Event, EventStore


@pytest.fixture
def patched_detector(monkeypatch):
    """Patch is_claude_running to return scripted values from a list.

    The list is mutated by the test; each poll pops the next value.
    """
    script: list[bool] = []

    def fake_running():
        if not script:
            return False
        return script.pop(0)

    monkeypatch.setattr(desktop_detector, "is_claude_running", fake_running)
    return script


def _drive(store, script, patched, *, iterations=3):
    """Run the poller for N iterations with a 0-second interval."""
    stop = threading.Event()
    patched.extend(script)

    def runner():
        nonlocal_counter = {"n": 0}

        original_wait = stop.wait

        def fake_wait(_timeout):
            nonlocal_counter["n"] += 1
            if nonlocal_counter["n"] >= iterations:
                stop.set()
            return original_wait(0)

        stop.wait = fake_wait  # type: ignore[assignment]
        desktop_detector.run_poller(store, stop, interval_seconds=0.0)

    t = threading.Thread(target=runner)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "poller thread did not terminate"


def test_emits_session_start_on_first_observation_if_running(patched_detector):
    store = EventStore()
    _drive(store, [True, False], patched_detector, iterations=2)
    events = store.all()
    types = [e.event_type for e in events]
    assert types == ["session_start", "session_end"]
    assert all(e.source == "desktop" for e in events)


def test_no_events_when_claude_never_running(patched_detector):
    store = EventStore()
    _drive(store, [False, False, False], patched_detector, iterations=3)
    assert store.all() == []


def test_session_end_flags_chat_only_true_when_no_code_activity(patched_detector):
    store = EventStore()
    _drive(store, [True, False], patched_detector, iterations=2)
    session_end = [e for e in store.all() if e.event_type == "session_end"][0]
    assert session_end.extras["chat_only"] is True
    assert session_end.extras["duration_seconds"] >= 0


def test_session_end_flags_chat_only_false_when_code_activity_overlaps(patched_detector):
    store = EventStore()
    stop = threading.Event()
    # Two polls: first sees running=True (session starts), second sees
    # running=False (session ends). Between them the parser emits a code
    # message, so the session should be classified chat_only=False.
    patched_detector.extend([True, False])
    call_count = {"n": 0}

    def fake_wait(_t):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Between poll 1 (start) and poll 2 (end): parser activity.
            store.add(
                Event(
                    user="u",
                    hostname="h",
                    source="code",
                    event_type="message",
                    timestamp=time.time(),
                    message_id="msg-x",
                    output_tokens=100,
                )
            )
            return False  # wake poll 2 immediately
        stop.set()
        return True

    stop.wait = fake_wait  # type: ignore[assignment]

    t = threading.Thread(
        target=desktop_detector.run_poller,
        args=(store, stop),
        kwargs={"interval_seconds": 0.0},
    )
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()

    session_ends = [e for e in store.all() if e.event_type == "session_end"]
    assert len(session_ends) == 1
    assert session_ends[0].extras["chat_only"] is False
