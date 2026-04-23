"""PyInstaller entry-point wrapper.

Run the tracker as a package so the relative imports inside tracker/
resolve. PyInstaller treats its entry script as the top-level module,
so pointing it straight at tracker/main.py breaks `from . import ...`.
This wrapper lives at the repo root and imports the package, which
gives PyInstaller the right sys.path layout.

Also writes any uncaught startup exception to a crash log so that
--windowed builds (no visible console) still leave a trail when they
die before the tray icon appears.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _crash_log_path() -> Path:
    # Prefer %APPDATA%\ClaudeTracker\ on Windows (same folder as config.json)
    # so the log lives alongside other tracker state.
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "ClaudeTracker" / "tracker-crash.log"
    return Path.home() / "ClaudeTracker-crash.log"


def _write_crash(exc: BaseException) -> None:
    try:
        path = _crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
    except Exception:
        # Never let crash-logging itself become the crash.
        pass


if __name__ == "__main__":
    try:
        from tracker.main import main
        sys.exit(main())
    except BaseException as exc:
        _write_crash(exc)
        raise
