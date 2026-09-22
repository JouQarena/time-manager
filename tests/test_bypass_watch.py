"""Phase 7: BypassWatch — silent extension while a STRICT website rule is armed."""

import pytest

from app.core.clock import FakeClock
from app.core.enforcement.adapters import RecordingNotifier
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.models import Rule
from app.core.security.bypass import BypassWatch, SUPPORTED_BROWSER_EXES
from app.core.types import Action, Mode, RuleType
from app.database.db import Database


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    notifier = RecordingNotifier()
    watch = BypassWatch(db, clock, notifier, silence_seconds=60, renotify_seconds=900)
    yield db, clock, watch, notifier
    db.close()


def make_rule(db, mode=Mode.STRICT, enabled=True, rtype=RuleType.WEBSITE):
    if rtype == RuleType.WEBSITE:
        kwargs = dict(target="youtube.com", domain="youtube.com")
    else:
        kwargs = dict(target="app.exe", executable="app.exe")
    rid = db.add_rule(Rule(
        name="YouTube", type=rtype, daily_limit_seconds=2700,
        warning_seconds=(60,), action=Action.BLOCK, mode=mode,
        enabled=enabled, **kwargs,
    ))
    return db.get_rule(rid)


def snap_with(*exes):
    return SystemSnapshot(
        processes=tuple(ProcessInfo(pid=i + 10, exe=e) for i, e in enumerate(exes)),
        captured_mono=0.0,
    )


def test_no_episode_without_a_browser(env):
    db, clock, watch, notifier = env
    rule = make_rule(db)
    alerts = watch.check([rule], snap_with("notepad.exe"), browsers_connected=0)
    assert alerts == []
    assert watch.silent_rule_names == ()


def test_silence_inside_grace_window_is_quiet(env):
    db, clock, watch, notifier = env
    rule = make_rule(db)
    assert watch.check([rule], snap_with("chrome.exe"), 0) == []
    clock.advance(59)
    assert watch.check([rule], snap_with("chrome.exe"), 0) == []
    assert watch.silent_rule_names == ()


def test_silent_episode_fires_after_grace(env):
    db, clock, watch, notifier = env
    rule = make_rule(db)
    watch.check([rule], snap_with("chrome.exe"), 0)
    clock.advance(61)
    alerts = watch.check([rule], snap_with("chrome.exe"), 0)

    assert len(alerts) == 1
    assert alerts[0].kind == "EXTENSION_SILENT"
    assert alerts[0].rule_name == "YouTube"
    assert watch.silent_rule_names == ("YouTube",)
    rows = db.list_audit(kind="EXTENSION_SILENT")
    assert len(rows) == 1 and rows[0]["severity"] == "warning"
    assert any("bypass warning" in t for t, _m, _u in notifier.messages)


def test_no_spam_then_renotify_on_cooldown(env):
    db, clock, watch, notifier = env
    rule = make_rule(db)
    watch.check([rule], snap_with("msedge.exe"), 0)
    clock.advance(61)
    assert len(watch.check([rule], snap_with("msedge.exe"), 0)) == 1

    clock.advance(30)
    assert watch.check([rule], snap_with("msedge.exe"), 0) == []  # quiet
    clock.advance(900)
    assert len(watch.check([rule], snap_with("msedge.exe"), 0)) == 1  # reminder


def test_episode_closes_when_extension_returns(env):
    db, clock, watch, notifier = env
    rule = make_rule(db)
    watch.check([rule], snap_with("chrome.exe"), 0)
    clock.advance(61)
    watch.check([rule], snap_with("chrome.exe"), 0)
    assert watch.silent_rule_names == ("YouTube",)

    watch.check([rule], snap_with("chrome.exe"), 1)  # extension reconnected
    assert watch.silent_rule_names == ()
    restored = db.list_audit(kind="EXTENSION_RESTORED")
    assert len(restored) == 1

    # A later silence starts a fresh episode (new audit row).
    clock.advance(10)
    watch.check([rule], snap_with("chrome.exe"), 0)
    clock.advance(61)
    watch.check([rule], snap_with("chrome.exe"), 0)
    assert len(db.list_audit(kind="EXTENSION_SILENT")) == 2


def test_episode_closes_when_browser_gone(env):
    db, clock, watch, _ = env
    rule = make_rule(db)
    watch.check([rule], snap_with("chrome.exe"), 0)
    clock.advance(61)
    watch.check([rule], snap_with("chrome.exe"), 0)
    watch.check([rule], snap_with("notepad.exe"), 0)  # browser closed entirely
    assert watch.silent_rule_names == ()


def test_normal_rules_and_app_rules_do_not_alert(env):
    db, clock, watch, _ = env
    normal_web = make_rule(db, mode=Mode.NORMAL)
    strict_app = make_rule(db, rtype=RuleType.APPLICATION)
    watch.check([normal_web, strict_app], snap_with("chrome.exe"), 0)
    clock.advance(120)
    assert watch.check([normal_web, strict_app], snap_with("chrome.exe"), 0) == []
    assert db.list_audit(kind="EXTENSION_SILENT") == []


def test_disabled_rules_do_not_alert(env):
    db, clock, watch, _ = env
    rule = make_rule(db, enabled=False)
    watch.check([rule], snap_with("chrome.exe"), 0)
    clock.advance(120)
    assert watch.check([rule], snap_with("chrome.exe"), 0) == []


def test_supported_browsers_cover_chrome_and_edge():
    assert "chrome.exe" in SUPPORTED_BROWSER_EXES
    assert "msedge.exe" in SUPPORTED_BROWSER_EXES
