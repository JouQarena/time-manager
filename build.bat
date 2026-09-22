@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Time Manager - build the exe

echo.
echo  This builds the real executables:
echo     dist\TimeManager\TimeManagerTray.exe   (the desktop app - run this)
echo     dist\TimeManager\TimeManager.exe       (the CLI)
echo     dist\TimeManager-v*-windows-portable.zip  (the shareable portable app)
echo.
echo  The full test suite runs first, so the build takes several minutes
echo  the first time. Run setup.bat once before this file.
echo.
REM --console-tray: diagnostic build - TimeManagerTray.exe gets a console
REM so a start-up failure can be seen by running it from a terminal.
set "TIME_MANAGER_TRAY_CONSOLE="
for %%a in (%*) do (
    if /i "%%~a"=="--console-tray" set "TIME_MANAGER_TRAY_CONSOLE=1"
)
if defined TIME_MANAGER_TRAY_CONSOLE (
    echo   DIAGNOSTIC MODE: TimeManagerTray.exe will have a console this
    echo   build - run it from a terminal to see the error the normal
    echo   windowed build swallows.
    echo.
    pause
)

pause

if not exist ".venv\Scripts\python.exe" (
    echo [X] No virtual environment found. Run setup.bat first, then build.bat.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\build_windows.ps1"
set "RC=%errorlevel%"

if "%RC%"=="0" (
    echo.
    echo  ============================================
    echo   Build finished - your exe files are ready.
    echo  ============================================
    echo.
    echo   Run the app from here ONLY:
    echo       dist\TimeManager\TimeManagerTray.exe
    echo.
    echo   Share / install:  dist\TimeManager-v*-windows-portable.zip
    echo                     ^(unzip anywhere, run TimeManagerTray.exe^)
    echo.
    echo   The PyInstaller scratch folder has been deleted - there is no
    echo   "build\" folder to confuse it with anymore. If you move the app
    echo   somewhere, move the WHOLE dist\TimeManager folder: the exe only
    echo   works with its _internal folder right next to it.
    echo.
    echo   First launch note: the exe is unsigned, so SmartScreen may ask
    echo   for confirmation - click "More info" then "Run anyway".
    echo.
    echo   Opening the folder with your exe now ...
    start "" explorer "dist\TimeManager"
) else (
    echo.
    echo [X] The build failed with code %RC% - read the messages above.
    echo     Most common causes: antivirus locked a file ^(run again^),
    echo     or the folder is on a network drive ^(build on a local disk^).
    echo     Saw "Failed to load Python DLL ... build\time-manager\..."?
    echo     That was the scratch-folder exe - use dist\TimeManager instead.
)
echo.
pause
exit /b %RC%
