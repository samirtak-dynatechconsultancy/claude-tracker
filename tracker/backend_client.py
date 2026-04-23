"""Uploads queued events to the central backend.

Contract:
    POST {backend_url}/events
    Headers: X-API-Key: <backend_api_key>
    Body: { "events": [ ...asdict(Event) ] }

    Backend returns 2xx on acceptance -> we ack the batch.
    Any other status (or transport failure) -> exponential backoff.
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


def _post(client: httpx.Client, url: str, api_key: str, batch: list[dict]) -> bool:
    try:
        r = client.post(
            url.rstrip("/") + "/events",
            json={"events": batch},
            headers={"X-API-Key": api_key},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        log.warning("backend POST failed: %s", e)
        return False
    if 200 <= r.status_code < 300:
        return True
    log.warning("backend POST rejected: %s %s", r.status_code, r.text[:200])
    # 4xx other than 429 is likely a config issue (bad API key, bad schema).
    # We still return False to trigger backoff rather than dropping data.
    return False


def run_uploader(
    queue: UploadQueue,
    config: Config,
    stop_event: threading.Event,
    interval_seconds: float = UPLOAD_INTERVAL_SECONDS,
) -> None:
    """Daemon thread body: drain the queue in batches, with backoff on errors."""
    if not config.backend_url:
        log.info("backend uploader disabled (no backend_url configured)")
        return

    log.info("backend uploader started -> %s", config.backend_url)
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
                payloads = [p for _, p in batch]
                ok = _post(client, config.backend_url, config.backend_api_key, payloads)
                if ok:
                    acked = queue.ack(ids)
                    log.info("uploaded %d events (queue depth now %d)",
                             acked, queue.depth())
                    backoff = INITIAL_BACKOFF
                    # Burn down the queue without pausing if more is pending.
                    continue

                # Failure path: back off.
                log.info("upload failed; backing off %.0fs (queue depth %d)",
                         backoff, queue.depth())
                stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception:
                log.exception("uploader iteration crashed")
                stop_event.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
