"""Deterministic end-to-end simulator: the whole Phase 3 pipeline on a fake
machine with a fake clock.

    python -m app.main --simulate            # 40 simulated minutes
    python -m app.main --simulate 20 --tick 5

It exercises the real objects — Database, Tracker, RuleEngine, ActivityResolver,
GameSessionGuard, KillPolicy, ProcessCloser, EnforcementExecutor, MonitorLoop
and the Phase 4 AgentBridge — replacing only the clock, the process table, the
OS close calls, and the browser extension (a simulated one feeds tab activity
and heartbeats exactly like the MV3 service worker does). The output doubles as
the end-to-end acceptance demo; tests/test_simulate.py asserts on the same run.

Scripted scenario (minute marks):
    0-10    Discord foreground                 -> warning at 7m/9m, CLOSE at 10m
    13      user relaunches Discord            -> closed again (limit still met)
    15-23   League match, detector confident   -> warn, limit hit mid-match,
            then detector becomes unsure       -> still WAITING (fail-safe)
            then match ends                    -> close; client ignores WM_CLOSE
                                                  -> escalation to FORCED
    25      user relaunches League (STRICT)    -> closed immediately
    27-31   user walks away (idle gate)        -> nothing counted
    31-34   YouTube tab active                 -> limit -> block deferred to
                                                  the browser extension
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.clock import FakeClock
from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict
from app.core.detection.registry import DetectorRegistry
from app.core.enforcement.adapters import EnforcementExecutor, RecordingNotifier
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.monitor import MonitorLoop
from app.core.rules.engine import RuleEngine
from app.core.rules.models import Rule
from app.core.tracking.tracker import Tracker
from app.core.types import Action, Mode, RuleType
from app.database.db import Database
from app.ipc.bridge import AgentBridge
from app.testing.fakes import FakeProcessController, FakeProcessSource

DEFAULT_MINUTES = 40
DEFAULT_TICK = 5


@dataclass(frozen=True)
class SimLine:
    minute: int
    rule: str
    used_seconds: int
    state: str
    action: str
    detail: str = ""

    def render(self) -> str:
        return (
            f"t+{self.minute:>2}m  {self.rule:<9} {self.used_seconds / 60:>6.1f}m  "
            f"{self.state:<24} {self.action:<22} {self.detail}"
        )


class SimulatedExtension:
    """Stands in for the MV3 extension: it is the broadcast sink and remembers
    what the agent pushed, so the demo can print the wire traffic."""

    def __init__(self) -> None:
        self.pushes: list[dict] = []

    def __call__(self, message: str) -> int:
        """Broadcast contract used by AgentBridge: return #clients reached."""
        self.pushes.append(json.loads(message))
        return 1

    def types(self) -> list[str]:
        return [message["type"] for message in self.pushes]

    def blocked_domains(self) -> list[str]:
        return [m["domain"] for m in self.pushes
                if m["type"] == "BLOCK_DECISION" and m["blocked"]]


class ScriptedGameDetector(GameSessionDetector):
    """Stand-in for Phase 6's LoL detector, driven by the simulator's script."""

    detector_id = "scripted"
    display_name = "Scripted game detector"
    known_executables = ("league of legends.exe",)

    def __init__(self) -> None:
        self.in_match = False
        self.sure = True  # False = "cannot tell right now" (fail-safe path)

    def probe(self, snapshot: SessionProbe) -> SessionVerdict:
        if not self.sure:
            return SessionVerdict(in_session=self.in_match, confidence=0.4,
                                  detail="scripted uncertainty")
        return SessionVerdict(
            in_session=self.in_match, confidence=1.0,
            detail="match running" if self.in_match else "not in a match",
        )


def build_demo_db(path: str | Path) -> tuple[Database, dict[str, int]]:
    db = Database(path).connect()
    ids = {
        "discord": db.add_rule(Rule(
            name="Discord", type=RuleType.APPLICATION, target="discord.exe",
            executable="discord.exe", daily_limit_seconds=10 * 60,
            warning_seconds=(180, 60), action=Action.CLOSE, mode=Mode.NORMAL)),
        "lol": db.add_rule(Rule(
            name="League", type=RuleType.GAME, target="League of Legends.exe",
            executable="league of legends.exe", daily_limit_seconds=6 * 60,
            warning_seconds=(180, 60), action=Action.WAIT_FOR_SESSION_END,
            mode=Mode.STRICT)),
        "yt": db.add_rule(Rule(
            name="YouTube", type=RuleType.WEBSITE, target="youtube.com",
            domain="youtube.com", daily_limit_seconds=3 * 60,
            warning_seconds=(60,), action=Action.BLOCK, mode=Mode.NORMAL)),
    }
    return db, ids


def run_simulation(
    minutes: int = DEFAULT_MINUTES,
    *,
    db_path: str | Path | None = None,
    tick_seconds: int = DEFAULT_TICK,
    verbose: bool = True,
) -> dict:
    keep_tmp = db_path is None
    tmpdir = tempfile.TemporaryDirectory(prefix="tm-sim-") if keep_tmp else None
    if db_path is None:
        assert tmpdir is not None
        db_path = Path(tmpdir.name) / "sim.db"
    db, ids = build_demo_db(db_path)

    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 9, 0))
    source = FakeProcessSource()
    source.clock = clock
    controller = FakeProcessController(source)
    tracker = Tracker(db, clock)
    engine = RuleEngine(db, tracker, clock)
    closer = ProcessCloser(controller, graceful_timeout=2 * tick_seconds)
    notifier = RecordingNotifier()
    extension = SimulatedExtension()
    bridge = AgentBridge(db, clock, token="sim", tracker=tracker)
    bridge.attach_broadcast(extension)
    executor = EnforcementExecutor(
        db, closer, clock, policy=KillPolicy(), notifier=notifier,
        browser_sink=bridge.browser_sink,  # website blocks -> simulated extension
    )

    detector = ScriptedGameDetector()
    registry = DetectorRegistry()
    registry.register(detector)
    guard = GameSessionGuard(registry)
    resolver = ActivityResolver(
        guard=guard,
        policy=ActivityPolicy(background_grace_seconds=5, idle_grace_seconds=120),
    )
    loop = MonitorLoop(db, engine, tracker, resolver, executor, source, clock,
                       closer=closer, guard=guard, interval=float(tick_seconds))

    loop.set_web_state_provider(bridge.web_state)
    loop.set_tick_hook(lambda report: bridge.on_tick(report.outcomes))

    BROWSER_ID = "simulated-extension-0001"
    web_tab_holder: dict[str, bool] = {"active": False}

    # ------------------------------------------------------------- the script
    source.launch("discord.exe")
    source.focus("discord.exe")
    lol_pid: int | None = None
    events: dict[int, str] = {}
    lines: list[SimLine] = []
    audit_markers: list[str] = []

    total_ticks = max(1, int(minutes * 60 / tick_seconds))
    for tick in range(total_ticks):
        elapsed = tick * tick_seconds
        minute = elapsed // 60

        # every 12m of sim time re-run the script hooks below
        if elapsed == 7 * 60:
            events[minute] = "Discord 3 min left"
        if elapsed == 9 * 60:
            events[minute] = "Discord 1 min left"
        if elapsed == 10 * 60:
            events[minute] = "Discord limit: closing gracefully"
        if elapsed == 13 * 60 and not source.running("discord.exe"):
            source.launch("discord.exe")
            source.focus("discord.exe")
            events[minute] = "user relaunches Discord"
        if elapsed == 15 * 60:
            lol_pid = source.launch("league of legends.exe")
            source.focus("league of legends.exe")
            detector.in_match = True
            events[minute] = "League match starts (detector confident)"
        if elapsed == 22 * 60:
            detector.sure = False
            events[minute] = "detector loses confidence -> must WAIT"
        if elapsed == 23 * 60:
            detector.sure = True
            detector.in_match = False
            controller.graceful_ok = False  # client ignores WM_CLOSE
            events[minute] = "match ends -> enforce; client ignores WM_CLOSE"
        if elapsed == 25 * 60:
            controller.graceful_ok = True
            if lol_pid is not None and not source.running("league of legends.exe"):
                source.launch("league of legends.exe")
                source.focus("league of legends.exe")
                events[minute] = "user relaunches League (STRICT, limit met)"
        if elapsed == 27 * 60:
            web_tab_holder["active"] = True
            source.launch("chrome.exe")
            source.focus("chrome.exe")   # the browser must be foreground too
            bridge.on_connect("chrome", BROWSER_ID)
            source.idle_seconds = 300.0
            events[minute] = "walks away with YouTube open (idle gate)"
        if elapsed == 31 * 60:
            source.idle_seconds = 0.0
            events[minute] = "back at the keyboard (YouTube keeps counting)"
        if elapsed == 34 * 60:
            events[minute] = "YouTube limit -> block pushed to the (fake) extension"

        if web_tab_holder["active"]:
            # The simulated extension behaves like the MV3 worker: a tab report
            # when the tab changes and a heartbeat every ~10 simulated seconds.
            bridge.on_tab_activity(
                "chrome", BROWSER_ID, tab_id=42, domain="youtube.com",
                active=True, window_focused=True, audible=False,
            )
            if tick % 2 == 0:
                bridge.on_heartbeat("chrome", BROWSER_ID)

        report = loop.tick_once()
        for outcome in report.outcomes:
            rid = outcome.rule.id
            acted = [
                r for r in report.records
                if r.rule_id == rid and r.outcome in ("EXECUTED", "FAILED", "PROTECTED", "DEFERRED")
            ]
            action_txt = "; ".join(f"{r.action}:{r.outcome}" for r in acted) or "-"
            lines.append(SimLine(
                minute=minute, rule=outcome.rule.name,
                used_seconds=outcome.today_used_seconds,
                state=outcome.decision.new_state.value, action=action_txt,
                detail=report.resolved_detail.get(rid, ""),
            ))
        for rec in report.records:
            if rec.outcome in ("EXECUTED", "FAILED", "DEFERRED") and rec.action != "NOTIFY_ONLY":
                audit_markers.append(f"t+{minute}m {rec.rule_name} {rec.action} {rec.outcome}")

        clock.advance(tick_seconds)

    if verbose:
        _print_report(db, loop, notifier, tracker, ids, lines, minutes, tick_seconds,
                      events, extension)

    return {
        "lines": lines, "db": db, "db_path": str(db_path), "ids": ids,
        "tracker": tracker, "loop": loop, "notifier": notifier, "closer": closer,
        "controller": controller, "source": source, "events": events,
        "audit_markers": audit_markers, "tmpdir": tmpdir,
        "bridge": bridge, "extension": extension,
    }


def _print_report(db, loop, notifier, tracker, ids, lines, minutes, tick_seconds, events,
                  extension=None) -> None:
    print(f"Simulated {minutes} min at {tick_seconds}s ticks "
          f"({len(lines)} rule-observations) — real Phase 3 code, fake machine.\n")
    print(f"  {'time':<7}{'rule':<10}{'used':>7}  {'state':<24}{'action':<22}why")
    print("  " + "-" * 118)
    last_seen: dict[str, tuple[str, str, str]] = {}
    announced: set[int] = set()
    for line in lines:
        if line.detail.startswith(("running in background", "not running")) and line.action == "-":
            continue  # quiet steady state: the interesting part is already shown
        signature = (line.state, line.action, line.detail)
        if last_seen.get(line.rule) == signature:
            continue
        last_seen[line.rule] = signature
        if line.minute in events and line.minute not in announced:
            announced.add(line.minute)
            print(f"  --- {line.minute}m: {events[line.minute]} ---")
        print("  " + line.render())
    print("\nEnforcement audit log (real `enforcement_log` rows; last 20):")
    rows = list(reversed(db.list_enforcement(limit=20)))
    for row in rows:
        print(f"  {row['day']}  rule={row['rule_id']:<2} {row['action']:<15}"
              f"{row['mode']:<8}{row['outcome']:<11}{row['detail'] or ''}")
    print(f"  ... {len(db.list_enforcement(limit=500))} rows total for the day")
    if extension is not None:
        print("\nMessages pushed to the simulated browser extension:")
        print(f"  {len(extension.pushes)} message(s) total: "
              f"{{{', '.join(sorted(set(extension.types())))}}}")
        blocked = extension.blocked_domains()
        if blocked:
            print(f"  BLOCK_DECISION {sorted(set(blocked))} -> blocked "
                  f"({len(blocked)} push(es); the extension keeps the block until told otherwise)")
        updates = [m for m in extension.pushes if m["type"] == "RULE_UPDATE"]
        if updates:
            print(f"  RULE_UPDATE    {len(updates)} payload(s), latest: "
                  f"{json.dumps(updates[-1]['website_rules'][:1])[:120]}")
    print("\nNotifications (real notifier calls):")
    for title, message, urgent in notifier.messages:
        print(f"  [{'URGENT' if urgent else ' info '}] {title}: {message}")
    print(f"\nStats: {loop.stats.ticks} ticks, {loop.stats.errors} errors, "
          f"{loop.stats.closes} process closes "
          f"(tick timings are fake-clock, so they read 0)")
    print("Totals: " + ", ".join(
        f"{name}={tracker.today_total(rid)}s" for name, rid in ids.items()))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Phase 3 deterministic simulator")
    parser.add_argument("minutes", nargs="?", type=int, default=DEFAULT_MINUTES)
    parser.add_argument("--tick", type=int, default=DEFAULT_TICK)
    args = parser.parse_args(argv)
    run_simulation(args.minutes, tick_seconds=args.tick)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
