"""Best-effort detection of Claude Desktop activity.

We can't see tokens — the app doesn't expose them locally. All we can
do is observe whether `Claude.exe` (Windows) / `Claude` (macOS) is
running, and emit session_start / session_end events around it.

On Windows we prefer `psutil`. If it's unavailable we fall back to the
`tasklist` command.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from typing import Optional

from .config import hostname, os_username
from .events import Event, EventStore

log = logging.getLogger(__name__)

SOURCE = "desktop"

# Process image names to look for, per platform.
_WINDOWS_NAMES = {"claude.exe"}
_MAC_NAMES = {"claude"}


def _claude_running_psutil() -> bool:
    import psutil  # imported lazily so the module loads on systems without it

    names = _WINDOWS_NAMES if platform.system() == "Windows" else _MAC_NAMES
    for proc in psutil.process_iter(attrs=["name"]):
        n = (proc.info.get("name") or "").lower()
        if n in names:
            return True
    return False


def _claude_running_tasklist() -> bool:
    """Windows fallback using `tasklist`. Returns True if Claude.exe is up."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=0x08000000,  # CREATE_NO_WINDOW, ignored on non-Windows
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return "Claude.exe" in (out.stdout or "")


def is_claude_running() -> bool:
    try:
        return _claude_running_psutil()
    except ImportError:
        if platform.system() == "Windows":
            return _claude_running_tasklist()
        return False
    except Exception:
        log.debug("psutil check failed", exc_info=True)
        return False


def _emit(
    store: EventStore,
    event_type: str,
    session_id: str,
    extras: Optional[dict] = None,
) -> None:
    store.add(
        Event(
            user=os_username(),
            hostname=hostname(),
            source=SOURCE,
            event_type=event_type,  # "session_start" | "session_end"
            timestamp=time.time(),
            session_id=session_id,
            extras=extras or {},
        )
    )


def run_poller(
    store: EventStore, stop_event: threading.Event, interval_seconds: float = 30.0
) -> None:
    """Emit session_start when Claude.exe appears; session_end when it goes.

    Claude Desktop bundles Claude Code — so a desktop session can overlap
    with code-source message events from the .jsonl parser. To avoid
    conflating "just chatting in the app" with "using Claude Code inside
    the app", we classify each session at session_end:

        chat_only=False  -> parser saw any code events during the session
        chat_only=True   -> no code events during the session; pure chat

    This doesn't change token totals (desktop events carry no tokens) —
    it's just metadata so the dashboard can separate chat time from
    coding time.
    """
    log.info("desktop detector started (interval=%ss)", interval_seconds)
    last_state: Optional[bool] = None
    current_session_id: Optional[str] = None
    session_start_wall: float = 0.0

    def start_session() -> None:
        nonlocal current_session_id, session_start_wall
        session_start_wall = time.time()
        current_session_id = f"desktop-{int(session_start_wall)}"
        _emit(store, "session_start", current_session_id)

    def end_session() -> None:
        nonlocal current_session_id, session_start_wall
        if not current_session_id:
            return
        had_code = store.has_code_activity_since(session_start_wall)
        _emit(
            store,
            "session_end",
            current_session_id,
            extras={
                "chat_only": not had_code,
                "duration_seconds": time.time() - session_start_wall,
            },
        )
        current_session_id = None
        session_start_wall = 0.0

    while not stop_event.is_set():
        try:
            running = is_claude_running()
            if last_state is None:
                if running:
                    start_session()
                last_state = running
            elif running and not last_state:
                start_session()
                last_state = True
            elif not running and last_state:
                end_session()
                last_state = False
        except Exception:
            log.exception("desktop detector poll failed")
        stop_event.wait(interval_seconds)
