-- Supabase schema for Claude Usage Tracker.
-- Apply once via the Supabase SQL Editor.
--
-- This file is designed for the anon-key deployment shape:
--   - The FastAPI backend connects with SUPABASE_URL + SUPABASE_ANON_KEY.
--   - RLS allows anon INSERT + SELECT on `events`. FastAPI's own
--     X-API-Key / HTTP Basic remain the real security perimeter.
--   - Summary aggregation lives in the `summary_v1` Postgres function,
--     called from Python via supabase-py's .rpc().
--
-- If you ever want a stricter posture (no public anon access), delete the
-- policies below and switch the backend to a service-role or DB-URL key.

-- ----------------------------------------------------------------------------
-- Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id                      BIGSERIAL PRIMARY KEY,
    "user"                  TEXT NOT NULL,
    hostname                TEXT NOT NULL DEFAULT '',
    source                  TEXT NOT NULL,      -- ai_web | code | desktop
    event_type              TEXT NOT NULL,      -- message | session_start | session_end
    timestamp               DOUBLE PRECISION NOT NULL,
    conversation_id         TEXT,
    message_id              TEXT,
    model                   TEXT,
    input_tokens            BIGINT NOT NULL DEFAULT 0,
    output_tokens           BIGINT NOT NULL DEFAULT 0,
    cache_creation_tokens   BIGINT NOT NULL DEFAULT 0,
    cache_read_tokens       BIGINT NOT NULL DEFAULT 0,
    session_id              TEXT,
    extras                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at             DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS events_user_ts   ON events ("user", timestamp);
CREATE INDEX IF NOT EXISTS events_source_ts ON events (source, timestamp);
CREATE INDEX IF NOT EXISTS events_timestamp ON events (timestamp);

-- ----------------------------------------------------------------------------
-- RLS — allow anon INSERT + SELECT. FastAPI gates real auth upstream.
-- ----------------------------------------------------------------------------
ALTER TABLE events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS events_anon_insert ON events;
CREATE POLICY events_anon_insert ON events
    FOR INSERT TO anon
    WITH CHECK (true);

DROP POLICY IF EXISTS events_anon_select ON events;
CREATE POLICY events_anon_select ON events
    FOR SELECT TO anon
    USING (true);

-- ----------------------------------------------------------------------------
-- Summary RPC — one function, returns the full dashboard payload as JSONB.
-- Called by the backend via supabase-py: .rpc("summary_v1", {...}).execute()
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION summary_v1(
    p_users    TEXT[]            DEFAULT NULL,
    p_sources  TEXT[]            DEFAULT NULL,
    p_start_ts DOUBLE PRECISION  DEFAULT NULL,
    p_end_ts   DOUBLE PRECISION  DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $func$
DECLARE
    v_now       DOUBLE PRECISION := EXTRACT(EPOCH FROM NOW());
    v_day_ago   DOUBLE PRECISION := v_now - 86400;
    v_week_ago  DOUBLE PRECISION := v_now - 7 * 86400;
    v_ts_start  DOUBLE PRECISION := COALESCE(p_start_ts, v_now - 30 * 86400);
    v_result    JSONB;
BEGIN
    WITH agg AS (
        SELECT "user", source, event_type,
               SUM(input_tokens)           AS input_tokens,
               SUM(output_tokens)          AS output_tokens,
               SUM(cache_creation_tokens)  AS cache_creation_tokens,
               SUM(cache_read_tokens)      AS cache_read_tokens,
               COUNT(*)                    AS n
        FROM events
        WHERE (p_users    IS NULL OR "user"  = ANY(p_users))
          AND (p_sources  IS NULL OR source  = ANY(p_sources))
          AND (p_start_ts IS NULL OR timestamp >= p_start_ts)
          AND (p_end_ts   IS NULL OR timestamp <  p_end_ts)
        GROUP BY "user", source, event_type
    ),
    per_user_by_source AS (
        SELECT "user", source,
               jsonb_build_object(
                 'messages',              COALESCE(SUM(CASE WHEN event_type='message'       THEN n             END), 0),
                 'input_tokens',          COALESCE(SUM(CASE WHEN event_type='message'       THEN input_tokens  END), 0),
                 'output_tokens',         COALESCE(SUM(CASE WHEN event_type='message'       THEN output_tokens END), 0),
                 'cache_creation_tokens', COALESCE(SUM(CASE WHEN event_type='message'       THEN cache_creation_tokens END), 0),
                 'cache_read_tokens',     COALESCE(SUM(CASE WHEN event_type='message'       THEN cache_read_tokens     END), 0),
                 'session_starts',        COALESCE(SUM(CASE WHEN event_type='session_start' THEN n             END), 0),
                 'session_ends',          COALESCE(SUM(CASE WHEN event_type='session_end'   THEN n             END), 0)
               ) AS bucket
        FROM agg
        GROUP BY "user", source
    ),
    per_user_total AS (
        SELECT "user",
               jsonb_build_object(
                 'messages',              COALESCE(SUM(CASE WHEN event_type='message'       THEN n             END), 0),
                 'input_tokens',          COALESCE(SUM(CASE WHEN event_type='message'       THEN input_tokens  END), 0),
                 'output_tokens',         COALESCE(SUM(CASE WHEN event_type='message'       THEN output_tokens END), 0),
                 'cache_creation_tokens', COALESCE(SUM(CASE WHEN event_type='message'       THEN cache_creation_tokens END), 0),
                 'cache_read_tokens',     COALESCE(SUM(CASE WHEN event_type='message'       THEN cache_read_tokens     END), 0),
                 'session_starts',        COALESCE(SUM(CASE WHEN event_type='session_start' THEN n             END), 0),
                 'session_ends',          COALESCE(SUM(CASE WHEN event_type='session_end'   THEN n             END), 0)
               ) AS total
        FROM agg
        GROUP BY "user"
    ),
    per_user AS (
        SELECT t."user",
               jsonb_build_object(
                 'total',     t.total,
                 'by_source', COALESCE((
                   SELECT jsonb_object_agg(source, bucket)
                   FROM per_user_by_source s WHERE s."user" = t."user"
                 ), '{}'::jsonb)
               ) AS user_obj
        FROM per_user_total t
    ),
    lb AS (
        SELECT "user",
               SUM(input_tokens + cache_creation_tokens + cache_read_tokens) AS in_tok,
               SUM(output_tokens) AS out_tok,
               COUNT(*) AS n,
               timestamp
        FROM events
        WHERE event_type = 'message'
          AND (p_users   IS NULL OR "user"  = ANY(p_users))
          AND (p_sources IS NULL OR source  = ANY(p_sources))
        GROUP BY "user", timestamp
    ),
    ts AS (
        SELECT "user",
               FLOOR(timestamp / 86400)::int AS day,
               SUM(input_tokens + cache_creation_tokens + cache_read_tokens) AS in_tok,
               SUM(output_tokens) AS out_tok
        FROM events
        WHERE event_type = 'message'
          AND timestamp >= v_ts_start
          AND (p_end_ts  IS NULL OR timestamp <  p_end_ts)
          AND (p_users   IS NULL OR "user"  = ANY(p_users))
          AND (p_sources IS NULL OR source  = ANY(p_sources))
        GROUP BY "user", day
    )
    SELECT jsonb_build_object(
        'per_user', COALESCE(
            (SELECT jsonb_object_agg("user", user_obj) FROM per_user),
            '{}'::jsonb
        ),
        'leaderboard', jsonb_build_object(
            'today', COALESCE((
                SELECT jsonb_agg(jsonb_build_object('user', "user", 'in_tok', sum_in, 'out_tok', sum_out, 'n', sum_n) ORDER BY sum_out DESC)
                FROM (
                    SELECT "user",
                           SUM(in_tok) AS sum_in,
                           SUM(out_tok) AS sum_out,
                           SUM(n) AS sum_n
                    FROM lb
                    WHERE timestamp >= v_day_ago
                    GROUP BY "user"
                ) x
            ), '[]'::jsonb),
            'week', COALESCE((
                SELECT jsonb_agg(jsonb_build_object('user', "user", 'in_tok', sum_in, 'out_tok', sum_out, 'n', sum_n) ORDER BY sum_out DESC)
                FROM (
                    SELECT "user",
                           SUM(in_tok) AS sum_in,
                           SUM(out_tok) AS sum_out,
                           SUM(n) AS sum_n
                    FROM lb
                    WHERE timestamp >= v_week_ago
                    GROUP BY "user"
                ) x
            ), '[]'::jsonb),
            'all', COALESCE((
                SELECT jsonb_agg(jsonb_build_object('user', "user", 'in_tok', sum_in, 'out_tok', sum_out, 'n', sum_n) ORDER BY sum_out DESC)
                FROM (
                    SELECT "user",
                           SUM(in_tok) AS sum_in,
                           SUM(out_tok) AS sum_out,
                           SUM(n) AS sum_n
                    FROM lb
                    GROUP BY "user"
                ) x
            ), '[]'::jsonb)
        ),
        'time_series_daily', COALESCE(
            (SELECT jsonb_agg(jsonb_build_object('user', "user", 'day', day, 'in_tok', in_tok, 'out_tok', out_tok) ORDER BY day) FROM ts),
            '[]'::jsonb
        ),
        'known_users',   COALESCE((SELECT jsonb_agg(DISTINCT "user" ORDER BY "user") FROM events), '[]'::jsonb),
        'known_sources', COALESCE((SELECT jsonb_agg(DISTINCT source ORDER BY source) FROM events), '[]'::jsonb),
        'filter', jsonb_build_object(
            'users',    COALESCE(to_jsonb(p_users),   '[]'::jsonb),
            'sources',  COALESCE(to_jsonb(p_sources), '[]'::jsonb),
            'start_ts', to_jsonb(p_start_ts),
            'end_ts',   to_jsonb(p_end_ts)
        ),
        'generated_at', v_now
    ) INTO v_result;

    RETURN v_result;
END;
$func$;

GRANT EXECUTE ON FUNCTION summary_v1(TEXT[], TEXT[], DOUBLE PRECISION, DOUBLE PRECISION) TO anon;
