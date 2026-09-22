# Phase 1 — Architecture & foundation (COMPLETE)

## Goal
Lock the architecture, contracts, and data model, then implement the
OS-independent foundation with tests — before any monitoring/GUI/extension code.

## Delivered
- `docs/ARCHITECTURE.md` — system overview, tech decisions, module boundaries,
  threading model, security/privacy.
- `docs/DATABASE.md` + `app/database/schema.sql` + `app/database/db.py` —
  versioned schema, centralized access, crash-recovery + backup helpers.
- `docs/PROTOCOL.md` + `app/ipc/protocol.py` — WS protocol v1, strict
  validation, rate limiting, token helper.
- `docs/STATE_MACHINE.md` + `app/core/enforcement/state_machine.py` —
  explicit per-rule/day state machine with warn-once + game fail-safe.
- `app/core/rules/` — Rule model/validation, exe+domain matching.
- `app/core/scheduling/` — days + windows incl. midnight-spanning.
- `app/core/timeutils.py` — local-day keys, monotonic durations, clock-skew probe.
- `app/core/detection/` — `GameSessionDetector` ABC + registry (LoL in Phase 6).
- `app/config/settings.py` — profile paths, JSON settings, IPC token mgmt.
- `app/main.py` — `--init-db / --status / --add-sample` CLI proving the stack.
- `browser-extension/manifest.json` + `shared/normalize.js` — contract mirror
  (full extension in Phase 5).
- `tests/` — 8 suites, all OS-independent, all passing (see verification).

## Verification (2026-09-21)
- `python3 -m pytest -q` → all tests pass.
- `python3 -m app.main --init-db --db /tmp/tm-phase1.db` → migrates, recovers,
  writes backup; `--add-sample` + `--status` render rules + progress bars.
- `python3 -m compileall -q app` → clean.

## Phase 2 preview (core engine)
Tracker service: open/heartbeat/close sessions from activity ticks, midnight
splitting, daily aggregation, warn/state persistence via the Phase 1 machine,
suspend-aware clock guard. New: `app/core/tracking/tracker.py`,
`app/core/rules/engine.py`, tests with a fake clock. No GUI, no Win32 yet.
