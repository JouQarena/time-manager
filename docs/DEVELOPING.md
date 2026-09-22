# Developing Time Manager

Everything you need to work on the codebase: environment setup, the test
strategy, where things live, and the recipes for the two most common changes
(a new game detector, a new rule field). Architecture and module
responsibilities: `docs/ARCHITECTURE.md`.

## 1. Environment

Any OS for core work; Windows 10/11 for the OS-integrated features.

```bash
py -3.12 -m venv .venv          # Windows (or python3.12 -m venv .venv)
.venv\Scripts\activate           # source .venv/bin/activate
pip install -r requirements.txt
pytest -q                        # the whole suite, ~30 s, no display needed
```

Optional dev extras: `pip install -r requirements-build.txt` (PyInstaller),
Inno Setup 6 for the installer. Qt tests run offscreen automatically
(`conftest.py` sets `QT_QPA_PLATFORM=offscreen`).

## 2. How to run things while developing

| What | Command |
| --- | --- |
| Desktop app (tray + dashboard) | `python -m app.main --gui` |
| Head-less agent | `python -m app.main --monitor` |
| Deterministic 40-min demo (fake machine + fake clock) | `python -m app.main --simulate` |
| Status / machine-readable status | `python -m app.main --status` / `--status-json` |
| Security audit | `python -m app.main --security` |
| Browser-link end-to-end test (real socket) | `python -m app.main --ipc-selftest` |
| Game-detector end-to-end proof (21 steps) | `python -m app.main --detector-selftest` |
| Regenerate dashboard screenshots | `python -m app.main --gui-shot` |

The simulator (`app/cli/simulate.py`) drives the *same* `AgentService` the GUI
runs, over a `FakeProcessSource` and `FakeClock` — use it to see enforcement
happen without waiting on real time.

## 3. Where things live (the 30-second map)

```
app/main.py           CLI wiring (argparse -> cmd_* functions)
app/service.py        AgentService: the one owner of lock+DB+monitor+IPC
app/core/
  rules/              Rule model + validation, matching, the tick engine
  tracking/           Tracker: monotonic sessions, daily aggregates, recovery
  scheduling/         Schedule parsing + evaluation
  enforcement/        state machine (pure) -> policy (pure) -> adapters (I/O)
  monitoring/         process snapshots, activity resolution, MonitorLoop
  detection/          detector ABC, registry, supervised host, plugins,
                      games/{league_of_legends,valorant,repo,teamfight_tactics}
  security/           Phase 7: lifecycle audit, usage floors, BypassWatch
  pause.py clock.py   time-boxed pause; Clock/FakeClock
app/database/         schema.sql (DDL) + db.py (ALL SQL lives here)
app/ipc/              WS server, protocol validation, browser state, bridge
app/windows/          Win32 adapters: foreground, power, startup, watchdog
app/ui/viewmodel.py   Qt-free presentation logic (every word the UI shows)
app/ui/qt/            widgets/dialogs/tray — thin over the viewmodel
app/testing/fakes.py  FakeProcessSource/Controller — used by tests AND --simulate
tests/                542 tests; naming: test_<module or feature>.py
browser-extension/    MV3 extension (Chrome/Edge first)
docs/                 architecture + per-phase records + user guides
packaging/ installer/ scripts/   Phase 9 build tooling
```

Dependency direction (enforced by convention, watch it in review):
`ui/qt -> ui/viewmodel -> service -> core -> database`. `windows/` and `ipc/`
are adapters called by core services, never the reverse. Pure logic (state
machine, policy, matching, scheduling) never imports I/O modules — that is
what makes 542 fast tests possible.

## 4. Test strategy

- **Pure logic** (state machine, policy, matching, schedule, floors, detector
  verdicts) gets table-style unit tests on `FakeClock`.
- **Orchestration** (engine, monitor loop, service) is tested with the fakes
  in `app/testing/fakes.py` — the same fakes `--simulate` drives, so tests
  exercise the shipping path.
- **Real I/O** gets exactly one end-to-end test each, run on demand:
  `--ipc-selftest` (real WebSocket), `--detector-selftest` (real detector
  host; its CI assertion lives in `tests/test_detector_selftest.py`).
- Conventions to keep: tests never sleep (fake clocks only); tests never kill
  real processes (fakes only); every bug fix lands with the test that failed.

## 5. Recipe: add a game detector

1. Create `app/core/detection/games/<game>.py` with a subclass of
   `GameSessionDetector` (see `valorant.py` for the fail-safe patterns):
   - **Decide the honest verdict table first** — research the real processes
     and signals (menu vs match!), and encode uncertainty as
     `confidence < 1.0`. The engine can only act on `(False, 1.0)`.
   - Any optional evidence (logs/API) may *delay* enforcement, never cause it;
     wrap it so failure = `None`.
   - Reuse the shared `settle_seconds` window for reconnect safety.
2. Register it in `builtin_detectors()` and `games/__init__.py`; if the game
   shares processes with another (TFT/LoL), verify the fail-safe merge in
   `GameSessionGuard.for_rule` covers your case (test in `test_game_guard.py`).
3. If it needs signals, wire them in `loader._builtin_with` (kept `None` when
   the setting disables them).
4. Tests: one file mirroring `test_valorant_detector.py` — every row of your
   verdict table plus crash/hang/quarantine. Add steps to
   `app/cli/detector_selftest.py` and update `docs/PHASE8.md` §4's status
   table honestly (automation ≠ live-game verification).
5. Document the detector (module docstring verdict table + PHASE8/LIMITATIONS
   notes) and add a caveat string in `rule_editor.PRESET_CAVEATS` if "wait"
   has a coarse meaning for your game.

## 6. Recipe: add a field to rules

1. `app/core/rules/models.py` — add the typed field + `validate()` rule.
2. `app/database/schema.sql` — additive column (or new table); bump
   `SCHEMA_VERSION` in `db.py` if anything beyond `CREATE TABLE IF NOT EXISTS`
   changes, and write the migration in `Database._migrate`.
3. `Rule.from_row` / add/update SQL in `db.py`.
4. Surface it: state machine input if it changes enforcement, `RuleRow` +
   `viewmodel` text, rule editor widget, `--status-json` projection.
5. Tests in `test_rules.py` (validation), `test_engine.py` (behaviour),
   `test_database.py` (persistence round-trip).

## 7. House rules

- Type hints everywhere; docstrings explain *why*, module docstrings carry the
  design/verdict tables.
- All SQL stays in `app/database/db.py`; all user-visible strings in the
  viewmodel; all security-sensitive decisions centralized (kill policy, guard).
- Fail-safe beats clever: when unsure, prefer the branch that *waits*, logs,
  and moves on. Never let an optional feature's exception break the monitor
  tick — that pattern (`try/except: log + degrade`) is everywhere on purpose.
- Keep files focused; if a module passes ~500 lines, split by responsibility.
- Update the docs in the same commit as behaviour changes (LIMITATIONS
  especially — it is the honesty contract).

## 8. Release checklist

1. `pytest -q` green; `--detector-selftest` 21/21; `--ipc-selftest` green.
2. Bump `app/__version__` (the installer/build pick it up automatically).
3. `python -m app.main --gui-shot` — refresh `docs/screenshots/` if the UI
   changed.
4. `scripts/build_windows.ps1` (Windows) — produces the portable zip +
   installer inputs; smoke checks run inside.
5. Update `docs/PHASE*.md` status lines and this file if conventions moved.
