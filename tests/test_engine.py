"""RuleEngine tick flows with a fake clock (Phase 2 core engine)."""

from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.rules.engine import ActivityInput, RuleEngine
from app.core.rules.models import Rule
from app.core.tracking.tracker import Tracker
from app.core.types import Action, LimitReason, Mode, RuleState, RuleType
from app.database.db import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()  # Monday 2026-09-21 10:00 local
    tracker = Tracker(db, clock)
    engine = RuleEngine(db, tracker, clock)
    yield db, clock, tracker, engine
    db.close()


def add_app(db, **kw):
    base = dict(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
                executable="discord.exe", daily_limit_seconds=120,
                warning_seconds=(60,), action=Action.CLOSE, mode=Mode.NORMAL)
    base.update(kw)
    return db.add_rule(Rule(**base))


def test_full_limit_lifecycle(env):
    db, clock, tracker, engine = env
    rid = add_app(db)
    on = {rid: ActivityInput(active=True)}

    o = engine.tick(on)[0]
    assert o.decision.new_state == RuleState.NORMAL

    clock.advance(70)  # 70/120 used -> 50s left -> 60s warning fires
    o = engine.tick(on)[0]
    assert o.today_used_seconds == 70
    assert o.decision.new_state == RuleState.WARNING
    assert o.decision.warnings_to_fire == (60,)

    clock.advance(10)  # warning must NOT refire
    o = engine.tick(on)[0]
    assert o.decision.warnings_to_fire == ()
    assert o.decision.new_state == RuleState.WARNING

    clock.advance(60)  # 140/120 -> over the daily limit
    o = engine.tick(on)[0]
    assert o.decision.new_state == RuleState.ENFORCED
    assert o.decision.action_to_execute == "CLOSE_APP"
    assert o.decision.reason == LimitReason.DAILY_LIMIT_REACHED

    # State persisted: a fresh engine on the same DB sees ENFORCED.
    engine2 = RuleEngine(db, Tracker(db, clock), clock)
    o2 = engine2.tick({rid: ActivityInput(active=False)})[0]
    assert o2.decision.new_state == RuleState.ENFORCED
    assert o2.today_used_seconds == 140


def test_schedule_gates_counting(env):
    db, clock, tracker, engine = env
    rid = add_app(db, schedule={"days": "WEEKDAYS", "windows": [["18:00", "22:00"]]})
    on = {rid: ActivityInput(active=True)}

    o = engine.tick(on)[0]  # Monday 10:00 -> outside schedule
    assert o.decision.new_state == RuleState.DISABLED
    clock.advance(300)
    o = engine.tick(on)[0]
    assert o.today_used_seconds == 0  # nothing counted outside schedule

    clock.set_wall(datetime(2026, 9, 21, 19, 0))  # Monday evening -> inside
    o = engine.tick(on)[0]
    assert o.decision.new_state == RuleState.NORMAL
    clock.advance(30)
    o = engine.tick(on)[0]
    assert o.today_used_seconds == 30


def test_game_wait_then_enforce(env):
    db, clock, tracker, engine = env
    rid = db.add_rule(Rule(
        name="LoL", type=RuleType.GAME, target="LeagueClient.exe",
        executable="LeagueClient.exe", daily_limit_seconds=100,
        action=Action.WAIT_FOR_SESSION_END, mode=Mode.STRICT))

    clock.advance(0)
    engine.tick({rid: ActivityInput(active=True, session_type="GAME",
                                    in_game_session=True)})
    clock.advance(110)  # limit hit mid-match
    o = engine.tick({rid: ActivityInput(active=True, session_type="GAME",
                                        in_game_session=True)})[0]
    assert o.decision.new_state == RuleState.WAITING_FOR_SESSION_END
    assert o.decision.action_to_execute is None

    clock.advance(600)  # match continues; overtime accrues honestly
    o = engine.tick({rid: ActivityInput(active=True, session_type="GAME",
                                        in_game_session=False)})[0]
    assert o.decision.new_state == RuleState.ENFORCED
    assert o.decision.action_to_execute == "CLOSE_APP"
    assert o.today_used_seconds == 710


def test_day_rollover_resets(env):
    db, clock, tracker, engine = env
    rid = add_app(db)  # 120s daily
    engine.tick({rid: ActivityInput(active=True)})
    clock.advance(130)
    o = engine.tick({rid: ActivityInput(active=True)})[0]
    assert o.decision.new_state == RuleState.ENFORCED

    from datetime import datetime as _dt
    clock.set_wall(_dt(2026, 9, 22, 8, 0))  # Tuesday morning
    o = engine.tick({rid: ActivityInput(active=False)})[0]
    assert o.decision.new_state == RuleState.NORMAL
    assert o.today_used_seconds == 0
    assert db.get_daily(rid, "2026-09-21") == 130  # yesterday intact


def test_disabled_rule_ignored(env):
    db, clock, tracker, engine = env
    rid = add_app(db, enabled=False)
    clock.advance(0)
    engine.tick({rid: ActivityInput(active=True)})
    clock.advance(60)
    o = engine.tick({rid: ActivityInput(active=True)})[0]
    assert o.decision.new_state == RuleState.DISABLED
    assert o.today_used_seconds == 0


def test_session_limit_independent_of_daily(env):
    db, clock, tracker, engine = env
    rid = add_app(db, daily_limit_seconds=3600, session_limit_seconds=60)
    on = {rid: ActivityInput(active=True)}
    engine.tick(on)
    clock.advance(45)
    o = engine.tick(on)[0]
    assert o.decision.new_state in (RuleState.NORMAL, RuleState.WARNING)
    clock.advance(20)  # session 65/60, daily far from cap
    o = engine.tick(on)[0]
    assert o.decision.reason == LimitReason.SESSION_LIMIT_REACHED
    assert o.decision.new_state == RuleState.ENFORCED
