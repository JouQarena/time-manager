"""MonitorLoop: wiring, sleep detection, error resilience, day rollover."""

import threading
import time
from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.enforcement.adapters import EnforcementExecutor, RecordingNotifier
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.monitor import MonitorLoop
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.engine import RuleEngine
from app.core.rules.models import Rule
from app.core.tracking.tracker import Tracker
from app.core.types import Action, RuleState, RuleType
from app.database.db import Database
from app.testing.fakes import FakeProcessController, FakeProcessSource


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 10, 0))
    source = FakeProcessSource()
    source.clock = clock
    controller = FakeProcessController(source)
    tracker = Tracker(db, clock)
    engine = RuleEngine(db, tracker, clock)
    closer = ProcessCloser(controller, graceful_timeout=5.0)
    notifier = RecordingNotifier()
    executor = EnforcementExecutor(db, closer, clock, policy=KillPolicy(), notifier=notifier)
    resolver = ActivityResolver(policy=ActivityPolicy(background_grace_seconds=5))
    loop = MonitorLoop(db, engine, tracker, resolver, executor, source, clock,
                       closer=closer, interval=5.0)
    yield db, clock, source, controller, tracker, loop, notifier
    db.close()


def add_rule(db, limit=120, action=Action.CLOSE, **kw):
    rule = Rule(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
                executable="discord.exe", daily_limit_seconds=limit, action=action,
                warning_seconds=(60,), **kw)
    return db.add_rule(rule)


def step(clock, loop, seconds=5):
    clock.advance(seconds)
    return loop.tick_once()


def test_warns_then_closes_end_to_end(env):
    db, clock, source, controller, tracker, loop, notifier = env
    rid = add_rule(db, limit=120)
    pid = source.launch("discord.exe")
    source.focus("discord.exe")

    loop.tick_once()
    for _ in range(5):  # 25s
        step(clock, loop)
    assert tracker.today_total(rid) == 25

    for _ in range(19):  # reach 120s -> limit
        step(clock, loop)
    assert tracker.today_total(rid) == 120
    assert controller.requests == [pid]  # closed exactly once
    assert not source.running("discord.exe")
    assert db.get_state(rid)[1] == RuleState.ENFORCED
    assert any("limit reached" in m[0] for m in notifier.messages)


def test_warning_notification_fires_once(env):
    db, clock, source, controller, tracker, loop, notifier = env
    add_rule(db, limit=120)
    source.launch("discord.exe")
    source.focus("discord.exe")
    for _ in range(30):  # 150s: crosses the 60s warning and the limit
        step(clock, loop)
    warnings = [m for m in notifier.messages if "left" in m[0]]
    assert len(warnings) == 1


def test_time_freezes_when_focus_moves_away(env):
    db, clock, source, controller, tracker, loop, notifier = env
    rid = add_rule(db)
    source.launch("discord.exe")
    source.focus("discord.exe")
    loop.tick_once()
    for _ in range(4):  # 20s foreground
        step(clock, loop)
    assert tracker.today_total(rid) == 20
    source.focus("notepad.exe")  # user works elsewhere
    for _ in range(20):  # 100s
        step(clock, loop)
    # Contract: counting stops within (grace + one tick). The tracker bills an
    # interval when the session was open across it, so the first poll that sees
    # "inactive" still closes out the interval it just measured.
    assert 20 <= tracker.today_total(rid) <= 20 + 5 + 5
    frozen = tracker.today_total(rid)
    for _ in range(20):  # another 100s in the background
        step(clock, loop)
    assert tracker.today_total(rid) == frozen  # and then it is truly frozen


def test_sleep_gap_triggers_suspend_handling(env):
    db, clock, source, controller, tracker, loop, notifier = env
    rid = add_rule(db)
    source.launch("discord.exe")
    source.focus("discord.exe")
    loop.tick_once()
    step(clock, loop)
    assert tracker.today_total(rid) == 5

    clock.advance(3 * 3600)  # laptop lid closed for three hours
    report = loop.tick_once()
    assert report.sleep_gap_seconds >= 3 * 3600 - 5
    assert loop.stats.last_gap_seconds > 0
    assert tracker.today_total(rid) == 5  # the nap is not billed
    rows = db.conn.execute("SELECT note FROM clock_log").fetchall()
    assert any("suspend" in (r["note"] or "") for r in rows)


def test_tick_errors_are_contained(env):
    db, clock, source, controller, tracker, loop, notifier = env
    rid = add_rule(db)

    class ExplodingSource(FakeProcessSource):
        explode = False

        def snapshot(self):
            if self.explode:
                raise OSError("process table unavailable")
            return super().snapshot()

    loop._source = ExplodingSource()
    loop._source.clock = clock
    loop._source.explode = True
    report = loop.tick_once()
    assert report.error and loop.stats.errors == 1
    # next tick works normally: the agent is not dead
    loop._source.explode = False
    report = loop.tick_once()
    assert not report.error and loop.stats.ticks == 2


def test_day_rollover_resets_notifications_and_counts(env):
    db, clock, source, controller, tracker, loop, notifier = env
    rid = add_rule(db, limit=60)
    source.launch("discord.exe")
    source.focus("discord.exe")
    loop.tick_once()  # opens the session (attributed 0 by design)
    for _ in range(14):  # 70s of ticks; the limit hits at 60s and closes it
        step(clock, loop)
    # 60s of legal usage + the one closing interval the tracker was mid-way
    # through when the app disappeared.
    assert tracker.today_total(rid) == 65

    clock.advance(23 * 3600)  # 10:00 Monday -> 09:00 Tuesday, mono and wall agree
    report = loop.tick_once()
    assert report.day == "2026-09-22"
    assert report.sleep_gap_seconds > 0  # 23h of "nobody was at the keyboard"
    assert report.outcomes[0].today_used_seconds == 0  # fresh day, fresh count
    assert report.outcomes[0].decision.new_state in (RuleState.NORMAL, RuleState.WARNING)

    source.launch("discord.exe")
    source.focus("discord.exe")
    step(clock, loop, 5)  # opens the new day's session (attributed 0)
    step(clock, loop, 5)
    assert tracker.today_total(rid) == 5
    # the new day starts from scratch, yesterday's total intact
    assert db.get_daily(rid, "2026-09-21") == 65


def test_website_activity_provider_is_used(env):
    db, clock, source, controller, tracker, loop, notifier = env
    rule = Rule(name="YT", type=RuleType.WEBSITE, target="youtube.com",
                domain="youtube.com", daily_limit_seconds=60, action=Action.BLOCK,
                warning_seconds=(30,))
    rid = db.add_rule(rule)
    # The extension's reports only count while a browser is the foreground app.
    source.launch("chrome.exe")
    source.focus("chrome.exe")
    loop.set_web_state_provider(lambda: {"youtube.com": clock.mono()})
    for _ in range(10):
        step(clock, loop)
    assert tracker.today_total(rid) >= 30  # counted from the provider, not the OS
    loop.set_web_state_provider(lambda: None)
    before = tracker.today_total(rid)
    for _ in range(10):
        step(clock, loop)
    # The extension going quiet closes the open session: at most the interval
    # it was last seen in, then nothing.
    assert tracker.today_total(rid) <= before + 5
    frozen = tracker.today_total(rid)
    for _ in range(5):
        step(clock, loop)
    assert tracker.today_total(rid) == frozen


def test_threaded_loop_starts_and_stops(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    source = FakeProcessSource()
    tracker = Tracker(db, clock)
    loop = MonitorLoop(
        db, RuleEngine(db, tracker, clock), tracker,
        ActivityResolver(), EnforcementExecutor(db, ProcessCloser(
            FakeProcessController(source)), clock), source, clock,
        interval=0.05,
    )
    loop.start()
    deadline = time.time() + 3
    while loop.stats.ticks < 3 and time.time() < deadline:
        time.sleep(0.02)
    loop.stop()
    assert loop.stats.ticks >= 3
    assert not loop.running
    assert loop.stats.errors == 0
    db.close()
