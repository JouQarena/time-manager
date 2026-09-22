"""AgentService — the one object that owns a running agent.

Everything that used to be wired by hand in `app/cli/monitor.py` (database,
tracker, engine, resolver, closer, executor, IPC server, monitor loop) lives
here, so the CLI (`--monitor`), the GUI (`--gui`) and the tests all start the
*same* thing with the same settings.

Responsibilities:
* own the profile: single-instance lock, DB open + crash recovery, IPC token,
* build and start the monitor loop and the browser link,
* expose a thread-safe `snapshot()` for the status surface (CLI JSON and GUI),
* offer the user-facing operations: pause/resume, backup, token rotation,
  rule CRUD (delegating to the DB and re-pushing rule updates to browsers),
* shut down cleanly (flush tracker, seal sessions, close sockets, release lock).

Nothing in here touches Qt: the GUI is a thin adapter over this class.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app import __version__
from app.config.lockfile import InstanceLock
from app.config.settings import (
    AppSettings,
    get_or_create_token,
    load_settings,
    profile_dir,
    regenerate_token,
    save_settings,
)
from app.core.clock import Clock, SystemClock
from app.core.detection.loader import host_from_settings
from app.core.enforcement.adapters import EnforcementExecutor, Notifier, RecordingNotifier
from app.core.enforcement.closer import ProcessCloser, SystemProcessController
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.monitor import MonitorLoop
from app.core.pause import PauseController
from app.core.rules.engine import RuleEngine
from app.core.rules.models import Rule
from app.core.security import BypassWatch, LifecycleAudit, UsageFloor, floor_path_for
from app.core.timeutils import local_day_str
from app.core.tracking.tracker import Tracker
from app.core.types import Mode, RuleState
from app.database.db import Database
from app.ipc.bridge import AgentBridge
from app.ipc.server import IpcServer
from app.windows.watchdog import WatchdogResult, WatchdogScheduler

log = logging.getLogger(__name__)


@dataclass
class RuleStatus:
    """Everything the UI needs about one rule, computed from the live data."""

    rule: Rule
    used_seconds: int
    session_seconds: int
    state: RuleState
    remaining_today: int | None
    remaining_session: int | None
    browsers_blocked: bool = False

    @property
    def name(self) -> str:
        return self.rule.name

    @property
    def used_ratio(self) -> float:
        limit = self.rule.daily_limit_seconds
        if not limit:
            return 0.0
        return min(1.0, self.used_seconds / limit)


@dataclass
class Snapshot:
    """Immutable view of the agent for the status surface."""

    agent_version: str = __version__
    day: str = ""
    running: bool = False
    read_only: bool = False
    paused: bool = False
    pause_remaining_seconds: int = 0
    pause_until: str = ""
    pause_exempt_rules: tuple[str, ...] = ()
    rules: list[RuleStatus] = field(default_factory=list)
    browsers_connected: int = 0
    browser_domains: list[str] = field(default_factory=list)
    ipc_running: bool = False
    ipc_port: int | None = None
    ipc_clients: list[dict] = field(default_factory=list)
    ipc_messages_in: int = 0
    ipc_messages_out: int = 0
    ipc_rate_limited: int = 0
    ipc_last_error: str = ""
    ipc_note: str = ""
    ticks: int = 0
    tick_errors: int = 0
    last_tick_error: str = ""
    closes: int = 0
    thread_running: bool = False
    last_sleep_gap: float = 0.0
    detectors: list[dict] = field(default_factory=list)
    detector_summary: dict = field(default_factory=dict)
    detector_rejected: list[dict] = field(default_factory=list)
    db_path: str = ""
    profile_dir: str = ""
    degraded: str = ""  # e.g. "foreground detection unavailable (non-Windows)"
    security: dict = field(default_factory=dict)  # Phase 7 anti-bypass status

    def to_dict(self) -> dict:
        """JSON-safe projection for `--status-json` and the GUI."""
        return {
            "agent_version": self.agent_version,
            "day": self.day,
            "running": self.running,
            "read_only": self.read_only,
            "paused": self.paused,
            "pause_remaining_seconds": self.pause_remaining_seconds,
            "pause_until": self.pause_until,
            "pause_exempt_rules": list(self.pause_exempt_rules),
            "browsers": {
                "connected": self.browsers_connected,
                "domains": list(self.browser_domains),
                "clients": list(self.ipc_clients),
            },
            "ipc": {
                "running": self.ipc_running,
                "port": self.ipc_port,
                "messages_in": self.ipc_messages_in,
                "messages_out": self.ipc_messages_out,
                "rate_limited": self.ipc_rate_limited,
                "last_error": self.ipc_last_error,
                "note": self.ipc_note,
            },
            "monitor": {
                "ticks": self.ticks,
                "errors": self.tick_errors,
                "last_error": self.last_tick_error,
                "closes": self.closes,
                "last_sleep_gap_seconds": round(self.last_sleep_gap, 1),
                "thread": self.thread_running,
                "degraded": self.degraded,
            },
            "detectors": {
                "enabled": self.detector_summary.get("enabled", True),
                "count": self.detector_summary.get("count", 0),
                "quarantined": self.detector_summary.get("quarantined", 0),
                "timeout_ms": self.detector_summary.get("timeout_ms", 0),
                "items": list(self.detectors),
                "rejected": list(self.detector_rejected),
            },
            "rules": [
                {
                    "id": r.rule.id,
                    "name": r.rule.name,
                    "type": r.rule.type.value,
                    "target": r.rule.target,
                    "mode": r.rule.mode.value,
                    "action": r.rule.action.value,
                    "enabled": r.rule.enabled,
                    "schedule": r.rule.schedule.to_json() if r.rule.schedule else None,
                    "used_seconds": r.used_seconds,
                    "session_seconds": r.session_seconds,
                    "daily_limit_seconds": r.rule.daily_limit_seconds,
                    "session_limit_seconds": r.rule.session_limit_seconds,
                    "remaining_today_seconds": r.remaining_today,
                    "remaining_session_seconds": r.remaining_session,
                    "state": r.state.value,
                    "used_ratio": round(r.used_ratio, 4),
                }
                for r in self.rules
            ],
            "storage": {"db_path": self.db_path, "profile_dir": self.profile_dir},
            "security": dict(self.security),
        }


class AgentAlreadyRunning(RuntimeError):
    def __init__(self, pid: int | None) -> None:
        super().__init__(
            f"Another Time Manager instance is already running (pid {pid})."
            if pid else "Another Time Manager instance is already running."
        )
        self.pid = pid


def build_notifier() -> Notifier:
    """Default notifier for head-less runs (the GUI injects a tray one)."""
    return RecordingNotifier()


class AgentService:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        clock: Clock | None = None,
        settings: AppSettings | None = None,
        notifier: Notifier | None = None,
        source=None,
        controller=None,
        concurrency_guard: bool = True,
        bind_ipc: bool = True,
        read_only_note: str = "",
        agent_version: str = __version__,
        watchdog: "WatchdogScheduler | None" = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.clock = clock or SystemClock()
        # resolved_db_path is a property: attribute access, no parentheses
        # (calling it would try to call the returned Path object).
        self.db_path = Path(db_path) if db_path else self.settings.resolved_db_path
        self.agent_version = agent_version
        self._lock = InstanceLock(self.db_path.with_suffix(".lock"))
        self._guard = concurrency_guard
        self._bind_ipc = bind_ipc
        #: Why this instance is read-only (e.g. another agent is live).
        self.read_only_note = read_only_note

        self.db: Database | None = None
        self.tracker: Tracker | None = None
        self.engine: RuleEngine | None = None
        self.monitor: MonitorLoop | None = None
        self.bridge: AgentBridge | None = None
        self.ipc: IpcServer | None = None
        self.pause_ctl: PauseController | None = None
        self.executor: EnforcementExecutor | None = None
        self.detectors = None  # DetectorHost, built on start()
        self.detector_report = None  # PluginReport from the last start()
        self.closer: ProcessCloser | None = None
        self.token: str = ""
        self.last_error: str = ""
        self.degraded: str = ""
        # Phase 7 — security & anti-bypass hardening.
        self.lifecycle: LifecycleAudit | None = None
        self.floor: UsageFloor | None = None
        self.bypass: BypassWatch | None = None
        self.watchdog = watchdog or WatchdogScheduler()
        self._unclean_stop: dict | None = None
        self._clock_regressed_seconds: float | None = None
        self._startup_repaired = False
        self._watchdog_status: dict = {}
        self._ready = False
        self._notifier = notifier
        self._source = source
        self._controller = controller
        self.started_mono: float | None = None
        self._snapshot_lock = threading.RLock()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Open the profile, start monitoring and the browser link.

        Returns True when the agent is running. Raises AgentAlreadyRunning when
        another live instance owns the profile. Never raises for a *missing*
        optional capability (e.g. no websockets) — that is reported in
        `degraded`/`last_error` instead.
        """
        if self._ready:
            return True
        if self._guard and not self._lock.acquire():
            raise AgentAlreadyRunning(self._lock.held_by_other())

        try:
            self.settings.validate()
            self.db = Database(self.db_path).connect()
            recovered = self.db.recover_open_sessions()
            if recovered:
                log.warning("Crash recovery: sealed %s unfinished session row(s).", recovered)

            # Phase 7 — security hardening. Read-only status views
            # (--status-json against a live agent) must never touch the audit
            # trail: no START/STOP rows, no tamper findings of their own.
            if self.read_only_note:
                self.floor = UsageFloor(floor_path_for(self.db_path), self.clock)
                self.floor.load()
            else:
                # Lifecycle audit: did the previous run die silently?
                # Detect BEFORE writing our own START row, then claim the run.
                self.lifecycle = LifecycleAudit(self.db, self.clock)
                finding = self.lifecycle.detect_unclean_stop()
                self.lifecycle.record_start(self.agent_version)
                if finding is not None:
                    self.lifecycle.report_unclean_stop(finding, self.agent_version)
                    self._unclean_stop = {
                        "last_start_at": finding.last_start_at,
                        "local": finding.started_local.isoformat(),
                    }

                # Usage floors live OUTSIDE the database on purpose: deleting
                # or rolling back the DB cannot erase them.
                self.floor = UsageFloor(
                    floor_path_for(self.db_path), self.clock, db=self.db
                )
                self.floor.load()
                regressed = self.floor.check_clock_regression()
                if regressed:
                    self._clock_regressed_seconds = round(regressed, 1)
                    detail = (
                        f"wall clock is {regressed:.0f}s before the last time the "
                        "agent was seen; daily limits may have been revived manually"
                    )
                    self.db.log_audit("CLOCK_REGRESSED", "critical", detail)
                    self.db.log_clock(f"startup: {detail}")
                    log.warning("Clock regression at startup: %s", detail)

            self.token = get_or_create_token()
            self.pause_ctl = PauseController(self.db, self.clock)
            self.tracker = Tracker(self.db, self.clock)

            source = self._source or self._build_source()
            controller = self._controller or SystemProcessController()
            self.closer = ProcessCloser(
                controller,
                graceful_timeout=float(
                    self.db.get_setting("graceful_close_timeout_seconds", 6) or 6
                ),
            )
            self.bridge = AgentBridge(
                self.db, self.clock, self.token,
                tracker=self.tracker, agent_version=self.agent_version,
            )
            protected = frozenset(
                str(e).lower() for e in (self.db.get_setting("protected_processes", []) or [])
            )
            notifier = self._notifier or build_notifier()
            self.executor = EnforcementExecutor(
                self.db, self.closer, self.clock,
                policy=KillPolicy(extra_protected=protected),
                notifier=notifier,
                browser_sink=self.bridge.browser_sink,
            )
            self.executor.pause_provider = self.pause_ctl.suppresses  # type: ignore[attr-defined]

            # Game detectors (Phase 6): built-ins wired to the local signals,
            # plus any drop-in plugins. A broken plugin is reported, never fatal.
            self.detectors, self.detector_report = host_from_settings(
                self.db, profile_dir_getter=profile_dir
            )
            if self.detector_report.rejected:
                log.warning("Detector plugin(s) rejected: %s", self.detector_report.rejected)
                self.degraded = self.degraded or (
                    f"{len(self.detector_report.rejected)} detector plugin(s) rejected"
                )
            guard = GameSessionGuard(
                self.detectors.registry, enabled=bool(self.detectors.enabled)
            )
            resolver = ActivityResolver(
                guard=guard,
                policy=ActivityPolicy(
                    enforce_foreground_only=bool(
                        self.db.get_setting("enforce_foreground_only", True)
                    ),
                    background_grace_seconds=int(
                        self.db.get_setting("background_grace_seconds", 5) or 5
                    ),
                    idle_grace_seconds=int(self.db.get_setting("idle_grace_seconds", 0) or 0),
                    website_grace_seconds=int(
                        self.db.get_setting("website_grace_seconds", 3) or 3
                    ),
                    website_stale_seconds=int(
                        self.db.get_setting("website_stale_seconds", 15) or 15
                    ),
                    require_browser_foreground=bool(
                        self.db.get_setting("require_browser_foreground", True)
                    ),
                ),
            )
            self.engine = RuleEngine(self.db, self.tracker, self.clock, floor=self.floor)
            self.monitor = MonitorLoop(
                self.db, self.engine, self.tracker, resolver, self.executor, source,
                self.clock, closer=self.closer, guard=guard,
                interval=float(self.db.get_setting("monitoring_interval", 1.0) or 1.0),
            )
            self.monitor.set_web_state_provider(self.bridge.web_state)
            self.monitor.set_tick_hook(lambda report: self.bridge.on_tick(report.outcomes))
            self.monitor.set_pause_provider(self.pause_ctl)

            # Phase 7 — runtime bypass observation (silent extension, etc.).
            silence_seconds = float(
                self.db.get_setting("extension_silence_seconds", 60) or 60
            )
            self.bypass = BypassWatch(self.db, self.clock, notifier,
                                      silence_seconds=silence_seconds)
            self.monitor.set_bypass_watch(
                self.bypass,
                browsers_connected_provider=lambda: (
                    self.bridge.browsers.summary()["connected"] if self.bridge else 0
                ),
            )

            # Port source of truth: the settings-table override (the UI's
            # Settings dialog writes it) — EXCEPT an explicit ipc_port=0 in
            # AppSettings, which means "OS, choose a free port" and exists so
            # the demo and the tests never fight a live agent for 17846
            # (field-observed on the first Windows machine that ran pytest
            # while its own agent was running). Not an `or` chain: 0 is
            # falsy and would silently fall through to the DB value.
            requested = int(self.settings.ipc_port)
            self.ipc = IpcServer(
                self.bridge,
                port=requested if requested == 0
                else int(self.db.get_setting("ipc_port", requested) or requested),
                agent_version=self.agent_version,
                rate_limit_count=int(self.db.get_setting("ipc_rate_limit_count", 30) or 30),
                rate_limit_window=float(self.db.get_setting("ipc_rate_limit_window", 10.0) or 10.0),
            )
            if not self._bind_ipc:
                # Read-only view (see --status-json): the port belongs to the
                # live agent, so neither claim it nor report a fake failure.
                self.ipc.disabled_note = self.read_only_note or "read-only view"
            elif not self.ipc.start():
                self.last_error = f"browser link unavailable: {self.ipc.last_error}"
                log.error("%s", self.last_error)

            if not self.read_only_note:
                # Phase 7 — STRICT hardening: a STRICT rule must not silently
                # lose its autostart entry, and (opt-in) a watchdog task can
                # revive a killed agent. Both are transparent and audited.
                notifier = self._notifier or build_notifier()
                self._startup_repaired = self._repair_startup_if_strict(notifier)
                self._ensure_watchdog()

            self.started_mono = self.clock.mono()
            self._ready = True
            return True
        except Exception:
            # Never leave a half-built agent holding the profile lock.
            self._teardown()
            raise

    def start_background(self) -> bool:
        """Start everything including the monitor thread."""
        started = self.start()
        if started and self.monitor is not None:
            self.monitor.start()
        return started

    def stop(self, *, backup: bool = True) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        # Phase 7: a deliberate stop is recorded, so the NEXT start can tell a
        # clean shutdown from a kill/crash. Never called by read-only views.
        if self.lifecycle is not None and self.db is not None:
            try:
                self.lifecycle.record_stop("clean shutdown")
            except Exception:  # noqa: BLE001 - bookkeeping must not break stop()
                log.warning("Could not record the lifecycle STOP row.", exc_info=True)
        if self.floor is not None:
            try:
                self.floor.flush()
            except Exception:  # noqa: BLE001
                log.warning("Could not flush the usage floor.", exc_info=True)
        self._teardown(backup=backup)

    def _teardown(self, *, backup: bool = False) -> None:
        self._ready = False
        with self._snapshot_lock:
            if self.ipc is not None:
                try:
                    self.ipc.stop()
                except Exception:  # noqa: BLE001
                    log.debug("IPC stop failed.", exc_info=True)
                self.ipc = None
            if self.tracker is not None:
                try:
                    self.tracker.close_all()  # flush + seal open sessions
                except Exception:  # noqa: BLE001
                    log.exception("Failed to flush the tracker during shutdown.")
                self.tracker = None
            if self.db is not None:
                if backup:
                    try:
                        target = self.db.backup(profile_dir() / "backups")
                        if target:
                            log.info("Backup written to %s", target)
                    except Exception:  # noqa: BLE001
                        log.warning("Backup failed.", exc_info=True)
                try:
                    self.db.close()
                except Exception:  # noqa: BLE001
                    log.debug("DB close failed.", exc_info=True)
                self.db = None
            self.monitor = None
            self.bridge = None
            self.engine = None
            self.closer = None
            self.pause_ctl = None
        self._lock.release()

    @property
    def running(self) -> bool:
        """The agent is up (database open, monitor + link built).

        Whether the worker *thread* is currently looping is separate — a CLI
        snapshot intentionally runs without it (`monitor["thread"]` in JSON).
        """
        return self._ready

    def _build_source(self):
        from app.windows.api import is_windows
        from app.windows.win_monitor import build_monitor_source

        if not is_windows():
            self.degraded = (
                "foreground detection unavailable on this OS "
                "(monitor runs, but app/website activity cannot be attributed)"
            )
        return build_monitor_source()

    # ------------------------------------------------------------ user actions
    def set_notifier(self, notifier: Notifier) -> None:
        """Swap the notifier (the GUI hands over the tray one at startup)."""
        self._notifier = notifier
        if self.executor is not None:
            self.executor.notifier = notifier
        if self.bypass is not None:
            self.bypass.notifier = notifier

    # ------------------------------------------------- Phase 7: anti-bypass
    def _repair_startup_if_strict(self, notifier: Notifier) -> bool:
        """Re-register autostart when STRICT rules are armed but the Run key
        is gone (deleted, or never set). Transparent: audited + notified.

        Returns True when a repair happened this start.
        """
        try:
            from app.windows import startup
            from app.windows.api import is_windows

            if not is_windows():
                return False
            strict_armed = any(
                r.enabled and r.mode == Mode.STRICT for r in self.db.list_rules()
            )
            if not strict_armed or startup.is_enabled():
                return False
            if not startup.enable():
                log.warning("Startup repair failed: could not write the Run key.")
                return False
            assert self.db is not None
            self.db.log_audit(
                "STARTUP_REPAIRED", "warning",
                "autostart entry restored because STRICT rules are active",
            )
            notifier.notify(
                "Time Manager",
                "Windows startup was re-enabled because STRICT rules are active. "
                "Switch those rules to Normal mode to unlock the setting.",
                urgent=True,
            )
            log.info("Startup entry repaired for armed STRICT rules.")
            return True
        except Exception:  # noqa: BLE001 - hardening must never break startup
            log.warning("Startup repair check failed.", exc_info=True)
            return False

    def _ensure_watchdog(self) -> None:
        """Match the Task Scheduler watchdog to the `strict_watchdog` setting."""
        assert self.db is not None
        if not self.settings.strict_watchdog:
            self._watchdog_status = self.watchdog.status() if self.watchdog.available \
                else {"available": False, "installed": None, "task_name": "", "command": ""}
            return
        result = self.watchdog.install()
        self._watchdog_status = self.watchdog.status() if self.watchdog.available \
            else {"available": False, "installed": None, "task_name": "", "command": ""}
        if result.ok and result.action == "INSTALLED":
            self.db.log_audit("WATCHDOG_INSTALLED", "info", result.detail)
        elif not result.ok and result.action != "UNAVAILABLE":
            log.warning("Watchdog install failed: %s", result.detail)
            self.degraded = self.degraded or f"watchdog unavailable: {result.detail}"
        elif result.action == "UNAVAILABLE":
            self.degraded = self.degraded or (
                "strict_watchdog is enabled but needs Windows Task Scheduler"
            )

    def set_watchdog_enabled(self, enabled: bool) -> WatchdogResult:
        """User-facing toggle (Settings): persist + install/remove + audit."""
        self.settings.strict_watchdog = bool(enabled)
        try:
            save_settings(self.settings)
        except ValueError:
            log.warning("Could not persist settings after watchdog toggle.")
        result = self.watchdog.ensure(bool(enabled))
        if self.db is not None:
            try:
                if result.ok and result.action == "INSTALLED":
                    self.db.log_audit("WATCHDOG_INSTALLED", "info", result.detail)
                elif result.ok and result.action == "REMOVED":
                    self.db.log_audit("WATCHDOG_REMOVED", "info", result.detail)
                elif not result.ok and result.action not in ("UNAVAILABLE", "NOT_PRESENT"):
                    self.db.log_audit(
                        "WATCHDOG_INSTALLED" if enabled else "WATCHDOG_REMOVED",
                        "warning", f"failed: {result.detail}",
                    )
            except Exception:  # noqa: BLE001
                log.warning("Could not audit the watchdog change.", exc_info=True)
        self._watchdog_status = self.watchdog.status() if self.watchdog.available \
            else {"available": False, "installed": None, "task_name": "", "command": ""}
        return result

    def security_audit(self, limit: int = 50) -> list[dict]:
        """Most recent security/lifecycle events (newest first)."""
        if self.db is None:
            return []
        return [dict(row) for row in self.db.list_audit(limit)]

    def pause(self, minutes: int) -> None:
        assert self.pause_ctl is not None
        self.pause_ctl.pause(minutes)

    def resume(self) -> None:
        assert self.pause_ctl is not None
        self.pause_ctl.resume()

    def backup(self) -> Path | None:
        assert self.db is not None
        return self.db.backup(profile_dir() / "backups")

    def regenerate_token(self) -> str:
        """Rotate the pairing token and restart the browser link with it."""
        self.token = regenerate_token()
        if self.ipc is not None and self.bridge is not None:
            self.ipc.stop()
            self.bridge.token = self.token
            self.ipc = IpcServer(
                self.bridge,
                port=self.ipc.port,
                agent_version=self.agent_version,
                rate_limit_count=self.ipc.rate_limit_count,
                rate_limit_window=self.ipc.rate_limit_window,
            )
            self.ipc.start()
        return self.token

    # -------------------------------------------------------------- rule CRUD
    def list_rules(self) -> list[Rule]:
        assert self.db is not None
        return self.db.list_rules()

    def save_rule(self, rule: Rule) -> int:
        """Create or update a rule, then push the new state to browsers."""
        assert self.db is not None
        if rule.id is None:
            rule_id = self.db.add_rule(rule)
        else:
            self.db.update_rule(rule)
            rule_id = rule.id
        self._push_rules()
        return rule_id

    def delete_rule(self, rule_id: int) -> None:
        assert self.db is not None
        self.db.delete_rule(rule_id)
        self.bridge.browsers.forget_all() if self.bridge else None  # noqa: B018
        self._push_rules()

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        assert self.db is not None
        rule = self.db.get_rule(rule_id)
        if rule is None:
            return
        rule.enabled = enabled
        self.db.update_rule(rule)
        self._push_rules()

    def _push_rules(self) -> None:
        if self.bridge is not None:
            try:
                self.bridge.on_tick(None)  # recompute payloads + transitions
            except Exception:  # noqa: BLE001
                log.warning("Rule push after edit failed.", exc_info=True)

    # ------------------------------------------------------------------ status
    def snapshot(self) -> Snapshot:
        """Thread-safe view for the status surface. Never raises."""
        try:
            return self._snapshot()
        except Exception as exc:  # noqa: BLE001 - the UI must never crash
            log.exception("Snapshot failed.")
            return Snapshot(running=self.running, last_tick_error=repr(exc))

    def _snapshot(self) -> Snapshot:
        snap = Snapshot(
            agent_version=self.agent_version,
            day=local_day_str(self.clock.local_now()),
            running=self.running,
            read_only=bool(self.read_only_note),
            db_path=str(self.db_path),
            profile_dir=str(profile_dir()),
            degraded=self.degraded,
        )
        with self._snapshot_lock:
            if self.db is None:
                return snap
            rules = self.db.list_rules()
            day = snap.day
            for rule in rules:
                assert rule.id is not None
                used = (
                    self.tracker.today_total(rule.id, day) if self.tracker is not None
                    else self.db.get_daily(rule.id, day)
                )
                if self.floor is not None:
                    # Phase 7: the status surface reports the same clamped usage
                    # the engine enforces (erased rows never show as "free").
                    used = self.floor.raise_for(rule, day, used)
                session_used = (
                    self.tracker.session_seconds(rule.id) if self.tracker is not None else 0
                )
                stored = self.db.get_state(rule.id)
                state = stored[1] if stored and stored[0] == day else RuleState.NORMAL
                remaining_today = (
                    None if rule.daily_limit_seconds is None
                    else max(0, rule.daily_limit_seconds - used)
                )
                remaining_session = (
                    None if rule.session_limit_seconds is None
                    else max(0, rule.session_limit_seconds - session_used)
                )
                snap.rules.append(RuleStatus(
                    rule=rule, used_seconds=used, session_seconds=session_used,
                    state=state, remaining_today=remaining_today,
                    remaining_session=remaining_session,
                ))

            if self.pause_ctl is not None:
                state = self.pause_ctl.state(rules)
                snap.paused = state.active
                snap.pause_remaining_seconds = state.remaining_seconds
                snap.pause_until = state.until_iso
                snap.pause_exempt_rules = state.exempt_rules

            if self.bridge is not None:
                summary = self.bridge.browsers.summary()
                snap.browsers_connected = summary["connected"]
                snap.browser_domains = summary["domains"]

            if self.ipc is not None:
                status = self.ipc.status()
                snap.ipc_running = bool(status["running"])
                snap.ipc_port = status["port"]
                snap.ipc_clients = list(status["clients"])
                snap.ipc_messages_in = status["messages_in"]
                snap.ipc_messages_out = status["messages_out"]
                snap.ipc_rate_limited = status.get("rate_limited", 0)
                snap.ipc_last_error = status["last_error"]
                snap.ipc_note = status.get("note", "")

            if self.detectors is not None:
                snap.detectors = self.detectors.status()
                snap.detector_summary = self.detectors.summary()
                if self.detector_report is not None:
                    snap.detector_rejected = self.detector_report.to_dict()["rejected"]

            if self.monitor is not None:
                stats = self.monitor.stats
                snap.ticks = stats.ticks
                snap.tick_errors = stats.errors
                snap.last_tick_error = stats.last_error
                snap.closes = stats.closes
                snap.thread_running = bool(self.monitor.running)
                snap.last_sleep_gap = stats.last_gap_seconds

            snap.security = self._security_section(day)
        return snap

    def _security_section(self, day: str) -> dict:
        """Phase 7 status surface: what the anti-bypass layer knows right now.

        Everything here is cheap and in-memory except the audit tail (one
        indexed query), so it is safe to call on every dashboard poll.
        """
        tamper: list[dict] = []
        if self.floor is not None and self.tracker is not None:
            for rule in self.db.list_rules():
                if rule.id is None:
                    continue
                stored = self.tracker.today_total(rule.id, day)
                alert = self.floor.tamper_check(rule, day, stored)
                if alert is not None:
                    tamper.append({
                        "rule": alert.rule_name,
                        "day": alert.day,
                        "stored_seconds": alert.stored_seconds,
                        "floor_seconds": alert.floor_seconds,
                    })
        return {
            "unclean_stop": self._unclean_stop,
            "clock_regressed_seconds": self._clock_regressed_seconds,
            "startup_repaired": self._startup_repaired,
            "watchdog": dict(self._watchdog_status) if self._watchdog_status else {
                "available": self.watchdog.available,
                "enabled": bool(self.settings.strict_watchdog),
                "installed": None,
            },
            "watchdog_enabled": bool(self.settings.strict_watchdog),
            "extension_silent_rules": list(
                self.bypass.silent_rule_names if self.bypass else ()
            ),
            "usage_tamper": tamper,
            "recent_audit": [
                {k: row[k] for k in ("recorded_at", "kind", "severity", "detail")}
                for row in self.db.list_audit(limit=10)
            ],
        }

    def enforcement_history(self, day: str | None = None, limit: int = 100) -> list[dict]:
        if self.db is None:
            return []
        return [dict(row) for row in self.db.list_enforcement(day, limit)]
