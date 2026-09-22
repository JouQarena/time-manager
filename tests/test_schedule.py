from datetime import datetime

from app.core.scheduling.schedule import Schedule, parse_schedule


def dt(day="2026-09-21", hm="19:00"):  # 2026-09-21 is a Monday
    return datetime.fromisoformat(f"{day}T{hm}:00").astimezone()


def test_none_means_always():
    assert parse_schedule(None) is None
    assert parse_schedule("") is None


def test_weekday_evening_window():
    s = parse_schedule({"days": "WEEKDAYS", "windows": [["18:00", "22:00"]]})
    assert isinstance(s, Schedule)
    assert s.is_active(dt("2026-09-21", "19:00"))  # Mon evening
    assert not s.is_active(dt("2026-09-21", "12:00"))  # Mon noon
    assert not s.is_active(dt("2026-09-26", "19:00"))  # Sat evening


def test_weekends_and_single_day():
    assert parse_schedule({"days": "WEEKENDS"}).is_active(dt("2026-09-26", "10:00"))
    assert not parse_schedule({"days": "WEEKENDS"}).is_active(dt("2026-09-21", "10:00"))
    assert parse_schedule({"days": ["MON"]}).is_active(dt("2026-09-21", "10:00"))
    assert not parse_schedule({"days": ["TUE"]}).is_active(dt("2026-09-21", "10:00"))


def test_midnight_spanning_window():
    s = parse_schedule({"days": ["MON"], "windows": [["22:00", "02:00"]]})
    assert s.is_active(dt("2026-09-21", "23:00"))  # Mon night
    assert s.is_active(dt("2026-09-22", "01:00"))  # Tue early = Mon's window spill
    assert not s.is_active(dt("2026-09-22", "03:00"))
    assert not s.is_active(dt("2026-09-21", "01:00"))  # belongs to Sunday's window


def test_roundtrip_and_errors():
    s = parse_schedule({"days": "WEEKDAYS", "windows": [["08:00", "22:00"]]})
    assert parse_schedule(s.to_json()).to_json() == s.to_json()
    try:
        parse_schedule({"days": ["FUNDAY"]})
    except ValueError:
        pass
    else:
        raise AssertionError("bad day accepted")
