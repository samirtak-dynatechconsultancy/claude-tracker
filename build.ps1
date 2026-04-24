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

# --onefile            single-file exe.
# --name               output filename.
# --noconfirm          overwrite any existing dist/build dirs.
# --collect-submodules pystray / uvicorn — hidden imports those libs do at runtime.
# --collect-submodules tracker — bundle every tracker/*.py (the loose entry
#     script otherwise misses some). The `backend` package is deliberately NOT
#     bundled: the tracker now pushes straight to Supabase over HTTPS and the
#     dashboard lives as a standalone deploy.
# Entry point is run_tracker.py (repo root) so `from . import ...` inside the
# tracker package resolves correctly.
pyinstaller `
    --noconfirm `
    --onefile `
    --name ClaudeTracker `
    --collect-submodules pystray `
    --collect-submodules uvicorn `
    --collect-submodules tracker `
    run_tracker.py

Write-Host ""
Write-Host "Built: dist\ClaudeTracker.exe" -ForegroundColor Green
