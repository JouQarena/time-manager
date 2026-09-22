"""Phase 7: usage floors — DB rollback cannot buy free time for STRICT rules."""

import json

import pytest

from app.core.clock import FakeClock
from app.core.rules.engine import ActivityInput, RuleEngine
from app.core.rules.models import Rule
from app.core.security.floor import UsageFloor, floor_path_for
from app.core.tracking.tracker import Tracker
from app.core.types import Action, Mode, RuleState, RuleType
from app.database.db import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()  # 2026-09-21 local
    floor = UsageFloor(tmp_path / "usage_floor.json", clock, db=db)
    yield db, clock, floor, tmp_path
    db.close()


def make_rule(db, mode=Mode.STRICT, **kw):
    base = dict(name="YouTube App", type=RuleType.APPLICATION, target="yt.exe",
                executable="yt.exe", daily_limit_seconds=120,
                warning_seconds=(60,), action=Action.CLOSE, mode=mode)
    base.update(kw)
    rid = db.add_rule(Rule(**base))
    return db.get_rule(rid)


DAY = "2026-09-21"


def test_strict_usage_is_clamped_to_the_floor(env):
    db, clock, floor, _ = env
    rule = make_rule(db, mode=Mode.STRICT)
    floor.observe(rule.id, DAY, 100)

    assert floor.raise_for(rule, DAY, 30) == 100   # rows erased -> floor wins
    assert floor.raise_for(rule, DAY, 100) == 100  # equal -> no finding
    assert floor.raise_for(rule, DAY, 150) == 150  # normal growth

    rows = db.list_audit(kind="USAGE_TAMPERED")
    assert len(rows) == 1  # deduped per rule/day despite repeated clamping
    assert rows[0]["severity"] == "critical"


def test_normal_rules_are_not_clamped(env):
    db, clock, floor, _ = env
    rule = make_rule(db, mode=Mode.NORMAL)
    floor.observe(rule.id, DAY, 100)
    assert floor.raise_for(rule, DAY, 5) == 5
    assert db.list_audit(kind="USAGE_TAMPERED") == []


def test_floor_is_per_day(env):
    db, clock, floor, _ = env
    rule = make_rule(db)
    floor.observe(rule.id, DAY, 100)
    assert floor.floor_for(rule.id, DAY) == 100
    assert floor.floor_for(rule.id, "2026-09-22") == 0  # midnight resets it
    floor.observe(rule.id, "2026-09-22", 7)
    assert floor.floor_for(rule.id, DAY) == 0  # old day dropped
    assert floor.floor_for(rule.id, "2026-09-22") == 7


def test_floor_survives_reload_and_never_lowers(env):
    db, clock, floor, tmp_path = env
    floor.observe(99, DAY, 42)
    clock.advance(10)
    floor.observe(99, DAY, 10)  # lower value must not lower the floor
    floor.flush()

    reloaded = UsageFloor(tmp_path / "usage_floor.json", clock, db=db)
    reloaded.load()
    assert reloaded.floor_for(99, DAY) == 42


def test_corrupt_floor_file_degrades_to_no_floors(env):
    db, clock, floor, tmp_path = env
    path = tmp_path / "usage_floor.json"
    path.write_text("{definitely not json", encoding="utf-8")
    fresh = UsageFloor(path, clock, db=db)
    fresh.load()  # must not raise
    assert fresh.floor_for(1, DAY) == 0


def test_floor_file_lives_outside_the_database(tmp_path):
    db_path = tmp_path / "timemanager.db"
    assert floor_path_for(db_path) == tmp_path / "timemanager.floor.json"
    assert floor_path_for(db_path) != db_path  # a sibling file, not the DB


def test_clock_regression_detection(tmp_path):
    clock = FakeClock()
    floor = UsageFloor(tmp_path / "f.json", clock)
    floor.observe(1, DAY, 10)
    floor.flush()

    assert floor.check_clock_regression() is None  # same instant
    clock.jump_wall(-30)  # small NTP-ish wobble inside tolerance (90 s)
    assert floor.check_clock_regression() is None
    clock.jump_wall(-3600)  # rolled back an hour: suspicious
    regressed = floor.check_clock_regression()
    assert regressed is not None and regressed > 3600


def test_engine_enforces_from_floor_after_db_rollback(env):
    """End-to-end: usage accrues, the DB is rolled back, the STRICT rule still
    sees its floor and stays enforced."""
    db, clock, floor, _ = env
    rule = make_rule(db, mode=Mode.STRICT, daily_limit_seconds=100)
    tracker = Tracker(db, clock)
    engine = RuleEngine(db, tracker, clock, floor=floor)

    on = {rule.id: ActivityInput(active=True)}
    engine.tick(on)
    for _ in range(30):  # 30 x 5s steps = 150s of real usage
        clock.advance(5)
        engine.tick(on)
    assert floor.floor_for(rule.id, DAY) == 150

    # The user stops the agent, zeroes today's usage in the DB, restarts.
    db.conn.execute("UPDATE daily_usage SET total_seconds=0 WHERE day=?", (DAY,))
    db.conn.commit()
    tracker2 = Tracker(db, clock)
    engine2 = RuleEngine(db, tracker2, clock, floor=floor)
    outcome = engine2.tick({rule.id: ActivityInput(active=False)})[0]

    assert outcome.today_used_seconds == 150  # floor, not the erased 0
    assert outcome.decision.new_state == RuleState.ENFORCED
    assert outcome.decision.reason.value == "DAILY_LIMIT_REACHED"
    assert db.list_audit(kind="USAGE_TAMPERED") != []


def test_tamper_check_is_non_mutating(env):
    db, clock, floor, _ = env
    rule = make_rule(db, mode=Mode.STRICT)
    floor.observe(rule.id, DAY, 80)
    alert = floor.tamper_check(rule, DAY, 10)
    assert alert is not None and alert.floor_seconds == 80
    assert alert.message.startswith(rule.name)
    assert db.list_audit(kind="USAGE_TAMPERED") == []  # check alone writes nothing
    assert floor.tamper_check(rule, DAY, 90) is None
