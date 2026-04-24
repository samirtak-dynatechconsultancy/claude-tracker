"""Entry point.

Layout:
    [main thread]   pystray icon loop
    [thread]        uvicorn (localhost API — serves the extension's /ping)
    [thread]        claude_code_parser poller (every 60s)
    [thread]        desktop_detector poller (every 30s)
    [thread]        backend_client uploader (every 60s, posts direct to Supabase)
"""

from __future__ import annotations

import logging
import os
import sys
import threading

from . import backend_client, claude_code_parser, desktop_detector, windows_setup
from .api_server import run_server
from .config import API_HOST, API_PORT, Config, app_data_dir, os_username
from .events import EventStore
from .tray import build_tray
from .upload_queue import UploadQueue


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every outbound request at INFO — one per Supabase insert.
    # Only failures matter here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # uvicorn's access log echoes every /ping from the extension.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()

    # Handle CLI flags: --uninstall removes registry entries and exits;
    # `claudetracker://start` (and variants) is forwarded by the Windows
    # protocol handler as argv[1] — ignore it and boot normally so clicking
    # the blocked-page button just launches the tracker.
    argv = sys.argv[1:]
    if any(a in ("--uninstall", "-u") for a in argv):
        windows_setup.unwrap_claude_shortcuts()
        windows_setup.uninstall()
        print("ClaudeTracker uninstalled (registry + shortcuts restored).")
        return 0

    launch_claude_requested = any(a == "--launch-claude" for a in argv)

    # Single-instance guard: if the API port is already bound, another
    # instance is live. The protocol handler launches a fresh process on
    # every click, so without this we'd pile up duplicates.
    if windows_setup.already_running_on(API_HOST, API_PORT):
        if launch_claude_requested:
            target = windows_setup.launch_claude_and_continue()
            if target:
                print(f"ClaudeTracker already running; launched Claude ({target}).")
            else:
                print("ClaudeTracker already running; Claude.exe not found.")
        else:
            print("ClaudeTracker is already running — exiting.")
        return 0

    if launch_claude_requested:
        target = windows_setup.launch_claude_and_continue()
        if target:
            print(f"Launching Claude Desktop ({target}) alongside tracker.")
        else:
            print("Claude Desktop not found — starting tracker only.")

    # Self-install on every run. Idempotent; no-op when running from source.
    try:
        windows_setup.install()
    except Exception as exc:  # noqa: BLE001 — never let registry issues crash startup
        logging.getLogger(__name__).warning("self-install failed: %s", exc)

    config = Config.load_or_create()
    store = EventStore()
    queue = UploadQueue(app_data_dir() / "upload_queue.db")
    store.subscribe(queue.append)

    stop_event = threading.Event()

    print(f"Claude Usage Tracker starting for user={os_username()}")
    print(f"API: http://{API_HOST}:{API_PORT}")
    print(f"Shared secret (first 8): {config.shared_secret[:8]}...")
    print(f"Supabase: {config.supabase_url} (queue depth={queue.depth()})")

    threads = [
        threading.Thread(
            target=run_server, args=(config, store), daemon=True, name="api"
        ),
        threading.Thread(
            target=claude_code_parser.run_poller,
            args=(store, stop_event),
            daemon=True,
            name="code-parser",
        ),
        threading.Thread(
            target=desktop_detector.run_poller,
            args=(store, stop_event),
            daemon=True,
            name="desktop-detector",
        ),
        threading.Thread(
            target=backend_client.run_uploader,
            args=(queue, config, stop_event),
            daemon=True,
            name="uploader",
        ),
    ]
    for t in threads:
        t.start()

    def on_quit() -> None:
        stop_event.set()
        os._exit(0)

    icon = build_tray(config, on_quit=on_quit)
    icon.run()  # blocks on main thread
    return 0


if __name__ == "__main__":
    sys.exit(main())
