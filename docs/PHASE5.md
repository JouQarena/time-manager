# Phase 5 — Desktop app (tray, dashboard, status surface)

**Status: complete.** 342 tests pass (`249` from Phases 1–4, `93` new here).
Version `0.5.0`.

Phase 5 is the desktop application: one long-lived agent process that owns the
profile, the database, the monitor loop, the browser link and the pauses — with
a Qt tray icon, a live dashboard, a rule editor and a settings dialog on top of
it, plus two machine-facing surfaces (`--status-json`, `--gui-shot`).

---

## 1. Why this scope

Phases 1–4 built the engine, the Windows layer and the authenticated browser
link. Nothing in the earlier plans named a single "Phase 5" deliverable: the
README slotted the *browser extension* there, and that shipped early, inside
Phase 4, because the IPC contract needed it.

So Phase 5 was chosen as the largest remaining gap in the original brief — a
production Windows time manager needs a face:

* the user must **see** what is being counted (per-rule usage, live)
* the user must be able to **create and edit rules** without hand-editing JSON
* the user must be able to **pause** tracking (a time-boxed, auditable pause)
* support and automation need a **machine-readable** status
* and the whole thing must be **one** process, not a pile of scripts

Everything below follows from those five requirements. The fail-safe LoL
detector plugin (Phase 6 in the original plan) remains next.

## 2. Architecture

```
                     ┌──────────────────────── app/ui/qt ────────────────────────┐
                     │  MainWindow (1 s refresh)   RuleDialog   SettingsDialog   │
                     │  TrayController + QtNotifier                              │
                     └───────────────▲───────────────────────────▲───────────────┘
                                     │ Snapshot / save_rule()     │ settings, token
                     ┌───────────────┴───────────────────────────┴───────────────┐
                     │ app/ui/viewmodel.py   (Qt-free formatting + rule rows)     │
                     └───────────────▲───────────────────────────────────────────┘
                                     │
┌────────────────────────────────────┴────────────────────────────────────────┐
│ app/service.py — AgentService (one owner of the profile)                    │
│  InstanceLock → Database → token → PauseController → Tracker → MonitorLoop  │
│  → EnforcementExecutor (notifier, browser sink, pause provider)             │
│  → AgentBridge → IpcServer (127.0.0.1)                                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

Layering rules that the tests enforce:

* `app/core/**`, `app/service.py` and `app/ui/viewmodel.py` import **no Qt** (a
  subprocess test asserts `PySide6` is absent from `sys.modules`).
* `app/ui/qt/**` never opens a socket; it reads `service.snapshot()` in-process.
* The Qt-free viewmodel owns every word and colour role the user reads, so the
  dashboard, the tray tooltip and `--status-json` cannot disagree.

## 3. What was built

| File | Purpose |
| --- | --- |
| `app/config/lockfile.py` | `InstanceLock`: `<db>.lock` with `pid:epoch`, stale dead-PID takeover, `held_by_other()` |
| `app/core/pause.py` | `PauseController`: 1–480 min pauses in the `pause_until` setting, STRICT-exempt, restart-safe, `clock_log` audit |
| `app/service.py` | `AgentService`: lock → db → token → pause → tracker → monitor → executor → bridge → IPC; `snapshot()`, rule CRUD, `backup()`, `regenerate_token()`, `set_notifier()` |
| `app/ui/viewmodel.py` | Qt-free text/roles: durations, resets, schedules, `RuleRow`, health, banner, panels, timeline, window title |
| `app/ui/qt/theme.py` | Dark stylesheet + `PALETTE` + role colours (no external assets) |
| `app/ui/qt/icons.py` | Tray/window icons drawn with `QPainter` (no image files) |
| `app/ui/qt/widgets.py` | `Chip`, `RuleCard`, `Panel`, `EmptyState` |
| `app/ui/qt/main_window.py` | Dashboard: header, pause menu, rule cards, browser + link panels, timeline, tray-aware close |
| `app/ui/qt/rule_editor.py` | `RuleDialog`: type-aware fields, builds the real `core.Rule` (validation lives there) |
| `app/ui/qt/settings_dialog.py` | `SettingsDialog`: interval, foreground/grace/idle, browser foreground, close timeout, port, token (mask/reveal/copy/regenerate), autostart, notifications, backup |
| `app/ui/qt/tray.py` | `TrayController` (status, pause 15/30/60, resume, quit) + `QtNotifier` |
| `app/ui/qt/app.py` | `create_app()`, `run_gui()`, `render_screenshots()` |
| `app/ui/qt/demo.py` | `demo_service()`: a fake-clock agent on demo data, used by `--gui-shot` and the tests |

Pause semantics (the one behavioural change to the engine): a pause is a **hard
boundary**. On the first paused tick the monitor seals every suppressible rule's
open session (`Tracker.close_rule`), so the interval in progress is never billed
after the user asked for a break. STRICT rules keep counting and are never
closed or blocked by a pause; the executor records `SKIPPED · paused by the
user` once per rule so the audit trail explains the missing action.

## 4. CLI surface

```bash
python -m app.main --gui                 # tray + dashboard (the real app)
python -m app.main --gui-shot [--out DIR]  # render the GUI offscreen to PNGs
python -m app.main --status-json         # machine-readable status, exit 0
```

Exit codes for `--gui`: `0` normal exit, `1` start failure (reported in a dialog),
`3` another instance is already running, `4` PySide6 is not installed.

`--status-json` is deliberately **read-only and safe at any time**: it takes no
lock and, when a live agent owns the profile, it does not try to claim the
browser port — the payload says so:

```json
{
  "read_only": true,
  "ipc": {"running": false, "port": null,
          "note": "another instance is running (pid 9123); read-only view"}
}
```

Payload keys: `agent_version`, `day`, `running`, `read_only`, `paused`,
`pause_remaining_seconds`, `pause_until`, `pause_exempt_rules`,
`browsers{connected,domains,clients}`, `ipc{running,port,messages_in,
messages_out,rate_limited,last_error,note}`, `monitor{ticks,errors,last_error,
closes,last_sleep_gap_seconds,thread,degraded}`, `rules[…]`, `storage{db_path,
profile_dir}`, `recent_enforcement[…]`, `token_present`.

## 5. Screenshots (rendered by the shipping code)

`--gui-shot` builds a `demo_service()` — a real `AgentService` on a `FakeClock`
with demo rules and one engine tick — then renders the actual widgets offscreen:

| File | What it shows |
| --- | --- |
| `docs/screenshots/dashboard.png` | Header (`Monitoring`, day, reset, version), pause menu, rule cards with state chips and progress bars, browser-extension panel, agent-link panel, enforcement timeline, footer status |
| `docs/screenshots/rule_editor.png` | The rule editor: type, target, limits, warnings, action, mode, schedule |
| `docs/screenshots/settings.png` | Monitoring, browser extension (masked token, regenerate) and general settings |

The screenshots are a build artifact, not hand-drawn documentation: the numbers
on them come from an engine run. Regenerate with:

```bash
QT_QPA_PLATFORM=offscreen python -m app.main --gui-shot
```

## 6. Verification

```
$ python -m pytest -q
342 passed

$ python -m pytest tests/test_pause.py tests/test_lockfile.py tests/test_service.py \
      tests/test_viewmodel.py tests/test_qt_ui.py tests/test_cli_gui.py -q
93 passed            # 10 pause · 6 lockfile · 17 service · 21 viewmodel
                     # 28 qt · 11 cli

$ QT_QPA_PLATFORM=offscreen python -m app.main --gui-shot --out /tmp/shots
  wrote /tmp/shots/dashboard.png        (131 KB)
  wrote /tmp/shots/rule_editor.png      (54 KB)
  wrote /tmp/shots/settings.png         (59 KB)

$ python -m app.main --status-json --db /tmp/s.json
{ "agent_version": "0.5.0", "running": true, "ipc": {"running": true,
  "port": 17846, ...}, "monitor": {"thread": false, "degraded":
  "foreground detection unavailable on this OS ..."}, ... }

$ QT_QPA_PLATFORM=offscreen timeout 12 python -m app.main --gui --db /tmp/gui.db
IPC server listening on ws://127.0.0.1:17846
Monitor loop started (interval 1.00s).        # no traceback, clean teardown
```

What the new tests pin down, file by file:

* **lockfile** — acquire/release, second instance refused against a live foreign
  PID, stale dead-PID takeover, corrupt lock treated as stale, a lock we do not
  own is never deleted.
* **pause** — countdown, auto-expiry (logs + clears the setting), manual resume,
  1–480 min clamping, STRICT never suppressed, survives restart, expired and
  unparseable settings ignored.
* **service** — lifecycle, DB+link+token built on start, sessions flushed and the
  lock released on stop, second instance refused (`pid` in the error), guard can
  be disabled for read-only tools, failed start leaves no lock, idempotent
  start, pause stops counting for NORMAL and keeps STRICT counting, pause
  prevents enforcement actions (`SKIPPED · paused by the user` in the audit),
  rule CRUD pushes `RULE_UPDATE`, invalid rules rejected before the DB, snapshot
  shape + JSON safety + tick errors, engine-owned states, token rotation
  restarts the link, backup writes a file.
* **viewmodel** — every formatter, each state's wording/colour, pause banner,
  health precedence, browser/link lines, timeline humanisation, titles.
* **qt** — app singleton + stylesheet, no external resources in the stylesheet,
  cards track the database, header/pause banner, timeline and link panels,
  pause/resume from the menu, enable/disable a rule, timer wiring, close hides
  to tray, the editor builds and rejects through the core model, round-trips an
  existing rule, settings save to config + database, token masking, on-demand
  backup, tray menu contents and status text, notifier wiring, `render_screenshots`
  writes real PNGs, a real grab is not blank, and `run_gui()` runs the full event
  loop with a live agent and exits cleanly.
* **cli** — `--status-json` validity/keys/rules/read-only behaviour, `--gui-shot`
  PNGs (including a pixel check that the dashboard is not blank), `--gui` exit
  codes for missing PySide6 / second instance / start failure, no Qt import in
  the core, no socket in the UI layer.

## 7. Manual checklist (needs a real desktop)

Not run here — this sandbox has no display and no Windows process table. Run on
Windows 11 (or a Linux desktop with a real X/Wayland session):

1. `python -m app.main --gui` — window appears with the dark theme; the tray
   icon is present and its menu opens.
2. Create a rule for `notepad.exe` with a 1-minute daily limit, `Close app`,
   NORMAL. Launch Notepad and watch the card's usage climb; at 1 minute it is
   closed gracefully and the card turns red (`Limit reached — closed`).
3. `Pause → 15 minutes` from the tray: the dashboard banner shows the countdown,
   the chip says `Paused`, the tray tooltip agrees, and usage stops climbing.
   The timeline shows nothing new while paused. `Resume` clears the banner.
4. Add a STRICT rule, pause again, and confirm the STRICT card keeps counting
   and still enforces — the banner names it as exempt.
5. Close the window: it hides to the tray, the agent keeps running (check
   `--status-json` from another shell: `running: true`, `read_only: true`).
6. `Quit Time Manager` from the tray: the process exits, the lock file is gone.
7. Start a second copy while the first runs: a dialog says another instance is
   already running (exit code 3).
8. Rule editor: try to save with no name, no target and no limits — each is
   refused with a readable message.
9. Settings: change the interval and the port, Save, restart, and confirm the
   agent picks both up; use *Back up now* and check the file in
   `%APPDATA%\TimeManager\backups`.
10. Pair a browser from the extension Options page using the token shown in
    Settings (`Show`), then confirm the Browser extension panel lists it and the
    Active line names the focused site.
11. Windows autostart: tick *Start Time Manager with Windows*, Save, log out and
    back in — the agent starts minimised (`start_minimized`).

## 8. Known limits (Phase 5)

* The GUI is desktop-first: it is designed for the Windows agent, and on
  non-Windows hosts it runs in read-only-ish dev mode (no foreground/idle
  detection, so app activity is never attributed — shown as
  `Running (limited OS support)`).
* `--gui` renders the tray when the platform provides one; otherwise the
  dashboard still works and closing the window hides it (`create_app` sets
  `quitOnLastWindowClosed(False)`), so use the tray *Quit* action or Ctrl-C in
  the console.
* Pause is bounded to 8 hours and cannot be made permanent from the UI — that is
  intentional (a bypass with no end is how these tools get uninstalled).
* The dashboard polls the service once a second; it is not a live-streaming UI.
  Counters can therefore lag by up to one monitoring interval.
* Notification text comes from the executor; the tray shows them only when the
  platform supports tray messages and notifications are enabled in Settings.
