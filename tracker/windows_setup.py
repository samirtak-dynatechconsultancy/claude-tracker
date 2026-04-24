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
WATCHDOG_TASK_NAME = "ClaudeTrackerWatchdog"
# API port the watchdog probes to decide whether the tracker is live —
# must match API_PORT in config.py. Kept local here to avoid a circular
# import (config imports nothing from windows_setup).
_WATCHDOG_PROBE_PORT = 47821

# Places Claude Desktop's installer drops shortcuts. We retarget any we
# find so that launching Claude goes through the tracker first.
_SHORTCUT_NAME = "Claude.lnk"
_SHORTCUT_BACKUP_SUFFIX = ".claudetracker.bak"

# Where the Anthropic installer puts Claude.exe — checked in order.
_CLAUDE_EXE_CANDIDATES = (
    r"%LOCALAPPDATA%\AnthropicClaude\Claude.exe",
    r"%LOCALAPPDATA%\Programs\claude-desktop\Claude.exe",
    r"%PROGRAMFILES%\Anthropic\Claude\Claude.exe",
)


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

    # 3. Wrap Claude Desktop shortcuts so clicking them goes through the
    #    tracker. Best-effort — failures here shouldn't block startup.
    try:
        if wrap_claude_shortcuts(exe):
            changed = True
    except Exception as wrap_exc:  # noqa: BLE001
        log.warning("shortcut wrap skipped: %s", wrap_exc)

    # 4. Watchdog scheduled task — self-heal if someone kills the tracker
    #    while Claude.exe is still running. Autostart covers logins; this
    #    covers the "user quit tracker, then opened Claude" edge case.
    try:
        if ensure_watchdog_task(exe):
            changed = True
    except Exception as wd_exc:  # noqa: BLE001
        log.warning("watchdog task install skipped: %s", wd_exc)

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

    # Remove the watchdog scheduled task if present.
    try:
        remove_watchdog_task()
    except Exception as exc:  # noqa: BLE001
        log.warning("watchdog uninstall skipped: %s", exc)

    log.info("Windows self-install removed")


def _read_default(key) -> str | None:
    try:
        import winreg

        val, _ = winreg.QueryValueEx(key, None)
        return val
    except (FileNotFoundError, OSError):
        return None


def find_claude_exe() -> str | None:
    """Locate the installed Claude Desktop executable, or None."""
    for candidate in _CLAUDE_EXE_CANDIDATES:
        expanded = os.path.expandvars(candidate)
        if os.path.isfile(expanded):
            return expanded
    return None


def _shortcut_locations() -> list[Path]:
    """Places we scan for Claude Desktop's Start Menu / Desktop shortcuts."""
    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots += [
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Anthropic",
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Claude",
        ]
    programdata = os.environ.get("PROGRAMDATA")
    if programdata:
        roots += [
            Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Anthropic",
        ]
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        roots.append(Path(userprofile) / "Desktop")
        roots.append(Path(userprofile) / "OneDrive" / "Desktop")
    return roots


def _run_powershell(script: str) -> tuple[int, str]:
    """Run a PowerShell snippet, return (rc, stderr)."""
    import subprocess

    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.returncode, (proc.stderr or "").strip()


def _read_shortcut(lnk_path: Path) -> dict | None:
    """Return {TargetPath, Arguments, WorkingDirectory} for a .lnk, or None."""
    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut('{lnk_path}'); "
        "Write-Output $l.TargetPath; "
        "Write-Output $l.Arguments; "
        "Write-Output $l.WorkingDirectory"
    )
    import subprocess

    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").splitlines()
    # Pad to 3 lines so empty Arguments / WorkingDirectory don't blow up.
    while len(lines) < 3:
        lines.append("")
    return {
        "TargetPath": lines[0].strip(),
        "Arguments": lines[1].strip(),
        "WorkingDirectory": lines[2].strip(),
    }


def _write_shortcut(lnk_path: Path, target: str, args: str, workdir: str) -> bool:
    """Overwrite a .lnk with the given target + arguments."""
    # Quote-escape the target/args for embedding in a PowerShell single-quoted string.
    def q(s: str) -> str:
        return s.replace("'", "''")

    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut('{q(str(lnk_path))}'); "
        f"$l.TargetPath = '{q(target)}'; "
        f"$l.Arguments = '{q(args)}'; "
        f"$l.WorkingDirectory = '{q(workdir)}'; "
        "$l.Save()"
    )
    rc, err = _run_powershell(ps)
    if rc != 0:
        log.warning("failed to write shortcut %s: %s", lnk_path, err)
        return False
    return True


def wrap_claude_shortcuts(tracker_exe: str) -> bool:
    """Retarget any Claude Desktop .lnk to run the tracker first.

    Strategy: find every ``Claude.lnk`` in Start Menu / Desktop folders.
    If its target is Claude.exe (not already our wrapper), back up the
    original TargetPath in a sibling ``.claudetracker.bak`` JSON file and
    rewrite the .lnk to point at ``ClaudeTracker.exe --launch-claude``.

    Returns True if anything was rewritten.
    """
    import json as _json

    changed = False
    tracker_exe_norm = os.path.normcase(os.path.abspath(tracker_exe))

    for root in _shortcut_locations():
        if not root.exists():
            continue
        for lnk in root.rglob(_SHORTCUT_NAME):
            info = _read_shortcut(lnk)
            if not info:
                continue
            target = info["TargetPath"]
            # Skip if already pointing at our tracker.
            if os.path.normcase(os.path.abspath(target or "")) == tracker_exe_norm:
                continue
            # Only retarget if the original looks like Claude Desktop.
            if "claude.exe" not in target.lower():
                continue

            # Save the original target/args so we can restore later and so
            # --launch-claude knows where to forward to even if we miss
            # Claude in the default install paths.
            backup = lnk.with_suffix(lnk.suffix + _SHORTCUT_BACKUP_SUFFIX)
            if not backup.exists():
                try:
                    backup.write_text(_json.dumps(info), encoding="utf-8")
                except OSError:
                    pass

            workdir = info["WorkingDirectory"] or os.path.dirname(target)
            if _write_shortcut(lnk, tracker_exe, "--launch-claude", workdir):
                log.info("wrapped Claude shortcut: %s", lnk)
                changed = True

    return changed


def unwrap_claude_shortcuts() -> None:
    """Restore any shortcuts we modified back to their original targets."""
    import json as _json

    for root in _shortcut_locations():
        if not root.exists():
            continue
        for backup in root.rglob(_SHORTCUT_NAME + _SHORTCUT_BACKUP_SUFFIX):
            try:
                info = _json.loads(backup.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            lnk = backup.with_name(backup.name[: -len(_SHORTCUT_BACKUP_SUFFIX)])
            _write_shortcut(
                lnk,
                info.get("TargetPath", ""),
                info.get("Arguments", ""),
                info.get("WorkingDirectory", ""),
            )
            try:
                backup.unlink()
            except OSError:
                pass


def launch_claude_and_continue() -> str | None:
    """Spawn Claude Desktop in the background. Return the path launched, or None."""
    import subprocess

    # Prefer the original target recorded in a backup file (covers custom
    # install paths where find_claude_exe() would miss it). Fall back to
    # the known default locations.
    for root in _shortcut_locations():
        if not root.exists():
            continue
        for backup in root.rglob(_SHORTCUT_NAME + _SHORTCUT_BACKUP_SUFFIX):
            try:
                import json as _json

                info = _json.loads(backup.read_text(encoding="utf-8"))
                target = info.get("TargetPath", "")
                if target and os.path.isfile(target):
                    subprocess.Popen(
                        [target] + (info.get("Arguments", "") or "").split(),
                        cwd=info.get("WorkingDirectory") or None,
                        close_fds=True,
                    )
                    return target
            except Exception:  # noqa: BLE001
                continue

    target = find_claude_exe()
    if target:
        subprocess.Popen([target], close_fds=True)
        return target
    return None


def _watchdog_script_path() -> Path:
    """Where we drop the watchdog helper script on disk."""
    base = os.environ.get("APPDATA")
    root = Path(base) / "ClaudeTracker" if base else Path.home() / ".claude-tracker"
    root.mkdir(parents=True, exist_ok=True)
    return root / "watchdog.ps1"


def _watchdog_script_contents(tracker_exe: str, probe_port: int) -> str:
    """The PowerShell the scheduled task runs every minute.

    Starts the tracker iff:
      * Claude.exe is a live process, AND
      * nothing is already bound to the tracker's API port.
    The single-instance guard inside the tracker is a belt; this is the
    suspenders — we avoid even firing Start-Process when we don't need to.
    """
    # Double any backslashes so the path survives embedding in the
    # single-quoted PowerShell literal below.
    exe_literal = tracker_exe.replace("'", "''")
    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "if (Get-Process -Name 'Claude' -ErrorAction SilentlyContinue) {\n"
        "  $probe = New-Object System.Net.Sockets.TcpClient\n"
        "  try { $probe.Connect('127.0.0.1', " + str(probe_port) + ") } catch {}\n"
        "  $up = $probe.Connected\n"
        "  $probe.Close()\n"
        "  if (-not $up) {\n"
        f"    Start-Process -FilePath '{exe_literal}' -WindowStyle Hidden\n"
        "  }\n"
        "}\n"
    )


def ensure_watchdog_task(tracker_exe: str) -> bool:
    """Register (or refresh) the watchdog scheduled task.

    Uses ``schtasks.exe`` because it's always available, requires no
    PowerShell execution-policy waivers, and runs under the current user
    (no admin / UAC prompt). Task runs every minute indefinitely.

    Returns True if we created or rewrote anything.
    """
    import subprocess

    script_path = _watchdog_script_path()
    desired = _watchdog_script_contents(tracker_exe, _WATCHDOG_PROBE_PORT)

    # Only rewrite the helper script when its contents changed — avoids
    # pointless disk churn on every startup.
    script_changed = False
    if not script_path.exists() or script_path.read_text(encoding="utf-8") != desired:
        script_path.write_text(desired, encoding="utf-8")
        script_changed = True

    # Check whether the task already exists AND points at the right script.
    query = subprocess.run(
        ["schtasks", "/Query", "/TN", WATCHDOG_TASK_NAME, "/V", "/FO", "LIST"],
        capture_output=True,
        text=True,
    )
    task_exists = query.returncode == 0
    task_ok = task_exists and str(script_path) in (query.stdout or "")

    if task_ok and not script_changed:
        return False

    # Rewrite from scratch. /F forces overwrite if it already exists.
    # /SC MINUTE /MO 1 = repeat every 1 minute forever.
    # /RL LIMITED = run as the current user with standard privileges.
    tr = (
        "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden "
        "-ExecutionPolicy Bypass "
        f'-File "{script_path}"'
    )
    create = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            WATCHDOG_TASK_NAME,
            "/SC",
            "MINUTE",
            "/MO",
            "1",
            "/TR",
            tr,
            "/RL",
            "LIMITED",
            "/F",
        ],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        log.warning(
            "schtasks create failed (rc=%s): %s",
            create.returncode,
            (create.stderr or create.stdout or "").strip(),
        )
        return False
    log.info("watchdog scheduled task registered (%s)", WATCHDOG_TASK_NAME)
    return True


def remove_watchdog_task() -> None:
    """Delete the watchdog task + its helper script. Safe if missing."""
    import subprocess

    subprocess.run(
        ["schtasks", "/Delete", "/TN", WATCHDOG_TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )
    try:
        _watchdog_script_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not delete watchdog script: %s", exc)


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
