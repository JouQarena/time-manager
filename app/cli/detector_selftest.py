"""`python -m app.main --detector-selftest` — proof that game detection is safe.

Runs the *real* detector stack (loader → host → LeagueOfLegendsDetector → guard
→ state machine → executor) against a scripted machine and a fake game
environment, and prints every step:

    1. loader           built-in detector registered with its known exes
    2. plugin dir       a drop-in plugin loads (source=plugin)
    3. bad plugin       rejected with a reason, the good ones unaffected
    4. crashing plugin  verdict becomes unknown, error counted
    5. wedged plugin    probe abandoned at the budget, quarantined after N
    6. launcher only    decisive "no match" -> the engine may act
    7. live match       decisive "in session" -> WAIT, never kill
    8. reconnect        process gone, inside the settle window -> unknown -> WAIT
    9. champ select     client log says ChampSelect -> WAIT
   10. live API         real local socket answers -> evidence, and its absence
                        without a visible process still -> WAIT
   11. log freshness    game logs being written -> WAIT
   12. full loop        limit hit mid-match: WAITING -> (settle) -> ENFORCED
   13. audit            the enforcement log says what actually happened
   14. VALORANT         menu == match (one process) -> WAIT; launcher -> decisive
   15. R.E.P.O.         running -> WAIT (documented coarseness); gone -> enforce
   16. TFT              shared match process never kills the wrong game; a
                        verified TFT mode is a confident WAIT
   17. shared routing   the real guard merges LoL+TFT verdicts fail-safe

Runs on any OS: the machine is the fake process source and the "game" is a
scripted set of exes plus a temporary log directory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.core.clock import FakeClock
from app.core.detection.base import SessionProbe, SessionVerdict
from app.core.detection.games.league_of_legends import LeagueOfLegendsDetector
from app.core.detection.games.signals import ClientPhaseReader, GameLogWatcher, LiveClientApi
from app.core.detection.host import DetectorHost
from app.core.detection.loader import build_host
from app.core.enforcement.adapters import EnforcementExecutor, RecordingNotifier
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.monitor import MonitorLoop
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.engine import RuleEngine
from app.core.rules.models import Rule
from app.core.tracking.tracker import Tracker
from app.core.types import Action, Mode, RuleState, RuleType
from app.database.db import Database
from app.testing.fakes import FakeProcessController, FakeProcessSource

GAME = "league of legends.exe"
CLIENT = "leagueclientux.exe"

PLUGIN_OK = '''
from app.core.detection.base import GameSessionDetector, SessionVerdict


class MinecraftDetector(GameSessionDetector):
    detector_id = "minecraft"
    display_name = "Minecraft"
    known_executables = ("javaw.exe",)

    def probe(self, snapshot):
        return SessionVerdict(in_session=False, confidence=1.0, detail="not a match")
'''

PLUGIN_CRASH = '''
def create():
    raise RuntimeError("this plugin is broken on purpose")
'''


class SlowDetector(LeagueOfLegendsDetector):
    """The built-in, artificially wedged for the timeout step."""

    detector_id = "slow_league"
    display_name = "League of Legends (slow)"

    def probe(self, snapshot: SessionProbe) -> SessionVerdict:
        time.sleep(1.0)
        return super().probe(snapshot)


@dataclass
class Step:
    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"  [{'PASS' if self.ok else 'FAIL'}] {self.name:<32} {self.detail}"


def _live_api_server(payload: dict):
    """A real local HTTP endpoint standing in for Riot's Live Client API.

    Like the real one, it only answers while a match is running: 503 otherwise.
    """
    state = {"live": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if not state["live"]:
                self.send_error(503, "no live game")
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.live_state = state  # type: ignore[attr-defined]
    return server


def _env(tmp: Path, clock: FakeClock):
    """A scripted machine with a League client, plus log/API signal providers."""
    logs = tmp / "Riot Games" / "League of Legends" / "Logs"
    (logs / "GameLogs").mkdir(parents=True, exist_ok=True)
    client_log = logs / "LeagueClientUx.log"
    client_log.write_text('2026-09-21 20:00:00 | info | "phase":"Lobby"\n')
    game_log = logs / "GameLogs" / "2026-09-21T20-00-00.log"

    server = _live_api_server({"gameData": {"gameTime": 300.0}})
    api = LiveClientApi(scheme="http", port=server.server_address[1], timeout=0.3,
                        cache_seconds=0)  # deterministic steps
    # Freshness is judged on the fake clock so the script controls it.
    watcher = GameLogWatcher(dirs=[logs], fresh_seconds=20, cache_seconds=0,
                             wall_clock=clock.wall)
    reader = ClientPhaseReader(dirs=[logs], cache_seconds=0)
    return {
        "dir": tmp, "logs": logs, "client_log": client_log, "game_log": game_log,
        "server": server, "api": api, "watcher": watcher, "reader": reader,
    }


def _probe_verdict(host: DetectorHost, detector_id: str, probe: SessionProbe) -> SessionVerdict:
    managed = host.managed(detector_id)
    assert managed is not None
    return managed.probe(probe)


def _snapshot(exes, clock: FakeClock, foreground: str | None = None) -> SystemSnapshot:
    processes = tuple(ProcessInfo(pid=100 + i, exe=exe)
                      for i, exe in enumerate(sorted(exes)))
    fg_pid = None
    if foreground is not None:
        fg_pid = next((p.pid for p in processes if p.exe == foreground), None)
    return SystemSnapshot(processes=processes, foreground_pid=fg_pid,
                          captured_mono=clock.mono())


def _lol_rule() -> Rule:
    rule = Rule(
        name="League of Legends", type=RuleType.GAME, target=CLIENT, executable=CLIENT,
        extra_executables=(GAME,), daily_limit_seconds=10 * 60,
        action=Action.WAIT_FOR_SESSION_END, mode=Mode.NORMAL,
    )
    rule.id = 1
    return rule


def run(verbose: bool = True) -> dict:
    steps: list[Step] = []
    # Plugin failures are expected here and are reported as steps, not tracebacks.
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)-7s %(name)s: %(message)s")

    with tempfile.TemporaryDirectory(prefix="tm-detectors-") as tmpdir:
        tmp = Path(tmpdir)
        plugin_dir = tmp / "detectors"
        plugin_dir.mkdir()
        (plugin_dir / "minecraft.py").write_text(PLUGIN_OK)
        (plugin_dir / "broken.py").write_text(PLUGIN_CRASH)

        clock = FakeClock()
        clock.set_wall(datetime(2026, 9, 21, 20, 0))
        env = _env(tmp, clock)

        # ---------------------------------------------------------------- 1
        host, report = build_host(
            plugin_dir=plugin_dir, timeout_ms=1000, max_failures=3,
            builtins=[LeagueOfLegendsDetector(
                live_api=env["api"], log_watcher=env["watcher"], phase_reader=env["reader"],
                settle_seconds=30.0, clock=clock.mono,
            )],
        )
        lol = host.managed("league_of_legends")
        steps.append(Step(
            "built-in detector loaded",
            lol is not None and "league_of_legends" in report.accepted
            and "league of legends.exe" in lol.known_executables,
            f"{len(host.ids())} detector(s): {', '.join(host.ids())}",
        ))

        # ---------------------------------------------------------------- 2
        minecraft = host.managed("minecraft")
        steps.append(Step(
            "plugin loaded from disk",
            minecraft is not None and minecraft.stats.source == "plugin"
            and minecraft.stats.origin.endswith("minecraft.py"),
            f"minecraft (plugin) from {Path(minecraft.stats.origin).name}" if minecraft else "missing",
        ))

        # ---------------------------------------------------------------- 3
        rejected = report.rejected[0] if report.rejected else ("", "")
        steps.append(Step(
            "broken plugin rejected",
            "broken on purpose" in rejected[1] and len(host.ids()) == 2,
            f"{Path(rejected[0]).name}: {rejected[1][:48]}",
        ))

        # ---------------------------------------------------------------- 4
        class Crashing(LeagueOfLegendsDetector):
            detector_id = "crashing_league"
            display_name = "League (crashing)"

            def probe(self, snapshot):
                raise RuntimeError("detector exploded on purpose")

        crash_host = DetectorHost(timeout_ms=1000, max_failures=3)
        crash_host.register(Crashing(clock=clock.mono))
        verdicts = [
            _probe_verdict(crash_host, "crashing_league", SessionProbe(
                running_exes=frozenset({GAME}), hints={}))
            for _ in range(4)   # 3 failures trip the budget, the 4th is refused
        ]
        stats = crash_host.managed("crashing_league").stats
        steps.append(Step(
            "crash -> unknown + quarantine",
            all(v.confidence == 0.0 for v in verdicts) and verdicts[0].in_session is True
            and stats.errors == 3 and stats.quarantined
            and "quarantined" in verdicts[-1].detail,
            f"{stats.errors} error(s), quarantined={stats.quarantined}, "
            f"last='{verdicts[-1].detail[:34]}'",
        ))

        # ---------------------------------------------------------------- 5
        slow_host = DetectorHost(timeout_ms=100, max_failures=2)
        slow = SlowDetector(clock=clock.mono)
        managed_slow = slow_host.register(slow)
        verdicts = [
            managed_slow.probe(SessionProbe(running_exes=frozenset({GAME}), hints={}))
            for _ in range(3)
        ]
        stats = managed_slow.stats
        steps.append(Step(
            "hang -> budget + quarantine",
            all(v.confidence == 0.0 for v in verdicts) and stats.quarantined
            and stats.timeouts >= 2 and stats.abandoned == 1,
            f"{stats.timeouts} failure(s) then quarantined; "
            f"{stats.abandoned} thread abandoned",
        ))

        # ---------------------------------------------------------------- 6
        probe_client = SessionProbe(running_exes=frozenset({CLIENT}), hints={})
        verdict = _probe_verdict(host, "league_of_legends", probe_client)
        steps.append(Step(
            "launcher only -> decisive",
            verdict.in_session is False and verdict.confidence == 1.0,
            f"'{verdict.detail}' (confidence {verdict.confidence:.2f})",
        ))

        # ---------------------------------------------------------------- 7
        env["server"].live_state["live"] = True  # kick-off: the API starts answering
        probe_game = SessionProbe(running_exes=frozenset({GAME, CLIENT}), hints={})
        verdict = _probe_verdict(host, "league_of_legends", probe_game)
        steps.append(Step(
            "live match -> WAIT",
            verdict.in_session is True and verdict.confidence == 1.0
            and "live game data" in verdict.detail,
            f"'{verdict.detail}'",
        ))

        # ---------------------------------------------------------------- 8
        clock.advance(5)
        verdict = _probe_verdict(host, "league_of_legends", probe_client)
        steps.append(Step(
            "reconnect window -> WAIT",
            verdict.in_session is True and verdict.confidence == 0.0
            and "reconnect window" in verdict.detail,
            f"'{verdict.detail[:60]}'",
        ))

        # ---------------------------------------------------------------- 9
        champ_dir = tmp / "champ-select-logs"   # a separate log: the shared one
        champ_dir.mkdir()                       # must keep reporting "Lobby"
        (champ_dir / "LeagueClientUx.log").write_text(
            '2026-09-21 20:10:00 | info | Gameflow phase: ChampSelect\n')
        fresh_reader = ClientPhaseReader(dirs=[champ_dir], cache_seconds=0)
        champ_host = DetectorHost(timeout_ms=1000)
        champ = LeagueOfLegendsDetector(phase_reader=fresh_reader, live_api=None,
                                        log_watcher=None, clock=clock.mono)
        champ_host.register(champ)
        verdict = _probe_verdict(champ_host, "league_of_legends", probe_client)
        steps.append(Step(
            "champ select -> WAIT",
            verdict.in_session is True and verdict.confidence == 0.5
            and "champselect" in verdict.detail,
            f"'{verdict.detail}'",
        ))

        # ---------------------------------------------------------------- 10
        api_verdict = LiveClientApi(scheme="http", port=env["server"].server_address[1],
                                    timeout=0.3, cache_seconds=0).all_game_data()
        api_host = DetectorHost(timeout_ms=1000)
        api_detector = LeagueOfLegendsDetector(
            live_api=LiveClientApi(scheme="http", port=env["server"].server_address[1],
                                   cache_seconds=0),
            log_watcher=None, phase_reader=None, clock=clock.mono,
        )
        api_host.register(api_detector)
        verdict = _probe_verdict(api_host, "league_of_legends", probe_client)
        steps.append(Step(
            "live API over a real socket",
            api_verdict is not None and verdict.confidence == 0.5
            and "live game data" in verdict.detail,
            f"payload seen, verdict: '{verdict.detail[:52]}'",
        ))

        # ---------------------------------------------------------------- 11
        env["game_log"].write_text("game is running\n")
        stamp = clock.wall()
        os.utime(env["game_log"], (stamp, stamp))  # "written just now" on the fake clock
        watch_host = DetectorHost(timeout_ms=1000)
        watch_host.register(LeagueOfLegendsDetector(
            live_api=None, log_watcher=GameLogWatcher(
                dirs=[env["logs"]], cache_seconds=0,
                wall_clock=clock.wall,  # judge freshness on the fake clock:
                # the file above is stamped with it; the machine's real time
                # of day must never matter (found on real Windows hardware).
            ),
            phase_reader=None, clock=clock.mono))
        verdict = _probe_verdict(watch_host, "league_of_legends", probe_client)
        steps.append(Step(
            "fresh game logs -> WAIT",
            verdict.confidence == 0.5 and "logs" in verdict.detail,
            f"'{verdict.detail}'",
        ))

        # ---------------------------------------------------------------- 12
        db = Database(tmp / "detector-selftest.db").connect()
        rule = _lol_rule()
        db.add_rule(rule)
        rules = db.list_rules()
        rid = rules[0].id
        source = FakeProcessSource()
        source.clock = clock
        controller = FakeProcessController(source)
        tracker = Tracker(db, clock)
        engine = RuleEngine(db, tracker, clock)
        closer = ProcessCloser(controller, graceful_timeout=2.0)
        executor = EnforcementExecutor(db, closer, clock, policy=KillPolicy(),
                                       notifier=RecordingNotifier())
        guard = GameSessionGuard(host.registry)
        resolver = ActivityResolver(
            guard=guard, policy=ActivityPolicy(background_grace_seconds=0),
        )
        loop = MonitorLoop(db, engine, tracker, resolver, executor, source, clock,
                           closer=closer, guard=guard, interval=5.0)

        # Remember process names as they appear: a closed process is gone from
        # the fake table, so look them up while they exist.
        seen: dict[int, str] = {}

        def tick() -> None:
            for process in source.processes:
                seen[process.pid] = process.exe
            loop.tick_once()

        source.launch(CLIENT)
        source.focus(CLIENT)
        tick()
        source.launch(GAME)          # kick-off: the match process appears
        source.focus(GAME)
        for _ in range(130):         # 650 s of match time, limit is 600 s
            clock.advance(5)
            tick()
        mid_match = db.get_state(rid)

        steps.append(Step(
            "limit hit mid-match -> WAIT",
            mid_match is not None and mid_match[1] is RuleState.WAITING_FOR_SESSION_END
            and not controller.requests,
            f"state {mid_match[1].value if mid_match else '?'}, "
            f"{len(controller.requests)} close request(s) so far",
        ))

        # Match over: the match process exits and Riot's API stops answering.
        source.kill(source.pid_of(GAME))
        env["server"].live_state["live"] = False

        for _ in range(4):           # 20 s, inside the 30 s settle window
            clock.advance(5)
            tick()
        during_settle = db.get_state(rid)
        steps.append(Step(
            "settle window -> WAIT",
            during_settle is not None
            and during_settle[1] is RuleState.WAITING_FOR_SESSION_END
            and not controller.requests,
            f"state {during_settle[1].value if during_settle else '?'} after 20 s, "
            "no close request",
        ))

        for _ in range(6):           # past the settle window: lobby is safe
            clock.advance(5)
            tick()
        final = db.get_state(rid)
        rows = [dict(r) for r in db.list_enforcement(limit=10)]
        closed = controller.requests
        steps.append(Step(
            "settled lobby -> ENFORCED",
            final is not None and final[1] is RuleState.ENFORCED and bool(closed),
            f"state {final[1].value if final else '?'}, "
            f"{len(closed)} close request(s), {len(rows)} audit row(s)",
        ))

        # ---------------------------------------------------------------- 13
        audit = " | ".join(
            f"{r['action']}:{r['outcome']}"
            + (f" ({r['detail'][:40]})" if r["detail"] else "") for r in rows[:3]
        )
        steps.append(Step(
            "audit trail explains it",
            bool(rows) and any(r["outcome"] in {"EXECUTED", "SKIPPED", "DEFERRED"} for r in rows),
            audit or "no rows",
        ))

        detector_status = host.status()
        steps.append(Step(
            "status surface is honest",
            bool(detector_status) and all("quarantined" in item for item in detector_status),
            f"{len(detector_status)} detector(s) reported, "
            f"quarantined={host.summary()['quarantined']}",
        ))

        # ------------------------------------------------- Phase 8: new games
        from app.core.detection.games.repo import RepoDetector
        from app.core.detection.games.teamfight_tactics import TftDetector
        from app.core.detection.games.valorant import ValorantDetector

        val_host = DetectorHost(timeout_ms=1000)
        val_host.register(ValorantDetector(clock=clock.mono, log_watcher=None))
        val_menu = _probe_verdict(val_host, "valorant", SessionProbe(
            running_exes=frozenset({"valorant-win64-shipping.exe",
                                    "riotclientservices.exe"}), hints={}))
        clock.advance(31)  # past the settle window: the game is really closed
        val_launcher = _probe_verdict(val_host, "valorant", SessionProbe(
            running_exes=frozenset({"riotclientservices.exe"}), hints={}))
        steps.append(Step(
            "VALORANT menu == match -> WAIT",
            val_menu.in_session is True and val_menu.confidence == 0.5
            and val_launcher.in_session is False and val_launcher.confidence == 1.0,
            f"game up: '{val_menu.detail[:44]}…' | launcher only: decisive",
        ))

        repo_host = DetectorHost(timeout_ms=1000)
        repo_host.register(RepoDetector(clock=clock.mono, log_watcher=None))
        repo_run = _probe_verdict(repo_host, "repo", SessionProbe(
            running_exes=frozenset({"repo.exe", "steam.exe"}), hints={}))
        clock.advance(31)  # past the settle window
        repo_gone = _probe_verdict(repo_host, "repo", SessionProbe(
            running_exes=frozenset({"steam.exe"}), hints={}))
        steps.append(Step(
            "R.E.P.O. running -> WAIT, gone -> enforce",
            repo_run.in_session is True and repo_run.confidence == 0.5
            and repo_gone.in_session is False and repo_gone.confidence == 1.0,
            f"running: '{repo_run.detail[:40]}…' | closed: '{repo_gone.detail}'",
        ))

        class TftApi:
            def __init__(self, mode):
                self._mode = mode

            def probe(self):
                return {"gameData": {"gameMode": self._mode}}

        tft_host = DetectorHost(timeout_ms=1000)
        tft_host.register(TftDetector(live_api=TftApi("CLASSIC"), clock=clock.mono))
        tft_shared = _probe_verdict(tft_host, "teamfight_tactics", SessionProbe(
            running_exes=frozenset({GAME, CLIENT}), hints={}))
        tft_host2 = DetectorHost(timeout_ms=1000)
        tft_host2.register(TftDetector(live_api=TftApi("TFT"), clock=clock.mono))
        tft_verified = _probe_verdict(tft_host2, "teamfight_tactics", SessionProbe(
            running_exes=frozenset({GAME, CLIENT}), hints={}))
        steps.append(Step(
            "TFT shared process: never kills the wrong game",
            tft_shared.in_session is True and tft_shared.confidence == 0.5
            and tft_verified.in_session is True and tft_verified.confidence == 1.0,
            f"mode CLASSIC -> WAIT 0.5; mode TFT -> '{tft_verified.detail[:32]}…'",
        ))

        # Shared-exe routing through the REAL guard: a TFT rule probing a live
        # League match (mode unknown) must WAIT, never close the process.
        guard_registry = host.registry
        lol_builtin = guard_registry.get("league_of_legends")
        lol_builtin.settle_seconds = 30.0  # ensure deterministic state
        tft_rule = Rule(
            name="Teamfight Tactics", type=RuleType.GAME, target=CLIENT,
            executable=CLIENT, extra_executables=(GAME,),
            daily_limit_seconds=10 * 60, action=Action.WAIT_FOR_SESSION_END,
            mode=Mode.NORMAL,
        )
        tft_rule.id = 99
        from app.core.enforcement.game_guard import GameSessionGuard as _Guard

        merged = _Guard(guard_registry).for_rule(
            tft_rule, _snapshot({GAME, CLIENT}, clock, foreground=GAME))
        steps.append(Step(
            "shared-exe guard merge -> WAIT",
            merged.in_session is True,
            f"verdict: in_session={merged.in_session}, confident={merged.confident}, "
            f"'{merged.detail[:48]}…'",
        ))

        # --------------------------------------------------------- summary bits
        steps.append(Step(
            "no detector left unhealthy",
            not host.unhealthy() and crash_host.unhealthy() == ["crashing_league"],
            f"main host clean, crash host quarantined: {crash_host.unhealthy()}",
        ))

        db.close()
        env["server"].shutdown()
        env["server"].server_close()

        report_out = {
            "ok": all(step.ok for step in steps),
            "steps": steps,
            "enforcement": rows,
            "mid_match_state": mid_match[1].value if mid_match else None,
            "final_state": final[1].value if final else None,
            "close_requests": [{"pid": pid, "exe": seen.get(pid)} for pid in controller.requests],
            "detector_status": detector_status,
            "plugin_report": report.to_dict(),
        }

        if verbose:
            print("Game-detector end-to-end selftest (real detector, scripted machine)\n")
            for step in steps:
                print(step.render())
            print()
            passed = sum(1 for step in steps if step.ok)
            print(f"{passed}/{len(steps)} steps passed"
                  f"{' — ALL PASS' if report_out['ok'] else ' — FAILURES ABOVE'}")
            print(f"mid-match state={report_out['mid_match_state']} "
                  f"final state={report_out['final_state']} "
                  f"closes={len(closed)}")
            print("enforcement rows:")
            for row in rows[:5]:
                print(f"    {row['action']:<18} {row['outcome']:<9} {row['detail'] or ''}")
            print("detectors:")
            for item in detector_status:
                print(f"    {item['id']:<20} calls={item['calls']:<4} errors={item['errors']:<3} "
                      f"timeouts={item['timeouts']:<3} quarantined={item['quarantined']}")

        return report_out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="game-detector end-to-end selftest")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    result = run(verbose=not args.json)
    if args.json:
        print(json.dumps({
            "ok": result["ok"],
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in result["steps"]],
            "mid_match_state": result["mid_match_state"],
            "final_state": result["final_state"],
            "detectors": result["detector_status"],
        }, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
