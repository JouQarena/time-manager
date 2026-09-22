"""PauseController: time-boxed pauses, STRICT immunity, persistence."""

from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.pause import MAX_PAUSE_MINUTES, PauseController
from app.core.rules.models import Rule
from app.core.types import Action, Mode, RuleType
from app.database.db import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 10, 0))
    yield db, clock, PauseController(db, clock)
    db.close()


def rule(mode=Mode.NORMAL, name="Discord"):
    r = Rule(name=name, type=RuleType.APPLICATION, target="discord.exe",
             executable="discord.exe", daily_limit_seconds=600,
             action=Action.CLOSE, mode=mode)
    r.id = 1
    return r


def test_starts_inactive(env):
    db, clock, ctl = env
    assert ctl.is_paused() is False
    assert ctl.remaining_seconds() == 0
    assert ctl.suppresses(rule()) is False


def test_pause_and_countdown(env):
    db, clock, ctl = env
    ctl.pause(15)
    assert ctl.is_paused() is True
    assert ctl.remaining_seconds() == 15 * 60
    clock.advance(60)  # one minute of fake time
    assert ctl.remaining_seconds() == 14 * 60
    assert ctl.suppresses(rule()) is True


def test_pause_auto_expires(env):
    db, clock, ctl = env
    ctl.pause(1)
    clock.advance(61)
    assert ctl.is_paused() is False
    assert ctl.remaining_seconds() == 0
    assert ctl.suppresses(rule()) is False
    # expiry is logged, and the setting is cleared so a restart stays unpaused
    notes = [row["note"] for row in db.conn.execute("SELECT note FROM clock_log")]
    assert any("pause ended (expired)" in (n or "") for n in notes)
    assert db.get_setting("pause_until") == ""


def test_strict_rules_ignore_pause(env):
    db, clock, ctl = env
    ctl.pause(30)
    assert ctl.suppresses(rule(Mode.NORMAL)) is True
    assert ctl.suppresses(rule(Mode.STRICT, name="League")) is False
    state = ctl.state([rule(Mode.NORMAL), rule(Mode.STRICT, name="League")])
    assert state.active is True
    assert state.exempt_rules == ("League",)


def test_limits_are_clamped(env):
    db, clock, ctl = env
    ctl.pause(0)  # below the minimum
    assert ctl.remaining_seconds() == 60
    ctl.pause(100_000)  # above the maximum
    assert ctl.remaining_seconds() == MAX_PAUSE_MINUTES * 60


def test_resume_clears_it(env):
    db, clock, ctl = env
    ctl.pause(60)
    ctl.resume()
    assert ctl.is_paused() is False
    notes = [row["note"] for row in db.conn.execute("SELECT note FROM clock_log")]
    assert any("pause ended (manual resume)" in (n or "") for n in notes)


def test_pause_survives_restart(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 10, 0))
    PauseController(db, clock).pause(30)
    db.close()

    # "Restart": a new controller on the same database, 10 minutes later.
    db2 = Database(tmp_path / "t.db").connect()
    clock2 = FakeClock()
    clock2.set_wall(datetime(2026, 9, 21, 10, 10))
    ctl2 = PauseController(db2, clock2)
    assert ctl2.is_paused() is True
    assert 19 * 60 <= ctl2.remaining_seconds() <= 20 * 60
    db2.close()


def test_expired_pause_is_not_restored(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 10, 0))
    PauseController(db, clock).pause(5)
    db.close()

    db2 = Database(tmp_path / "t.db").connect()
    clock2 = FakeClock()
    clock2.set_wall(datetime(2026, 9, 21, 11, 0))  # long past the deadline
    ctl2 = PauseController(db2, clock2)
    assert ctl2.is_paused() is False
    assert db2.get_setting("pause_until") == ""
    db2.close()


def test_unparseable_setting_is_ignored(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    db.set_setting("pause_until", "not-a-date")
    clock = FakeClock()
    ctl = PauseController(db, clock)
    assert ctl.is_paused() is False
    db.close()


def test_state_reports_until_iso(env):
    db, clock, ctl = env
    ctl.pause(10)
    state = ctl.state([rule()])
    assert state.until_iso.startswith("2026-09-21T10:10")
    assert state.minutes_left == 10
