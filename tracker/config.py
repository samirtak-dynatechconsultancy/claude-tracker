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

# Backend upload (optional). Can be overridden in config.json or via env vars
# CLAUDE_TRACKER_BACKEND_URL / CLAUDE_TRACKER_BACKEND_API_KEY.
DEFAULT_BACKEND_URL = ""  # empty disables uploads
DEFAULT_BACKEND_API_KEY = ""
UPLOAD_INTERVAL_SECONDS = 60
UPLOAD_BATCH_SIZE = 100


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
    # Optional: run the FastAPI backend inside this process so one exe
    # serves both tracking and the dashboard. Typically only true on the
    # machine that acts as the central server.
    run_backend: bool = False
    backend_host: str = "127.0.0.1"
    backend_port: int = 8080
    # Extra env vars pushed into os.environ before the embedded backend
    # imports — e.g. CLAUDE_TRACKER_ADMIN_USER/PASS, SUPABASE_URL,
    # SUPABASE_ANON_KEY, CLAUDE_TRACKER_BACKEND_DB.
    backend_env: dict = field(default_factory=dict)
    launch_time: float = field(default_factory=time.time)

    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    @classmethod
    def load_or_create(cls) -> "Config":
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
                backend_url=data.get("backend_url", DEFAULT_BACKEND_URL),
                backend_api_key=data.get("backend_api_key", DEFAULT_BACKEND_API_KEY),
                run_backend=data.get("run_backend", False),
                backend_host=data.get("backend_host", "127.0.0.1"),
                backend_port=data.get("backend_port", 8080),
                backend_env=data.get("backend_env", {}),
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            cfg = cls(shared_secret=secrets.token_urlsafe(32), created_at=time.time())
            cfg.save()

        # Env overrides win — useful for enterprise rollouts without
        # baking the URL into per-user config.
        env_url = os.environ.get("CLAUDE_TRACKER_BACKEND_URL")
        env_key = os.environ.get("CLAUDE_TRACKER_BACKEND_API_KEY")
        if env_url is not None:
            cfg.backend_url = env_url
        if env_key is not None:
            cfg.backend_api_key = env_key
        return cfg

    def save(self) -> None:
        with self._lock:
            config_path().write_text(
                json.dumps(
                    {
                        "shared_secret": self.shared_secret,
                        "created_at": self.created_at,
                        "paused": self.paused,
                        "backend_url": self.backend_url,
                        "backend_api_key": self.backend_api_key,
                        "run_backend": self.run_backend,
                        "backend_host": self.backend_host,
                        "backend_port": self.backend_port,
                        "backend_env": self.backend_env,
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
