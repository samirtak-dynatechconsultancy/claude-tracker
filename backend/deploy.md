# Backend — deploy

Minimal FastAPI + SQLite app. Zero cloud dependencies.

## Run locally (dev)

```powershell
cd "C:\Workspace\Claude Analyzer"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt

$env:CLAUDE_TRACKER_API_KEY = "replace-me-with-a-long-random-string"
$env:CLAUDE_TRACKER_ADMIN_USER = "admin"
$env:CLAUDE_TRACKER_ADMIN_PASS = "replace-me"

python -m backend.main
```

- POST ingestion: `http://localhost:8080/events` (requires `X-API-Key`).
- Dashboard: `http://localhost:8080/` (HTTP Basic auth with the admin creds above).

## Point the tracker at the backend

Edit `%APPDATA%\ClaudeTracker\config.json` on each user's machine:

```json
{
  "shared_secret": "...",
  "backend_url": "http://<your-host>:8080",
  "backend_api_key": "replace-me-with-a-long-random-string"
}
```

Or set env vars on the machine running the tracker:

```powershell
setx CLAUDE_TRACKER_BACKEND_URL "http://<your-host>:8080"
setx CLAUDE_TRACKER_BACKEND_API_KEY "replace-me-with-a-long-random-string"
```

Restart the tracker. On each 60-second upload tick it will drain the SQLite queue in `%APPDATA%\ClaudeTracker\upload_queue.db` to the backend.

## Deploy to a VPS

Any Linux box with Python 3.11+ works. Example with `systemd`:

```ini
# /etc/systemd/system/claude-tracker-backend.service
[Unit]
Description=Claude Usage Tracker backend
After=network.target

[Service]
Environment="CLAUDE_TRACKER_API_KEY=<long-random>"
Environment="CLAUDE_TRACKER_ADMIN_USER=admin"
Environment="CLAUDE_TRACKER_ADMIN_PASS=<strong-password>"
Environment="CLAUDE_TRACKER_BACKEND_DB=/var/lib/claude-tracker/events.sqlite"
WorkingDirectory=/opt/claude-tracker
ExecStart=/opt/claude-tracker/.venv/bin/python -m backend.main
Restart=on-failure
User=claude-tracker

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-tracker-backend
```

Put it behind a reverse proxy (nginx/caddy) with TLS — the tracker's uploader will work over HTTPS without any changes.

## Data

- SQLite file: `./data/events.sqlite` by default, or `$CLAUDE_TRACKER_BACKEND_DB`.
- Schema: `backend/db.py` (`events` table, additive).
- Backups: plain `sqlite3 events.sqlite ".backup events-$(date +%F).sqlite"` works.

## Security notes

- The `/events` ingest endpoint is API-key-protected — rotate the key by changing the env var and updating each tracker's config.
- The dashboard is behind HTTP Basic. That's fine over HTTPS; do not expose it on a plain-HTTP public endpoint.
- The backend stores token counts and conversation IDs only. No message content.
