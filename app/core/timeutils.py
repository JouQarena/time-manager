"""Time helpers: local-day boundaries, monotonic durations, formatting.

Two clocks, two jobs:
- `time.monotonic()` -> how LONG something lasted (immune to clock changes).
- local wall clock   -> which CALENDAR DAY usage belongs to (resets at 00:00).

All datetimes stored in SQLite are UTC ISO-8601 strings; conversion to the
local day happens here so DST/timezone changes are handled in exactly one place.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo  # py3.9+; unused directly, documents intent
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def utcnow_iso() -> str:
    """Current UTC time as ISO-8601 string for SQLite storage."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string; assume UTC if naive."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_day(dt: datetime | None = None) -> date:
    """Local calendar day for `dt` (default: now). Day resets at 00:00 local."""
    if dt is None:
        return datetime.now().astimezone().date()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


def local_day_str(dt: datetime | None = None) -> str:
    """`YYYY-MM-DD` local day string used as `daily_usage.day` key."""
    return local_day(dt).isoformat()


def monotonic() -> float:
    """Monotonic clock seconds (duration truth, never goes backwards)."""
    return time.monotonic()


def duration_since(monotonic_start: float) -> int:
    """Whole seconds elapsed since a `monotonic()` reading (clamped >= 0)."""
    return max(0, int(monotonic() - monotonic_start))


def next_local_midnight(now: datetime | None = None) -> datetime:
    """Next 00:00 local time after `now` (used for 'resets at' messages)."""
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()
    nxt = (now + __import__("datetime").timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # If now is exactly midnight, reset is *this* midnight (start of new day).
    if now.hour == 0 and now.minute == 0 and now.second == 0:
        return now.replace(microsecond=0)
    return nxt


def format_duration(total_seconds: int) -> str:
    """Human duration: `45s`, `31m`, `1h 32m`, `2h`, `1d 3h`."""
    total_seconds = max(0, int(total_seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, _ = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def detect_clock_jump(last_wall: float, last_mono: float) -> tuple[float, str | None]:
    """Compare wall advancement vs monotonic advancement.

    Returns `(skew_seconds, note)`. Positive skew = wall jumped forward faster
    than real time (or sleep on platforms where monotonic pauses); negative =
    wall went backwards. `note` is None when everything looks sane (<60 s).
    """
    import time as _time

    wall_delta = _time.time() - last_wall
    mono_delta = monotonic() - last_mono
    return wall_delta - mono_delta, classify_skew(wall_delta, mono_delta)


def classify_skew(wall_delta: float, mono_delta: float, tolerance: float = 60.0) -> str | None:
    """Clock-agnostic skew check (works with injected/fake clocks).

    Returns a human note when `wall_delta` and `mono_delta` disagree by more
    than `tolerance` seconds, else None.
    """
    skew = wall_delta - mono_delta
    if abs(skew) < tolerance:
        return None
    if skew > 0:
        return f"wall clock jumped forward +{skew:.0f}s vs monotonic (sleep/time change?)"
    return f"wall clock jumped backward {skew:.0f}s vs monotonic (manual change?)"


def epoch_to_local(epoch: float) -> datetime:
    """Epoch seconds -> timezone-aware local datetime."""
    return datetime.fromtimestamp(epoch).astimezone()


def day_str_of_epoch(epoch: float) -> str:
    """Local `YYYY-MM-DD` for an epoch timestamp."""
    return epoch_to_local(epoch).date().isoformat()


def local_midnights_between(start_epoch: float, end_epoch: float) -> list[float]:
    """Epoch timestamps of local midnights strictly inside `(start, end]`.

    Built by iterating calendar dates (robust across DST transitions).
    Capped at ~1 year of midnights as a sanity guard.
    """
    from datetime import timedelta as _td

    if end_epoch <= start_epoch:
        return []
    out: list[float] = []
    day = epoch_to_local(start_epoch).date()
    while len(out) < 370:
        day += _td(days=1)
        midnight = datetime(day.year, day.month, day.day).astimezone()
        ts = midnight.timestamp()
        if ts > end_epoch:
            break
        out.append(ts)
    return out
