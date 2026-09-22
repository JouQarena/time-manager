"""Phase 7 end-to-end: AgentService wires lifecycle audit, usage floors,
startup repair, watchdog settings and the bypass watch."""

from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.enforcement.adapters import RecordingNotifier
from app.core.rules.models import Rule
from app.core.types import Action, Mode, RuleType
from app.database.db import Database
from app.service import AgentService
from app.config.settings import AppSettings
from app.windows.watchdog import WatchdogScheduler
from app.testing.fakes import (
    FakeProcessController,
    FakeProcessSource,
    FakeSchtasksRunner,
)


def build(tmp_path, *, name="sec.db", notifier=None, clock=None, **kwargs):
    if clock is None:
        clock = FakeClock()
        clock.set_wall(datetime(2026, 9, 21, 9, 0))
    source = FakeProcessSource()
    source.clock = clock
    service = AgentService(
        tmp_path / name, clock=clock, source=source,
        controller=FakeProcessController(source),
        notifier=notifier or RecordingNotifier(),
        # Isolated settings + in-memory watchdog (see test_service.build).
        settings=kwargs.pop("settings", None) or AppSettings(ipc_port=0),
        watchdog=kwargs.pop("watchdog", None)
        or WatchdogScheduler(runner=FakeSchtasksRunner()),
        **kwargs,
    )
    return service, clock, source


def advance(svc, clock, seconds, *, step=5):
    remaining = seconds
    while remaining > 0:
        delta = min(step, remaining)
        clock.advance(delta)
        svc.monitor.tick_once()
        remaining -= delta


def add_rule(svc, **kw):
    base = dict(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
                executable="discord.exe", daily_limit_seconds=600,
                warning_seconds=(120,), action=Action.CLOSE, mode=Mode.STRICT)
    base.update(kw)
    return svc.db.add_rule(Rule(**base))


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    """Redirect the profile dir (usage floor, backups) into the test."""
    target = tmp_path / "profile"
    monkeypatch.setattr("app.service.profile_dir", lambda: target)
    return target


# --------------------------------------------------------------- lifecycle
def test_clean_stop_then_restart_reports_nothing(profile, tmp_path):
    svc, clock, source = build(tmp_path)
    svc.start()
    svc.stop(backup=False)

    clock.advance(120)
    svc2, clock, source = build(tmp_path, clock=clock)
    svc2.start()
    assert svc2.snapshot().security["unclean_stop"] is None
    assert [dict(r)["kind"] for r in svc2.db.list_audit(kind="UNEXPECTED_STOP")] == []
    svc2.stop(backup=False)


def test_killed_agent_is_reported_on_next_start(profile, tmp_path):
    svc, clock, source = build(tmp_path)
    svc.start()
    assert svc.snapshot().security["unclean_stop"] is None  # first run ever
    clock.advance(600)
    # Simulate a kill: teardown without the lifecycle STOP row.
    svc._teardown(backup=False)

    clock.advance(120)
    notifier = RecordingNotifier()
    svc2, clock, source = build(tmp_path, notifier=notifier, clock=clock)
    svc2.start()
    sec = svc2.snapshot().security
    assert sec["unclean_stop"] is not None
    assert sec["unclean_stop"]["last_start_at"]
    rows = svc2.db.list_audit(kind="UNEXPECTED_STOP")
    assert len(rows) == 1 and rows[0]["severity"] == "critical"

    # The finding is not re-reported on later restarts.
    svc2.stop(backup=False)
    clock.advance(120)
    svc3, clock, source = build(tmp_path, clock=clock)
    svc3.start()
    assert svc3.snapshot().security["unclean_stop"] is None
    svc3.stop(backup=False)


def test_status_json_includes_security_section(profile, tmp_path):
    svc, clock, source = build(tmp_path)
    svc.start()
    sec = svc.snapshot().to_dict()["security"]
    for key in ("unclean_stop", "clock_regressed_seconds", "startup_repaired",
                "watchdog", "extension_silent_rules", "usage_tamper", "recent_audit"):
        assert key in sec
    svc.stop(backup=False)


# -------------------------------------------------------------- usage floor
def test_db_rollback_is_clamped_by_the_floor(profile, tmp_path):
    svc, clock, source = build(tmp_path)
    svc.start()
    rid = add_rule(svc)  # STRICT app rule
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()
    advance(svc, clock, 100)
    assert svc.tracker.today_total(rid) == 100
    svc.stop(backup=False)  # clean stop flushes the floor file

    # The user zeroes today's usage behind the agent's back.
    db = Database(svc.db_path).connect()
    db.conn.execute("UPDATE daily_usage SET total_seconds=0")
    db.conn.commit()
    db.close()

    clock.advance(300)  # the restart happens a few minutes later
    svc2, clock, source = build(tmp_path, clock=clock)
    svc2.start()
    rows = [r for r in svc2.snapshot().rules if r.rule.id == rid]
    assert rows[0].used_seconds == 100  # floor, not the erased 0
    sec = svc2.snapshot().security
    assert sec["usage_tamper"] and sec["usage_tamper"][0]["floor_seconds"] == 100
    assert svc2.db.list_audit(kind="USAGE_TAMPERED") != []
    svc2.stop(backup=False)


def test_normal_rule_usage_is_not_clamped(profile, tmp_path):
    svc, clock, source = build(tmp_path)
    svc.start()
    rid = add_rule(svc, mode=Mode.NORMAL)
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()
    advance(svc, clock, 50)
    svc.stop(backup=False)

    db = Database(svc.db_path).connect()
    db.conn.execute("UPDATE daily_usage SET total_seconds=0")
    db.conn.commit()
    db.close()

    clock.advance(300)
    svc2, clock, source = build(tmp_path, clock=clock)
    svc2.start()
    rows = [r for r in svc2.snapshot().rules if r.rule.id == rid]
    assert rows[0].used_seconds == 0  # NORMAL: the user's edit stands
    assert svc2.snapshot().security["usage_tamper"] == []
    svc2.stop(backup=False)


# ------------------------------------------------------------ startup repair
def test_startup_repaired_when_strict_rules_armed(profile, tmp_path, monkeypatch):
    monkeypatch.setattr("app.windows.api.is_windows", lambda: True)
    calls = {"enabled": 0}
    monkeypatch.setattr("app.windows.startup.is_enabled", lambda *a, **k: False)
    monkeypatch.setattr(
        "app.windows.startup.enable",
        lambda *a, **k: calls.__setitem__("enabled", calls["enabled"] + 1) or True,
    )

    svc, clock, source = build(tmp_path)
    svc.start()
    add_rule(svc, mode=Mode.STRICT)
    assert svc._startup_repaired is False  # no rules existed at that start
    svc.stop(backup=False)

    clock.advance(120)
    notifier = RecordingNotifier()
    svc2, clock, source = build(tmp_path, notifier=notifier, clock=clock)
    svc2.start()
    assert svc2._startup_repaired is True
    assert calls["enabled"] == 1
    assert svc2.snapshot().security["startup_repaired"] is True
    assert svc2.db.list_audit(kind="STARTUP_REPAIRED") != []
    assert any("startup was re-enabled" in m for _t, m, _u in notifier.messages)
    svc2.stop(backup=False)


def test_no_startup_repair_without_strict_rules(profile, tmp_path, monkeypatch):
    monkeypatch.setattr("app.windows.api.is_windows", lambda: True)
    monkeypatch.setattr("app.windows.startup.is_enabled", lambda *a, **k: False)
    monkeypatch.setattr("app.windows.startup.enable", lambda *a, **k: True)

    svc, clock, source = build(tmp_path)
    svc.start()
    add_rule(svc, mode=Mode.NORMAL)
    svc.stop(backup=False)

    clock.advance(120)
    svc2, clock, source = build(tmp_path, clock=clock)
    svc2.start()
    assert svc2._startup_repaired is False
    assert svc2.db.list_audit(kind="STARTUP_REPAIRED") == []
    svc2.stop(backup=False)


# ------------------------------------------------------------- bypass watch
def test_silent_extension_surfaces_in_snapshot(profile, tmp_path):
    svc, clock, source = build(tmp_path)
    svc.start()
    add_rule(svc, name="YouTube", type=RuleType.WEBSITE, target="youtube.com",
             domain="youtube.com", action=Action.BLOCK, mode=Mode.STRICT,
             executable=None)
    source.launch("chrome.exe")

    advance(svc, clock, 30)  # inside the 60 s reconnect grace
    assert svc.snapshot().security["extension_silent_rules"] == []

    advance(svc, clock, 40)  # 70 s of silence -> episode opens
    assert svc.snapshot().security["extension_silent_rules"] == ["YouTube"]
    assert svc.db.list_audit(kind="EXTENSION_SILENT") != []

    source.kill(source.pid_of("chrome.exe"))  # browser gone -> hole closed
    svc.monitor.tick_once()
    assert svc.snapshot().security["extension_silent_rules"] == []
    assert svc.db.list_audit(kind="EXTENSION_RESTORED") != []
    svc.stop(backup=False)


# ------------------------------------------------------------------ watchdog
def test_watchdog_toggle_persists_the_setting(profile, tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr("app.service.save_settings", lambda s: saved.append(s))
    svc, clock, source = build(tmp_path)
    svc.start()
    result = svc.set_watchdog_enabled(True)
    # Off-Windows the task cannot be installed, but the user's intent is kept.
    assert result.action in ("UNAVAILABLE", "INSTALLED", "FAILED")
    assert saved and saved[-1].strict_watchdog is True
    assert svc.settings.strict_watchdog is True

    result2 = svc.set_watchdog_enabled(False)
    assert result2.action in ("UNAVAILABLE", "REMOVED", "NOT_PRESENT", "FAILED")
    assert saved[-1].strict_watchdog is False
    svc.stop(backup=False)


# ----------------------------------------------------------- clock regression
def test_clock_regression_at_startup_is_audited(profile, tmp_path):
    svc, clock, source = build(tmp_path)
    svc.start()
    rid = add_rule(svc)  # usage must accrue so the floor records last-seen time
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()
    advance(svc, clock, 60)
    svc.stop(backup=False)

    # Next boot happens with the wall clock rolled back an hour.
    clock.jump_wall(-3600)
    clock.advance(30)
    svc2, clock, source = build(tmp_path, clock=clock)
    svc2.start()
    sec = svc2.snapshot().security
    assert sec["clock_regressed_seconds"] and sec["clock_regressed_seconds"] > 3000
    assert svc2.db.list_audit(kind="CLOCK_REGRESSED") != []
    svc2.stop(backup=False)
