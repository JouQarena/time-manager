# Packaging & distribution (Phase 9)

Time Manager ships as a **portable Windows build** produced by PyInstaller,
with an optional installer script. Everything here was verified by the
cross-platform smoke build (`scripts/build_smoke.sh`) and is exercised on
Windows by `scripts/build_windows.ps1`, which refuses to package a red test
suite.

## 1. What a build produces

```
dist/TimeManager/
├── TimeManager.exe        console CLI: --monitor, --status, --security,
│                          --init-db, --simulate, --show-token, ...
├── TimeManagerTray.exe    windowed app: tray + dashboard (runs the monitor
│                          thread). No console flash — this is what autostart
│                          and the "Time Manager Watchdog" task execute.
├── _internal/
│   ├── browser-extension/ the unpacked extension, shipped inside the build
│   └── ...                shared Python/Qt libraries (PyInstaller layout)
```

Two executables, one bundle: `TimeManagerTray.exe` is built `console=False`
so a scheduled task can relaunch it every minute without flashing a terminal;
`TimeManager.exe` is the every-day CLI. Both carry the version resource
generated from `app.__version__` and the app icon (`assets/app.ico`, built
from the extension icon set).

## 2. How to build (Windows 10/11)

> **One-click:** double-click **`build.bat`** at the project root (after
> `setup.bat`). It runs the exact sequence below — tests, build, smoke
> checks, zip — and tells you where the exe landed. The rest of this section
> explains what it does.


Prerequisites: Python 3.12+ (`py` launcher). No admin rights.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

The script: creates a throwaway venv (`.build-venv`), installs
`requirements.txt` + `requirements-build.txt`, **runs the full test suite**
(aborts on failure), runs PyInstaller with `time-manager.spec`, smoke-checks
the frozen executables, and zips the result:

```
dist/TimeManager-v<version>-windows-portable.zip
```

Manual equivalent:

```powershell
py -3.12 -m venv .venv; .\.venv\Scripts\activate
pip install -r requirements.txt -r requirements-build.txt
pytest -q
pyinstaller time-manager.spec --noconfirm --clean
```

Cross-platform smoke build (Linux/macOS; same spec, platform-specific pieces
skipped automatically):

```bash
./scripts/build_smoke.sh
```

## 3. How to install

**Portable (recommended, zero privileges):** unzip
`TimeManager-v<version>-windows-portable.zip` anywhere you like —
`%LOCALAPPDATA%\TimeManager` is a good default — and run
`TimeManagerTray.exe`.

**Installer (optional):** after a build, compile the Inno Setup script
(`iscc installer\time-manager.iss`) to get `TimeManager-<version>-setup.exe`
(Start-menu shortcuts, optional desktop icon, optional autostart entry,
proper uninstaller). The installer only writes to Program Files and HKCU.

First run:
1. `TimeManager.exe --show-token` (or the dashboard's Settings) — copy the pairing token.
2. `TimeManager.exe --show-extension-path` — shows the shipped extension folder.
3. Chrome/Edge → `chrome://extensions` → Developer mode → **Load unpacked** → select that folder → paste the token in the extension's Options.
4. Add rules in the dashboard. Enable *Launch at startup* and (if you use STRICT rules) the watchdog in Settings.

## 4. How to uninstall

- Portable: stop the agent (tray → Exit), delete the folder, then
  - remove the autostart entry: Settings unchecks it, or
    `reg delete HKCU\...\CurrentVersion\Run /v TimeManager /f`
  - remove the watchdog if enabled: Settings toggle, or
    `schtasks /Delete /TN "Time Manager Watchdog" /F`
  - remove the unpacked extension in `chrome://extensions`.
- Installer: Settings → disable autostart/watchdog, then uninstall from
  Windows' "Installed apps". The uninstaller stops the agent first.

**Your data is never deleted by an uninstall.** The database, config, backups
and logs live in `%APPDATA%\TimeManager` (next section). Delete that folder
manually if you truly want a pristine machine — it is documented, not hidden.

## 5. How to update

1. Stop the agent (tray → Exit; kill `TimeManager*.exe` if needed).
2. Replace the folder's contents with the new `dist/TimeManager/` (keep the
   same path so the autostart entry and watchdog task stay valid — if the
   path *changed*, open Settings and re-save "Launch at startup" once, and
   re-enable the watchdog).
3. Start `TimeManagerTray.exe`. Your rules, usage history and settings live
   in `%APPDATA%\TimeManager` and survive every update untouched; the
   database schema migrates forward automatically. Downgrading is not
   supported (schema versions only move forward) — the periodic backups in
   `%APPDATA%\TimeManager\backups` cover you either way.

## 6. Where everything lives

| What | Where |
| --- | --- |
| Database | `%APPDATA%\TimeManager\timemanager.db` (WAL; backups in `backups\`) |
| Usage floor (STRICT tamper defence) | `timemanager.floor.json` next to the database |
| Config + pairing token | `%APPDATA%\TimeManager\config.json`, `agent_token` |
| Agent log (packaged builds) | `%APPDATA%\TimeManager\logs\agent.log` (3 × 1 MB rotating) |
| Watchdog task | Task Scheduler → "Time Manager Watchdog" (opt-in, Settings) |
| Autostart | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` → `TimeManager` |

## 7. Browser extension in a packaged install

The unpacked extension ships inside the bundle
(`_internal\browser-extension`). It must still be loaded unpacked
(chrome://extensions → Load unpacked → that folder) because store
distribution is a later milestone (Phase: post-1.0). `--show-extension-path`
prints the exact folder for the running install. Keep the folder in place;
do not run the agent from inside a zip.

## 8. Troubleshooting

| Symptom | Explanation / fix |
| --- | --- |
| SmartScreen warns on first run | The build is not code-signed (no certificate in this project yet). "More info" → "Run anyway". Signing is the documented next step; nothing in the app asks you to weaken Defender. |
| Some antivirus flags the PyInstaller exe | Generic PyInstaller-loader heuristic, common to all unsigned PyInstaller apps. Build it yourself from source (§2) — the tests gate the build. |
| Tray app starts but the dashboard is blank | Check `%APPDATA%\TimeManager\logs\agent.log`; a corrupt config falls back to defaults and says so in the log. |
| "Another Time Manager instance is already running" | The single-instance lock is doing its job. Tray → Exit, or kill the exe. |
| Watchdog relaunches the agent right after you close it | By design while `strict_watchdog` is on. Turn it off in Settings → Strict-mode protection first. |
| Windowed exe "does nothing" | It started silently by design (no console). Check the tray and `logs\agent.log`. `TimeManager.exe --status` works from any terminal. |
| Extension shows NO_EXTENSION | The agent isn't running, or the token changed (Settings → Regenerate). Re-paste the token in the extension options. |

## 9. Size & trimming

The bundle is ~200 MB unpacked (portable zip ~80 MB): Python + PySide6 + the
Qt runtime the dashboard needs. `time-manager.spec` already excludes
QtWebEngine, QtQuick/Qml, QtCharts, QtPdf, QtSql, QtTest and tkinter. If size
matters further, the next candidates are `PySide6.QtNetwork` dependents and
unused platform plugins — measure with `dir /s` on `_internal` before cutting,
and re-run the smoke checks afterwards.

### Double-clicking TimeManagerTray.exe does nothing at all

No window, no dialog, no process, and **no** `logs\\tray-stdio.log` in
`%APPDATA%\\TimeManager` means the Python interpreter inside the exe never
started. In that order, check:

1. `TimeManager.exe --version` in the same folder still works (it almost
   always does - that is the telling part: same files, different subsystem).
2. Look in your antivirus' protection history - **unsigned, windowed
   PyInstaller executables are a classic silent-block target**, often while
   the console sibling runs happily. Excluding the `TimeManager` folder (or
   the whole project) resolves it.
3. Windows Event Viewer -> Windows Logs -> Application, around the exact
   double-click time.

**No-console workaround that always works:** double-click
`Start Time Manager.vbs` next to the exes (installed there by the build) -
it hides the console exe's window instead. `build.bat --console-tray`
produces a diagnostic TimeManagerTray.exe with a console, which prints the
real error when run from a terminal.

### "Failed to load Python DLL '...build\\time-manager\\_internal\\python312.dll'"

You launched an exe from the **`build\`** folder — that directory is
PyInstaller's scratch space, not the app. Run
**`dist\TimeManager\TimeManagerTray.exe`** instead. The same error also
appears if the exe is copied *without* its `_internal` folder: always move
the whole `TimeManager` folder (or use the portable zip, which keeps the
layout intact). Since v1.0.2 the build deletes the `build\` scratch folder
on success, so the confusing half-wired exes no longer exist to be run.
