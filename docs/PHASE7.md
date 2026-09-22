# Phase 7 — Security & Anti-Bypass Hardening (v0.7.0)

Status: **complete**. Every mechanism here follows the two project rules for
security work: it is **transparent** (the user can always see what the agent
does — no stealth, no hooks, no kernel tricks), and it **fails toward
enforcement** (when something is tampered with, the user loses convenience,
never gains free time).

## 1. Threat model: what a determined user could try

The spec (§15/§25) names the basic bypass attempts. This is the honest map of
what each one hits in v0.7.0:

| # | Bypass attempt | Countermeasure | Where |
|---|---|---|---|
| 1 | Close the Time Manager window / kill the process | Kill leaves no STOP row → the next start reports `UNEXPECTED_STOP` (critical) with the last-seen timestamp | `app/core/security/lifecycle.py` |
| 2 | Restart Windows after deleting the autostart entry | While STRICT rules are armed, a missing Run key is re-registered at start; audited `STARTUP_REPAIRED` + urgent notification | `app/service.py::_repair_startup_if_strict` |
| 3 | Restart the target application | STRICT relaunch prevention (every tick) — unchanged from Phase 3 | `app/core/enforcement/policy.py` |
| 4 | Restart the browser / disable the extension | `EXTENSION_SILENT` alert while a STRICT website rule is armed and a supported browser runs without a connection; re-notified on a 15 min cooldown | `app/core/security/bypass.py` |
| 5 | Change the system clock backwards | Durations are monotonic (Phase 2); a backwards jump **at startup** is detected against the last-seen wall time and reported `CLOCK_REGRESSED` (critical) | `app/core/security/floor.py` |
| 6 | Change the clock while running | Wall-vs-monotonic disagreement > 60 s closes sessions safely and logs to `clock_log` (Phase 2, unchanged) | `app/core/tracking/tracker.py` |
| 7 | Edit / roll back / delete `daily_usage` rows | Per-day usage **floors** live in a sibling file (`timemanager.floor.json`), outside the DB; STRICT rules evaluate `max(stored, floor)` and a `USAGE_TAMPERED` finding is audited once per rule/day | `app/core/security/floor.py` |
| 8 | Kill the monitoring process entirely | Opt-in watchdog: a plain Task Scheduler task runs the normal `--monitor` entry point every minute; a killed agent is revived within ~60 s (a live one makes the new instance exit with code 3 on the profile lock) | `app/windows/watchdog.py` |
| 9 | Forged/malformed extension messages | Token auth, strict schema, per-connection rate limit, agent-side timestamps (Phase 4, unchanged) | `app/ipc/*` |
| 10 | Pause instead of obeying | Pause is time-boxed 1–480 min, visible, audited, and can never suppress STRICT rules (Phase 5, unchanged) | `app/core/pause.py` |

What is deliberately **not** done (spec: "this is a productivity tool, not
parental-control malware"): no kernel drivers, no hidden processes, no
self-protection against an admin killing the agent mid-run (detected, not
prevented), no blocking of Task Manager, no encrypted-or-else storage.

## 2. The mechanisms

### 2.1 Agent lifecycle audit (`lifecycle.py`)

Every run writes a `START` row when it takes over the profile and a `STOP`
row on a clean `stop()`. At startup, *before* claiming the profile:

```
last lifecycle row == START   ->  the previous run never said goodbye
                              ->  UNEXPECTED_STOP (critical) + dashboard notice
last lifecycle row == STOP    ->  all clear
no rows                       ->  first ever run, nothing to compare
```

The `UNEXPECTED_STOP` row consumes the finding, so restarting five times
produces one report per kill, not five. Read-only status runs
(`--status-json` against a live agent) never write lifecycle rows.

### 2.2 Usage floors (`floor.py`)

While the agent runs, it keeps a high-water mark of each rule's usage for the
current local day in `timemanager.floor.json` — a small JSON file **next to
the database, not inside it**. Deleting or rolling back `daily_usage` cannot
touch it. For STRICT rules the engine then evaluates limits against
`max(stored, floor)`:

- `stored >= floor` → nothing happens (the normal case; the floor is a
  shadow copy that only matters after tampering).
- `stored < floor` → the floor wins; one `USAGE_TAMPERED` (critical) audit
  row per rule per day; the dashboard and `--status` show the clamp.

Scope decisions (deliberate):

- **STRICT rules only.** NORMAL rules are lenient by design; re-arming one by
  editing the DB is the user's own choice, and the audit trail says so.
- The floor is **per-day**; midnight resets it exactly like `daily_usage`.
- It cannot resurrect a *deleted rule* (nothing left to enforce), but the
  deletion itself is visible in the audit trail.
- A corrupted floor file is logged loudly and treated as absent — the file
  can only weaken enforcement, never strengthen it, so faking it buys nothing.
- Writes are atomic (tmp + `os.replace`); the file is flushed every ≥5 s and
  on clean shutdown.

### 2.3 Startup repair (STRICT)

At every non-read-only start with armed STRICT rules, if the
`HKCU\...\Run` autostart entry is missing it is re-registered, audited
(`STARTUP_REPAIRED`, warning) and announced with an urgent notification that
explains how to stop it legitimately (switch the rules to Normal mode).
Windows-only; off-Windows this is a no-op.

### 2.4 Watchdog (opt-in, Windows)

`strict_watchdog: true` (Settings → *Strict-mode protection*, or
`svc.set_watchdog_enabled()`) registers a scheduled task named
**"Time Manager Watchdog"**:

```
schtasks /Create /F /SC MINUTE /MO 1 /TN "Time Manager Watchdog" /TR "<agent> --monitor"
```

- It invokes the **normal startup path** — if the agent lives, the new
  process hits the single-instance lock and exits 3 within a second; if the
  agent was killed, it takes over. One code path, nothing duplicated.
- User-scope task: no admin rights, visible in Task Scheduler, removable
  there or in Settings. Install/remove are audited
  (`WATCHDOG_INSTALLED` / `WATCHDOG_REMOVED`).

### 2.5 BypassWatch (`bypass.py`)

One check per tick, cheap and side-effect-free apart from audit rows:

```
STRICT website rule enabled
AND chrome.exe / msedge.exe is running
AND zero authenticated extension connections for > 60 s
→ EXTENSION_SILENT (warning) + urgent notification
   re-notify every 15 min while the hole stays open
→ extension reconnects (or browser closes)
→ EXTENSION_RESTORED (info), episode closed
```

This converts the platform limitation ("a browser cannot be forced to keep an
extension") into a *visible* state instead of a silent hole.

### 2.6 Clock-regression check at startup

The floor file also stores the wall time the agent was last seen. If a new
run starts more than 90 s **before** that (NTP corrections stay inside the
tolerance), the start is audited `CLOCK_REGRESSED` (critical), noted in
`clock_log`, and shown on the dashboard. Mid-run jumps were already handled
in Phase 2.

## 3. Where the user sees all of this

| Surface | What it shows |
|---|---|
| Dashboard `SecurityBanner` | Worst-first stack: unclean stop, clock rollback, tampered usage (red), silent extension (amber), startup repair (info). Hidden when all clear. |
| Settings → *Strict-mode protection* | The watchdog toggle + a plain-language summary of what STRICT does. |
| `python -m app.main --security` | Armed STRICT rules, floor state, watchdog status, regression/unclean-stop flags, last 15 audit events. |
| `python -m app.main --status` | Five-line audit tail. |
| `--status-json` → `"security"` | Everything machine-readable: `unclean_stop`, `clock_regressed_seconds`, `startup_repaired`, `watchdog`, `extension_silent_rules`, `usage_tamper`, `recent_audit`. |

## 4. Audit event reference (`agent_audit`, schema v3)

| kind | severity | meaning |
|---|---|---|
| `START` / `STOP` | info | the agent took over / released the profile |
| `UNEXPECTED_STOP` | critical | the previous run never recorded a stop |
| `USAGE_TAMPERED` | critical | `daily_usage` < floor for a STRICT rule (once per rule/day) |
| `CLOCK_REGRESSED` | critical | startup wall clock far behind the last seen time |
| `STARTUP_REPAIRED` | warning | STRICT armed but the autostart entry was missing |
| `WATCHDOG_INSTALLED` / `WATCHDOG_REMOVED` | info / warning | Task Scheduler task registered or removed |
| `EXTENSION_SILENT` | warning | STRICT website rule + running browser + no extension for > 60 s |
| `EXTENSION_RESTORED` | info | the silent episode ended |

## 5. Honest limitations

- **An admin can still win.** Killing the agent *and never restarting it*
  ends enforcement — detection needs a next run. The watchdog shortens the
  window to ~a minute; it cannot survive "delete the task + kill the agent",
  which is visible (task missing) but not preventable at user level.
- **Full profile wipe** (DB + floor file + config deleted) is equivalent to a
  reinstall; there is nothing left to detect with. Mitigation: the DB backup
  folder (`%APPDATA%\TimeManager\backups`) survives a casual wipe, and
  daily-limit resets are only worthwhile if the user repeats them daily —
  at which point they should simply use different settings.
- **The floor defends table edits, not rule deletion.** Deleting the rule
  removes the enforcement target; that action is its own audit trail entry
  (rules CRUD), and creating rules is a visible GUI action.
- **Browsers other than Chrome/Edge are invisible** to BypassWatch (Phase 4
  scope). Firefox support arrives with extension support for it.
- **Watchdog granularity is 1 minute** (Task Scheduler's finest repeating
  interval). A kill-and-use window of up to ~60 s remains; the lifecycle
  audit still reports it afterwards.

## 6. Tests

| Suite | Covers |
|---|---|
| `tests/test_lifecycle_audit.py` | schema v3, clean vs killed runs, finding consumption, audit ordering |
| `tests/test_usage_floor.py` | clamping, NORMAL exemption, per-day reset, reload, corrupt file, clock regression, engine end-to-end after DB rollback |
| `tests/test_bypass_watch.py` | grace window, episode open/close, renotify cooldown, NORMAL/app/disabled exemptions |
| `tests/test_watchdog.py` | schtasks command shape, idempotence, failure paths, off-Windows unavailability |
| `tests/test_security_integration.py` | full AgentService wiring: kill → report, DB rollback → clamp, startup repair, silent extension in snapshot, watchdog toggle, clock regression at boot |
| viewmodel / Qt tests | notice wording/order, banner show/hide, watchdog toggle in Settings |

`pytest -q` — 497 passed.
