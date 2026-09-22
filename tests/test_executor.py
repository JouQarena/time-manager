"""EnforcementExecutor: decisions -> notified + logged + closed, exactly once."""

from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.enforcement.adapters import EnforcementExecutor, RecordingNotifier
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.engine import ActivityInput, RuleOutcome
from app.core.rules.models import Rule
from app.core.enforcement import state_machine as sm
from app.core.types import Action, Mode, RuleState, RuleType
from app.database.db import Database
from app.testing.fakes import FakeProcessController, FakeProcessSource

DAY = "2026-09-21"


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    source = FakeProcessSource()
    source.clock = clock
    controller = FakeProcessController(source)
    closer = ProcessCloser(controller, graceful_timeout=6.0)
    notifier = RecordingNotifier()
    executor = EnforcementExecutor(db, closer, clock, policy=KillPolicy(), notifier=notifier)
    yield db, clock, source, controller, closer, notifier, executor
    db.close()


def make_rule(db, action=Action.CLOSE, mode=Mode.NORMAL, rule_type=RuleType.APPLICATION,
              exe="discord.exe", **kw):
    fields = dict(name="Discord", type=rule_type, target=exe, executable=exe,
                  daily_limit_seconds=600, action=action, mode=mode, **kw)
    if rule_type == RuleType.WEBSITE:
        return db.add_rule(Rule(name="YT", type=RuleType.WEBSITE, target="youtube.com",
                                domain="youtube.com", daily_limit_seconds=600,
                                action=action, mode=mode, **kw))
    return db.add_rule(Rule(**fields))


def outcome(db, rid, state, action=None, warnings=(), message="limit reached"):
    rule = db.get_rule(rid)
    decision = sm.Decision(new_state=state, action_to_execute=action,
                           warnings_to_fire=tuple(warnings), user_message=message)
    return RuleOutcome(rule=rule, decision=decision, today_used_seconds=700,
                       session_used_seconds=100, session_id=1, tracked_active=True)


def test_close_action_terminates_matched_process(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db)
    pid = source.launch("discord.exe")
    recs = executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")],
                           source.snapshot(), DAY)
    assert controller.requests == [pid]
    assert recs[0].outcome == "EXECUTED" and f"pids={pid}" in recs[0].detail
    rows = db.list_enforcement(DAY)
    assert len(rows) == 1 and rows[0]["action"] == "CLOSE_APP"
    assert any("limit reached" in m[0] for m in notifier.messages)


def test_limit_exceeded_but_target_not_running_logs_once(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db)
    for _ in range(5):  # five ticks in a row with nothing to do
        executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")],
                        source.snapshot(), DAY)
    rows = db.list_enforcement(DAY)
    assert len(rows) == 1  # no heartbeat spam in the audit trail
    assert rows[0]["outcome"] == "SKIPPED"


def test_repeated_close_requests_do_not_duplicate_the_row(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db)
    controller.graceful_ok = False  # process stays alive during the grace period
    pid = source.launch("discord.exe")
    for _ in range(4):
        executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")],
                        source.snapshot(), DAY)
    rows = db.list_enforcement(DAY)
    assert controller.requests[0] == pid
    assert len([r for r in rows if r["outcome"] == "EXECUTED"]) == 2
    # one "started closing" + one "already being closed" — then silence


def test_close_again_after_relaunch(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db)
    source.launch("discord.exe")
    executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")], source.snapshot(), DAY)
    executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")], source.snapshot(), DAY)
    new_pid = source.launch("discord.exe")  # user relaunches
    executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")], source.snapshot(), DAY)
    assert new_pid in controller.requests


def test_normal_block_grandfathers_then_blocks_relaunch(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db, action=Action.BLOCK, mode=Mode.NORMAL)
    old = source.launch("discord.exe")
    recs = executor.handle([outcome(db, rid, RuleState.ENFORCED, "PREVENT_LAUNCH")],
                           source.snapshot(), DAY)
    assert controller.requests == []  # existing instance left alone
    assert recs[0].outcome == "SKIPPED" and f"grandfathered={old}" in recs[0].detail

    new = source.launch("discord.exe")  # relaunch
    executor.handle([outcome(db, rid, RuleState.ENFORCED, "PREVENT_LAUNCH")],
                    source.snapshot(), DAY)
    assert controller.requests == [new]  # only the newcomer dies
    assert old in [p.pid for p in source.processes]


def test_strict_block_kills_on_sight(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db, action=Action.BLOCK, mode=Mode.STRICT)
    pid = source.launch("discord.exe")
    executor.handle([outcome(db, rid, RuleState.ENFORCED, "PREVENT_LAUNCH")],
                    source.snapshot(), DAY)
    assert controller.requests == [pid]


def test_protected_process_is_logged_not_killed(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db, exe="explorer.exe")
    source.launch("explorer.exe")
    recs = executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")],
                           source.snapshot(), DAY)
    assert controller.requests == []
    assert recs[0].outcome == "PROTECTED"
    assert db.list_enforcement(DAY)[0]["outcome"] == "PROTECTED"


def test_warn_only_never_kills(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db, action=Action.WARN_ONLY)
    source.launch("discord.exe")
    recs = executor.handle([outcome(db, rid, RuleState.ENFORCED, "NOTIFY_ONLY")],
                           source.snapshot(), DAY)
    assert controller.requests == []
    assert recs[0].outcome == "EXECUTED" and "warning only" in recs[0].detail


def test_website_block_is_deferred_until_phase4(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db, action=Action.BLOCK, rule_type=RuleType.WEBSITE)
    for _ in range(6):
        recs = executor.handle([outcome(db, rid, RuleState.ENFORCED, "BLOCK_WEBSITE")],
                               source.snapshot(), DAY)
    assert recs[0].outcome == "DEFERRED"
    assert len(db.list_enforcement(DAY)) == 1  # logged once, not per tick


def test_website_block_uses_browser_sink_when_available(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    source = FakeProcessSource()
    closer = ProcessCloser(FakeProcessController(source))
    pushed = []

    def sink(rule, decision):
        pushed.append(rule.id)
        return True

    executor = EnforcementExecutor(db, closer, clock, browser_sink=sink)
    rid = make_rule(db, action=Action.BLOCK, rule_type=RuleType.WEBSITE)
    recs = executor.handle([outcome(db, rid, RuleState.ENFORCED, "BLOCK_WEBSITE")],
                           source.snapshot(), DAY)
    assert pushed == [rid]
    assert recs[0].outcome == "EXECUTED"
    db.close()


def test_warnings_notify_once_per_threshold(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db)
    first = outcome(db, rid, RuleState.WARNING, warnings=(60,), message="1m left")
    again = outcome(db, rid, RuleState.WARNING, warnings=(60,), message="1m left")
    executor.handle([first], source.snapshot(), DAY)
    executor.handle([again], source.snapshot(), DAY)
    assert len(notifier.messages) == 1
    # ...and fire again the next day
    executor.reset_day()
    executor.handle([again], source.snapshot(), DAY)
    assert len(notifier.messages) == 2


def test_notifier_failure_does_not_break_enforcement(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db)

    class Broken:
        def notify(self, title, message, *, urgent=False):
            raise RuntimeError("no tray available")

    executor._notifier = Broken()
    pid = source.launch("discord.exe")
    recs = executor.handle([outcome(db, rid, RuleState.ENFORCED, "CLOSE_APP")],
                           source.snapshot(), DAY)
    assert recs[0].outcome == "EXECUTED" and controller.requests == [pid]


def test_nothing_happens_below_enforcement(env):
    db, clock, source, controller, closer, notifier, executor = env
    rid = make_rule(db)
    source.launch("discord.exe")
    recs = executor.handle([outcome(db, rid, RuleState.NORMAL)], source.snapshot(), DAY)
    assert recs == [] and controller.requests == [] and db.list_enforcement(DAY) == []
