from app.core.enforcement.state_machine import Decision, TickInput, tick
from app.core.rules.models import Rule
from app.core.types import LimitReason, RuleState


def app_rule(**kw):
    base = dict(
        name="Discord", type="APPLICATION", target="discord.exe",
        executable="discord.exe", daily_limit_seconds=3600, action="CLOSE",
    )
    base.update(kw)
    return Rule(**base)


def game_rule(**kw):
    base = dict(
        name="LoL", type="GAME", target="LeagueClient.exe",
        executable="LeagueClient.exe", daily_limit_seconds=7200,
        action="WAIT_FOR_SESSION_END",
    )
    base.update(kw)
    return Rule(**base)


def test_normal_stays_normal():
    d = tick(TickInput(rule=app_rule(), today_used_seconds=100,
                       session_used_seconds=100, target_active_now=True,
                       in_game_session=None, stored_day="2026-09-21", today_day="2026-09-21"))
    assert d.new_state == RuleState.NORMAL and d.action_to_execute is None


def test_warning_fires_once_per_threshold():
    rule = app_rule(warning_seconds=(600, 60))
    d1 = tick(TickInput(rule=rule, today_used_seconds=3600 - 500,
                        session_used_seconds=10, target_active_now=True,
                        in_game_session=None, stored_day="D", today_day="D"))
    assert d1.new_state == RuleState.WARNING and d1.warnings_to_fire == (600,)
    # Same tick with 600 already fired -> no refire.
    d2 = tick(TickInput(rule=rule, today_used_seconds=3600 - 500,
                        session_used_seconds=10, target_active_now=True,
                        in_game_session=None, stored_state=RuleState.WARNING,
                        warned_thresholds=frozenset({600}), stored_day="D", today_day="D"))
    assert d2.warnings_to_fire == ()


def test_daily_limit_enforces_close():
    d = tick(TickInput(rule=app_rule(), today_used_seconds=3600,
                       session_used_seconds=10, target_active_now=True,
                       in_game_session=None, stored_day="D", today_day="D"))
    assert d.new_state == RuleState.ENFORCED
    assert d.action_to_execute == "CLOSE_APP"
    assert d.reason == LimitReason.DAILY_LIMIT_REACHED


def test_session_limit_independent():
    rule = app_rule(daily_limit_seconds=7200, session_limit_seconds=900)
    d = tick(TickInput(rule=rule, today_used_seconds=1000,
                       session_used_seconds=900, target_active_now=True,
                       in_game_session=None, stored_day="D", today_day="D"))
    assert d.reason == LimitReason.SESSION_LIMIT_REACHED
    assert d.new_state == RuleState.ENFORCED


def test_game_waits_for_match_then_enforces():
    rule = game_rule()
    waiting = tick(TickInput(rule=rule, today_used_seconds=7200,
                             session_used_seconds=10, target_active_now=True,
                             in_game_session=True, stored_day="D", today_day="D"))
    assert waiting.new_state == RuleState.WAITING_FOR_SESSION_END
    assert waiting.action_to_execute is None  # must NOT kill mid-match
    ended = tick(TickInput(rule=rule, today_used_seconds=7300,
                           session_used_seconds=10, target_active_now=True,
                           in_game_session=False,
                           stored_state=RuleState.WAITING_FOR_SESSION_END,
                           stored_day="D", today_day="D"))
    assert ended.new_state == RuleState.ENFORCED
    assert ended.action_to_execute == "CLOSE_APP"


def test_game_unknown_state_fails_safe():
    rule = game_rule()
    d = tick(TickInput(rule=rule, today_used_seconds=7200,
                       session_used_seconds=10, target_active_now=True,
                       in_game_session=None, detector_confident=False,
                       stored_day="D", today_day="D"))
    assert d.new_state == RuleState.WAITING_FOR_SESSION_END
    assert d.action_to_execute is None
    assert "could not be verified" in d.user_message


def test_day_reset_and_disabled():
    rule = app_rule()
    d = tick(TickInput(rule=rule, today_used_seconds=9999,
                       session_used_seconds=0, target_active_now=False,
                       in_game_session=None, stored_state=RuleState.ENFORCED,
                       stored_day="2026-09-20", today_day="2026-09-21"))
    # New day with 0 *session* but stale daily total would still enforce; the
    # tracker resets daily counters at midnight, so simulate properly:
    d2 = tick(TickInput(rule=rule, today_used_seconds=0,
                        session_used_seconds=0, target_active_now=False,
                        in_game_session=None, stored_state=RuleState.ENFORCED,
                        stored_day="2026-09-20", today_day="2026-09-21"))
    assert d2.new_state == RuleState.NORMAL
    assert d.new_state == RuleState.ENFORCED  # stale total still enforced (safe)

    off = app_rule(enabled=False)
    d3 = tick(TickInput(rule=off, today_used_seconds=0, session_used_seconds=0,
                        target_active_now=False, in_game_session=None))
    assert d3.new_state == RuleState.DISABLED


def test_stale_disabled_clears_when_back_in_schedule():
    # Regression: a DISABLED left by an out-of-schedule tick must NOT stick
    # once the rule is enabled and back inside its schedule.
    rule = app_rule()
    d = tick(TickInput(rule=rule, today_used_seconds=10,
                       session_used_seconds=10, target_active_now=True,
                       in_game_session=None, stored_state=RuleState.DISABLED,
                       stored_day="D", today_day="D"))
    assert d.new_state == RuleState.NORMAL


def test_warn_only_never_acts():
    rule = app_rule(action="WARN_ONLY")
    d = tick(TickInput(rule=rule, today_used_seconds=9999,
                       session_used_seconds=0, target_active_now=True,
                       in_game_session=None, stored_day="D", today_day="D"))
    assert d.new_state == RuleState.ENFORCED
    assert d.action_to_execute == "NOTIFY_ONLY"
