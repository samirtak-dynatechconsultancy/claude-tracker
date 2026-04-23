# Claude Usage Tracker — Install Guide

Thanks for installing. This tool tracks per-user token usage across a
shared claude.ai account, Claude Desktop, and Claude Code so your team
can see who's using what.

## What you got

- `ClaudeTracker.exe` — the desktop tracker. Runs in the system tray.
- `ClaudeTracker-extension.zip` — the browser extension. Unpack and load
  as an unpacked extension in Chrome/Edge (Manifest V3).

## Install — desktop tracker

1. Move `ClaudeTracker.exe` somewhere permanent (e.g. `C:\Program Files\ClaudeTracker\`).
2. Double-click to launch. A small tray icon appears; there's no window.
3. First launch creates `%APPDATA%\ClaudeTracker\config.json` with a
   shared secret. The browser extension will read this during handshake.
4. *(Optional)* Add to Startup so it runs on login:
   - `Win+R` → `shell:startup` → paste a shortcut to `ClaudeTracker.exe`.

## Install — browser extension

1. Unzip `ClaudeTracker-extension.zip` somewhere permanent (don't delete
   the folder after loading — Chrome loads from disk).
2. Open `chrome://extensions/` (or `edge://extensions/`).
3. Toggle **Developer mode** on.
4. Click **Load unpacked** and select the unzipped folder.
5. Pin the extension. Click it — it should show "Connected". If it says
   "Handshake failed", make sure the tracker exe is running.

## Connecting to a central dashboard (optional)

If your team runs a central backend, paste the URL + API key into
`%APPDATA%\ClaudeTracker\config.json`:

```json
{
  "shared_secret": "...",
  "created_at": 1234567890,
  "paused": false,
  "backend_url": "https://your-backend.example.com",
  "backend_api_key": "the-api-key-your-admin-gave-you"
}
```

Then restart the tracker (right-click tray icon → Exit, then relaunch).

## Uninstall

1. Right-click the tray icon → **Exit**.
2. Delete `ClaudeTracker.exe`.
3. Delete `%APPDATA%\ClaudeTracker\` if you want to wipe the local queue.
4. Remove the extension from `chrome://extensions/`.

## Troubleshooting

- **Nothing is being recorded.** Visit `http://127.0.0.1:47821/stats` in
  the browser — you should see counters increment as you use Claude.
- **Handshake window closed.** The handshake is only valid for 60s after
  the tracker launches. Quit the tracker, relaunch, then click "Connect"
  in the extension within 60 seconds.
- **Backend shows disabled.** Double-check `backend_url` in `config.json`
  is a valid URL and *not* an empty string.
