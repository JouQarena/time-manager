# Phase 6 — Game detectors (`LeagueOfLegendsDetector` first)

**Status: complete.** 448 tests pass (`342` from Phases 1–5, `106` new here) and
`--detector-selftest` reports **17/17 ALL PASS**. Version `0.6.0`.

Phase 6 answers one question safely: *is the user in a live match right now?*
Everything in it is built around a single non-negotiable rule —

> **Never terminate a game unless we are sure no match is running.**
> Anything uncertain becomes *unknown*, and unknown means **wait**.

---

## 1. What was built

| File | Purpose |
| --- | --- |
| `app/core/detection/games/league_of_legends.py` | `LeagueOfLegendsDetector`: the verdict table, fail-safe by construction |
| `app/core/detection/games/signals.py` | Local evidence: `LiveClientApi` (Riot's `127.0.0.1:2999`), `GameLogWatcher`, `ClientPhaseReader` |
| `app/core/detection/host.py` | `DetectorHost` / `ManagedDetector`: probe budget, error counting, quarantine, status |
| `app/core/detection/loader.py` | Drop-in plugin loading + validation; `host_from_settings()` |
| `app/cli/detector_selftest.py` | `--detector-selftest`: end-to-end proof on a scripted machine |
| `app/ui/qt/rule_editor.py` | "Known game" presets straight from the registry (no guessing exe names) |
| `app/service.py`, `app/cli/monitor.py` | Real detectors wired into the agent (service, CLI, snapshot, GUI panel) |

The interface (`GameSessionDetector.probe`, `SessionProbe`, `SessionVerdict`)
and the guard (`GameSessionGuard`) already existed from Phases 1/3 — Phase 6
supplies the plugins, the host, the loader and the acceptance test.

## 2. The verdict table (what actually decides)

| Observation | Verdict | Effect |
| --- | --- | --- |
| `League of Legends.exe` running (that process only exists during loading/in-game/reconnect) | in session, **confidence 1.0** | WAIT |
| Match process gone for less than `game_settle_seconds` (30 s default) | unknown, **0.0** | WAIT |
| Riot's Live Client API answers although no match process is visible | in session, **0.5** | WAIT |
| Client log's last phase is `ChampSelect` / `GameStart` / `InProgress` / `Reconnect` / … | in session, **0.5** | WAIT |
| `Logs/GameLogs/*.log` still being written (match logs only) | in session, **0.5** | WAIT |
| Client up, no match process for longer than the settle window, nothing else happening | not in session, **1.0** | enforce |
| No Riot process at all | not in session, **1.0** | enforce |

Only the last two rows let the engine act. Evidence can only ever *delay*
enforcement — never cause it. The reasoning behind the settle window is the
reconnect case: a player whose match process crashes and reconnects has *no*
`League of Legends.exe` for a few seconds, and killing the client there loses the
game.

Two things the selftest caught, worth recording:

* **a fresh client log is not match evidence.** The League *client* log is
  written continuously, in the lobby as well — treating its freshness as
  "in a match" would stall a WAIT rule forever. Only `GameLogs/` files count.
* **a fake Live Client API that answered while `live: false`** looked
  "corroborating" but lied; the stand-in now mirrors Riot (503 outside a match).

## 3. Plugin safety (`DetectorHost`)

Plugins are code, and code misbehaves. A misbehaving detector must degrade into
"wait", never into "kill":

| Plugin behaviour | Host response | Enforcement effect |
| --- | --- | --- |
| returns a verdict | pass through, count, record stats | normal |
| raises | count the error, return unknown | WAIT |
| hangs past the probe budget (300 ms default) | abandon the thread (daemonised), count the timeout, return unknown | WAIT |
| hangs *and* is called again | one thread only; further calls answered as failures instead of piling up | WAIT |
| keeps failing (`game_plugin_max_failures`, default 5) | **quarantine** the plugin; it is no longer called | WAIT + visible warning |
| returns a non-`SessionVerdict` | treated as a failure | WAIT |

Quarantine is visible everywhere: `--status-json`, the dashboard's *Game
detectors* panel, and the log. `host.release()` clears it.

*(Bug found while testing: `probe()` held a plain `Lock` while calling `_fail()`,
which takes the same lock — the first detector failure deadlocked the monitoring
thread. It is an `RLock` now; there is a regression test.)*

## 4. Drop-in plugins

Built-ins plus any `*.py` in `<profile>/detectors` (overridable with the
`detector_plugins_dir` setting). A plugin file exposes a detector as either

```python
def create() -> GameSessionDetector: ...     # preferred
DETECTORS = [MyDetector()]                   # or instances
class MyDetector(GameSessionDetector): ...   # or just define the class
```

and it must implement `GameSessionDetector` with a non-empty `detector_id`,
lowercase `known_executables`, and survive a smoke probe on an empty snapshot.
Everything else is **rejected with a reason** that appears in the status payload
— a detector that is wrong in the "no match" direction gets a game killed, so
the bar is deliberately high, and a broken plugin never stops the good ones.

## 5. Where it is wired

* `AgentService.start()` builds the host from the settings, keeps the guard
  fail-safe (`self.detectors.enabled`), and surfaces
  `detectors{enabled,count,quarantined,timeout_ms,items[],rejected[]}` in
  `snapshot()` → `--status-json` and the dashboard.
* `--monitor` builds the *same* host (one code path, `host_from_settings`), so a
  head-less agent and the desktop app cannot disagree about a match.
* Settings (per profile): `game_detector_enabled`, `game_settle_seconds`,
  `game_probe_timeout_ms`, `game_plugin_max_failures`, `game_live_api_enabled`,
  `game_live_api_port`, `game_log_scan_enabled`, `game_log_fresh_seconds`,
  `game_log_dirs`, `detector_plugins_dir`.
* The rule editor's **Known game** list is generated from the registry, fills
  the client exe as the target and the rest as "also match", and explains what
  the detector will do.

## 6. Verification

```
$ python -m pytest -q
448 passed

$ python -m app.main --detector-selftest
  [PASS] built-in detector loaded         2 detector(s): league_of_legends, minecraft
  [PASS] plugin loaded from disk          minecraft (plugin) from minecraft.py
  [PASS] broken plugin rejected           broken.py: create() raised: RuntimeError: this plugin is br
  [PASS] crash -> unknown + quarantine    3 error(s), quarantined=True
  [PASS] hang -> budget + quarantine      2 failure(s) then quarantined; 1 thread abandoned
  [PASS] launcher only -> decisive        'launcher only — no match process for over 30s' (1.00)
  [PASS] live match -> WAIT               'match process is running (live game data)'
  [PASS] reconnect window -> WAIT         'match process exited 5s ago — waiting out the reconnect wind'
  [PASS] champ select -> WAIT             'client phase champselect — match is being set up'
  [PASS] live API over a real socket      payload seen over a real local socket
  [PASS] fresh game logs -> WAIT          'game logs are still being written'
  [PASS] limit hit mid-match -> WAIT      state WAITING_FOR_SESSION_END, 0 close request(s)
  [PASS] settle window -> WAIT            state WAITING_FOR_SESSION_END after 20 s
  [PASS] settled lobby -> ENFORCED        state ENFORCED, 1 close request(s), 3 audit row(s)
  [PASS] audit trail explains it          CLOSE_APP:EXECUTED (pids=1000)
  [PASS] status surface is honest         2 detector(s) reported, quarantined=0
  [PASS] no detector left unhealthy       main host clean, crash host quarantined: ['crashing_league']

17/17 steps passed — ALL PASS
```

New tests, file by file (`106`):

| File | Count | What it pins down |
| --- | --- | --- |
| `tests/test_detector_host.py` | 18 | budget, abandonment, quarantine, streak reset, wrong return type, duplicate ids, thread safety, guard-level "never a kill" |
| `tests/test_detector_loader.py` | 21 | three plugin forms, every rejection reason, broken file ≠ broken agent, duplicate ids, deterministic order |
| `tests/test_lol_detector.py` | 21 | the whole verdict table, settle window, signal degradation, hostile providers, guard+state-machine integration |
| `tests/test_game_signals.py` | 26 | Live Client API over a **real local HTTP server** (cache, 503, timeouts, bad payload), log freshness, client-phase parsing, tail-only reads, no sockets outside loopback |
| `tests/test_detector_selftest.py` | 13 | every selftest step passes, CLI exit codes/JSON, service wiring, profile plugin directory, `--status-json` detector block |
| `tests/test_viewmodel.py` (+3), `tests/test_qt_ui.py` (+4) | 7 | detector panel wording, quarantine visibility, "Known game" presets end to end |

## 7. Manual checklist (needs the real game)

Not run here — no Windows, no League of Legends, and Riot's API only exists
during a real match. Run on a Windows box, with the test account:

1. `python -m app.main --monitor` (or `--gui`), then launch League and sit in the
   lobby. `--status-json` → `detectors.items[0].last_detail` should read
   `launcher only …`. Confirm the dashboard's *Game detectors* panel agrees.
2. Rule: League, 1-minute daily limit, action *Wait for the match to end*.
   Queue a bot game. At 1 minute in-game: no close happens, state is
   `WAITING_FOR_SESSION_END`, notification says the match will not be interrupted.
3. Finish the match. Within ~30 s of the game process exiting the client is
   closed (or blocked, per the rule) — and the audit log shows exactly one
   `CLOSE_APP`. Confirm no close happened during the loading screen.
4. Alt-tab out and back mid-match: the WAIT state must survive (the detector does
   not use foreground state).
5. Kill `League of Legends.exe` with Task Manager mid-match, then let the client
   reconnect: no enforcement during the reconnect window (the settle window).
6. During champ select (before the game exe appears): state must be WAIT, with
   `phase champselect` in `last_detail`.
7. Delete/rename the client log dir and re-run step 2: the verdict falls back to
   the process table (still correct).
8. While the client is up, confirm the agent makes **no** network call other
   than `127.0.0.1:2999` (netstat / a firewall prompt): privacy ceiling intact.
9. Drop a plugin in `%APPDATA%\TimeManager\detectors\` — it appears in
   `--status-json` as `source: plugin`; break it on purpose and confirm it is
   listed under `rejected` with a reason while the LoL detector keeps working.

## 8. Known limits (Phase 6)

* The LoL detector's signals are **version-tolerant by design, not by contract**:
  Riot's log format and phase names can change, and the Live Client Data API is
  only present during a match. The process table is the ground truth; the extra
  signals only ever add caution.
* `game_settle_seconds` (30 s default) is a trade-off: too short risks closing a
  client during a reconnect, too long delays enforcement. It is a setting.
* A quarantined detector means its games are *never* interrupted until it is
  fixed — enforcement stays fail-safe, which is the intended direction, but the
  panel/`--status-json` must be checked (that is why it is surfaced loudly).
* Abandoned probe threads cannot be killed in Python; they are daemonised and
  the host refuses to spawn more, so a wedged plugin costs one thread, not a
  thread per tick.
* Detectors are a Phase 6 mechanism, not a Phase 6 promise about other games:
  the loader, host and fail-safe behaviour are game-agnostic, but only League of
  Legends ships a detector. Minecraft/Riot-adjacent plugins are examples.
