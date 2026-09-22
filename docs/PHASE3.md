# Phase 3 — Windows Monitoring & Enforcement (COMPLETE, 1 manual pass outstanding)

## Goal
Turn the pure Phase 2 engine into an agent that actually watches the machine:
process snapshots, foreground detection, activity resolution, enforcement with
graceful-then-force closing, autostart, suspend awareness — plus the CLI entry
point to run it all before any GUI exists.

## What was built

| Area | File | Notes |
|---|---|---|
| Snapshot model | `app/core/monitoring/processes.py` | `SystemSnapshot` — the only machine view the engine sees; `PsutilProcessSource`, `NullMonitorSource` |
| Activity rules | `app/core/monitoring/activity.py` | foreground/grace/idle policy per rule type |
| Game correlation | `app/core/enforcement/game_guard.py` | detector verdicts → fail-safe `in_game_session`/`confident` |
| Kill policy | `app/core/enforcement/policy.py` | protected list, NORMAL grandfathering vs STRICT, `GrandpaStore` |
| Closer | `app/core/enforcement/closer.py` | graceful request → grace period → forced terminate |
| Executor | `app/core/enforcement/adapters.py` | notifications, closes, website hand-off, `enforcement_log` audit |
| Loop | `app/core/monitoring/monitor.py` | tiered tick, sleep detection, error containment, thread control |
| win32 | `app/windows/api.py` | foreground PID, idle seconds, WM_CLOSE, TerminateProcess |
| win32 | `app/windows/{win_monitor,startup,power}.py` | source factory, Run-key autostart, suspend/resume + `GapDetector` |
| CLI | `app/cli/{monitor,simulate}.py` | `--monitor` (real) and `--simulate` (deterministic demo) |
| DB | `schema.sql` v2, `db.py` | `enforcement_log` audit table + Phase 3 settings |
| Fakes | `app/testing/fakes.py` | shared by tests *and* the simulator |

## Decisions worth remembering

**Counting rule.** Being installed/running is not usage. A rule counts while the
target is in the **foreground**, plus a short `background_grace_seconds` (5 s)
so alt-tabbing does not punch holes in the accounting. STRICT mode changes
*enforcement harshness only* — never what counts. `enforce_foreground_only=False`
is available for "running counts" setups.

**Accounting accuracy.** An interval is billed when the tracked session was open
across it (the target was active at one of the two polls bounding it). Effect: a
focus change or a shutdown costs **at most one monitoring interval** (1 s at the
default cadence). Sessions that start mid-interval lose that first interval.
Documented and covered by tests — the numbers in the UI are honest to ±1 tick.

**Idle gate (opt-in, default off).** With `idle_grace_seconds > 0`, no rule
counts anything after N seconds without keyboard/mouse input: a paused film or a
coffee break is not screen time. Unknown idle (non-Windows) never blocks.

**NORMAL vs STRICT, precisely.**
| | `CLOSE` | `BLOCK` (app = prevent launch) |
|---|---|---|
| **NORMAL** | close matched processes now; relaunches are closed too | instances already running when the limit hit are **grandfathered** to the end of their run; anything launched after is closed on sight |
| **STRICT** | same, no leniency anywhere | **every** matching process is closed, every tick, while the limit stands |

Grandfathered PID sets are persisted per rule/day, so a restart mid-enforcement
does not turn a pre-existing instance into a "new launch".

**Never kill these.** A built-in protected list (shell/broker processes:
`explorer.exe`, `csrss.exe`-class, `winlogon.exe`, …), user-configured
`protected_processes`, PID ≤ 4, and the agent's own process/image. Enforced in
`KillPolicy` *and* again in `SystemProcessController` — on POSIX, signalling
pid 0 or a negative pid would hit a whole process group, so the second guard is
safety, not politeness.

**Closing is polite first.** `WM_CLOSE` to visible top-level windows (what
clicking X does), wait `graceful_close_timeout_seconds` (default 6 s), then
`TerminateProcess`. The audit log records `GRACEFUL`, `FORCED` ("grace expired"
vs "no graceful path") and failures. A client that ignores WM_CLOSE is
force-closed after the grace — verified in the simulator.

**Fail-safe games.** Unknown game state (low detector confidence, detector
exception, no verdict) keeps the rule in `WAITING_FOR_SESSION_END`. Killing a
live match is the one unrecoverable mistake this project can make.

**Websites** are resolved from extension tab events (Phase 4). Without a link
the resolver reports "no browser link yet" and enforcement records `DEFERRED` —
nothing pretends to have happened. The simulator feeds a fake tab provider so
the whole path is exercised today.

**The audit trail records changes, not heartbeats.** One row per real action, or
per new no-op condition — a rule that stays exceeded for three hours generates a
handful of rows, not 10 000.

**The loop never dies.** Any tick exception is logged, counted in
`MonitorStats.errors` and retried next interval: a dead monitor means unlimited
screen time.

## The simulator (deterministic acceptance demo)

```bash
python -m app.main --simulate        # 40 simulated minutes, 5 s ticks
python -m app.main --simulate 20 --tick 5
```

Runs the real `Database → Tracker → RuleEngine → ActivityResolver →
GameSessionGuard → KillPolicy → ProcessCloser → EnforcementExecutor →
MonitorLoop` stack against a fake machine (clock, process table, close calls) and
prints a minute-by-minute story plus the real `enforcement_log` and notification
list. Scripted beats: Discord warned→closed→relaunched→re-closed; League
limit hit mid-match → WAIT → detector loses confidence → still WAIT → match ends
→ close → client ignores WM_CLOSE → **forced**; idle gate suppresses counting;
YouTube devolves to the extension. `tests/test_simulate.py` asserts every beat,
so the printed story cannot drift from the code.

## Verification

Automated (this machine, Linux, Python 3.13):
- `python -m pytest -q` → **181 passed** (57 from Phases 1–2 + 124 new).
- `python -m compileall -q app` → clean.
- `python -m app.main --simulate` → full 40-minute scenario, 0 loop errors.
- `python -m app.main --monitor` on Linux → refuses with a clear message
  (exit 2): foreground detection is Windows-only, and inventing numbers would be
  worse than refusing.

### Manual pass on Windows 10/11 (required — do this before Phase 4)

1. `pip install -r requirements.txt` then `python -m app.main --init-db`.
2. Create harmless rules *before* touching real ones: **Notepad**
   (`notepad.exe`, daily 2 min, CLOSE, NORMAL, warnings 60/30) and **YouTube**
   (`youtube.com`, daily 2 min, BLOCK).
3. `python -m app.main --monitor`, then:
   - [ ] Open Notepad and type. The console prints warnings at 1 min and 30 s.
   - [ ] At 2 min the window closes by itself **without** a "force" line in the
         log (graceful WM_CLOSE worked).
   - [ ] Reopen Notepad: it closes within a second (limit still stands).
   - [ ] Focus another window: the console stays quiet and no time accrues
         (check with `python -m app.main --status` while the monitor runs).
   - [ ] Lock the screen (`Win+L`) for 2 minutes: on unlock, no time jumps —
         working "idle" behaviour requires `idle_grace_seconds` (set it to 120 in
         `%APPDATA%\TimeManager\config.json` to test), and the suspend path logs a
         `clock_log` entry if the machine actually slept.
   - [ ] `Ctrl-C` stops cleanly; a `timemanager-*.db` backup appears next to the
         database.
   - [ ] Autostart: `python -c "from app.windows import startup; print(startup.enable(), startup.status())"`
         → shows the Run entry in `regedit` under
         `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
   - [ ] Process explorer (`explorer.exe`) as a rule target: the log must say
         `PROTECTED`, and Explorer must not die.
4. Report anything that fails; the numbers in the log (`pids=…`, `GRACEFUL`,
   `FORCED`) make failures diagnosable.

Known limitations (in addition to `docs/LIMITATIONS.md`): foreground detection
cannot see elevated (admin) windows from a non-elevated agent, and Windows'
"service host" chrome means an app that uses multiple PIDs (`name.exe` +
helpers) is matched only by image name.

## Phase 4 preview (browser extension link)
Authenticated loopback WebSocket server inside the agent
(`websockets`, 127.0.0.1:17846, 32-byte token), HELLO/WELCOME + TAB_ACTIVITY +
BLOCK_QUERY/BLOCK_DECISION + RULE_UPDATE + HEARTBEAT, feeding
`MonitorLoop.set_web_state_provider()` and the `browser_sink` hook that today
records `DEFERRED`; MV3 service-worker implementation with `normalize.js`
matching `matcher.py`.
