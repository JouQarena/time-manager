"""ActivityResolver: what counts as "using it right now"."""

import pytest

from app.core.detection.registry import DetectorRegistry
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.models import Rule
from app.core.types import Action, RuleType


def app_rule(rid=1, exe="discord.exe", **kw):
    base = dict(name="Discord", type=RuleType.APPLICATION, target=exe,
                executable=exe, daily_limit_seconds=600, action=Action.CLOSE)
    base.update(kw)
    rule = Rule(**base)
    rule.id = rid
    return rule


def game_rule(rid=2, exe="league of legends.exe"):
    rule = Rule(name="LoL", type=RuleType.GAME, target=exe, executable=exe,
                daily_limit_seconds=600, action=Action.WAIT_FOR_SESSION_END)
    rule.id = rid
    return rule


def web_rule(rid=3, domain="youtube.com"):
    rule = Rule(name="YT", type=RuleType.WEBSITE, target=domain, domain=domain,
                daily_limit_seconds=600, action=Action.BLOCK)
    rule.id = rid
    return rule


def snap(procs=(), fg=None, idle=None, mono=1000.0):
    return SystemSnapshot(
        processes=tuple(ProcessInfo(pid=p, exe=e) for p, e in procs),
        foreground_pid=fg, idle_seconds=idle, captured_mono=mono,
    )


def browser_snap(idle=None, mono=1000.0, exe="chrome.exe"):
    """A snapshot where a browser genuinely owns the foreground window."""
    return SystemSnapshot(
        processes=(ProcessInfo(pid=99, exe=exe),),
        foreground_pid=99, idle_seconds=idle, captured_mono=mono,
    )


def test_foreground_counts():
    r = app_rule()
    res = ActivityResolver().resolve([r], snap([(10, "discord.exe")], fg=10))
    assert res.activities[1].active is True
    assert res.detail[1] == "foreground"
    assert res.foreground_exe == "discord.exe"


def test_background_does_not_count():
    r = app_rule()
    res = ActivityResolver().resolve([r], snap([(10, "discord.exe")], fg=99))
    assert res.activities[1].active is False
    assert "background" in res.detail[1]


def test_grace_after_losing_focus():
    r = app_rule()
    resolver = ActivityResolver(policy=ActivityPolicy(background_grace_seconds=5))
    assert resolver.resolve([r], snap([(10, "discord.exe")], fg=10, mono=100.0)).activities[1].active
    # Tab away 2s later: still counted (grace)
    res = resolver.resolve([r], snap([(10, "discord.exe")], fg=99, mono=102.0))
    assert res.activities[1].active and "grace" in res.detail[1]
    # 10s later: counted no more
    res = resolver.resolve([r], snap([(10, "discord.exe")], fg=99, mono=110.0))
    assert not res.activities[1].active


def test_not_running_is_inactive():
    r = app_rule()
    res = ActivityResolver().resolve([r], snap([(10, "other.exe")], fg=10))
    assert not res.activities[1].active and res.detail[1] == "not running"


def test_foreground_only_can_be_disabled():
    r = app_rule()
    resolver = ActivityResolver(policy=ActivityPolicy(enforce_foreground_only=False))
    res = resolver.resolve([r], snap([(10, "discord.exe")], fg=99))
    assert res.activities[1].active
    assert "foreground-only counting disabled" in res.detail[1]


def test_idle_gate_blocks_apps():
    r = app_rule()
    resolver = ActivityResolver(policy=ActivityPolicy(idle_grace_seconds=120))
    res = resolver.resolve([r], snap([(10, "discord.exe")], fg=10, idle=300.0))
    assert not res.activities[1].active
    assert res.idle_blocked and "idle" in res.detail[1]
    # Unknown idle (non-Windows) never blocks
    res = resolver.resolve([r], snap([(10, "discord.exe")], fg=10, idle=None))
    assert res.activities[1].active


def test_idle_gate_blocks_websites():
    r = web_rule()
    resolver = ActivityResolver(policy=ActivityPolicy(
        idle_grace_seconds=60, website_grace_seconds=0))
    web = {"youtube.com": 999.0}
    res = resolver.resolve([r], browser_snap(idle=300.0), web_active=web)
    assert not res.activities[3].active
    assert "idle" in res.detail[3]


def test_disabled_rule_never_counts():
    r = app_rule(enabled=False)
    res = ActivityResolver().resolve([r], snap([(10, "discord.exe")], fg=10))
    assert not res.activities[1].active and res.detail[1] == "rule disabled"


# --------------------------------------------------------------------- games
def test_game_rule_gets_detector_verdict():
    from app.core.enforcement.game_guard import GameSessionGuard
    from app.core.detection.base import GameSessionDetector, SessionVerdict

    class InMatch(GameSessionDetector):
        detector_id = "t"
        known_executables = ("league of legends.exe",)

        def probe(self, snapshot):
            return SessionVerdict(in_session=True, confidence=1.0)

    reg = DetectorRegistry()
    reg.register(InMatch())
    resolver = ActivityResolver(guard=GameSessionGuard(reg))
    rule = game_rule()
    res = resolver.resolve([rule], snap([(10, "league of legends.exe")], fg=10))
    a = res.activities[2]
    assert a.active is True
    assert a.session_type == "GAME"
    assert a.in_game_session is True and a.detector_confident is True


def test_game_rule_without_detector_is_confidently_out_of_session():
    from app.core.enforcement.game_guard import GameSessionGuard

    resolver = ActivityResolver(guard=GameSessionGuard(DetectorRegistry()))
    res = resolver.resolve([game_rule()], snap([(10, "league of legends.exe")], fg=10))
    a = res.activities[2]
    assert a.in_game_session is False and a.detector_confident is True
    assert "no detector registered" in res.detail[2]


# ------------------------------------------------------------------ websites
def test_website_without_extension_link_never_counts():
    res = ActivityResolver().resolve([web_rule()], browser_snap())
    assert not res.activities[3].active
    assert "no browser extension connected" in res.detail[3]


def test_website_does_not_count_when_a_browser_is_not_foreground():
    """Defense in depth: extension events alone are not enough (a buggy or
    hostile page must not keep a website timer running in the background)."""
    r = web_rule()
    resolver = ActivityResolver(policy=ActivityPolicy(website_grace_seconds=0))
    web = {"youtube.com": 1000.0}
    # Notepad owns the foreground; chrome is only running in the background.
    res = resolver.resolve(
        [r],
        SystemSnapshot(processes=(ProcessInfo(pid=99, exe="chrome.exe"),
                                  ProcessInfo(pid=100, exe="notepad.exe")),
                       foreground_pid=100, idle_seconds=0.0, captured_mono=1000.0),
        web_active=web,
    )
    assert not res.activities[3].active
    assert "no browser in the foreground" in res.detail[3]

    # A browser in the foreground (any Chromium family) counts again.
    res = resolver.resolve([r], browser_snap(exe="msedge.exe"), web_active=web)
    assert res.activities[3].active


def test_browser_foreground_check_can_be_disabled():
    r = web_rule()
    resolver = ActivityResolver(policy=ActivityPolicy(
        website_grace_seconds=0, require_browser_foreground=False))
    res = resolver.resolve([r], snap(), web_active={"youtube.com": 1000.0})
    assert res.activities[3].active


def test_website_debounce_and_stale():
    r = web_rule()
    resolver = ActivityResolver(policy=ActivityPolicy(
        website_grace_seconds=3, website_stale_seconds=15))
    # fresh activity: debounce not elapsed yet
    res = resolver.resolve([r], browser_snap(mono=100.0), web_active={"youtube.com": 100.0})
    assert not res.activities[3].active and "debounce" in res.detail[3]
    # 4s of continuous activity: counted
    res = resolver.resolve([r], browser_snap(mono=104.0), web_active={"youtube.com": 104.0})
    assert res.activities[3].active
    # stale event (20s old): not counted
    res = resolver.resolve([r], browser_snap(mono=124.0), web_active={"youtube.com": 104.0})
    assert not res.activities[3].active and "no active tab" in res.detail[3]


def test_website_subdomain_matches_but_lookalike_does_not():
    r = web_rule(domain="youtube.com")
    resolver = ActivityResolver(policy=ActivityPolicy(website_grace_seconds=0))
    res = resolver.resolve([r], browser_snap(mono=100.0),
                           web_active={"music.youtube.com": 100.0})
    assert res.activities[3].active
    resolver2 = ActivityResolver(policy=ActivityPolicy(website_grace_seconds=0))
    res = resolver2.resolve([r], browser_snap(mono=100.0),
                            web_active={"notyoutube.com": 100.0})
    assert not res.activities[3].active
