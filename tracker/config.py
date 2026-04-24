"""Configuration: shared secret, paths, OS username.

The shared secret is generated on first run and persisted to
%APPDATA%\\ClaudeTracker\\config.json. The browser extension reads it
from the desktop app via the one-time /handshake endpoint.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

API_HOST = "127.0.0.1"
API_PORT = 47821
HANDSHAKE_WINDOW_SECONDS = 60

# Upload tuning (applied to the direct-to-Supabase uploader).
UPLOAD_INTERVAL_SECONDS = 60
UPLOAD_BATCH_SIZE = 100

# Central Supabase project. Anon key is deliberately baked in — Supabase
# RLS on the ``events`` table is the real security boundary (see
# backend/supabase_schema.sql). Rotate via the Supabase dashboard if a
# machine goes missing; teammates pick up the new key on next release.
DEFAULT_SUPABASE_URL = "https://mbezrhsfiewdpulxmtrk.supabase.co"
DEFAULT_SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1iZXpyaHNmaWV3ZHB1bHhtdHJrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY5MjIyNDMsImV4cCI6MjA5MjQ5ODI0M30.ineH3EyjkS8m8CYmm1NACu9qksS5u4cs94q1Hq9MxF4"

# Where "Open Dashboard" in the tray menu points. The dashboard lives as
# a standalone deploy now (see backend/ — run it wherever you want and
# replace this URL before building). Empty string falls back to /ping.
DEFAULT_DASHBOARD_URL = ""


def app_data_dir() -> Path:
    """Return the per-user config directory. Windows-first (APPDATA)."""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "ClaudeTracker"
    # Fallback for non-Windows (macOS/Linux support deferred).
    return Path.home() / ".claude-tracker"


def config_path() -> Path:
    return app_data_dir() / "config.json"


def os_username() -> str:
    """Identify the current user. No prompts, no overrides."""
    try:
        return os.getlogin()
    except OSError:
        return getpass.getuser()


def hostname() -> str:
    return socket.gethostname()


@dataclass
class Config:
    shared_secret: str
    created_at: float
    paused: bool = False
    # Where the uploader POSTs rows. Baked in — never read from config.json,
    # never overridden by env, so teammates' installs stay in lockstep.
    supabase_url: str = DEFAULT_SUPABASE_URL
    supabase_key: str = DEFAULT_SUPABASE_KEY
    dashboard_url: str = DEFAULT_DASHBOARD_URL
    launch_time: float = field(default_factory=time.time)

    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    @classmethod
    def load_or_create(cls) -> "Config":
        """Load config.json, creating it on first run.

        Only user-mutable state (shared_secret, created_at, paused) is
        read from / written to disk. All embedded-backend settings come
        from the baked DEFAULT_* constants and are never overridden by
        config.json — this keeps teammates' installs working even if
        they leave a stale config from an older build lying around.
        """
        path = config_path()
        if path.exists():
            # utf-8-sig transparently strips a leading BOM if present —
            # Notepad adds one when users hand-edit config.json, which the
            # plain "utf-8" codec surfaces as a JSONDecodeError.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            cfg = cls(
                shared_secret=data["shared_secret"],
                created_at=data["created_at"],
                paused=data.get("paused", False),
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            cfg = cls(shared_secret=secrets.token_urlsafe(32), created_at=time.time())
            cfg.save()
        return cfg

    def save(self) -> None:
        with self._lock:
            config_path().write_text(
                json.dumps(
                    {
                        "shared_secret": self.shared_secret,
                        "created_at": self.created_at,
                        "paused": self.paused,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = paused
            self.save()

    def handshake_open(self) -> bool:
        """The /handshake endpoint is only valid for a short window after launch."""
        return (time.time() - self.launch_time) < HANDSHAKE_WINDOW_SECONDS
