"""The monitor loop: one tick = snapshot -> resolve -> track/evaluate -> enforce.

Design notes that matter for a background agent:

* **`tick_once()` is public and synchronous.** Tests and the simulator drive it
  directly with a fake clock; `start()`/`run_forever()` only add a thread and a
  sleep. Deterministic core, thin scheduler.
* **A failing tick never kills the agent.** Any exception is logged, counted in
  `MonitorStats.errors`, and the loop continues on the next interval. A dead
  monitor means unlimited screen time — the worst failure mode there is.
* **Sleep/hibernate is detected from the monotonic clock**, because a
  suspended machine stops ticking: a gap far larger than the interval means we
  were frozen. The tracker is told (it never accrues suspended time) and the
  event is written to `clock_log`. The precise Win32 power-broadcast listener
  in `app/windows/power.py` calls the same tracker hook when available.
* **Missed intervals are not back-filled.** Time is only counted for real
  elapsed time the target was active; a stalled tick simply loses a second.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from app.core.clock import Clock
from app.core.enforcement.adapters import EnforcementExecutor, EnforcementRecord
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.monitoring.activity import ActivityResolver
from app.core.monitoring.processes import ProcessSource, SystemSnapshot
from app.core.rules.engine import RuleEngine, RuleOutcome
from app.core.timeutils import local_day_str
from app.database.db import Database

log = logging.getLogger(__name__)


@dataclass
class MonitorStats:
    ticks: int = 0
    errors: int = 0
    closes: int = 0
    skips: int = 0
    last_gap_seconds: float = 0.0
    last_error: str = ""
    tick_durations: list[float] = field(default_factory=list)

    def note_tick(self, duration: float) -> None:
        self.ticks += 1
        self.tick_durations.append(round(duration, 4))
        del self.tick_durations[:-120]

    @property
    def avg_tick(self) -> float:
        return sum(self.tick_durations) / len(self.tick_durations) if self.tick_durations else 0.0


@dataclass(frozen=True)
class TickReport:
    day: str
    outcomes: list[RuleOutcome]
    records: list[EnforcementRecord]
    snapshot: SystemSnapshot
    resolved_detail: dict[int, str]
    sleep_gap_seconds: float = 0.0
    error: str = ""


class MonitorLoop:
    def __init__(
        self,
        db: Database,
        engine: RuleEngine,
        tracker: object,  # Tracker (duck-typed: notify_suspend / flush)
        resolver: ActivityResolver,
        executor: EnforcementExecutor,
        source: ProcessSource,
        clock: Clock,
        *,
        closer: ProcessCloser | None = None,
        guard: GameSessionGuard | None = None,
        interval: float = 1.0,
        sleep_gap_factor: float = 3.0,
        sleep_gap_floor: float = 5.0,
    ) -> None:
        self._db = db
        self._engine = engine
        self._tracker = tracker
        self._resolver = resolver
        self._executor = executor
        self._source = source
        self._clock = clock
        self._closer = closer
        self._guard = guard
        self.interval = max(0.25, float(interval))
        self._gap_factor = sleep_gap_factor
        self._gap_floor = sleep_gap_floor

        self.stats = MonitorStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_mono: float | None = None
        self._last_day: str | None = None
        self._web_state_provider = None  # Phase 4 sets this
        self._tick_hook = None  # Phase 4: bridge.on_tick
        self._pause_provider = None  # Phase 5: PauseController
        self._was_paused = False
        self._bypass_watch = None  # Phase 7: optional BypassWatch
        self._browsers_connected_provider = None  # Phase 7: () -> int

    # ------------------------------------------------------------------ wiring
    def set_web_state_provider(self, provider) -> None:
        """Phase 4: callable returning {domain: mono_of_last_activity}."""
        self._web_state_provider = provider

    def set_pause_provider(self, provider) -> None:
        """Phase 5: an object with `suppresses(rule)` / `state(rules)`.

        A pause stops counting and lifts enforcement for ordinary rules;
        STRICT rules ignore it (see app/core/pause.py).
        """
        self._pause_provider = provider

    def set_bypass_watch(self, watch, browsers_connected_provider=None) -> None:
        """Phase 7: per-tick observation of runtime bypass signals.

        `browsers_connected_provider` returns the number of authenticated
        extension connections (0 when the link is down). The watch only
        produces alerts; it never participates in enforcement decisions.
        """
        self._bypass_watch = watch
        self._browsers_connected_provider = browsers_connected_provider

    def set_tick_hook(self, hook) -> None:
        """Phase 4: called with each TickReport (IPC push, UI refresh).

        A hook that throws is logged and ignored — it must never cost the user
        their enforcement.
        """
        self._tick_hook = hook

    @property
    def tracker(self) -> object:
        return self._tracker

    @property
    def db(self) -> Database:
        return self._db

    # ------------------------------------------------------------------- ticks
    def tick_once(self) -> TickReport:
        started = self._clock.mono()
        try:
            return self._tick(started)
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            self.stats.errors += 1
            self.stats.last_error = repr(exc)
            log.exception("Monitor tick failed; continuing.")
            return TickReport(
                day=self._day(), outcomes=[], records=[],
                snapshot=SystemSnapshot(captured_mono=started),
                resolved_detail={}, error=repr(exc),
            )
        finally:
            self.stats.note_tick(self._clock.mono() - started)

    def _tick(self, started: float) -> TickReport:
        rules = self._db.list_rules()
        # Day rollover: notifications may fire again, and the enforcement
        # executor's dedupe set is day-scoped.
        day = self._day()
        if self._last_day is None:
            self._last_day = day
        elif day != self._last_day:
            log.info("Local day rolled over: %s -> %s", self._last_day, day)
            self._executor.reset_day()
            self._last_day = day

        gap = self._detect_sleep(started)
        if gap:
            self._db.log_clock(f"monitor gap {gap:.1f}s treated as suspend/resume")
            try:
                self._tracker.notify_suspend()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                log.warning("notify_suspend failed.", exc_info=True)

        snapshot = self._source.snapshot()
        web = self._web_state_provider() if self._web_state_provider else None
        resolved = self._resolver.resolve(rules, snapshot, web_active=web)

        # Pause gate: suppressed rules are forced inactive (no counting) and
        # get no enforcement actions; STRICT rules are untouched.
        paused_now = bool(self._pause_provider and self._pause_provider.is_paused())
        if paused_now and not self._was_paused:
            # A pause is a hard boundary: seal open sessions so the interval in
            # progress is not billed *after* the user asked for a break. (The
            # tracker's closing-interval rule would otherwise credit up to one
            # more tick.)
            self._close_sessions_for_pause(rules)
        self._was_paused = paused_now
        if self._pause_provider is not None and paused_now:
            from app.core.rules.engine import ActivityInput  # local: avoid cycle

            for rule in rules:
                assert rule.id is not None
                if self._pause_provider.suppresses(rule):
                    resolved.activities[rule.id] = ActivityInput(
                        active=False,
                        session_type=resolved.activities[rule.id].session_type,
                        in_game_session=resolved.activities[rule.id].in_game_session,
                        detector_confident=resolved.activities[rule.id].detector_confident,
                    )
                    resolved.detail[rule.id] = "paused"

        outcomes = self._engine.tick(resolved.activities)
        records = self._executor.handle(outcomes, snapshot, day)

        if self._bypass_watch is not None:
            # Phase 7: observe runtime bypass signals (silent extension while
            # a STRICT website rule is armed). Observation failures must never
            # cost the user a tick of enforcement.
            try:
                connected = 0
                if self._browsers_connected_provider is not None:
                    connected = int(self._browsers_connected_provider() or 0)
                self._bypass_watch.check(rules, snapshot, connected)
            except Exception:  # noqa: BLE001
                log.warning("Bypass watch check failed.", exc_info=True)

        if self._closer is not None:
            mode_by_rule = {r.id: r.mode.value for r in rules if r.id is not None}
            for res in self._closer.poll(self._clock.mono()):
                if res.ok:
                    self.stats.closes += 1
                self._db.log_enforcement(
                    res.rule_id, day, "CLOSE_APP",
                    mode_by_rule.get(res.rule_id, "NORMAL"),
                    "EXECUTED" if res.ok else "FAILED", res.as_detail(),
                )

        for rec in records:
            if rec.outcome in ("SKIPPED", "PROTECTED", "DEFERRED"):
                self.stats.skips += 1

        report = TickReport(
            day=day, outcomes=outcomes, records=records, snapshot=snapshot,
            resolved_detail=resolved.detail, sleep_gap_seconds=gap or 0.0,
        )
        if self._tick_hook is not None:
            try:
                self._tick_hook(report)
            except Exception:  # noqa: BLE001 - a broken hook must not stop the loop
                log.warning("Tick hook failed.", exc_info=True)
        return report

    def _close_sessions_for_pause(self, rules) -> None:
        suppressible = [
            rule.id for rule in rules
            if rule.id is not None and self._pause_provider.suppresses(rule)
        ]
        if not suppressible:
            return
        try:
            closed = sum(1 for rule_id in suppressible if self._tracker.close_rule(rule_id))
            if closed:
                log.info("Pause started: sealed %s open session(s).", closed)
        except Exception:  # noqa: BLE001 - never let bookkeeping break the loop
            log.warning("Could not close sessions for the pause boundary.", exc_info=True)

    def _detect_sleep(self, now: float) -> float | None:
        previous, self._last_mono = self._last_mono, now
        if previous is None:
            return None
        gap = now - previous
        limit = max(self.interval * self._gap_factor, self._gap_floor)
        if gap > limit:
            log.warning("Monitor gap of %.1fs detected (sleep/hibernate/blocked).", gap)
            self.stats.last_gap_seconds = gap
            return gap
        return None

    def _day(self) -> str:
        return local_day_str(self._clock.local_now())

    # -------------------------------------------------------------- scheduling
    def run_forever(self) -> None:
        """Blocking loop (CLI / worker thread body)."""
        log.info("Monitor loop started (interval %.2fs).", self.interval)
        while not self._stop.is_set():
            self.tick_once()
            self._stop.wait(self.interval)
        log.info("Monitor loop stopped after %s ticks (%s errors).",
                 self.stats.ticks, self.stats.errors)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="monitor", daemon=True)
        self._thread.start()

    def stop(self, *, join: bool = True, timeout: float = 5.0) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
