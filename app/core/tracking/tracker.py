"""Usage tracker: per-tick activity flags -> sessions + daily totals.

Design (see docs/ARCHITECTURE.md):
- At most ONE open in-memory session per rule; durations come from the
  monotonic clock, so wall-clock edits can never inflate or erase time.
- The wall clock only decides which local DAY each second belongs to. A
  session spanning midnight is split into one DB row per day (exact
  proportional split), while the *continuous* session total keeps running.
- DB writes are batched: `daily_usage` is flushed every `persist_seconds`
  (default 5 s) and on session close / midnight split / suspend / flush().
  `today_total()` returns DB + unflushed remainder, so readers never under-see.
- Interval attribution: the interval between two polls is billed when the
  session was open across it (the rule was active at either bounding poll).
  A session therefore opens with 0 credited seconds, and the interval in which
  the target disappears is still billed. Net effect: transitions cost at most
  ONE monitoring interval (1 s at the default cadence), always in the user's
  disfavour, so a limit can never be exceeded undetected. Documented in
  docs/PHASE3.md and pinned by tests.
- Wall-vs-monotonic disagreement > 60 s closes all sessions safely, logs to
  `clock_log`, and resumes fresh (current tick attributes 0).
- Suspend (Phase 3 wires WM_POWERBROADCAST -> `notify_suspend()`) splits
  sessions; the gap accrues nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.clock import Clock
from app.core.timeutils import (
    classify_skew,
    day_str_of_epoch,
    local_midnights_between,
)
from app.database.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Activity:
    active: bool
    session_type: str = "FOREGROUND"


@dataclass(frozen=True)
class TrackInfo:
    rule_id: int
    tracked_active: bool  # session open after this tick
    session_id: int | None
    session_seconds: int  # continuous session total (spans midnight splits)
    day: str  # local day of the current segment
    attributed_seconds: int  # seconds attributed by THIS tick


@dataclass
class _Active:
    session_id: int
    session_type: str
    day: str  # local day the current DB row attributes to
    continuous_seconds: int = 0
    segment_seconds: int = 0  # seconds in the current DB row
    unpersisted: int = 0  # seconds counted but not yet in daily_usage (for `day`)
    last_mono: float = 0.0
    last_wall: float = 0.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class Tracker:
    SKEW_TOLERANCE = 60.0

    def __init__(
        self,
        db: Database,
        clock: Clock,
        persist_seconds: int = 5,
        max_single_delta: int = 600,
    ) -> None:
        if persist_seconds < 1 or max_single_delta < 1:
            raise ValueError("persist_seconds and max_single_delta must be >= 1.")
        self._db = db
        self._clock = clock
        self._persist_seconds = persist_seconds
        self._max_single_delta = max_single_delta
        self._active: dict[int, _Active] = {}
        self._baseline: tuple[float, float] | None = None  # (wall, mono)

    # ------------------------------------------------------------------ main
    def poll(self, activities: dict[int, Activity]) -> dict[int, TrackInfo]:
        """Advance all rules to now. Rules absent from the map count as inactive."""
        now_wall = self._clock.wall()
        now_mono = self._clock.mono()
        if self._baseline is None:
            self._baseline = (now_wall, now_mono)
            return self._poll_inner(activities, now_wall, now_mono, fresh=True)
        last_wall, last_mono = self._baseline
        note = classify_skew(now_wall - last_wall, now_mono - last_mono, self.SKEW_TOLERANCE)
        if note is not None:
            log.warning("Clock anomaly: %s — closing sessions safely.", note)
            self._db.log_clock(f"tracker anomaly, sessions safely closed: {note}")
            self._close_all_internal()
            self._baseline = (now_wall, now_mono)
            return self._poll_inner(activities, now_wall, now_mono, fresh=True)
        self._baseline = (now_wall, now_mono)
        return self._poll_inner(activities, now_wall, now_mono, fresh=False)

    def _poll_inner(
        self, activities: dict[int, Activity], now_wall: float, now_mono: float, fresh: bool
    ) -> dict[int, TrackInfo]:
        merged: dict[int, Activity] = dict(activities)
        for rid in self._active:
            merged.setdefault(rid, Activity(active=False))
        today = day_str_of_epoch(now_wall)
        infos: dict[int, TrackInfo] = {}
        for rule_id, act in merged.items():
            st = self._active.get(rule_id)
            if st is None:
                if act.active:
                    sid = self._db.open_session(
                        rule_id, act.session_type, started_at=_iso(now_wall)
                    )
                    self._active[rule_id] = _Active(
                        session_id=sid,
                        session_type=act.session_type,
                        day=today,
                        last_mono=now_mono,
                        last_wall=now_wall,
                    )
                    infos[rule_id] = TrackInfo(rule_id, True, sid, 0, today, 0)
                else:
                    infos[rule_id] = TrackInfo(rule_id, False, None, 0, today, 0)
                continue
            delta = 0 if fresh else max(0, int(now_mono - st.last_mono))
            if delta > self._max_single_delta:
                # Belt & braces for a missed suspend event: cap, log, continue.
                self._db.log_clock(
                    f"rule {rule_id}: single-tick delta {delta}s capped "
                    f"to {self._max_single_delta}s"
                )
                delta = self._max_single_delta
            attributed = 0
            if delta > 0:
                attributed = self._attribute(rule_id, st, st.last_wall, now_wall, delta)
            st.last_mono, st.last_wall = now_mono, now_wall
            if not act.active:
                continuous = st.continuous_seconds
                self._close_rule(rule_id, st)
                infos[rule_id] = TrackInfo(rule_id, False, None, continuous, st.day, attributed)
            else:
                if st.unpersisted >= self._persist_seconds:
                    self._persist_rule(rule_id, st)
                infos[rule_id] = TrackInfo(
                    rule_id, True, st.session_id, st.continuous_seconds, st.day, attributed
                )
        return infos

    # ------------------------------------------------------------- accounting
    def _attribute(
        self, rule_id: int, st: _Active, last_wall: float, now_wall: float, delta: int
    ) -> int:
        """Attribute `delta` monotonic seconds across days; returns delta."""
        cuts = [last_wall]
        if now_wall > last_wall:
            cuts += local_midnights_between(last_wall, now_wall)
        cuts.append(now_wall)
        total_wall = now_wall - last_wall
        acc = 0
        for i in range(len(cuts) - 1):
            a, b = cuts[i], cuts[i + 1]
            if i < len(cuts) - 2 and total_wall > 0:
                share = int(delta * (b - a) / total_wall)
                acc += share
            else:
                share = delta - acc  # last segment takes the remainder (exact sum)
            if share <= 0:
                continue
            day = day_str_of_epoch(a if b > a else now_wall)
            if day != st.day:
                self._roll_day(rule_id, st, day, at_epoch=a)
            st.continuous_seconds += share
            st.segment_seconds += share
            st.unpersisted += share
        return delta

    def _roll_day(self, rule_id: int, st: _Active, new_day: str, at_epoch: float) -> None:
        """Midnight split: seal the old segment row, open a fresh one."""
        self._persist_rule(rule_id, st)
        self._db.close_session(st.session_id, st.segment_seconds)
        st.session_id = self._db.open_session(
            rule_id, st.session_type, started_at=_iso(at_epoch)
        )
        st.day = new_day
        st.segment_seconds = 0

    def _persist_rule(self, rule_id: int, st: _Active) -> None:
        if st.unpersisted > 0:
            self._db.add_daily(rule_id, st.day, st.unpersisted)
            st.unpersisted = 0
        self._db.heartbeat_session(st.session_id, st.segment_seconds)

    def _close_rule(self, rule_id: int, st: _Active) -> None:
        self._persist_rule(rule_id, st)
        self._db.close_session(st.session_id, st.segment_seconds)
        del self._active[rule_id]

    def _close_all_internal(self) -> None:
        for rule_id in list(self._active):
            st = self._active[rule_id]
            self._persist_rule(rule_id, st)
            # Sessions closed here kept only already-attributed time (their
            # heartbeat value) — the anomalous gap itself attributes 0.
            self._db.close_session(st.session_id, st.segment_seconds)
            del self._active[rule_id]

    # ------------------------------------------------------------------ reads
    def today_total(self, rule_id: int, day: str | None = None) -> int:
        """DB aggregate + unflushed remainder (never under-reads)."""
        day = day if day is not None else day_str_of_epoch(self._clock.wall())
        total = self._db.get_daily(rule_id, day)
        st = self._active.get(rule_id)
        if st is not None and st.day == day:
            total += st.unpersisted
        return total

    def session_seconds(self, rule_id: int) -> int:
        st = self._active.get(rule_id)
        return st.continuous_seconds if st else 0

    def open_rule_ids(self) -> list[int]:
        return sorted(self._active)

    # -------------------------------------------------------------- lifecycle
    def flush(self) -> None:
        """Persist all unflushed time + heartbeat open rows."""
        for rule_id, st in self._active.items():
            self._persist_rule(rule_id, st)

    def close_all(self) -> None:
        """Graceful shutdown: persist + seal every open session."""
        self._close_all_internal()

    def close_rule(self, rule_id: int) -> bool:
        """Persist + seal one rule's open session (True if one was open).

        Used when counting must stop at a hard boundary — a pause starting, for
        example — so the interval in progress is not billed afterwards.
        """
        st = self._active.get(rule_id)
        if st is None:
            return False
        self._close_rule(rule_id, st)
        return True

    def notify_suspend(self) -> None:
        """Machine is sleeping: seal sessions; the gap accrues nothing."""
        self._db.log_clock("suspend: open sessions sealed, gap will not accrue")
        self._close_all_internal()
        self._baseline = None  # resume starts fresh (skew check restarts)
