## Claude Usage Tracker

Per-user token/message tracking across claude.ai, Claude Desktop, and Claude Code.

### Downloads

- **`ClaudeTracker.exe`** — Windows desktop tracker (single-file, no install).
- **`ClaudeTracker-extension.zip`** — Chrome/Edge extension (unpack + load unpacked).
- **`README.md`** — install guide.

### Install

See [README.md](./README.md). Short version:

1. Run `ClaudeTracker.exe`.
2. Unzip the extension, load it at `chrome://extensions/` with Developer mode on.
3. Click the extension icon → Connect.

### Notes

- The tracker stores everything locally until a central backend is configured.
- Open `http://127.0.0.1:47821/stats` to inspect counters.
- Edit `%APPDATA%\ClaudeTracker\config.json` to point at a central backend.
