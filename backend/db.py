"""SQLite schema + helpers for the backend.

One table (`events`), no migrations — the schema is additive and CREATE
IF NOT EXISTS. If you need to evolve it in real deployments, add
columns with ALTER TABLE and keep the reader tolerant.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    hostname TEXT NOT NULL,
    source TEXT NOT NULL,           -- ai_web | code | desktop
    event_type TEXT NOT NULL,       -- message | session_start | session_end
    timestamp REAL NOT NULL,
    conversation_id TEXT,
    message_id TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    extras TEXT NOT NULL DEFAULT '{}',
    received_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS events_user_ts      ON events (user, timestamp);
CREATE INDEX IF NOT EXISTS events_source_ts    ON events (source, timestamp);
CREATE INDEX IF NOT EXISTS events_timestamp    ON events (timestamp);
"""


class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def insert_events(self, events: Iterable[dict], received_at: float) -> int:
        rows = []
        for e in events:
            rows.append((
                e.get("user", ""),
                e.get("hostname", ""),
                e.get("source", ""),
                e.get("event_type", ""),
                float(e.get("timestamp") or 0.0),
                e.get("conversation_id"),
                e.get("message_id"),
                e.get("model"),
                int(e.get("input_tokens") or 0),
                int(e.get("output_tokens") or 0),
                int(e.get("cache_creation_tokens") or 0),
                int(e.get("cache_read_tokens") or 0),
                e.get("session_id"),
                json.dumps(e.get("extras") or {}),
                received_at,
            ))
        with self._lock:
            cur = self._conn.executemany(
                """INSERT INTO events (
                    user, hostname, source, event_type, timestamp,
                    conversation_id, message_id, model,
                    input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                    session_id, extras, received_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return cur.rowcount

    def summary(
        self,
        now: float,
        *,
        users: Optional[list[str]] = None,
        sources: Optional[list[str]] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> dict:
        day_ago = now - 86400
        week_ago = now - 7 * 86400

        # Build the shared WHERE fragment for the filtered views.
        # Leaderboard windows (today/week) intentionally ignore `start_ts`
        # so they keep their fixed semantics; they do honour user/source.
        def _filter_clause(use_time: bool) -> tuple[str, list]:
            conds: list[str] = []
            p: list = []
            if users:
                conds.append("user IN (" + ",".join(["?"] * len(users)) + ")")
                p.extend(users)
            if sources:
                conds.append("source IN (" + ",".join(["?"] * len(sources)) + ")")
                p.extend(sources)
            if use_time and start_ts is not None:
                conds.append("timestamp >= ?")
                p.append(start_ts)
            if use_time and end_ts is not None:
                conds.append("timestamp < ?")
                p.append(end_ts)
            return (" AND ".join(conds), p)

        where_frag, where_params = _filter_clause(use_time=True)
        where = f"WHERE {where_frag}" if where_frag else ""

        with self._lock:
            # Enumerate known users + sources across the full dataset so the
            # filter UI has a stable list even when the current filter is empty.
            known_users = [
                r[0] for r in self._conn.execute(
                    "SELECT DISTINCT user FROM events ORDER BY user"
                ).fetchall()
            ]
            known_sources = [
                r[0] for r in self._conn.execute(
                    "SELECT DISTINCT source FROM events ORDER BY source"
                ).fetchall()
            ]

            cur = self._conn.execute(
                f"""
                SELECT user, source, event_type,
                       SUM(input_tokens)  AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_creation_tokens) AS cache_creation_tokens,
                       SUM(cache_read_tokens) AS cache_read_tokens,
                       COUNT(*) AS n,
                       MIN(timestamp) AS first_ts,
                       MAX(timestamp) AS last_ts
                FROM events {where}
                GROUP BY user, source, event_type
                """,
                where_params,
            )
            rows = cur.fetchall()

            # Leaderboard windows: user/source filters apply, time window is fixed.
            lb_frag, lb_params = _filter_clause(use_time=False)

            def window_totals(since: Optional[float]) -> list[dict]:
                q = (
                    "SELECT user, SUM(input_tokens + cache_creation_tokens + cache_read_tokens) AS in_tok, "
                    "SUM(output_tokens) AS out_tok, COUNT(*) AS n FROM events "
                    "WHERE event_type='message'"
                )
                p = list(lb_params)
                if lb_frag:
                    q += " AND " + lb_frag
                if since is not None:
                    q += " AND timestamp >= ?"
                    p.append(since)
                q += " GROUP BY user ORDER BY out_tok DESC"
                return [dict(r) for r in self._conn.execute(q, p).fetchall()]

            today = window_totals(day_ago)
            week = window_totals(week_ago)
            alltime = window_totals(None)

            # Time series: respect the full filter (including time window) but
            # default to last 30 days when no explicit start_ts was supplied.
            ts_start = start_ts if start_ts is not None else (now - 30 * 86400)
            ts_frag_parts = ["event_type = 'message'", "timestamp >= ?"]
            ts_params: list = [ts_start]
            if end_ts is not None:
                ts_frag_parts.append("timestamp < ?")
                ts_params.append(end_ts)
            if users:
                ts_frag_parts.append(
                    "user IN (" + ",".join(["?"] * len(users)) + ")"
                )
                ts_params.extend(users)
            if sources:
                ts_frag_parts.append(
                    "source IN (" + ",".join(["?"] * len(sources)) + ")"
                )
                ts_params.extend(sources)
            ts_cur = self._conn.execute(
                f"""
                SELECT user,
                       CAST(timestamp / 86400 AS INTEGER) AS day,
                       SUM(input_tokens + cache_creation_tokens + cache_read_tokens) AS in_tok,
                       SUM(output_tokens) AS out_tok
                FROM events
                WHERE {' AND '.join(ts_frag_parts)}
                GROUP BY user, day
                ORDER BY day
                """,
                ts_params,
            )
            time_series = [dict(r) for r in ts_cur.fetchall()]

        # Reshape per-user buckets from the first query.
        per_user: dict[str, dict] = defaultdict(
            lambda: {
                "by_source": defaultdict(_zero_bucket),
                "total": _zero_bucket(),
            }
        )
        for r in rows:
            d = dict(r)
            u = d["user"]
            src = d["source"]
            et = d["event_type"]
            bucket = per_user[u]["by_source"][src]
            total = per_user[u]["total"]
            if et == "message":
                bucket["messages"] += int(d["n"])
                bucket["input_tokens"] += int(d["input_tokens"] or 0)
                bucket["output_tokens"] += int(d["output_tokens"] or 0)
                bucket["cache_creation_tokens"] += int(d["cache_creation_tokens"] or 0)
                bucket["cache_read_tokens"] += int(d["cache_read_tokens"] or 0)
                total["messages"] += int(d["n"])
                total["input_tokens"] += int(d["input_tokens"] or 0)
                total["output_tokens"] += int(d["output_tokens"] or 0)
            elif et == "session_start":
                bucket["session_starts"] += int(d["n"])
                total["session_starts"] += int(d["n"])
            elif et == "session_end":
                bucket["session_ends"] += int(d["n"])
                total["session_ends"] += int(d["n"])

        # Normalize defaultdicts for JSON.
        result_users = {}
        for u, v in per_user.items():
            result_users[u] = {
                "total": v["total"],
                "by_source": dict(v["by_source"]),
            }

        return {
            "per_user": result_users,
            "leaderboard": {
                "today": today,
                "week": week,
                "all": alltime,
            },
            "time_series_daily": time_series,
            "known_users": known_users,
            "known_sources": known_sources,
            "filter": {
                "users": users or [],
                "sources": sources or [],
                "start_ts": start_ts,
                "end_ts": end_ts,
            },
            "generated_at": now,
        }


def _zero_bucket() -> dict:
    return {
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "session_starts": 0,
        "session_ends": 0,
    }


def db_path() -> Path:
    """Database location. Override via CLAUDE_TRACKER_BACKEND_DB env var."""
    override = os.environ.get("CLAUDE_TRACKER_BACKEND_DB")
    if override:
        return Path(override)
    return Path(__file__).parent / "data" / "events.sqlite"


def get_db():
    """Pick the storage backend based on env. Supabase wins if configured.

    Supabase is selected when SUPABASE_URL and an anon/publishable key
    (SUPABASE_ANON_KEY, or SUPABASE_KEY as a fallback alias) are both set.
    Otherwise we fall back to local SQLite at `db_path()`.
    """
    has_supabase = (
        bool(os.environ.get("SUPABASE_URL", "").strip())
        and bool(
            os.environ.get("SUPABASE_ANON_KEY", "").strip()
            or os.environ.get("SUPABASE_KEY", "").strip()
        )
    )
    if has_supabase:
        from .db_supabase import SupabaseDB
        return SupabaseDB()
    return DB(db_path())
