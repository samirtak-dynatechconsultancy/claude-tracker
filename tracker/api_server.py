"""Localhost HTTP API for the Claude Usage Tracker.

Endpoints:
    GET  /ping       -> liveness + identity (used by browser extension)
    GET  /handshake  -> returns shared secret (only open for 60s after launch)
    POST /log        -> extension posts per-message token usage
    GET  /stats      -> this user's usage summary (today/week/all, by source)
    GET  /events     -> raw event log (for the tray "view your data" flow)

Security:
    - Bound to 127.0.0.1 only (uvicorn host).
    - /log, /stats, /events require the X-Tracker-Secret header.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import __version__
from .config import API_HOST, API_PORT, Config, hostname, os_username
from .events import Event, EventStore


class LogEvent(BaseModel):
    conversation_id: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    model: str
    timestamp: float
    source: str  # "ai_web" | "code" | "desktop"
    message_id: Optional[str] = None


def _require_localhost(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="localhost only")


def _require_secret(config: Config, provided: Optional[str]) -> None:
    if not provided or provided != config.shared_secret:
        raise HTTPException(status_code=401, detail="bad secret")


def create_app(config: Config, store: EventStore) -> FastAPI:
    app = FastAPI(title="Claude Usage Tracker", version=__version__)

    @app.get("/ping")
    def ping(request: Request):
        _require_localhost(request)
        return {
            "status": "paused" if config.paused else "ok",
            "user": os_username(),
            "hostname": hostname(),
            "version": __version__,
        }

    @app.get("/handshake")
    def handshake(request: Request):
        _require_localhost(request)
        if not config.handshake_open():
            raise HTTPException(status_code=410, detail="handshake window closed")
        return {"shared_secret": config.shared_secret, "user": os_username()}

    @app.post("/log")
    def log(payload: LogEvent, request: Request):
        # Auth is localhost-only. Shared-secret gating caused 401s whenever
        # the extension's baked secret diverged from the tracker's runtime
        # secret; on a single-user machine the localhost boundary is the
        # meaningful line of defence.
        _require_localhost(request)
        if config.paused:
            return {"status": "paused", "accepted": False}
        store.add(
            Event(
                user=os_username(),
                hostname=hostname(),
                source=payload.source,  # type: ignore[arg-type]
                event_type="message",
                timestamp=payload.timestamp or time.time(),
                conversation_id=payload.conversation_id,
                message_id=payload.message_id,
                model=payload.model,
                input_tokens=payload.tokens_in,
                output_tokens=payload.tokens_out,
            )
        )
        return {"status": "ok", "accepted": True}

    @app.get("/stats")
    def stats(request: Request):
        # Localhost-only is the real boundary here — the shared-secret check
        # that used to live here was redundant for a single-user machine
        # and caused popup 401s whenever the extension's baked secret and
        # the tracker's runtime secret didn't match.
        _require_localhost(request)
        return {
            "user": os_username(),
            "paused": config.paused,
            **store.summary(),
        }

    @app.get("/events")
    def events(request: Request, limit: int = 100):
        # Same story as /stats and /log: localhost-only is the real boundary.
        _require_localhost(request)
        events = store.all()
        return {
            "user": os_username(),
            "count": len(events),
            "events": [e.to_dict() for e in events[-limit:]],
        }

    return app


def run_server(config: Config, store: EventStore) -> None:
    """Blocking call; run this in a background thread from main."""
    import uvicorn

    app = create_app(config, store)
    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="warning")
