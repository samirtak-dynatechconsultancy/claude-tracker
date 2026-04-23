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

# Backend upload defaults. Every tracker runs its own embedded FastAPI
# backend (see run_backend below) that forwards into the central Supabase.
# Because the tracker and backend run in the same exe, backend_url is
# localhost and backend_api_key is a cosmetic shared constant.
# These are baked in and NOT read from config.json — there's no sane
# reason for a teammate to point the upload queue anywhere other than
# their own local embedded backend.
DEFAULT_BACKEND_URL = "http://127.0.0.1:8080"
DEFAULT_BACKEND_API_KEY = "claude-tracker-internal"
UPLOAD_INTERVAL_SECONDS = 60
UPLOAD_BATCH_SIZE = 100

# Defaults for the embedded backend. These are applied to every fresh
# config.json so new installs auto-configure. Anon Supabase key + admin
# creds are deliberately baked in — they only protect the central store
# via Supabase RLS, and shipping them is how a zero-config team deploy
# works. Rotate them in Supabase if a machine goes missing.
DEFAULT_RUN_BACKEND = True
DEFAULT_BACKEND_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8080
DEFAULT_BACKEND_ENV = {
    "SUPABASE_URL": "https://mbezrhsfiewdpulxmtrk.supabase.co",
    "SUPABASE_ANON_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1iZXpyaHNmaWV3ZHB1bHhtdHJrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY5MjIyNDMsImV4cCI6MjA5MjQ5ODI0M30.ineH3EyjkS8m8CYmm1NACu9qksS5u4cs94q1Hq9MxF4",
    "CLAUDE_TRACKER_ADMIN_USER": "admin",
    "CLAUDE_TRACKER_ADMIN_PASS": "admin",
}


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
    backend_url: str = DEFAULT_BACKEND_URL
    backend_api_key: str = DEFAULT_BACKEND_API_KEY
    # Every exe runs its own embedded FastAPI backend by default, all
    # pointing at the shared Supabase — see DEFAULT_BACKEND_ENV above.
    run_backend: bool = DEFAULT_RUN_BACKEND
    backend_host: str = DEFAULT_BACKEND_HOST
    backend_port: int = DEFAULT_BACKEND_PORT
    # Extra env vars pushed into os.environ before the embedded backend
    # imports — e.g. CLAUDE_TRACKER_ADMIN_USER/PASS, SUPABASE_URL,
    # SUPABASE_ANON_KEY, CLAUDE_TRACKER_BACKEND_DB.
    backend_env: dict = field(default_factory=lambda: dict(DEFAULT_BACKEND_ENV))
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
