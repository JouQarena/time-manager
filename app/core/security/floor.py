"""Usage floors — erasing database rows cannot buy free time.

Bypass vector this closes (spec §15/§25): the user stops the agent, edits or
deletes `daily_usage` rows (or the whole database file), restarts, and the
STRICT rule's day looks fresh again.

Defence: while the agent runs, it remembers the *highest* usage it has seen
for each rule for the current local day, in a small JSON file that lives next
to the config — **outside** the database. Deleting/rolling back the DB cannot
touch it. Limits for STRICT rules are then evaluated against
`max(stored_usage, floor)`:

- stored >= floor  → normal operation, nothing happens.
- stored <  floor  → somebody removed usage: the floor wins, and a
  `USAGE_TAMPERED` audit row (critical) is written once per rule per day.

Deliberate scope:
- Floors apply to **STRICT rules only**. NORMAL rules are lenient by design;
  re-arming them after a DB edit is the user's own choice.
- Floors cannot resurrect *deleted rules* — a rule that no longer exists has
  nothing to enforce. Creating rules is a visible action; that is enough.
- The floor file is per-day; midnight resets it exactly like `daily_usage`.
- The file also stores the wall time the agent was last seen alive, which the
  service uses at startup to detect a regressed system clock.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.clock import Clock
from app.core.rules.models import Rule
from app.core.types import Mode
from app.database.db import Database

log = logging.getLogger(__name__)

FLOOR_FILENAME = "usage_floor.json"

#: A startup clock reading may be this far *before* the last seen wall time
#: before we call it a regression (NTP corrections, dual-boot UTC clocks).
CLOCK_REGRESSION_TOLERANCE = 90.0


def floor_path_for(db_path: str | Path) -> Path:
    """Sibling of the database file (`timemanager.db` -> `timemanager.floor.json`).

    Keyed to the DB (not a global profile dir) so second profiles and the
    simulator never see each other's floors — while still living OUTSIDE the
    database file, which is the whole point: rewriting `daily_usage` rows
    cannot touch it.
    """
    return Path(db_path).with_suffix(".floor.json")


@dataclass(frozen=True)
class TamperAlert:
    """`daily_usage` reads lower than the floor we remember for this day."""

    rule_id: int
    rule_name: str
    day: str
    stored_seconds: int
    floor_seconds: int

    @property
    def message(self) -> str:
        return (
            f"{self.rule_name}: recorded usage ({self.stored_seconds}s) is below "
            f"the remembered floor ({self.floor_seconds}s) for {self.day}; "
            "the floor is used."
        )


class UsageFloor:
    """Per-day usage high-water marks, persisted outside the database."""

    def __init__(
        self,
        path: str | Path,
        clock: Clock,
        db: Database | None = None,
        *,
        persist_every: float = 5.0,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._db = db
        self._persist_every = max(0.5, float(persist_every))
        self._day: str | None = None
        self._rules: dict[int, int] = {}
        self._last_seen_wall: str | None = None  # ISO-8601 UTC
        self._dirty = False
        self._last_persist_mono = 0.0
        self._audited: set[tuple[int, str]] = set()  # (rule_id, day) reported

    # ------------------------------------------------------------------- load
    def load(self) -> None:
        """Read the floor file. Missing file = fresh start; corrupt file is
        logged loudly (it *may* itself be a tamper attempt, but it can only
        weaken enforcement, so we say so and continue)."""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._day = data.get("day")
            self._rules = {int(k): int(v) for k, v in (data.get("rules") or {}).items()}
            self._last_seen_wall = data.get("last_seen_wall")
        except (ValueError, OSError, TypeError) as exc:
            log.warning("Usage floor file unreadable (%s); starting without floors.", exc)
            self._day, self._rules, self._last_seen_wall = None, {}, None

    # ---------------------------------------------------------------- observe
    def observe(self, rule_id: int, day: str, seconds: int) -> None:
        """Record actual usage; raises the floor, never lowers it."""
        if day != self._day:
            self._day, self._rules = day, {}
        if seconds > self._rules.get(rule_id, -1):
            self._rules[rule_id] = int(seconds)
            self._dirty = True
        self._last_seen_wall = datetime.fromtimestamp(
            self._clock.wall(), tz=timezone.utc
        ).isoformat()
        self._maybe_persist()

    def floor_for(self, rule_id: int, day: str) -> int:
        if day != self._day:
            return 0
        return self._rules.get(rule_id, 0)

    def raise_for(self, rule: Rule, day: str, stored_seconds: int) -> int:
        """Usage to evaluate limits against: max(stored, floor) for STRICT.

        On a finding, writes one `USAGE_TAMPERED` audit row per rule per day.
        """
        floor = self.floor_for(rule.id if rule.id is not None else -1, day)
        if rule.mode != Mode.STRICT or stored_seconds >= floor:
            return stored_seconds
        key = (rule.id if rule.id is not None else -1, day)
        if key not in self._audited:
            self._audited.add(key)
            detail = (
                f"rule={rule.name!r} day={day} stored={stored_seconds}s floor={floor}s"
            )
            log.warning("Usage tamper detected: %s", detail)
            if self._db is not None:
                try:
                    self._db.log_audit("USAGE_TAMPERED", "critical", detail)
                except Exception:  # noqa: BLE001 - audit must never break ticking
                    log.exception("Could not write USAGE_TAMPERED audit row.")
        return floor

    def tamper_check(self, rule: Rule, day: str, stored_seconds: int) -> TamperAlert | None:
        """Non-mutating view of whether the floor would override `stored`."""
        if rule.mode != Mode.STRICT:
            return None
        floor = self.floor_for(rule.id if rule.id is not None else -1, day)
        if stored_seconds >= floor:
            return None
        return TamperAlert(
            rule_id=rule.id if rule.id is not None else -1,
            rule_name=rule.name,
            day=day,
            stored_seconds=stored_seconds,
            floor_seconds=floor,
        )

    # ---------------------------------------------------------- clock regress
    def last_seen_wall_epoch(self) -> float | None:
        if not self._last_seen_wall:
            return None
        try:
            return datetime.fromisoformat(self._last_seen_wall).timestamp()
        except ValueError:
            return None

    def check_clock_regression(self) -> float | None:
        """Seconds the current wall clock sits BEFORE the last seen wall time,
        when beyond tolerance; else None. A large negative jump at startup
        usually means the clock was rolled back to revive a daily limit."""
        last = self.last_seen_wall_epoch()
        if last is None:
            return None
        delta = self._clock.wall() - last
        if delta < -CLOCK_REGRESSION_TOLERANCE:
            return -delta
        return None

    # ---------------------------------------------------------------- persist
    def _maybe_persist(self) -> None:
        now = self._clock.mono()
        if self._dirty and now - self._last_persist_mono >= self._persist_every:
            self.flush()

    def flush(self) -> None:
        """Atomic write (tmp + replace) so a crash never leaves a torn file."""
        if not self._dirty and self._path.exists():
            return
        payload = {
            "day": self._day,
            "rules": {str(k): v for k, v in sorted(self._rules.items())},
            "last_seen_wall": self._last_seen_wall,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            last_error: OSError | None = None
            for attempt in range(3):
                try:
                    os.replace(tmp, self._path)
                    last_error = None
                    break
                except OSError as err:  # Windows: AV scanners briefly lock
                    last_error = err   # freshly written files; retry.
                    time.sleep(0.05 * (attempt + 1))
            if last_error is not None:
                raise last_error
            self._dirty = False
            self._last_persist_mono = self._clock.mono()
        except OSError:
            log.warning("Could not persist the usage floor to %s.", self._path, exc_info=True)

    def snapshot(self) -> dict:
        return {
            "day": self._day,
            "rules": dict(self._rules),
            "last_seen_wall": self._last_seen_wall,
        }
