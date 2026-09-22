"""AgentBridge: what the browser is told, and that it matches the database."""

import json
from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.enforcement.adapters import EnforcementExecutor, RecordingNotifier
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.monitor import MonitorLoop
from app.core.rules.engine import RuleEngine
from app.core.rules.models import Rule
from app.core.tracking.tracker import Tracker
from app.core.types import Action, LimitReason, RuleState, RuleType
from app.core.enforcement import state_machine as sm
from app.core.tracking.tracker import Activity
from app.database.db import Database
from app.ipc import protocol
from app.ipc.bridge import AgentBridge
from app.testing.fakes import FakeProcessController, FakeProcessSource

DAY = "2026-09-21"


class FakeBroadcast:
    """Collects pushed messages; `sink` can refuse (0 sent) to test DEFERRED."""

    def __init__(self, accept: bool = True) -> None:
        self.messages: list[str] = []
        self.accept = accept
        self.bound_port = 17846

    def __call__(self, message: str) -> int:
        if not self.accept:
            return 0
        self.messages.append(message)
        return 1

    def types(self) -> list[str]:
        return [json.loads(m)["type"] for m in self.messages]

    def last(self, wanted: str) -> dict:
        for raw in reversed(self.messages):
            message = json.loads(raw)
            if message["type"] == wanted:
                return message
        raise AssertionError(f"no {wanted} pushed (got {self.types()})")


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 9, 0))
    tracker = Tracker(db, clock)
    bridge = AgentBridge(db, clock, token="t" * 64, tracker=tracker)
    broadcast = FakeBroadcast()
    bridge.attach_broadcast(broadcast)
    yield db, clock, tracker, bridge, broadcast
    db.close()


def make_decision(rule, reason: LimitReason) -> sm.Decision:
    """A blocked Decision exactly like the state machine emits at the limit."""
    return sm.Decision(
        new_state=RuleState.ENFORCED, action_to_execute="BLOCK_WEBSITE",
        reason=reason, user_message=f"{rule.name}: limit reached.",
    )


def add_web_rule(db, domain="youtube.com", **kw):
    base = dict(name="YouTube", type=RuleType.WEBSITE, target=domain, domain=domain,
                daily_limit_seconds=600, warning_seconds=(60,), action=Action.BLOCK)
    base.update(kw)
    return db.add_rule(Rule(**base))


# ------------------------------------------------------------------ payloads
def test_welcome_payload_shape(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db)
    payload = bridge.rule_payload()
    assert len(payload) == 1
    rule = payload[0]
    assert rule["domain"] == "youtube.com"
    assert rule["blocked"] is False
    assert rule["remaining_seconds"] == 600
    assert rule["used_seconds"] == 0
    # WELCOME must be valid protocol JSON
    welcome = json.loads(protocol.welcome("0.4.0", payload))
    assert welcome["type"] == "WELCOME" and welcome["website_rules"][0]["domain"] == "youtube.com"


def test_payload_never_leaks_app_rules_or_paths(env):
    db, clock, tracker, bridge, broadcast = env
    db.add_rule(Rule(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
                     executable="discord.exe", daily_limit_seconds=600, action=Action.CLOSE))
    add_web_rule(db)
    payload = bridge.rule_payload()
    assert [r["domain"] for r in payload] == ["youtube.com"]
    blob = json.dumps(payload)
    assert "discord" not in blob.lower()
    assert "exe" not in blob


def test_remaining_seconds_follow_tracked_usage(env):
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db, daily_limit_seconds=600)
    db.add_daily(rid, DAY, 400)  # 400s of tracked time
    payload = bridge.rule_payload()[0]
    assert payload["used_seconds"] == 400
    assert payload["remaining_seconds"] == 200
    assert payload["blocked"] is False


# ------------------------------------------------------------------ decisions
def test_block_decision_free_domain(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db)
    decision = bridge.block_decision("example.com")
    assert decision.blocked is False and decision.reason == "UNBLOCKED"


def test_block_decision_subdomain_matching(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db, domain="youtube.com")
    assert bridge.block_decision("music.youtube.com").blocked is False
    assert bridge.block_decision("notyoutube.com").blocked is False
    assert bridge.match_rule("www.youtube.com") is not None
    assert bridge.match_rule("youtube.com.evil.com") is None


def test_block_decision_when_daily_limit_reached(env):
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db, daily_limit_seconds=300)
    db.add_daily(rid, DAY, 300)
    decision = bridge.block_decision("youtube.com")
    assert decision.blocked is True
    assert decision.reason == "DAILY_LIMIT_REACHED"
    assert decision.reset_at.startswith("2026-09-22T00:00:00")
    assert "Daily limit reached" in decision.message
    # the emitted JSON is protocol-valid
    message = json.loads(decision.to_message())
    assert message["type"] == "BLOCK_DECISION" and message["blocked"] is True


def test_block_decision_from_enforcement_state(env):
    """ENFORCED in the DB is enough even if the counter reads below the limit."""
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db)
    db.put_state(rid, DAY, RuleState.ENFORCED, frozenset())
    assert bridge.block_decision("youtube.com").blocked is True


def test_block_decision_schedule(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db, schedule={"days": "WEEKDAYS", "windows": [["18:00", "22:00"]]})
    # clock is Monday 09:00 -> outside the window
    decision = bridge.block_decision("youtube.com")
    assert decision.blocked is True and decision.reason == "SCHEDULE_BLOCKED"
    clock.set_wall(datetime(2026, 9, 21, 19, 0))
    assert bridge.block_decision("youtube.com").blocked is False


def test_block_decision_session_limit(env):
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db, daily_limit_seconds=3600, session_limit_seconds=60)
    # One continuous session that ran past the session limit.
    tracker.poll({rid: Activity(active=True, session_type="WEBSITE")})
    clock.advance(61)
    tracker.poll({rid: Activity(active=True, session_type="WEBSITE")})
    assert tracker.session_seconds(rid) == 61
    decision = bridge.block_decision("youtube.com")
    assert decision.blocked is True and decision.reason == "SESSION_LIMIT_REACHED"


def test_disabled_rule_is_free(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db, enabled=False)
    assert bridge.block_decision("youtube.com").blocked is False


def test_empty_domain_is_never_blocked(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db)
    assert bridge.block_decision("").blocked is False


# ------------------------------------------------------------ engine hand-off
def test_browser_sink_marks_enforcement_executed(env):
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db)
    decision = make_decision(rule=db.get_rule(rid), reason=LimitReason.DAILY_LIMIT_REACHED)
    assert bridge.browser_sink(db.get_rule(rid), decision) is True
    pushed = broadcast.last("BLOCK_DECISION")
    assert pushed["domain"] == "youtube.com" and pushed["blocked"] is True


def test_browser_sink_reports_failure_when_nobody_listens(env):
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db)
    broadcast.accept = False  # no live connections
    decision = make_decision(rule=db.get_rule(rid), reason=LimitReason.DAILY_LIMIT_REACHED)
    assert bridge.browser_sink(db.get_rule(rid), decision) is False


# ------------------------------------------------------------------ tick push
def test_tick_push_on_block_and_unblock(env):
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db, daily_limit_seconds=300)

    # Below the limit: only a RULE_UPDATE goes out.
    bridge.on_tick([])
    assert "RULE_UPDATE" in broadcast.types()

    # Limit reached -> BLOCK_DECISION(true)
    db.add_daily(rid, DAY, 300)
    bridge.on_tick([])
    assert broadcast.last("BLOCK_DECISION")["blocked"] is True

    # New day -> BLOCK_DECISION(false) so the browser unblocks itself.
    clock.set_wall(datetime(2026, 9, 22, 9, 0))
    bridge.on_tick([])
    assert broadcast.last("BLOCK_DECISION")["blocked"] is False


def test_rule_updates_are_throttled_and_change_driven(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db)
    for _ in range(10):
        bridge.on_tick([])
    updates = [m for m in broadcast.types() if m == "RULE_UPDATE"]
    assert len(updates) == 1  # one push, not ten

    clock.advance(6.0)  # past the throttle window
    rid = add_web_rule(db, domain="reddit.com")
    bridge.on_tick([])
    assert len([m for m in broadcast.types() if m == "RULE_UPDATE"]) == 2  # payload changed


def test_tick_hook_never_breaks_the_monitor(env, tmp_path):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db)
    source = FakeProcessSource()
    source.clock = clock
    loop = MonitorLoop(
        db, RuleEngine(db, tracker, clock), tracker, ActivityResolver(),
        EnforcementExecutor(db, ProcessCloser(FakeProcessController(source)), clock),
        source, clock, interval=5.0,
    )
    loop.set_web_state_provider(bridge.web_state)

    def angry_hook(_report):
        raise RuntimeError("hook is broken")

    loop.set_tick_hook(angry_hook)
    report = loop.tick_once()
    assert report.error == ""  # the loop survived
    loop.set_tick_hook(lambda report: bridge.on_tick(report.outcomes))
    assert loop.tick_once().error == ""


def test_web_state_flows_into_website_rules(env):
    db, clock, tracker, bridge, broadcast = env
    rid = add_web_rule(db, daily_limit_seconds=3600)
    resolver = ActivityResolver(policy=ActivityPolicy(website_grace_seconds=0))
    source = FakeProcessSource()
    source.clock = clock
    loop = MonitorLoop(
        db, RuleEngine(db, tracker, clock), tracker, resolver,
        EnforcementExecutor(db, ProcessCloser(FakeProcessController(source)), clock),
        source, clock, interval=5.0,
    )
    loop.set_web_state_provider(bridge.web_state)
    loop.set_tick_hook(lambda report: bridge.on_tick(report.outcomes))

    # Nothing connected yet: website rules must not count.
    clock.advance(5)
    loop.tick_once()
    assert tracker.today_total(rid) == 0

    # Extension reports an active YouTube tab AND chrome is foreground.
    source.launch("chrome.exe")
    source.focus("chrome.exe")
    bridge.on_connect("chrome", "uuid-1")
    bridge.on_tab_activity("chrome", "uuid-1", tab_id=1, domain="youtube.com",
                           active=True, window_focused=True, audible=False)
    for _ in range(4):
        clock.advance(5)
        bridge.on_heartbeat("chrome", "uuid-1")
        loop.tick_once()
    assert tracker.today_total(rid) == 15

    # Heartbeats stop -> the link goes stale -> counting stops, even though
    # chrome is (per the OS snapshot) still the foreground app. The bound is
    # the 15 s staleness window plus the one interval the tracker was mid-way
    # through when the link died.
    for _ in range(6):
        clock.advance(5)
        loop.tick_once()
    frozen = tracker.today_total(rid)
    assert 15 <= frozen <= 15 + 15 + 5

    for _ in range(6):  # still silent: nothing more may accrue
        clock.advance(5)
        loop.tick_once()
    assert tracker.today_total(rid) == frozen


def test_status_is_json_safe(env):
    db, clock, tracker, bridge, broadcast = env
    add_web_rule(db)
    status = bridge.status()
    assert json.dumps(status)  # no unserializable values
    assert status["website_rules"] == 1
