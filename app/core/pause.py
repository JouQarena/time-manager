"""Pause controller — "stop counting for N minutes", without losing the plot.

Rules of the feature (decided once, enforced everywhere):

* A pause stops **counting** and lifts **blocks** for ordinary rules, so a
  break is a real break.
* **STRICT rules ignore pauses.** That is what STRICT means in this project
  ("harder to pause"); the UI says so before the user confirms, and the pause
  banner lists which rules keep running.
* A pause is always **time-boxed and visible**: it auto-expires, it is written
  to `clock_log` (start *and* end), and the tray/dashboard show the remaining
  minutes. There is no invisible "off" switch.
* Enforcement state is *not* rewritten by a pause: when the pause ends, the
  rules that were exceeded are still exceeded — nothing is granted retroactively.

The controller stores the *wall-clock* deadline (so it survives a restart) and
recomputes the monotonic deadline on load; a wall-clock jump while paused can
only shorten/lengthen the visible countdown, never hide the pause.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.clock import Clock

log = logging.getLogger(__name__)

SETTING_KEY = "pause_until"  # ISO wall timestamp, stored in `settings`
MIN_PAUSE_MINUTES = 1
MAX_PAUSE_MINUTES = 8 * 60


@dataclass(frozen=True)
class PauseState:
    active: bool
    until_iso: str = ""
    remaining_seconds: int = 0
    exempt_rules: tuple[str, ...] = ()  # names of STRICT rules still enforced

    @property
    def minutes_left(self) -> int:
        return max(0, self.remaining_seconds // 60)


class PauseController:
    def __init__(self, db, clock: Clock) -> None:
        self._db = db
        self._clock = clock
        self._until_mono: float | None = None
        self._until_iso: str = ""
        self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> None:
        raw = self._db.get_setting(SETTING_KEY, "") or ""
        if not isinstance(raw, str) or not raw:
            return
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            log.warning("Ignoring unparseable pause deadline %r.", raw)
            return
        remaining = (deadline - self._clock.local_now()).total_seconds()
        if remaining <= 0:
            self._db.set_setting(SETTING_KEY, "")
            return
        self._until_mono = self._clock.mono() + remaining
        self._until_iso = raw
        log.info("Resumed an active pause with %.0f s left.", remaining)

    def is_paused(self) -> bool:
        if self._until_mono is None:
            return False
        if self._clock.mono() >= self._until_mono:
            self.clear(reason="expired")
            return False
        return True

    def remaining_seconds(self) -> int:
        if not self.is_paused():
            return 0
        assert self._until_mono is not None
        return max(0, int(self._until_mono - self._clock.mono()))

    def state(self, rules=()) -> PauseState:
        active = self.is_paused()
        exempt = tuple(
            sorted(
                r.name for r in rules
                if getattr(r, "mode", None) is not None and r.mode.value == "STRICT"
            )
        )
        return PauseState(
            active=active,
            until_iso=self._until_iso,
            remaining_seconds=self.remaining_seconds(),
            exempt_rules=exempt if active else (),
        )

    # ------------------------------------------------------------------ change
    def pause(self, minutes: int) -> PauseState:
        minutes = max(MIN_PAUSE_MINUTES, min(int(minutes), MAX_PAUSE_MINUTES))
        deadline = self._clock.local_now() + timedelta(minutes=minutes)
        self._until_iso = deadline.isoformat()
        self._until_mono = self._clock.mono() + minutes * 60
        self._db.set_setting(SETTING_KEY, self._until_iso)
        self._db.log_clock(f"pause started for {minutes} min (until {self._until_iso})")
        log.info("Monitoring paused for %s minutes.", minutes)
        return self.state()

    def resume(self) -> PauseState:
        if self._until_mono is not None:
            self.clear(reason="manual resume")
        return self.state()

    def clear(self, *, reason: str) -> None:
        if self._until_mono is not None or self._until_iso:
            self._db.log_clock(f"pause ended ({reason})")
            log.info("Pause ended (%s).", reason)
        self._until_mono = None
        self._until_iso = ""
        self._db.set_setting(SETTING_KEY, "")

    # ------------------------------------------------------------ per-rule API
    def suppresses(self, rule) -> bool:
        """True when this rule must stop counting / being enforced right now.

        STRICT rules are never suppressed: that is the mode's whole point.
        """
        if not self.is_paused():
            return False
        mode = getattr(rule, "mode", None)
        return not (mode is not None and mode.value == "STRICT")
