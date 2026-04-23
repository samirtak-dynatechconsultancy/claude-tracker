"""Tests for tracker.claude_code_parser.

Run with:
    python -m pytest tests/ -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracker import claude_code_parser
from tracker.claude_code_parser import scan_once
from tracker.events import EventStore


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _assistant(msg_id: str, output_tokens: int, *, input_tokens: int = 100,
               cache_read: int = 0, cache_create: int = 0,
               session: str = "sess-1", ts: str = "2026-04-22T09:34:06.672Z",
               entrypoint: str = "cli") -> dict:
    return {
        "type": "assistant",
        "uuid": f"uuid-{msg_id}-{output_tokens}",
        "sessionId": session,
        "timestamp": ts,
        "entrypoint": entrypoint,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


@pytest.fixture
def fake_projects(tmp_path, monkeypatch):
    """Redirect PROJECTS_DIR and STATE_PATH to tmp_path for isolation."""
    projects = tmp_path / "projects"
    projects.mkdir()
    state = tmp_path / "parser_state.json"
    monkeypatch.setattr(claude_code_parser, "PROJECTS_DIR", projects)
    monkeypatch.setattr(claude_code_parser, "STATE_PATH", state)
    return projects


def test_ignores_non_assistant_lines(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "summary", "summary": "x"},
    ])
    store = EventStore()
    assert scan_once(store) == 0
    assert store.all() == []


def test_emits_one_event_per_message_id(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [
        _assistant("msg-1", output_tokens=50, input_tokens=100),
    ])
    store = EventStore()
    assert scan_once(store) == 1
    events = store.all()
    assert len(events) == 1
    assert events[0].message_id == "msg-1"
    assert events[0].input_tokens == 100
    assert events[0].output_tokens == 50
    assert events[0].source == "code"
    assert events[0].event_type == "message"


def test_dedupes_streaming_chunks_within_one_pass(fake_projects):
    """Multiple entries with the same message.id (streaming chunks) should
    produce exactly one event with the max output_tokens."""
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [
        _assistant("msg-1", output_tokens=10, input_tokens=100),
        _assistant("msg-1", output_tokens=50, input_tokens=100),
        _assistant("msg-1", output_tokens=252, input_tokens=100),
    ])
    store = EventStore()
    assert scan_once(store) == 1
    events = store.all()
    assert len(events) == 1
    assert events[0].output_tokens == 252
    assert events[0].input_tokens == 100


def test_does_not_reemit_on_second_pass(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [_assistant("msg-1", output_tokens=252)])
    store = EventStore()
    assert scan_once(store) == 1
    assert scan_once(store) == 0
    assert len(store.all()) == 1


def test_emits_delta_when_streaming_spans_passes(fake_projects):
    """Streaming chunk appears in pass 1 with 50 tokens; continues in pass 2
    to 252 tokens. We should emit two events: first with 50 output_tokens
    (and full input), second with 202 output_tokens (delta, zero input)."""
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [_assistant("msg-1", output_tokens=50, input_tokens=100)])
    store = EventStore()
    assert scan_once(store) == 1

    _write_jsonl(f, [_assistant("msg-1", output_tokens=252, input_tokens=100)])
    assert scan_once(store) == 1

    events = store.all()
    assert len(events) == 2
    assert events[0].input_tokens == 100
    assert events[0].output_tokens == 50
    assert events[0].extras.get("delta") is False
    assert events[1].input_tokens == 0  # already counted
    assert events[1].output_tokens == 202  # delta only
    assert events[1].extras.get("delta") is True


def test_processes_new_lines_only(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [_assistant("msg-1", output_tokens=100)])
    store = EventStore()
    scan_once(store)

    _write_jsonl(f, [_assistant("msg-2", output_tokens=200)])
    assert scan_once(store) == 1
    events = store.all()
    assert [e.message_id for e in events] == ["msg-1", "msg-2"]


def test_handles_multiple_projects_and_sessions(fake_projects):
    _write_jsonl(fake_projects / "proj-a" / "s1.jsonl", [
        _assistant("msg-a", output_tokens=10, session="sess-a"),
    ])
    _write_jsonl(fake_projects / "proj-b" / "s2.jsonl", [
        _assistant("msg-b", output_tokens=20, session="sess-b"),
    ])
    store = EventStore()
    assert scan_once(store) == 2
    by_msg = {e.message_id: e for e in store.all()}
    assert by_msg["msg-a"].conversation_id == "sess-a"
    assert by_msg["msg-b"].conversation_id == "sess-b"


def test_skips_malformed_lines(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("w", encoding="utf-8") as fp:
        fp.write("{not valid json\n")
        fp.write(json.dumps(_assistant("msg-1", output_tokens=50)) + "\n")
        fp.write("\n")  # blank
    store = EventStore()
    assert scan_once(store) == 1


def test_assistant_without_message_id_is_ignored(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [{
        "type": "assistant",
        "uuid": "x",
        "sessionId": "s",
        "timestamp": "2026-04-22T09:34:06.672Z",
        "message": {"usage": {"output_tokens": 50}},  # no id
    }])
    store = EventStore()
    assert scan_once(store) == 0


def test_entrypoint_is_propagated_to_event_extras(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [
        _assistant("msg-1", output_tokens=50, entrypoint="claude-desktop"),
        _assistant("msg-2", output_tokens=60, entrypoint="cli"),
        _assistant("msg-3", output_tokens=70, entrypoint="claude-vscode"),
    ])
    store = EventStore()
    scan_once(store)
    eps = {e.message_id: e.extras["entrypoint"] for e in store.all()}
    assert eps == {
        "msg-1": "claude-desktop",
        "msg-2": "cli",
        "msg-3": "claude-vscode",
    }


def test_summary_breaks_out_by_entrypoint(fake_projects):
    f = fake_projects / "proj-a" / "s1.jsonl"
    _write_jsonl(f, [
        _assistant("msg-1", output_tokens=50, entrypoint="claude-desktop"),
        _assistant("msg-2", output_tokens=60, entrypoint="cli"),
        _assistant("msg-3", output_tokens=70, entrypoint="cli"),
    ])
    store = EventStore()
    scan_once(store)
    summary = store.summary()
    by_ep = summary["by_entrypoint"]
    assert by_ep["claude-desktop"]["messages"] == 1
    assert by_ep["claude-desktop"]["output_tokens"] == 50
    assert by_ep["cli"]["messages"] == 2
    assert by_ep["cli"]["output_tokens"] == 130


def test_partial_trailing_line_is_not_consumed(fake_projects):
    """If a line is appended without a trailing newline (mid-write by Claude
    Code), we must not consume it — otherwise the completed line in the next
    scan would be missed."""
    f = fake_projects / "proj-a" / "s1.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    complete = json.dumps(_assistant("msg-1", output_tokens=10)) + "\n"
    partial = json.dumps(_assistant("msg-2", output_tokens=20))  # no \n
    f.write_text(complete + partial, encoding="utf-8")

    store = EventStore()
    assert scan_once(store) == 1
    assert store.all()[0].message_id == "msg-1"

    # Complete the second line.
    with f.open("a", encoding="utf-8") as fp:
        fp.write("\n")
    assert scan_once(store) == 1
    assert store.all()[1].message_id == "msg-2"
