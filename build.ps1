# Build the Claude Usage Tracker as a single-file Windows exe.
#
# Usage (from repo root):
#     .\build.ps1
#
# Output: dist\ClaudeTracker.exe
#
# Prereqs:
#     python -m venv .venv
#     .venv\Scripts\Activate.ps1
#     pip install -r tracker\requirements.txt pyinstaller

$ErrorActionPreference = "Stop"

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error "pyinstaller not found. Run: pip install pyinstaller"
}

# --windowed hides the console window (tray-only app).
# --name sets the exe filename.
# --noconfirm overwrites any existing dist/build dirs.
# --collect-submodules pystray/uvicorn picks up backend modules that would
#   otherwise be missed.
# --collect-submodules tracker bundles every tracker/*.py so the package
#   imports resolve at runtime (the loose script entry misses some of them).
# Entry point is run_tracker.py (repo root) not tracker\main.py — PyInstaller
# treats its entry script as the top-level module, so pointing at main.py
# directly breaks `from . import ...` inside the package.
pyinstaller `
    --noconfirm `
    --onefile `
    --name ClaudeTracker `
    --collect-submodules pystray `
    --collect-submodules uvicorn `
    --collect-submodules tracker `
    --collect-submodules backend `
    --add-data "backend/dashboard;backend/dashboard" `
    run_tracker.py

Write-Host ""
Write-Host "Built: dist\ClaudeTracker.exe" -ForegroundColor Green
