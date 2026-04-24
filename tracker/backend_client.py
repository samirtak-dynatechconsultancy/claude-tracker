"""Uploads queued events directly to Supabase PostgREST.

Previously routed through an embedded FastAPI backend; now we hit
``{SUPABASE_URL}/rest/v1/events`` straight from the tracker, which drops
an entire process boundary, the httpx→uvicorn→httpx double hop, and the
need to bundle the supabase-py SDK into the exe. RLS on the ``events``
table is the real security boundary — see backend/supabase_schema.sql.

Contract:
    POST {SUPABASE_URL}/rest/v1/events
    Headers:
        apikey: <anon key>
        Authorization: Bearer <anon key>
        Content-Type: application/json
        Prefer: return=minimal
    Body: JSON array of row dicts (one per event).

PostgREST returns 201 on success. Anything else -> exponential backoff.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from .config import Config, UPLOAD_BATCH_SIZE, UPLOAD_INTERVAL_SECONDS
from .upload_queue import UploadQueue

log = logging.getLogger(__name__)

INITIAL_BACKOFF = 5.0
MAX_BACKOFF = 300.0  # 5 min


def _row(event: dict, received_at: float) -> dict:
    """Shape a queued event into a Supabase ``events`` row.

    Kept in sync with backend/db_supabase.py:SupabaseDB.insert_events —
    that path still exists for the standalone dashboard/backend, but no
    longer runs in the tracker.
    """
    return {
        "user": event.get("user", ""),
        "hostname": event.get("hostname", ""),
        "source": event.get("source", ""),
        "event_type": event.get("event_type", ""),
        "timestamp": float(event.get("timestamp") or 0.0),
        "conversation_id": event.get("conversation_id"),
        "message_id": event.get("message_id"),
        "model": event.get("model"),
        "input_tokens": int(event.get("input_tokens") or 0),
        "output_tokens": int(event.get("output_tokens") or 0),
        "cache_creation_tokens": int(event.get("cache_creation_tokens") or 0),
        "cache_read_tokens": int(event.get("cache_read_tokens") or 0),
        "session_id": event.get("session_id"),
        "extras": event.get("extras") or {},
        "received_at": received_at,
    }


def _post(client: httpx.Client, url: str, key: str, rows: list[dict]) -> bool:
    try:
        r = client.post(
            url,
            json=rows,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # return=minimal skips echoing the inserted rows back — smaller
                # response, no JSON-parse on our side for the happy path.
                "Prefer": "return=minimal",
            },
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        log.warning("Supabase POST failed: %s", e)
        return False
    if 200 <= r.status_code < 300:
        return True
    # 401/403 = bad/expired anon key; 400 = schema drift. Both are config
    # issues worth loud-logging rather than silently retrying forever, but
    # we still back off (don't drop data) in case it's transient.
    log.warning("Supabase POST rejected: %s %s", r.status_code, r.text[:200])
    return False


def run_uploader(
    queue: UploadQueue,
    config: Config,
    stop_event: threading.Event,
    interval_seconds: float = UPLOAD_INTERVAL_SECONDS,
) -> None:
    """Daemon thread body: drain the queue in batches, with backoff on errors."""
    if not config.supabase_url or not config.supabase_key:
        log.info("backend uploader disabled (Supabase URL/key missing)")
        return

    endpoint = config.supabase_url.rstrip("/") + "/rest/v1/events"
    log.info("backend uploader started -> %s", endpoint)
    backoff = INITIAL_BACKOFF
    with httpx.Client() as client:
        while not stop_event.is_set():
            try:
                batch = queue.next_batch(UPLOAD_BATCH_SIZE)
                if not batch:
                    backoff = INITIAL_BACKOFF  # idle; reset backoff
                    stop_event.wait(interval_seconds)
                    continue

                ids = [bid for bid, _ in batch]
                received_at = time.time()
                rows = [_row(p, received_at) for _, p in batch]
                ok = _post(client, endpoint, config.supabase_key, rows)
                if ok:
                    acked = queue.ack(ids)
                    log.info(
                        "uploaded %d events (queue depth now %d)",
                        acked, queue.depth(),
                    )
                    backoff = INITIAL_BACKOFF
                    # Burn down the queue without pausing if more is pending.
                    continue

                log.info(
                    "upload failed; backing off %.0fs (queue depth %d)",
                    backoff, queue.depth(),
                )
                stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception:
                log.exception("uploader iteration crashed")
                stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
