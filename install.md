# Install — Claude Usage Tracker

Three components:

1. **Desktop tracker** — runs on each user's machine; parses Claude Code logs, watches Claude Desktop, exposes a localhost API.
2. **Browser extension** — on each user's Chrome/Firefox; blocks claude.ai access unless the tracker is up, and observes per-conversation tokens.
3. **Central backend** — one instance (self-hosted FastAPI+SQLite); aggregates everyone's events and serves a dashboard. See [backend/deploy.md](backend/deploy.md).

This file covers #1 and #2. Do #3 first if you want numbers to flow into the dashboard.

---

## 1. Desktop tracker (Windows)

### Dev run

```powershell
cd "C:\Workspace\Claude Analyzer"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r tracker\requirements.txt
python -m tracker.main
```

You should see:

- A green circle icon in the system tray.
- Console output with the API URL and the first 8 chars of the shared secret.
- `http://127.0.0.1:47821/ping` returns `{"status":"ok","user":"<you>",...}` in a browser.

### Build a single-file exe

```powershell
pip install pyinstaller
.\build.ps1
```

Output: `dist\ClaudeTracker.exe`.

### Point the tracker at the central backend (optional)

Edit `%APPDATA%\ClaudeTracker\config.json`:

```json
{
  "shared_secret": "...",
  "backend_url": "https://tracker.example.com",
  "backend_api_key": "<the key you set on the backend>"
}
```

Or use env vars (good for silent enterprise rollouts):

```powershell
setx CLAUDE_TRACKER_BACKEND_URL "https://tracker.example.com"
setx CLAUDE_TRACKER_BACKEND_API_KEY "<key>"
```

Restart the tracker. Events accumulate in `%APPDATA%\ClaudeTracker\upload_queue.db` and drain on a 60-second timer with exponential backoff on failure. Offline periods are safe.

### Autostart on login

Drop a shortcut into the Startup folder:

1. `Win+R`, type `shell:startup`, Enter.
2. New shortcut → target: `C:\path\to\dist\ClaudeTracker.exe`.
3. Sign out / sign in to confirm it launches.

For more control (delay, retry, hidden window), use Task Scheduler with an "At log on" trigger.

### Verify

```powershell
# Liveness
curl.exe http://127.0.0.1:47821/ping

# Secret (only in the first 60s after launch)
curl.exe http://127.0.0.1:47821/handshake

# Stats (need the secret)
$s = (Get-Content $env:APPDATA\ClaudeTracker\config.json | ConvertFrom-Json).shared_secret
curl.exe -H "X-Tracker-Secret: $s" http://127.0.0.1:47821/stats
```

### Config + data

- `%APPDATA%\ClaudeTracker\config.json` — shared secret, pause state, backend URL/key.
- `%APPDATA%\ClaudeTracker\parser_state.json` — per-file byte offsets + reported message IDs for dedup.
- `%APPDATA%\ClaudeTracker\upload_queue.db` — SQLite queue of events awaiting upload.

Delete any of these to reset that piece. Deleting `parser_state.json` causes the next scan to re-ingest all historical `.jsonl` messages.

---

## 2. Browser extension

See [extension/README.md](extension/README.md) for step-by-step install in Chrome/Edge/Firefox. Short version:

1. Ensure the tracker is running.
2. `chrome://extensions` → Developer mode → Load unpacked → pick the `extension/` folder.
3. Pin the extension. First visit to claude.ai will trigger a handshake.

---

## Uninstall

1. Remove the browser extension from `chrome://extensions`.
2. Quit the tracker via the tray ("Quit").
3. Remove its startup shortcut.
4. Delete `%APPDATA%\ClaudeTracker\`.
5. Delete build output: `dist\`, `build\`, `ClaudeTracker.spec`.
