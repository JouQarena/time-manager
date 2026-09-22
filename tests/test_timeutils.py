from datetime import datetime, timedelta, timezone

from app.core.timeutils import (
    duration_since,
    format_duration,
    local_day_str,
    monotonic,
    next_local_midnight,
    parse_iso,
    utcnow_iso,
)


def test_iso_roundtrip_and_day_key():
    now = utcnow_iso()
    assert parse_iso(now).tzinfo is not None
    assert len(local_day_str().split("-")) == 3  # YYYY-MM-DD


def test_midnight_boundary():
    # 23:50 and next-day 00:05 must be different days.
    late = datetime.now().astimezone().replace(hour=23, minute=50)
    early = (late + timedelta(minutes=15))
    assert local_day_str(late) != local_day_str(early)


def test_monotonic_duration_sane():
    start = monotonic()
    assert duration_since(start) >= 0
    assert duration_since(start + 3600) == 0  # future start clamps to 0


def test_format_duration():
    assert format_duration(45) == "45s"
    assert format_duration(31 * 60) == "31m"
    assert format_duration(92 * 60) == "1h 32m"
    assert format_duration(7200) == "2h"
    assert format_duration(90000) == "1d 1h"


def test_next_midnight():
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    nxt = next_local_midnight(noon)
    assert (nxt - noon).total_seconds() == 12 * 3600
    assert nxt.hour == 0 and nxt.minute == 0
