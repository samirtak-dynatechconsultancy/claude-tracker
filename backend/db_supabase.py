"""Supabase backend via the HTTP PostgREST API (supabase-py).

Selected when SUPABASE_URL + SUPABASE_ANON_KEY are both set. Inserts go
through the `events` table directly (RLS policies in
`supabase_schema.sql` allow anon INSERT). Summary aggregation is
delegated to the `summary_v1` Postgres function via RPC — PostgREST
cannot express our GROUP BY + day-bucket logic otherwise.

The FastAPI layer's own auth (X-API-Key on /events, HTTP Basic on
/api/summary) remains the real security perimeter. The anon key is
considered publishable; nothing here relies on it being secret.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterable, Optional

from supabase import Client, create_client


def _client() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_ANON_KEY", "").strip()
        or os.environ.get("SUPABASE_KEY", "").strip()
    )
    if not url or not key:
        raise RuntimeError(
            "Supabase backend selected but SUPABASE_URL / SUPABASE_ANON_KEY missing"
        )
    return create_client(url, key)


class SupabaseDB:
    def __init__(self) -> None:
        self._client = _client()

    def insert_events(self, events: Iterable[dict], received_at: float) -> int:
        rows = []
        for e in events:
            rows.append({
                "user": e.get("user", ""),
                "hostname": e.get("hostname", ""),
                "source": e.get("source", ""),
                "event_type": e.get("event_type", ""),
                "timestamp": float(e.get("timestamp") or 0.0),
                "conversation_id": e.get("conversation_id"),
                "message_id": e.get("message_id"),
                "model": e.get("model"),
                "input_tokens": int(e.get("input_tokens") or 0),
                "output_tokens": int(e.get("output_tokens") or 0),
                "cache_creation_tokens": int(e.get("cache_creation_tokens") or 0),
                "cache_read_tokens": int(e.get("cache_read_tokens") or 0),
                "session_id": e.get("session_id"),
                # supabase-py serializes dicts to JSON for jsonb columns automatically.
                "extras": e.get("extras") or {},
                "received_at": received_at,
            })
        if not rows:
            return 0
        res = self._client.table("events").insert(rows).execute()
        return len(res.data or [])

    def summary(
        self,
        now: float,
        *,
        users: Optional[list[str]] = None,
        sources: Optional[list[str]] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> dict:
        # All aggregation lives in the `summary_v1` Postgres function —
        # see backend/supabase_schema.sql. Python just forwards filter args.
        params = {
            "p_users": list(users) if users else None,
            "p_sources": list(sources) if sources else None,
            "p_start_ts": start_ts,
            "p_end_ts": end_ts,
        }
        res = self._client.rpc("summary_v1", params).execute()
        data = res.data
        # RPC returns the JSONB payload directly. supabase-py may return it
        # either as a dict already (python-side JSON parse) or as a string
        # depending on version; handle both.
        if isinstance(data, str):
            data = json.loads(data)
        if data is None:
            # No data yet — fabricate an empty shape so the dashboard renders.
            data = {
                "per_user": {},
                "leaderboard": {"today": [], "week": [], "all": []},
                "time_series_daily": [],
                "known_users": [],
                "known_sources": [],
                "filter": {
                    "users": users or [],
                    "sources": sources or [],
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                },
                "generated_at": now,
            }
        return data
