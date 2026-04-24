"""Entry point.

Layout:
    [main thread]   pystray icon loop
    [thread]        uvicorn (localhost API)
    [thread]        claude_code_parser poller (every 60s)
    [thread]        desktop_detector poller (every 30s)
    [thread]        backend_client uploader (every 60s, if configured)
    [thread]        embedded FastAPI backend (only if config.run_backend)
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


def _run_embedded_backend(config: Config) -> None:
    """Boot the FastAPI backend in a background thread.

    The tracker's upload key and the backend's expected key MUST match or
    every POST /events gets rejected with 401 — so we copy the tracker's
    `backend_api_key` into `CLAUDE_TRACKER_API_KEY` unconditionally (env
    var wins over whatever the process inherited). Other env vars from
    config.backend_env are merged in with `setdefault` so pre-existing
    shell env still takes priority.
    """
    os.environ["CLAUDE_TRACKER_API_KEY"] = config.backend_api_key
    for k, v in (config.backend_env or {}).items():
        os.environ.setdefault(k, str(v))

    # Import lazily so the backend package only loads when actually used —
    # keeps tracker-only startups fast and PyInstaller bundles lean if
    # this code path is stripped out later.
    import asyncio
    import uvicorn
    from backend.main import app as backend_app

    uv_config = uvicorn.Config(
        backend_app,
        host=config.backend_host,
        port=config.backend_port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)
    # uvicorn installs SIGINT/SIGTERM handlers that only work on the main
    # thread; skip them so running inside a non-main thread doesn't blow up.
    server.install_signal_handlers = lambda: None
    asyncio.run(server.serve())


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every outbound request at INFO — the supabase client fires
    # one per insert, which floods the console during queue drains. We only
    # care about failures from here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # uvicorn's access log echoes every /events POST. Kept at WARNING so
    # 4xx/5xx still surface but 200s don't.
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
            # Tracker's up — just hand off to Claude Desktop and exit.
            target = windows_setup.launch_claude_and_continue()
            if target:
                print(f"ClaudeTracker already running; launched Claude ({target}).")
            else:
                print("ClaudeTracker already running; Claude.exe not found.")
        else:
            print("ClaudeTracker is already running — exiting.")
        return 0

    # In --launch-claude mode, spawn Claude alongside booting the tracker
    # so the user doesn't stare at nothing while uvicorn starts up.
    if launch_claude_requested:
        target = windows_setup.launch_claude_and_continue()
        if target:
            print(f"Launching Claude Desktop ({target}) alongside tracker.")
        else:
            print("Claude Desktop not found — starting tracker only.")

    # Self-install on every run. Idempotent; only writes if anything changed,
    # and silently no-ops when running from source.
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
    if config.backend_url:
        print(f"Backend: {config.backend_url} (queue depth={queue.depth()})")
    else:
        print("Backend: disabled (set backend_url in config.json to enable)")
    if config.run_backend:
        print(
            f"Embedded backend: http://{config.backend_host}:{config.backend_port}"
        )

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
    if config.run_backend:
        threads.append(
            threading.Thread(
                target=_run_embedded_backend,
                args=(config,),
                daemon=True,
                name="backend",
            )
        )
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
