# Claude Usage Tracker — Browser Extension

Manifest V3 extension for Chrome (and Firefox ≥115). Works together with the desktop tracker:

- **Before each claude.ai navigation**, pings `http://127.0.0.1:47821/ping`. If the desktop app is down or paused, redirects to a block page.
- **Inside claude.ai**, hooks `fetch` to observe streaming completion responses and extract `input_tokens` / `output_tokens` per assistant message.
- **Forwards** events to the desktop app's `POST /log` with the shared secret; the desktop app handles queueing and upload to the central backend.

## Install (unpacked)

### Chrome / Edge

1. Make sure the desktop tracker is running (see repo-root `install.md`).
2. Open `chrome://extensions` (or `edge://extensions`).
3. Toggle **Developer mode** on (top right).
4. Click **Load unpacked** and select this `extension/` folder.
5. Pin the extension from the puzzle-piece menu so you can see the popup.
6. The first time it loads, the extension attempts a handshake to retrieve the shared secret. If that window (60s after tracker launch) has already closed, click the popup → **Re-handshake**, restart the tracker, then retry.

### Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on** and select `extension/manifest.json`.
3. (Temporary add-ons are removed on restart. For persistent use, package + sign or use the Developer Edition with `xpinstall.signatures.required = false`.)

## What it tracks

- `conversation_id` — the claude.ai conversation UUID from the request URL.
- `message_id` — Anthropic API message ID from the SSE `message_start` event.
- `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens` — summed into `tokens_in`.
- `output_tokens` — cumulative count from the final `message_delta` event.
- `model` — from `message_start.message.model`.
- `timestamp` — wall-clock time when the response stream completed.

It does **not** read or forward message contents.

## Troubleshooting

- **Popup says "not running":** start the tracker. If it's already running, check `http://127.0.0.1:47821/ping` from a terminal — it should return `{"status":"ok",...}`.
- **Popup says "paused":** right-click the tracker's tray icon → **Resume Tracking**.
- **"No shared secret yet":** the handshake window is 60s after tracker launch. Quit the tracker, relaunch it, then click the popup's **Re-handshake** button.
- **No tokens being logged:** claude.ai may have changed its streaming endpoint shape. Open DevTools → Network, reproduce a message, and verify the URL still matches `/api/organizations/*/chat_conversations/*/completion`. Update the regex in `page-hook.js` if needed.

## File layout

```
extension/
  manifest.json        MV3 manifest
  background.js        service worker — pre-nav check, handshake, message forwarder
  content.js           isolated-world glue that injects page-hook.js
  page-hook.js         page-world fetch() wrapper, SSE parser
  popup.html / .js     toolbar popup — status + today/week totals
  blocked.html / .js   shown when the tracker isn't running
```
