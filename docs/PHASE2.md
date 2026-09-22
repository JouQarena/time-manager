# Phase 2 — Core Engine (COMPLETE)

## Goal
Turn per-tick activity flags into accounted time + enforcement decisions,
with no GUI, no Win32, no network — fully testable on any OS with a fake clock.

## Delivered
- `app/core/clock.py` — `Clock` protocol, `SystemClock`, `FakeClock`
  (wall/mono advance together; wall jumps leave mono steady).
- `app/core/timeutils.py` — added `classify_skew` (clock-agnostic),
  `epoch_to_local`, `day_str_of_epoch`, `local_midnights_between`
  (date-iterated, DST-robust).
- `app/core/tracking/tracker.py` — `Tracker.poll()`:
  one open session per rule, monotonic durations, exact midnight splits
  (one DB row per day, continuous session total preserved), batched
  persistence (default 5 s) with exact `today_total()` reads,
  60 s skew guard + 600 s single-tick cap, `notify_suspend()` /
  `close_all()` / `flush()` lifecycle.
- `app/core/rules/engine.py` — `RuleEngine.tick()`: schedule gating
  (outside schedule counts nothing), tracker poll, exact totals,
  state-machine evaluation, state + warned-threshold persistence.
- `tests/test_clock.py`, `test_tracker.py`, `test_engine.py` — 20 new tests.

## Key behaviors (decided + tested)
- Interval is billed when the session was open across it (active at either
  bounding poll); idle accrues nothing; transitions cost ≤ 1 monitoring
  interval. Precise statement in docs/PHASE3.md.
- Midnight: seconds split exactly across days; sessions keep one continuous
  total (a session spanning midnight can still hit the session limit — it's
  genuinely one continuous session).
- Overtime during `WAITING_FOR_SESSION_END` accrues honestly (dashboard will
  show e.g. `2h 14m / 2h`).
- Wall jumps can never inflate or erase counted time (mono accounting); the
  anomalous tick attributes 0 and everything is logged to `clock_log`.
- Restart-safe: graceful `close_all()` persists all; crash rows are sealed by
  `recover_open_sessions()` exactly once (Phase 1, covered by tests).

## Verification (2026-09-21)
- `python3 -m pytest -q` → all 57 pass (39 Phase 1 + 18 new).
- `python3 -m compileall -q app` → clean.

## Phase 3 preview (Windows monitoring)
`psutil` process snapshots, `pywin32` foreground detection
(`GetForegroundWindow` → PID → exe), monitor loop wiring activity flags into
`RuleEngine.tick()`, graceful CLOSE enforcement (`WM_CLOSE` → escalate),
strict relaunch-guard, suspend/resume listeners, startup registration.
Manual tests on Win10/11 with harmless apps (notepad) per `docs/TESTING.md`.
