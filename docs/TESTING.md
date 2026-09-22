# Testing strategy

## Automated (`pytest`, runs on any OS)

| Area | File | What it proves |
|---|---|---|
| Domain normalization + matching | `tests/test_matcher.py` | `www/m.` folding, subdomain scoping, evil-suffix rejection, IDNA |
| Rule validation | `tests/test_rules.py` | required fields per type, limits sane, bad domains/exes rejected |
| Schedule evaluation | `tests/test_schedule.py` | weekdays, windows, midnight-spanning windows, empty=always |
| Time utilities | `tests/test_timeutils.py` | local-day boundaries, monotonic durations, formatting |
| State machine | `tests/test_state_machine.py` | NORMAL→WARNING→LIMIT→ENFORCED/WAIT paths, warn-once, day reset, fail-safe unknown game state |
| IPC protocol validation | `tests/test_protocol.py` | accept good messages, reject malformed/oversize/evil, rate limit |
| Database | `tests/test_database.py` | migrations, rule CRUD, sessions, daily upsert, state upsert, recovery idempotency |
| Simulator | `tests/test_simulate.py` | deterministic 40-min script: closes, block push, totals |
| Browser link | `tests/test_ipc_*.py`, `test_extension_assets.py` | protocol, rate limits, staleness, parity with the extension's normalizer |
| Single-instance lock | `tests/test_lockfile.py` | second instance refused, stale takeover, foreign locks untouched |
| Pause | `tests/test_pause.py` | countdown, expiry, restart, clamping, STRICT exemption |
| Agent service | `tests/test_service.py` | lifecycle, lock, snapshot, rule CRUD, pause end-to-end |
| Viewmodel | `tests/test_viewmodel.py` | every string/role the UI shows (no Qt needed) |
| GUI (offscreen) | `tests/test_qt_ui.py` | real widgets, editor validation, settings, tray, screenshots, `run_gui` end-to-end |
| CLI surface | `tests/test_cli_gui.py` | `--status-json`, `--gui-shot`, `--gui` exit codes, layering |
| Detector host | `tests/test_detector_host.py` | probe budget, abandonment, quarantine, duplicate ids, "never a kill" |
| Plugin loading | `tests/test_detector_loader.py` | three plugin forms, every rejection reason, isolation between plugins |
| LoL detector | `tests/test_lol_detector.py` | the full verdict table, settle window, degraded/hostile signals |
| Game signals | `tests/test_game_signals.py` | Live Client API over a real local socket, log freshness, phase parsing |
| Detector selftest | `tests/test_detector_selftest.py` | `--detector-selftest` steps, wiring, profile plugin dir, status block |

Run: `pytest -q` from `time-manager/` (**448 tests**, Phases 1–6), plus
`python -m app.main --ipc-selftest` and `python -m app.main --detector-selftest`
as end-to-end acceptance runs. CI gate (Phase 8): all green on Windows + Linux,
plus `python -m compileall`.

## Manual Windows test plan (abridged; full matrix in Phase 8)

- **T1 harmless app:** rule on `notepad.exe`, 1-min daily CLOSE. Open, focus,
  background, close → dashboard counts foreground only; at limit Notepad
  closes + notification.
- **T2 midnight:** set clock 23:59→00:01 (or wait), verify new `daily_usage` row.
- **T3 crash:** kill agent mid-session, restart → session recovered once, no
  double count.
- **T4 extension:** unpacked load, token paste, youtube rule 1 min → active tab
  counts, background tab/minimize doesn't, block page at limit, reset time shown.
- **T5 LoL (needs game):** launcher-only at limit → closes; in-match at limit →
  WAITING, post-match → enforced. Record process snapshots for detector docs.
- **T6 desktop app (Phase 5):** the 11-step tray/dashboard/pause/settings
  checklist at the end of `docs/PHASE5.md`.
- **T7 game detection (Phase 6):** the 9-step League of Legends checklist at the
  end of `docs/PHASE6.md` (real match, reconnect, champ select, plugin drop-in).
- **T8 game detection coverage (Phase 8):** the per-game manual matrix below
  (`docs/PHASE8.md` §3).

## Manual Phase 8 matrix — one game at a time (needs the real game, Win10/11)

For each game (LoL, VALORANT, R.E.P.O., TFT), create a GAME rule via the rule
editor's *Known game* list, action *Wait for the match to end*, 2-minute daily
limit. Run `python -m app.main --monitor` and step through the states, noting
`--status-json` → `detectors.items[].last_*` after each step:

| Step | Expect (all games) |
| --- | --- |
| 1. launcher/client open, no match | rule state NORMAL; last verdict `not in session, 1.0` once settled |
| 2. queue / loading | LoL: ChampSelect/InProgress phase → WAIT 0.5. VALORANT/REPO: game process up → WAIT 0.5. TFT: InProgress phase → WAIT 0.5 |
| 3. in an active match | WAIT (`WAITING_FOR_SESSION_END` at the 2-min limit); nothing is closed |
| 4. finish the match normally | LoL/TFT: client settles 30 s → ENFORCED, client closes/blocks per rule. VALORANT/REPO: back in menu → still WAIT (documented); after the game is fully closed 30 s → ENFORCED |
| 5. Alt-F4 mid-match | process gone → settle window WAIT → then ENFORCED |
| 6. game crashes (kill the process from Task Manager) | same as 5 — never an enforcement DURING uncertainty |
| 7. rule with action CLOSE (re-run step 3) | closes immediately when the limit is reached — same app path as T1 |

Record: process names seen (Task Manager → Details), log lines if any, and
the detector's last verdict per step. A ✓ for "active session" in
`docs/PHASE8.md` §4 means this table ran green for that game.

## What "tested" means here

A feature is DONE only when its automated tests pass AND (for Windows-only
behavior) its manual test has been executed on Win10/11 and logged. Phase 1
features are OS-independent and fully covered by `pytest`.
