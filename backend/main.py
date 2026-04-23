"""Central backend for the Claude Usage Tracker.

Endpoints:
    POST /events            -> ingest from desktop trackers. X-API-Key required.
    GET  /api/summary       -> aggregated stats (JSON). Admin auth required.
    GET  /                  -> dashboard (HTML). Admin auth required.
    GET  /healthz           -> liveness probe (unauthenticated).

Auth:
    - Trackers authenticate with CLAUDE_TRACKER_API_KEY (X-API-Key header).
    - Dashboard uses HTTP Basic with CLAUDE_TRACKER_ADMIN_USER /
      CLAUDE_TRACKER_ADMIN_PASS (both default to "admin" for dev; set
      real values in production).

Config via env vars:
    CLAUDE_TRACKER_API_KEY       (required for /events)
    CLAUDE_TRACKER_ADMIN_USER    (default: admin)
    CLAUDE_TRACKER_ADMIN_PASS    (default: admin)
    CLAUDE_TRACKER_BACKEND_DB    (default: ./data/events.sqlite)
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Load `.env` at repo root (if present) before we read any env vars. This
# lets people run `python -m backend.main` without a pre-populated shell.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv is optional in dev

from . import __version__
from .db import get_db

logger = logging.getLogger("backend")


API_KEY_ENV = "CLAUDE_TRACKER_API_KEY"
ADMIN_USER = os.environ.get("CLAUDE_TRACKER_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("CLAUDE_TRACKER_ADMIN_PASS", "admin")

basic_auth = HTTPBasic()


class EventIn(BaseModel):
    user: str
    hostname: str = ""
    source: str
    event_type: str
    timestamp: float
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    session_id: Optional[str] = None
    extras: dict = Field(default_factory=dict)


class EventBatch(BaseModel):
    events: list[EventIn]


def _require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get(API_KEY_ENV, "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"server misconfigured: {API_KEY_ENV} not set",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="bad api key")


def _require_admin(creds: HTTPBasicCredentials = Depends(basic_auth)) -> str:
    if not (
        secrets.compare_digest(creds.username, ADMIN_USER)
        and secrets.compare_digest(creds.password, ADMIN_PASS)
    ):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def create_app() -> FastAPI:
    app = FastAPI(title="Claude Usage Tracker — Backend", version=__version__)
    db = get_db()
    backend_name = type(db).__name__
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Storage backend: %s", backend_name)
    dashboard_dir = Path(__file__).parent / "dashboard"

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.post("/events", dependencies=[Depends(_require_api_key)])
    def ingest(batch: EventBatch):
        n = db.insert_events([e.model_dump() for e in batch.events], time.time())
        return {"accepted": n}

    @app.get("/api/summary", dependencies=[Depends(_require_admin)])
    def summary(
        user: Optional[str] = None,
        source: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ):
        # `user` and `source` are comma-separated lists; `start`/`end` are
        # unix seconds. All are optional — omitted means no filter on that axis.
        users = [u for u in (user.split(",") if user else []) if u]
        sources = [s for s in (source.split(",") if source else []) if s]
        return JSONResponse(
            db.summary(
                time.time(),
                users=users or None,
                sources=sources or None,
                start_ts=start,
                end_ts=end,
            )
        )

    # Static dashboard assets.
    if (dashboard_dir / "assets").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(dashboard_dir / "assets")),
            name="assets",
        )

    @app.get("/")
    def dashboard(_user: str = Depends(_require_admin)):
        index = dashboard_dir / "index.html"
        if not index.exists():
            raise HTTPException(status_code=500, detail="dashboard not found")
        return FileResponse(str(index))

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=os.environ.get("CLAUDE_TRACKER_BACKEND_HOST", "0.0.0.0"),
        port=int(os.environ.get("CLAUDE_TRACKER_BACKEND_PORT", "8080")),
        log_level="info",
    )
