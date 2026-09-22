@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Time Manager

if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment found.
    echo Run setup.bat first - it installs everything - then start run.bat again.
    echo.
    pause
    exit /b 1
)

REM Default: the desktop app (tray + dashboard). Any arguments are passed
REM through, so "run.bat --monitor" starts the head-less agent instead.
set "ARGS=%*"
if "%ARGS%"=="" set "ARGS=--gui"

".venv\Scripts\python.exe" -m app.main %ARGS%
set "RC=%errorlevel%"

if "%RC%"=="3" echo Time Manager is already running - use the existing tray icon.
if "%RC%"=="4" echo PySide6 is missing - run setup.bat again.
if "%RC%"=="1" echo Time Manager exited with an error - see the messages above.
if not "%RC%"=="0" if not "%RC%"=="3" pause
exit /b %RC%
