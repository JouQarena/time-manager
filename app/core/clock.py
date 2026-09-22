"""Clock abstraction: real time in production, fake time in tests.

Contract:
- `mono()` is the duration truth (never goes backwards, immune to wall jumps).
- `wall()` / `local_now()` decide calendar-day attribution only.
- `FakeClock` lets tests time-travel deterministically (midnight crossings,
  multi-day runs, forward/backward wall jumps with monotonic held steady).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def wall(self) -> float:
        """Wall-clock epoch seconds (date attribution)."""
        ...

    def mono(self) -> float:
        """Monotonic seconds (durations)."""
        ...

    def local_now(self) -> datetime:
        """Timezone-aware local now."""
        ...


class SystemClock:
    def wall(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()

    def local_now(self) -> datetime:
        return datetime.now().astimezone()


class FakeClock:
    """Deterministic clock. `advance()` moves wall+mono together (real time
    passing); `jump_wall()` / `set_wall()` move the wall only (user edits the
    clock, DST shift, timezone change)."""

    def __init__(self, start: datetime | None = None, mono_start: float = 1000.0) -> None:
        if start is None:
            start = datetime(2026, 9, 21, 10, 0, 0)  # naive = local; a Monday
        if start.tzinfo is None:
            start = start.astimezone()
        self._wall = start.timestamp()
        self._mono = float(mono_start)

    def wall(self) -> float:
        return self._wall

    def mono(self) -> float:
        return self._mono

    def local_now(self) -> datetime:
        return datetime.fromtimestamp(self._wall).astimezone()

    def advance(self, seconds: float) -> "FakeClock":
        """Real time passes: wall and monotonic advance together."""
        if seconds < 0:
            raise ValueError("advance() needs non-negative seconds (use jump_wall for jumps).")
        self._wall += seconds
        self._mono += seconds
        return self

    def jump_wall(self, seconds: float) -> "FakeClock":
        """Wall clock jumps (manual change); monotonic unaffected."""
        self._wall += seconds
        return self

    def set_wall(self, dt: datetime) -> "FakeClock":
        """Teleport the wall clock; monotonic unaffected."""
        if dt.tzinfo is None:
            dt = dt.astimezone()
        self._wall = dt.timestamp()
        return self
