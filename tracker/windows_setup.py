"""Windows self-install: URL protocol handler + autostart + uninstall.

Writes HKCU registry entries so that:

  * clicking ``claudetracker://start`` in a browser launches the exe, and
  * the exe runs automatically at every Windows login.

Everything lives under HKCU so no admin/UAC prompt is needed. Called
idempotently from ``main()`` on every startup — writes only if missing.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

PROTOCOL = "claudetracker"
RUN_VALUE_NAME = "ClaudeTracker"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _is_frozen_exe() -> bool:
    """True when running inside the PyInstaller-built exe."""
    return getattr(sys, "frozen", False)


def _exe_path() -> str | None:
    """Absolute path to the running exe, or None if running from source.

    We only self-install when launched from the built exe — installing
    registry entries that point at ``python.exe tracker/main.py`` would
    just break on the next rebuild.
    """
    if not _is_frozen_exe():
        return None
    return str(Path(sys.executable).resolve())


def install(quiet: bool = False) -> bool:
    """Register protocol handler + autostart. Idempotent.

    Returns True if anything was written, False if everything was already
    in place (or we're not running from an exe).
    """
    exe = _exe_path()
    if not exe:
        if not quiet:
            log.info("skipping Windows self-install (not running from exe)")
        return False

    try:
        import winreg  # stdlib on Windows only
    except ImportError:
        return False

    changed = False

    # 1. URL protocol handler: HKCU\Software\Classes\claudetracker\...
    #    Docs: https://learn.microsoft.com/en-us/previous-versions/windows/internet-explorer/ie-developer/platform-apis/aa767914(v=vs.85)
    proto_root = rf"Software\Classes\{PROTOCOL}"
    desired_command = f'"{exe}" "%1"'

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, proto_root) as k:
        existing = _read_default(k)
        if existing != f"URL:{PROTOCOL} Protocol":
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f"URL:{PROTOCOL} Protocol")
            changed = True
        # The "URL Protocol" named value (empty string) is the magic marker
        # that tells Windows this key is a pluggable protocol, not a file
        # association. Without it, browsers won't hand the URL to us.
        try:
            winreg.QueryValueEx(k, "URL Protocol")
        except FileNotFoundError:
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            changed = True

    cmd_path = rf"{proto_root}\shell\open\command"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_path) as k:
        if _read_default(k) != desired_command:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, desired_command)
            changed = True

    # 2. Autostart: HKCU\Software\Microsoft\Windows\CurrentVersion\Run
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
        try:
            existing, _ = winreg.QueryValueEx(k, RUN_VALUE_NAME)
        except FileNotFoundError:
            existing = None
        autostart_cmd = f'"{exe}"'
        if existing != autostart_cmd:
            winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ, autostart_cmd)
            changed = True

    if changed:
        log.info("Windows self-install wrote registry entries (exe=%s)", exe)
    return changed


def uninstall() -> None:
    """Remove everything install() added. Safe to call if not installed."""
    try:
        import winreg
    except ImportError:
        return

    # Delete protocol handler keys (leaf-first).
    for sub in (
        rf"Software\Classes\{PROTOCOL}\shell\open\command",
        rf"Software\Classes\{PROTOCOL}\shell\open",
        rf"Software\Classes\{PROTOCOL}\shell",
        rf"Software\Classes\{PROTOCOL}",
    ):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete %s: %s", sub, exc)

    # Delete autostart entry.
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            try:
                winreg.DeleteValue(k, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass

    log.info("Windows self-install removed")


def _read_default(key) -> str | None:
    try:
        import winreg

        val, _ = winreg.QueryValueEx(key, None)
        return val
    except (FileNotFoundError, OSError):
        return None


def already_running_on(host: str, port: int) -> bool:
    """Return True if *another* process is bound to host:port.

    Used as a cheap single-instance lock — if the tracker's API port is
    already taken, another tracker is up, so this copy should bail out.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()
