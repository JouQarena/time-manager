# Phase 9 — Packaging (v0.9.0)

Status: **complete and smoke-verified**. The project now builds into a
portable Windows application; the practical guide for users lives in
**[docs/PACKAGING.md](docs/PACKAGING.md)** (build / install / uninstall /
update / data locations / extension / troubleshooting). This record is the
engineering summary.

## What was built

### `time-manager.spec` — one spec, two executables

PyInstaller one-folder bundle (`dist/TimeManager/`) with shared libraries:

| Executable | Console | Role |
| --- | --- | --- |
| `TimeManager.exe` | yes | the CLI: `--monitor`, `--status`, `--security`, `--init-db`, `--simulate`, `--show-token`, `--show-extension-path`, `--detector-selftest`, ... |
| `TimeManagerTray.exe` | **no** (windowed) | the desktop app (tray + dashboard, monitor thread included). Windowed on purpose: Windows autostart and the per-minute watchdog task must not flash a console. |

Details that matter:

- **Version resource generated from `app.__version__`** inside the spec — it
  can never drift from the code.
- **Data files:** `browser-extension/` (so a packaged install can load it
  unpacked; `--show-extension-path` prints where) and
  `app/database/schema.sql` (read at runtime by `db.py`; the Linux smoke
  build caught this on the first run — which is exactly why the smoke build
  exists).
- **Windows-only pieces guarded:** icon + version resource + pywin32 hidden
  imports activate only on `sys.platform == "win32"`, so the same spec is the
  cross-platform smoke test.
- **Excludes:** QtWebEngine, QtQuick/Qml, QtCharts, QtPdf, QtSql, QtTest,
  tkinter, pytest — keeps the bundle at ~200 MB unpacked / ~80 MB zipped.

### Frozen-mode code support

- `app/resources.py` — `bundle_dir()` / `extension_dir()` / `is_frozen()`
  resolve `_MEIPASS` in frozen builds and the project root in source runs
  (unit-tested for both modes).
- `packaging/entry_console.py` — CLI entry; packaged builds show `--help`
  with no arguments instead of launching the demo simulator.
- `packaging/entry_tray.py` — windowed entry, always `--gui`.
- `_setup_frozen_file_logging()` — packaged builds write
  `%APPDATA%\TimeManager\logs\agent.log` (3 × 1 MB rotating): a windowed exe
  has no console, and the monitor must leave a trail.
- `startup.launch_command(gui=...)` — autostart registers the **desktop app**
  (`--gui`); the watchdog task keeps `--monitor`. On a frozen build the
  command is the running executable itself, so both keep working when the
  install moves (path change ⇒ re-save Settings once; STRICT startup repair
  also fixes a stale entry).

### Build & distribution tooling

- `scripts/build_windows.ps1` — venv → deps → **full test-suite gate** →
  PyInstaller → frozen smoke checks (`--version`, `--show-extension-path`,
  `--init-db`, `--status`) → `dist/TimeManager-v<version>-windows-portable.zip`
  → writes `build/version.ini` for the installer.
- `scripts/build_smoke.sh` — cross-platform smoke build used during this
  phase to verify the spec (frozen `--simulate`, `--detector-selftest`,
  `--gui-shot` from the windowed exe all pass).
- `installer/time-manager.iss` — optional Inno Setup installer: Start-menu
  shortcuts, optional desktop icon, optional HKCU autostart entry, an
  uninstaller that stops the agent first — and deliberately leaves
  `%APPDATA%\TimeManager` (user data) alone, documented in the script.
- `requirements-build.txt` — PyInstaller only; the shipped app needs
  `requirements.txt` alone.

## Verification

- Linux smoke build of the same spec: frozen CLI (`--version`, `--init-db`,
  `--status`, `--security`, `--simulate`, `--detector-selftest`) and the
  windowed exe (`--gui-shot` rendering the real dashboard offscreen) all pass;
  the shipped extension folder is intact inside `_internal`.
- 542 tests green (new: resource paths frozen/source, packaged CLI helpers,
  autostart/gui command split).
- The Windows exe itself is produced by running `build_windows.ps1` on a
  Windows 10/11 machine — the spec is platform-guarded so the first Windows
  build differs only in the guarded pieces (icon resource, pywin32).

## Honest limitations

- **Unsigned binaries:** no code-signing certificate in this project, so
  SmartScreen will warn on first run (documented, with the self-build
  alternative). Signing is the natural next step and needs a cert, not code.
- **Antivirus heuristics** sometimes flag unsigned PyInstaller loaders; the
  answer is the same: build from source, tests gate it.
- **No auto-update channel** (offline-first by design): updates are
  replace-the-folder; the DB/config live in `%APPDATA%` and migrate forward.
- **The extension still loads unpacked** (Developer mode). Store publication
  (Chrome Web Store / Edge Add-ons) is a post-1.0 step and needs store assets
  and review, not code from this phase.
- **Size:** ~200 MB unpacked is the honest cost of shipping PySide6; the
  trimming notes in PACKAGING.md §9 list the next candidates if it matters.
