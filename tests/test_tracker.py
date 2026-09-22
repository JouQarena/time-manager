from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.rules.models import Rule
from app.core.tracking.tracker import Activity, Tracker
from app.database.db import Database


@pytest.fixture()
def setup(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    tracker = Tracker(db, clock)
    rid = db.add_rule(Rule(
        name="Discord", type="APPLICATION", target="discord.exe",
        executable="discord.exe", daily_limit_seconds=3600, action="CLOSE"))
    yield db, clock, tracker, rid
    db.close()


def active_map(rid, on=True):
    return {rid: Activity(active=on)}


def test_counts_active_seconds(setup):
    db, clock, tracker, rid = setup
    tracker.poll(active_map(rid))  # opens, attributes 0
    clock.advance(10)
    info = tracker.poll(active_map(rid))[rid]
    assert info.attributed_seconds == 10
    assert info.session_seconds == 10
    assert tracker.today_total(rid) == 10  # exact even before flush
    clock.advance(7)
    tracker.poll(active_map(rid))
    assert tracker.today_total(rid) == 17


def test_pause_and_resume(setup):
    db, clock, tracker, rid = setup
    tracker.poll(active_map(rid))
    clock.advance(10)
    tracker.poll(active_map(rid))
    clock.advance(5)
    info = tracker.poll(active_map(rid, on=False))[rid]  # closing tick
    assert info.attributed_seconds == 5  # final interval counted (was active)
    assert not info.tracked_active
    assert tracker.today_total(rid) == 15
    clock.advance(100)
    tracker.poll(active_map(rid, on=False))
    assert tracker.today_total(rid) == 15  # idle accrues nothing
    assert db.get_daily(rid, "2026-09-21") == 15  # close flushes to DB


def test_midnight_split_exact(setup):
    db, clock, tracker, rid = setup
    clock.set_wall(datetime(2026, 9, 21, 23, 59, 50))
    tracker.poll(active_map(rid))
    clock.advance(15)  # -> 00:00:05 next day
    info = tracker.poll(active_map(rid))[rid]
    assert info.session_seconds == 15  # continuous total keeps running
    tracker.flush()
    assert db.get_daily(rid, "2026-09-21") == 10
    assert db.get_daily(rid, "2026-09-22") == 5
    rows = db.conn.execute(
        "SELECT COUNT(*) c FROM usage_sessions WHERE rule_id=?", (rid,)).fetchone()
    assert rows["c"] == 2  # one segment row per day


def test_suspend_accrues_nothing(setup):
    db, clock, tracker, rid = setup
    tracker.poll(active_map(rid))
    clock.advance(10)
    tracker.poll(active_map(rid))
    tracker.notify_suspend()
    clock.advance(3600)  # an hour "passes" while suspended
    info = tracker.poll(active_map(rid))[rid]
    assert info.session_seconds == 0  # fresh session after resume
    assert tracker.today_total(rid) == 10
    assert tracker.open_rule_ids() == [rid]


def test_forward_wall_jump_is_safe(setup):
    db, clock, tracker, rid = setup
    tracker.poll(active_map(rid))
    clock.advance(10)
    tracker.poll(active_map(rid))
    clock.jump_wall(7200)  # user pushes clock forward 2h; mono unchanged
    info = tracker.poll(active_map(rid))[rid]
    assert info.attributed_seconds == 0  # anomalous tick attributes nothing
    assert tracker.today_total(rid) == 10  # counted time neither inflated...
    clock.advance(5)
    tracker.poll(active_map(rid))
    assert tracker.today_total(rid) == 15  # ...nor lost; counting resumes
    n = db.conn.execute("SELECT COUNT(*) c FROM clock_log").fetchone()["c"]
    assert n >= 1


def test_backward_wall_jump_is_safe(setup):
    db, clock, tracker, rid = setup
    tracker.poll(active_map(rid))
    clock.advance(30)
    tracker.poll(active_map(rid))
    clock.jump_wall(-3600)  # user rewinds the clock: no free time granted
    tracker.poll(active_map(rid))
    assert tracker.today_total(rid) == 30
    # Day key moved back an hour (same date here); total for the (same) local
    # day still reads 30 — mono-based accounting is immune to the rewind.


def test_single_tick_cap_guards_missed_suspend(setup):
    db, clock, tracker, rid = setup
    tracker.poll(active_map(rid))
    clock.advance(3600)  # e.g. suspend event missed; wall+mono agree (no skew)...
    info = tracker.poll(active_map(rid))[rid]
    assert info.attributed_seconds == 600  # ...but the cap still bounds damage
    assert tracker.today_total(rid) == 600


def test_restart_persists_without_double_count(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    rid = db.add_rule(Rule(
        name="YT", type="WEBSITE", target="youtube.com", domain="youtube.com",
        daily_limit_seconds=2700, action="BLOCK"))
    clock = FakeClock()
    t1 = Tracker(db, clock)
    t1.poll({rid: Activity(active=True, session_type="WEBSITE")})
    clock.advance(42)
    t1.poll({rid: Activity(active=True, session_type="WEBSITE")})
    t1.close_all()  # graceful shutdown persists everything
    # "Restart": brand-new tracker on the same DB.
    t2 = Tracker(db, clock)
    assert t2.today_total(rid) == 42
    clock.advance(8)
    t2.poll({rid: Activity(active=True, session_type="WEBSITE")})
    # Fresh session attributes only new intervals: 42 + 8... first post-restart
    # tick opens the session (0), so advance once more to accrue.
    clock.advance(8)
    t2.poll({rid: Activity(active=True, session_type="WEBSITE")})
    assert t2.today_total(rid) == 42 + 8
    t2.close_all()
    # Crash path: open row left behind -> recovery seals it exactly once.
    sid = db.open_session(rid, "WEBSITE")
    db.heartbeat_session(sid, 11)
    assert db.recover_open_sessions() == 1
    assert db.recover_open_sessions() == 0
    db.close()
