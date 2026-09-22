"""Optional per-rule schedules: days + time windows.

JSON shape (stored in `rules.schedule`):
    {"days": "EVERYDAY"|"WEEKDAYS"|"WEEKENDS"|["MON",...],
     "windows": [["08:00","22:00"], ...]}

- `days` omitted/None -> every day. `windows` omitted/empty -> whole day.
- Windows may span midnight: `["22:00","02:00"]` covers 22:00-23:59 AND
  00:00-02:00 (the day-membership is evaluated against the *current* local day
  for the start, and the previous day's spill-over is handled by checking both
  today's and yesterday's windows).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time as dtime

DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def _parse_hhmm(value: str) -> dtime:
    try:
        hh, mm = value.split(":")
        t = dtime(int(hh), int(mm))
    except Exception as exc:
        raise ValueError(f"Invalid time {value!r}, expected HH:MM.") from exc
    return t


@dataclass(frozen=True)
class Schedule:
    days: frozenset[str] | None  # None = every day
    windows: tuple[tuple[dtime, dtime], ...] = ()  # empty = whole day

    def to_json(self) -> str:
        if self.days is None:
            days: object = "EVERYDAY"
        elif self.days == frozenset({"MON", "TUE", "WED", "THU", "FRI"}):
            days = "WEEKDAYS"
        elif self.days == frozenset({"SAT", "SUN"}):
            days = "WEEKENDS"
        else:
            days = sorted(self.days, key=DAY_NAMES.index)
        return json.dumps(
            {
                "days": days,
                "windows": [[s.strftime("%H:%M"), e.strftime("%H:%M")] for s, e in self.windows],
            }
        )

    def is_active(self, now: datetime | None = None) -> bool:
        """True if `now` (local, default now) falls inside the schedule."""
        now = now or datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        today = DAY_NAMES[now.weekday()]
        yesterday = DAY_NAMES[(now.weekday() - 1) % 7]
        now_t = now.time().replace(second=0, microsecond=0)

        def day_ok(day: str) -> bool:
            return self.days is None or day in self.days

        if not self.windows:
            return day_ok(today)

        for start, end in self.windows:
            if start <= end:
                # Same-day window.
                if day_ok(today) and start <= now_t < end:
                    return True
            else:
                # Midnight-spanning: evening part belongs to `today`,
                # morning part belongs to `yesterday`'s window.
                if day_ok(today) and now_t >= start:
                    return True
                if day_ok(yesterday) and now_t < end:
                    return True
        return False


def parse_schedule(value: str | dict | None) -> Schedule | None:
    """Parse stored JSON/dict into a Schedule; None/empty -> None (always on)."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid schedule JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Schedule must be a JSON object.")
    raw_days = value.get("days", "EVERYDAY")
    if raw_days in (None, "EVERYDAY", "EVERY_DAY", "ALL", []):
        days = None
    elif raw_days == "WEEKDAYS":
        days = frozenset({"MON", "TUE", "WED", "THU", "FRI"})
    elif raw_days == "WEEKENDS":
        days = frozenset({"SAT", "SUN"})
    elif isinstance(raw_days, (list, tuple, set)):
        days = frozenset(d.upper() for d in raw_days)
        unknown = days - set(DAY_NAMES)
        if unknown:
            raise ValueError(f"Unknown day names: {sorted(unknown)}")
        if not days:
            days = None
    else:
        raise ValueError(f"Invalid schedule days: {raw_days!r}")

    windows: list[tuple[dtime, dtime]] = []
    for item in value.get("windows", []) or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"Invalid window {item!r}, expected [start, end].")
        start, end = _parse_hhmm(item[0]), _parse_hhmm(item[1])
        if start == end:
            raise ValueError(f"Empty window {item!r}.")
        windows.append((start, end))
    return Schedule(days=days, windows=tuple(windows))
