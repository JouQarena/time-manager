"""ViewModel: Qt-free formatting and rule-state projection."""

from datetime import datetime

import pytest

from app.core.rules.models import Rule
from datetime import time as dtime

from app.core.scheduling.schedule import Schedule
from app.core.types import Action, Mode, RuleState, RuleType
from app.service import RuleStatus, Snapshot
from app.ui import viewmodel as vm


def status(**kw):
    rule = kw.pop("rule", None) or Rule(
        name="Discord", type=RuleType.APPLICATION, target="discord.exe",
        executable="discord.exe", daily_limit_seconds=600, session_limit_seconds=300,
        warning_seconds=(120,), action=Action.CLOSE, enabled=kw.pop("enabled", True),
    )
    rule.id = kw.pop("id", 1)
    base = dict(rule=rule, used_seconds=0, session_seconds=0, state=RuleState.NORMAL,
                remaining_today=600, remaining_session=300)
    base.update(kw)
    return RuleStatus(**base)


# --------------------------------------------------------------- formatting
@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (45, "45s"), (60, "1m 00s"), (90, "1m 30s"),
    (3600, "1h 00m"), (3661, "1h 01m"), (None, "—"),
])
def test_format_duration(seconds, expected):
    assert vm.format_duration(seconds) == expected


def test_format_remaining_reads_naturally():
    assert vm.format_remaining(None) == "unlimited"
    assert vm.format_remaining(0) == "0s left"
    assert vm.format_remaining(125) == "2m 05s left"


def test_format_reset_counts_to_local_midnight():
    now = datetime(2026, 9, 21, 23, 10)  # local, naive = local time
    assert vm.format_reset(now) == "resets at midnight (50m 00s)"
    assert vm.format_reset(datetime(2026, 9, 21, 0, 45)) == "resets at midnight (23h 15m)"


def test_schedule_text():
    assert vm.schedule_text(None) == "Always"
    weekdays = Schedule(days=frozenset({"MON", "TUE", "WED", "THU", "FRI"}),
                        windows=((dtime(16, 0), dtime(19, 0)),))
    assert vm.schedule_text(weekdays) == "Mon–Fri 16:00–19:00"
    assert vm.schedule_text(Schedule(days=frozenset({"SAT", "SUN"}))) == "Sat–Sun"
    assert vm.schedule_text(Schedule(days=None)) == "Every day"
    custom = Schedule(days=frozenset({"MON", "THU"}), windows=())
    assert vm.schedule_text(custom) == "Mon/Thu"


def test_action_label_is_human():
    assert vm.action_label(status().rule) == "Close app"
    site = Rule(name="YouTube", type=RuleType.WEBSITE, target="youtube.com",
                domain="youtube.com", daily_limit_seconds=60,
                action=Action.BLOCK, mode=Mode.STRICT)
    assert vm.action_label(site) == "Block site"
    # The same BLOCK action means something different for a desktop app in the
    # editor, so the label must follow the rule type.
    app_rule = Rule(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
                    executable="discord.exe", daily_limit_seconds=60, action=Action.BLOCK)
    assert vm.action_label(app_rule) == "Prevent launch"
    game = Rule(name="League", type=RuleType.GAME, target="league of legends.exe",
                executable="league of legends.exe", daily_limit_seconds=60,
                action=Action.WAIT_FOR_SESSION_END)
    assert vm.action_label(game) == "Wait for match to end"


# ------------------------------------------------------------------- states
def test_state_text_for_each_state():
    assert vm.state_text_and_role(status())[0].startswith("Ready")
    warning = status(state=RuleState.WARNING, used_seconds=480, remaining_today=120)
    text, role = vm.state_text_and_role(warning)
    assert text.startswith("Warning") and "2m 00s left" in text and role == vm.ROLE_WARN
    enforced = status(state=RuleState.ENFORCED, used_seconds=600, remaining_today=0)
    text, role = vm.state_text_and_role(enforced)
    assert text.startswith("Limit reached") and role == vm.ROLE_BAD
    disabled = status(rule=Rule(name="X", type=RuleType.APPLICATION, target="x.exe",
                                executable="x.exe", daily_limit_seconds=60,
                                action=Action.CLOSE, enabled=False))
    assert vm.state_text_and_role(disabled)[0] == "Off"


def test_paused_rules_are_flagged_on_the_card():
    text, role = vm.state_text_and_role(status(state=RuleState.ENFORCED, used_seconds=600),
                                        paused_rule=True)
    assert "paused" in text.lower() and role == vm.ROLE_MUTED
    # STRICT rules are not suppressed, so they must not claim to be paused.
    strict = status(rule=Rule(name="League", type=RuleType.GAME, target="league.exe",
                              executable="league.exe", daily_limit_seconds=60,
                              action=Action.CLOSE, mode=Mode.STRICT),
                    state=RuleState.ENFORCED, used_seconds=60)
    assert "paused" not in vm.state_text_and_role(strict, paused_rule=False)[0].lower()


def test_rule_rows_from_snapshot():
    snapshot = Snapshot(day="2026-09-21")
    snapshot.rules = [status(used_seconds=120, remaining_today=480),
                      # A session-only rule: no daily limit at all.
                      status(id=2, rule=Rule(name="Steam", type=RuleType.APPLICATION,
                                             target="steam.exe", executable="steam.exe",
                                             session_limit_seconds=1800,
                                             action=Action.CLOSE),
                             remaining_today=None, used_seconds=30, session_seconds=30)]
    snapshot.rules[0].rule.name = "Discord"
    rows = vm.rule_rows(snapshot)
    assert [r.name for r in rows] == ["Discord", "Steam"]
    assert rows[0].used_text == "2m 00s" and rows[0].remaining_text == "8m 00s left"
    assert rows[1].remaining_text == "unlimited"
    assert 0 < rows[0].progress < 1
    assert rows[1].progress == 0.0  # no daily limit: nothing to draw a bar from
    assert rows[1].session_text == "session 30s"


def test_rule_row_subtitle_carries_the_essentials():
    row = vm.rule_rows(Snapshot(day="d", rules=[status()]))[0]
    subtitle = row.subtitle  # a property: the card binds it directly
    assert "discord.exe" in subtitle and "Close app" in subtitle
    assert "Normal" in subtitle
    assert "Always" not in subtitle  # an always-on schedule is not worth a word


# ------------------------------------------------------------- dashboard bits
def test_health_and_status_line():
    snapshot = Snapshot(day="2026-09-21", running=True, ipc_running=True, ipc_port=17846)
    assert vm.health(snapshot) == (vm.ROLE_OK, "Monitoring")  # (role, text)
    assert "Tick 0" in vm.status_line(snapshot)
    assert "no browser connected" in vm.status_line(snapshot)

    stopped = Snapshot(day="2026-09-21", running=False)
    assert vm.health(stopped) == (vm.ROLE_MUTED, "Stopped")


def test_health_reports_trouble_first():
    degraded = Snapshot(day="d", running=True, ticks=10, tick_errors=3,
                        last_tick_error="boom", ipc_running=False)
    role, text = vm.health(degraded)
    assert role == vm.ROLE_BAD and "error" in text.lower()
    # A pause is the user's own doing and the most relevant thing to show, even
    # if a tick also failed (the status line still spells the error out).
    paused = Snapshot(day="d", running=True, paused=True, pause_remaining_seconds=60,
                      tick_errors=1, last_tick_error="boom")
    assert vm.health(paused) == (vm.ROLE_WARN, "Paused — 1m 00s left")
    assert "boom" in vm.status_line(paused)
    # Limited OS support (no foreground detection) is a warning, not a failure
    linux = Snapshot(day="d", running=True, degraded="no foreground detection")
    assert vm.health(linux) == (vm.ROLE_WARN, "Running (limited OS support)")


def test_pause_banner_only_when_paused():
    assert vm.pause_banner(Snapshot(day="d")) is None
    banner = vm.pause_banner(Snapshot(day="d", paused=True, pause_remaining_seconds=125,
                                      pause_exempt_rules=("League",)))
    assert "2m 05s" in banner and "League" in banner


def test_browser_and_link_lines():
    snapshot = Snapshot(day="d", browsers_connected=2, browser_domains=["youtube.com"],
                        ipc_running=True, ipc_port=17846, ipc_messages_in=12,
                        ipc_messages_out=7)
    lines = vm.browser_lines(snapshot)
    # Client rows are per-connection; without them the summary line comes first.
    assert lines == ["Active: youtube.com"]
    named = Snapshot(day="d", browsers_connected=1, browser_domains=[],
                     ipc_clients=[{"browser": "chrome", "version": "0.2.0"}])
    assert vm.browser_lines(named) == ["chrome · 0.2.0", "Connected — no tracked site in focus"]
    assert any("17846" in line for line in vm.link_lines(snapshot))
    assert any("12" in line for line in vm.link_lines(snapshot))

    offline = Snapshot(day="d")
    assert "No browser connected" in vm.browser_lines(offline)[0]


def test_detector_lines_report_healthy_detectors():
    snapshot = Snapshot(day="d", detector_summary={"count": 1, "timeout_ms": 300, "quarantined": 0})
    snapshot.detectors = [{
        "id": "league_of_legends", "name": "League of Legends", "source": "builtin",
        "quarantined": False, "errors": 0, "timeouts": 0,
        "last_in_session": False, "last_detail": "launcher only — no match process for over 30s",
    }]
    lines = vm.detector_lines(snapshot)
    assert "League of Legends" in lines[0]
    assert "no session" in lines[0] and "launcher only" in lines[0]
    assert "Budget 300 ms" in lines[1]


def test_detector_lines_flag_trouble():
    snapshot = Snapshot(day="d", detector_summary={"count": 1, "timeout_ms": 300, "quarantined": 1})
    snapshot.detectors = [{
        "id": "league_of_legends", "name": "League of Legends", "source": "plugin",
        "quarantined": True, "errors": 5, "timeouts": 2, "last_detail": "detector error",
        "last_in_session": None,
    }]
    snapshot.detector_rejected = [{"origin": "/tmp/detectors/broken.py", "reason": "import failed"}]
    lines = vm.detector_lines(snapshot)
    assert "quarantined" in lines[0]
    assert "matches will not be closed" in lines[0]
    assert "2 timeout(s), 5 error(s)" in lines[0]
    assert any("broken.py" in line for line in lines)


def test_detector_lines_when_nothing_is_registered():
    assert vm.detector_lines(Snapshot(day="d")) == [
        "No game detectors loaded — games count like any other app."]


def test_timeline_formats_records():
    records = [
        {"created_at": "2026-09-21T15:31:48+00:00", "action": "BLOCK_WEBSITE",
         "outcome": "EXECUTED", "detail": "rule pushed to browser", "rule_id": 1},
        {"created_at": "2026-09-21T15:07:03", "action": "CLOSE_APP",
         "outcome": "SKIPPED", "detail": None, "rule_id": 2},
    ]
    lines = vm.timeline(records)
    assert len(lines) == 2
    assert lines[0].startswith("15:31") and "EXECUTED" in lines[0]
    assert "Block site" in lines[0] and "rule pushed" in lines[0]
    assert lines[1].startswith("15:07")
    assert vm.timeline([]) == []


def test_window_title_shows_pause():
    assert "Time Manager" in vm.window_title(Snapshot(day="d", running=True))
    paused = vm.window_title(Snapshot(day="d", running=True, paused=True,
                                      pause_remaining_seconds=60))
    assert "Paused" in paused and "1m" in paused


# ------------------------------------------------------------------ Phase 7
def test_security_notices_empty_when_all_clear():
    assert vm.security_notices(Snapshot(day="d", security={})) == []
    assert vm.security_notices(Snapshot(day="d")) == []


def test_security_notices_report_every_finding_worst_first():
    snap = Snapshot(day="d", security={
        "unclean_stop": {"last_start_at": "2026-09-20T22:00:00+00:00",
                         "local": "2026-09-21T00:00:00+02:00"},
        "clock_regressed_seconds": 3700.0,
        "usage_tamper": [{"rule": "YouTube", "day": "2026-09-21",
                          "stored_seconds": 0, "floor_seconds": 5400}],
        "extension_silent_rules": ["YouTube", "Reddit"],
        "startup_repaired": True,
    })
    notices = vm.security_notices(snap)
    roles = [role for role, _ in notices]
    texts = [text for _, text in notices]
    # worst (critical findings) first, info last
    assert roles == ["bad", "bad", "bad", "warn", "info"]
    assert any("stopped unexpectedly" in t for t in texts)
    assert any("behind the last time the agent ran" in t for t in texts)
    assert any("YouTube: recorded usage" in t and "1h 30m" in t for t in texts)
    assert any("NOT enforced" in t and "YouTube, Reddit" in t for t in texts)
    assert any("startup was re-enabled" in t for t in texts)


def test_security_notices_partial_findings():
    snap = Snapshot(day="d", security={"extension_silent_rules": ["x.com"]})
    notices = vm.security_notices(snap)
    assert len(notices) == 1 and notices[0][0] == "warn"
