"""Claude Code .jsonl log parser.

Scans ~/.claude/projects/*/*.jsonl, reads only new bytes since the
previous scan, and emits one Event per Anthropic API message.id.

Dedup semantics
---------------
Claude Code's streaming protocol writes multiple assistant entries
with the SAME message.id as a response is produced (partial chunks).
Each entry has a unique uuid but the `message.id` field is shared.

Rules (the simplest thing that works):
  - Dedup key = message.id.
  - For each message.id we keep the MAX output_tokens observed
    (input/cache tokens are stable across chunks; the last one wins if
    they ever differ).
  - We persist a `reported_output_tokens` per message.id. On each pass:
      * First time we see a message.id -> emit an event with the current
        totals.
      * Subsequent passes, if output_tokens grew (streaming continued
        across poll intervals) -> emit a delta event with the new output
        tokens only. Input/cache tokens are emitted with the first event.
  - We persist a per-file byte offset so the next scan only reads the
    tail. If a file shrinks or is rotated, we restart from 0.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .config import app_data_dir, hostname, os_username
from .events import Event, EventStore

log = logging.getLogger(__name__)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
STATE_PATH = app_data_dir() / "parser_state.json"
SOURCE = "code"


@dataclass
class ParsedMessage:
    message_id: str
    session_id: str
    model: str
    timestamp: float  # unix seconds
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    entrypoint: str  # "claude-desktop" | "cli" | "claude-vscode" | ""


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log.warning("parser state corrupt; resetting", exc_info=True)
    return {"offsets": {}, "reported": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _iter_jsonl_lines(path: Path, offset: int) -> Iterator[tuple[dict, int]]:
    """Yield (parsed_line, new_offset) from `offset` forward.

    If the file has shrunk below `offset` (rotation/truncation) we start
    from 0. Partial trailing lines (no \\n yet) are NOT consumed; the
    returned new_offset stops before them so the next scan picks up the
    full line.
    """
    size = path.stat().st_size
    if size < offset:
        offset = 0

    with path.open("rb") as f:
        f.seek(offset)
        buf = b""
        pos = offset
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                pos += len(line) + 1
                if not line.strip():
                    continue
                try:
                    yield json.loads(line.decode("utf-8")), pos
                except json.JSONDecodeError:
                    log.debug("skipping malformed line in %s", path.name)
                    continue
        # buf now holds the unterminated trailing line (if any); leave it.


def _parse_timestamp(s: str) -> float:
    try:
        # Claude Code writes ISO 8601 with trailing Z.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


def _extract_message(line: dict) -> Optional[ParsedMessage]:
    """Return a ParsedMessage if `line` is an assistant message with usage."""
    if line.get("type") != "assistant":
        return None
    msg = line.get("message") or {}
    usage = msg.get("usage") or {}
    msg_id = msg.get("id")
    if not msg_id:
        return None
    return ParsedMessage(
        message_id=msg_id,
        session_id=line.get("sessionId") or "",
        model=msg.get("model") or "unknown",
        timestamp=_parse_timestamp(line.get("timestamp") or ""),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        entrypoint=line.get("entrypoint") or "",
    )


def scan_once(store: EventStore) -> int:
    """Run one scan pass over all project .jsonl files.

    Returns the number of events emitted.
    """
    if not PROJECTS_DIR.exists():
        return 0

    state = _load_state()
    offsets: dict[str, int] = state.get("offsets", {})
    reported: dict[str, int] = state.get("reported", {})
    # Persist per-session entrypoint so we can attribute assistant lines
    # that don't carry the field themselves (only some do).
    session_entrypoints: dict[str, str] = state.get("session_entrypoints", {})

    # Aggregate latest (max output_tokens) per message.id across this pass.
    latest: dict[str, ParsedMessage] = {}

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        key = str(jsonl)
        start = int(offsets.get(key, 0))
        new_offset = start
        try:
            for line, pos in _iter_jsonl_lines(jsonl, start):
                new_offset = pos
                # Record any entrypoint we observe, keyed by sessionId.
                sid = line.get("sessionId") or ""
                ep_here = line.get("entrypoint")
                if sid and ep_here and sid not in session_entrypoints:
                    session_entrypoints[sid] = ep_here

                parsed = _extract_message(line)
                if parsed is None:
                    continue
                # Fall back to session-level entrypoint if this line lacks one.
                if not parsed.entrypoint:
                    parsed.entrypoint = session_entrypoints.get(parsed.session_id, "")
                prev = latest.get(parsed.message_id)
                if prev is None or parsed.output_tokens > prev.output_tokens:
                    latest[parsed.message_id] = parsed
        except OSError:
            log.warning("failed to read %s", jsonl, exc_info=True)
            continue
        offsets[key] = new_offset

    emitted = 0
    user = os_username()
    host = hostname()
    for msg_id, parsed in latest.items():
        prev_reported = reported.get(msg_id, 0)
        if parsed.output_tokens <= prev_reported:
            # Nothing new for this message.id (it stabilized before we saw it).
            continue

        is_first = msg_id not in reported
        # On first emission, report full input/cache tokens + all output tokens
        # seen so far. On delta emission (streaming spanned poll intervals),
        # only report the additional output tokens; input/cache already counted.
        event = Event(
            user=user,
            hostname=host,
            source=SOURCE,
            event_type="message",
            timestamp=parsed.timestamp,
            conversation_id=parsed.session_id,
            message_id=msg_id,
            model=parsed.model,
            input_tokens=parsed.input_tokens if is_first else 0,
            output_tokens=parsed.output_tokens - prev_reported,
            cache_creation_tokens=parsed.cache_creation_tokens if is_first else 0,
            cache_read_tokens=parsed.cache_read_tokens if is_first else 0,
            extras={
                "delta": not is_first,
                "entrypoint": parsed.entrypoint,
            },
        )
        store.add(event)
        reported[msg_id] = parsed.output_tokens
        emitted += 1

    state["offsets"] = offsets
    state["reported"] = reported
    state["session_entrypoints"] = session_entrypoints
    _save_state(state)
    return emitted


def run_poller(
    store: EventStore, stop_event: threading.Event, interval_seconds: float = 60.0
) -> None:
    """Blocking loop intended to run in a daemon thread."""
    log.info("claude code parser poller started (interval=%ss)", interval_seconds)
    # One immediate scan so we don't wait the full interval at startup.
    while not stop_event.is_set():
        try:
            n = scan_once(store)
            if n:
                log.info("claude code parser: emitted %d event(s)", n)
        except Exception:
            log.exception("claude code parser scan failed")
        stop_event.wait(interval_seconds)
