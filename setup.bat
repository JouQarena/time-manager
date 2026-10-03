@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Time Manager - setup

echo.
echo  ==========================================
echo   Time Manager - one-file setup for Windows
echo  ==========================================
echo.

REM --------------------------------------------------------- optional flags
set "FRESH="
set "BUILD="
set "NOTEST="
for %%a in (%*) do (
    if /i "%%~a"=="--fresh"   set "FRESH=1"
    if /i "%%~a"=="--build"   set "BUILD=1"
    if /i "%%~a"=="--no-test" set "NOTEST=1"
)

REM ----------------------------------------------------- 1. find a Python 3.12+
set "PY="
py -3.12 -c "print()" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 -c "print()" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "print()" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [X] No Python found. Install Python 3.12 or newer from
    echo     https://www.python.org/downloads/
    echo     Tick "Add python.exe to PATH" during installation,
    echo     then run this file again.
    echo.
    pause
    exit /b 1
)
echo [1/4] Using Python: %PY%
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [X] Python 3.12 or newer is required, an older version was found.
    echo     Install it from https://www.python.org/downloads/ and re-run setup.
    echo.
    pause
    exit /b 1
)

REM ----------------------------------------------------- 2. the virtual env
if defined FRESH (
    if exist .venv (
        echo [2/4] Removing the old .venv because --fresh was given ...
        rmdir /s /q .venv
    )
)
if exist ".venv\Scripts\python.exe" (
    echo [2/4] Reusing the existing .venv -- delete it or pass --fresh to rebuild.
) else (
    echo [2/4] Creating the virtual environment .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [X] Could not create the virtual environment.
        echo.
        pause
        exit /b 1
    )
)
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [X] .venv\Scripts\python.exe not found - venv creation failed.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------- 3. install deps
echo [3/4] Installing dependencies from requirements.txt ...
"%VPY%" -m pip install --upgrade pip --quiet 2>nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Installing the dependencies failed - check your internet
    echo     connection and the error above, then run setup.bat again.
    echo.
    pause
    exit /b 1
)
if defined BUILD (
    echo       Installing build tools from requirements-build.txt ...
    "%VPY%" -m pip install -r requirements-build.txt
    if errorlevel 1 (
        echo [X] Installing the build tools failed - see the error above.
        echo.
        pause
        exit /b 1
    )
)

REM ---------------------------------------------------- 4. verify it works
if defined NOTEST goto verify
echo [4/4] Running the test suite - the whole suite, about half a minute ...
"%VPY%" -m pytest -q
if errorlevel 1 (
    echo [X] Some tests failed - the install is not healthy. Copy the output
    echo     above if you ask for help.
    echo.
    pause
    exit /b 1
)
:verify
"%VPY%" -c "import PySide6, psutil, websockets; import app.main" >nul 2>&1
if errorlevel 1 (
    echo [X] The imports failed after installation - run setup.bat with
    echo     --fresh to rebuild the environment from scratch.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------- done
echo.
echo  ==========================================
echo   Setup complete - everything is installed.
echo  ==========================================
echo.
echo   Start the app now:        double-click run.bat
echo   Start from a terminal:    .venv\Scripts\python -m app.main --gui
echo   Head-less agent:          run.bat --monitor
echo   CLI status:               .venv\Scripts\python -m app.main --status
echo.
echo   Build the portable exe:   scripts\build_windows.ps1
echo                             ^(see docs\PACKAGING.md^)
echo   Re-run setup anytime;     pass --fresh to rebuild, --build to also
echo                             install the exe build tools.
echo.
pause
exit /b 0
