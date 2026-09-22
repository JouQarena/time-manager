"""`python -m app.main --monitor` — the real agent loop, no GUI.

This is what Phase 3 ships for hands-on verification on Windows before the
Phase 7 tray/GUI exists. Ctrl-C stops it cleanly (tracker closed, DB flushed,
open sessions sealed).

On non-Windows hosts it refuses to start: foreground detection does not exist,
so the numbers would be fiction. Use `--simulate` there instead.
"""

from __future__ import annotations

import logging
import signal
from contextlib import suppress

from app.config.settings import config_path, load_settings
from app.core.clock import SystemClock
from app.core.detection.loader import host_from_settings
from app.core.enforcement.adapters import EnforcementExecutor
from app.core.enforcement.closer import ProcessCloser, SystemProcessController
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.monitor import MonitorLoop
from app.core.rules.engine import RuleEngine
from app.core.tracking.tracker import Tracker
from app.database.db import Database
from app.ipc.bridge import AgentBridge
from app.ipc.server import IpcServer
from app.windows.api import is_windows
from app.windows.win_monitor import build_monitor_source

log = logging.getLogger(__name__)


class ConsoleNotifier:
    """Phase 7 replaces this with tray toasts; stderr is fine until then."""

    def notify(self, title: str, message: str, *, urgent: bool = False) -> None:
        tag = "!" if urgent else "-"
        print(f"  {tag} {title}: {message}", flush=True)


def build_agent(db_path: str | None = None) -> tuple[MonitorLoop, Database, IpcServer]:
    settings = load_settings()
    db = Database(db_path or settings.resolved_db_path).connect()
    recovered = db.recover_open_sessions()
    if recovered:
        log.warning("Crash recovery: sealed %s unfinished session row(s).", recovered)

    clock = SystemClock()
    tracker = Tracker(db, clock)
    engine = RuleEngine(db, tracker, clock)

    # Game detectors (Phase 6) — same wiring as the service: built-ins with the
    # real local signals, plus drop-in plugins from <profile>/detectors.
    detectors, detector_report = host_from_settings(db)
    if detector_report.rejected:
        log.warning("Detector plugin(s) rejected: %s", detector_report.rejected)
    guard = GameSessionGuard(detectors.registry, enabled=bool(detectors.enabled))
    resolver = ActivityResolver(
        guard=guard,
        policy=ActivityPolicy(
            enforce_foreground_only=bool(db.get_setting("enforce_foreground_only", True)),
            background_grace_seconds=int(db.get_setting("background_grace_seconds", 5) or 5),
            idle_grace_seconds=int(db.get_setting("idle_grace_seconds", 0) or 0),
            website_grace_seconds=int(db.get_setting("website_grace_seconds", 3) or 3),
            website_stale_seconds=int(db.get_setting("website_stale_seconds", 15) or 15),
        ),
    )
    protected = frozenset(
        str(e).lower() for e in (db.get_setting("protected_processes", []) or [])
    )
    closer = ProcessCloser(
        SystemProcessController(),
        graceful_timeout=float(db.get_setting("graceful_close_timeout_seconds", 6) or 6),
    )
    executor = EnforcementExecutor(
        db, closer, clock,
        policy=KillPolicy(extra_protected=protected),
        notifier=ConsoleNotifier(),
        browser_sink=None,  # wired to the IPC bridge below
    )
    # ------------------------------------------- browser link (Phase 4)
    from app.config.settings import get_or_create_token

    token = get_or_create_token()
    bridge = AgentBridge(db, clock, token, tracker=tracker)
    ipc = IpcServer(
        bridge,
        port=int(db.get_setting("ipc_port", 17846) or 17846),
        rate_limit_count=int(db.get_setting("ipc_rate_limit_count", 30) or 30),
        rate_limit_window=float(db.get_setting("ipc_rate_limit_window", 10.0) or 10.0),
    )
    executor.set_browser_sink(bridge.browser_sink)  # website blocks reach the browser

    loop = MonitorLoop(
        db, engine, tracker, resolver, executor, build_monitor_source(), clock,
        closer=closer, guard=guard,
        interval=float(db.get_setting("monitoring_interval", 1.0) or 1.0),
    )
    loop.set_web_state_provider(bridge.web_state)
    loop.set_tick_hook(lambda report: bridge.on_tick(report.outcomes))

    if not ipc.start():
        log.error("Browser link unavailable (%s). Website rules will not be "
                  "enforced in the browser.", ipc.last_error)

    # Precise suspend/resume hook; the loop's gap detector covers the rest.
    from app.windows import power

    if power.attach_to_tracker(tracker):
        log.info("Suspend/resume listener attached.")
    return loop, db, ipc


def run_monitor(db_path: str | None = None) -> int:
    if not is_windows():
        print(
            "Refusing to start: foreground-window detection requires Windows.\n"
            "  * On Linux/macOS use: python -m app.main --simulate\n"
            "  * Production target is Windows 10/11 (see docs/PHASE3.md)."
        )
        return 2

    loop, db, ipc = build_agent(db_path)
    rules = db.list_rules()
    print(f"Time Manager monitor — {len(rules)} rule(s), interval {loop.interval}s.")
    if ipc.bound_port:
        print(f"  browser link: ws://{ipc.host}:{ipc.bound_port} "
              f"(token in {config_path()})")
    else:
        print(f"  browser link: UNAVAILABLE — {ipc.last_error}")
    for rule in rules:
        print(f"  * {rule.name:<14} {rule.type.value:<11} {rule.action.value:<20} "
              f"{rule.mode.value:<7} {rule.target}")
    print("Ctrl-C to stop.\n")

    def _shutdown(_signum, _frame):  # pragma: no cover - signal path
        print("\nStopping…")
        loop.stop(join=False)

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    try:
        loop.run_forever()
    finally:
        status = ipc.status()
        if status["clients"]:
            print(f"  {len(status['clients'])} browser session(s) were connected.")
        ipc.stop()
        try:
            tracker = loop.tracker
            tracker.close_all()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.exception("Failed to flush tracker on shutdown.")
        with suppress(Exception):
            loop.db.backup(loop.db.path.parent / "backups")
        with suppress(Exception):
            loop.db.close()
        print(f"Stopped after {loop.stats.ticks} ticks, {loop.stats.errors} errors.")
    return 0
