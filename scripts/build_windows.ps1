# Time Manager — Windows build script (Phase 9).
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#
# Produces:
#   build\...             PyInstaller work files (safe to delete)
#   dist\TimeManager\     the portable app (TimeManager.exe + TimeManagerTray.exe)
#   dist\TimeManager-v<version>-windows-portable.zip
#
# Requirements: Python 3.12+ (`py` launcher) on Windows 10/11. No admin rights.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "== Time Manager build ==" -ForegroundColor Cyan

# 1. Fresh build venv ---------------------------------------------------------
if (Test-Path ".build-venv") { Remove-Item -Recurse -Force ".build-venv" }
py -3.12 -m venv .build-venv
if ($LASTEXITCODE -ne 0) { py -3 -m venv .build-venv }
.\.build-venv\Scripts\python -m pip install --upgrade pip
.\.build-venv\Scripts\python -m pip install -r requirements.txt -r requirements-build.txt

# 2. Test-suite gate: never ship a red build ----------------------------------
Write-Host "== Running the test suite ==" -ForegroundColor Cyan
.\.build-venv\Scripts\python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed - aborting the build." }

# 3. Bundle --------------------------------------------------------------------
Write-Host "== PyInstaller ==" -ForegroundColor Cyan
& ".\.build-venv\Scripts\python.exe" -m PyInstaller time-manager.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

# 4. Frozen smoke checks -------------------------------------------------------
Write-Host "== Smoke checks on the frozen build ==" -ForegroundColor Cyan
$exe = ".\dist\TimeManager\TimeManager.exe"
& $exe --version
& $exe --show-extension-path
& $exe --init-db --db "$env:TEMP\tm-smoke.db"
& $exe --status --db "$env:TEMP\tm-smoke.db"
Remove-Item "$env:TEMP\tm-smoke.db*" -ErrorAction SilentlyContinue

# 5. Portable zip ----------------------------------------------------------------
$version = & .\.build-venv\Scripts\python -c "from app import __version__; print(__version__)"
New-Item -ItemType Directory -Force -Path "build" | Out-Null
Set-Content -Path "build\version.ini" -Value "[app]`nversion=$version"   # read by installer\time-manager.iss
$zip = "dist\TimeManager-v$version-windows-portable.zip"
Compress-Archive -Path "dist\TimeManager\*" -DestinationPath $zip -Force

# 5b. A no-console launcher next to the exes: starts the (always working)
# console exe with its window hidden. Lifesaver when some antivirus blocks
# the windowed TimeManagerTray.exe but leaves the console one alone.
Copy-Item "scripts\Start Time Manager.vbs" "dist\TimeManager\" -Force

# 6. Remove PyInstaller's scratch folder -------------------------------
# Its half-wired exes ("Failed to load Python DLL" when run) have twice
# been mistaken for the finished app on a real machine. The next build
# recreates it from scratch anyway.
Remove-Item -Recurse -Force "build\time-manager" -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Build OK:" -ForegroundColor Green
Write-Host "  dist\TimeManager\TimeManagerTray.exe    <- run THIS one"
Write-Host "  $zip"
Write-Host '  (the build\ folder is PyInstaller scratch - never run exes from there)'
Write-Host ""
Write-Host "Next steps for users: docs\PACKAGING.md (install / update / extension)."
