"""AgentService: lifecycle, single-instance guard, pause, status snapshot."""

from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.rules.models import Rule
from app.core.types import Action, Mode, RuleState, RuleType
from app.config.settings import AppSettings
from app.windows.watchdog import WatchdogScheduler
from app.service import AgentAlreadyRunning, AgentService
from app.testing.fakes import (
    FakeProcessController,
    FakeProcessSource,
    FakeSchtasksRunner,
)


def build(tmp_path, *, name="t.db", **kwargs):
    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 9, 0))
    source = FakeProcessSource()
    source.clock = clock
    service = AgentService(
        tmp_path / name, clock=clock, source=source,
        controller=FakeProcessController(source),
        concurrency_guard=kwargs.pop("concurrency_guard", True),
        # Isolated settings + an in-memory watchdog: on the developer's
        # Windows machine these tests used to read her real config, bind the
        # real agent port (already held by her running agent) and even
        # register a REAL scheduled task mid-run.
        settings=kwargs.pop("settings", None) or AppSettings(ipc_port=0),
        watchdog=kwargs.pop("watchdog", None)
        or WatchdogScheduler(runner=FakeSchtasksRunner()),
        **kwargs,
    )
    return service, clock, source


def advance(svc, clock, seconds, *, step=5):
    """Move time the way a live agent sees it: small steps, one tick each.

    A single large jump is *deliberately* treated as sleep/hibernate by the
    monitor (Phase 3), which would drop the interval from the accounting — so
    tests that care about counted time must step like real time.
    """
    remaining = seconds
    while remaining > 0:
        delta = min(step, remaining)
        clock.advance(delta)
        svc.monitor.tick_once()
        remaining -= delta


def add_rule(service, **kw):
    base = dict(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
                executable="discord.exe", daily_limit_seconds=600,
                warning_seconds=(120,), action=Action.CLOSE)
    base.update(kw)
    return service.db.add_rule(Rule(**base))


@pytest.fixture()
def service(tmp_path):
    svc, clock, source = build(tmp_path)
    yield svc, clock, source
    svc.stop(backup=False)


# ------------------------------------------------------------------ lifecycle
def test_start_creates_database_and_starts_everything(service):
    svc, clock, source = service
    assert svc.start_background() is True
    assert svc.running is True
    assert svc.db is not None and svc.db_path.exists()
    assert svc.ipc is not None and svc.ipc.bound_port  # browser link is up
    assert svc.token  # pairing token created/reused
    assert svc.pause_ctl is not None and svc.pause_ctl.is_paused() is False


def test_stop_flushes_sessions_and_releases_the_lock(service):
    svc, clock, source = service
    svc.start()
    rid = add_rule(svc)
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()  # opens a session
    advance(svc, clock, 30)
    assert svc.tracker.today_total(rid) == 30
    lock_path = svc.db_path.with_suffix(".lock")

    svc.stop(backup=False)
    assert svc.running is False
    assert svc.db is None
    assert not lock_path.exists()  # lock released
    # Reopening proves the data was flushed, not lost in memory.
    from app.database.db import Database

    db = Database(svc.db_path).connect()
    assert db.get_daily(rid, "2026-09-21") == 30
    db.close()


def test_second_instance_is_refused(tmp_path, foreign_pid):
    """Two agents in *different processes* is the real scenario; the lock file
    identifies the owner by PID, so this test claims the lock for a live foreign
    pid (see conftest.foreign_pid)."""
    path = tmp_path / "t.lock"
    first, _, _ = build(tmp_path)
    second, _, _ = build(tmp_path)
    try:
        first.start()
        path.write_text(f"{foreign_pid}:0")  # a live, foreign owner
        with pytest.raises(AgentAlreadyRunning) as exc:
            second.start()
        assert exc.value.pid == foreign_pid
        assert second.db is None  # a refused start leaves nothing half-built
        assert first.db is not None  # the first instance is untouched
    finally:
        first.stop(backup=False)
        second.stop(backup=False)


def test_guard_can_be_disabled_for_read_only_tools(tmp_path):
    first, _, _ = build(tmp_path)
    reader, _, _ = build(tmp_path, concurrency_guard=False)
    try:
        first.start()
        assert reader.start() is True  # e.g. --status-json alongside the agent
    finally:
        first.stop(backup=False)
        reader.stop(backup=False)


def test_failed_start_releases_the_lock(tmp_path):
    svc, _, _ = build(tmp_path)
    svc.db_path.parent.mkdir(parents=True, exist_ok=True)
    # Force a failure inside start(): an invalid monitoring interval.
    svc.settings.monitoring_interval = 999
    with pytest.raises(Exception):
        svc.start()
    assert not svc.db_path.with_suffix(".lock").exists()
    assert svc.db is None


def test_start_is_idempotent(service):
    svc, _, _ = service
    assert svc.start() is True
    assert svc.start() is True  # second call is a no-op, not a second agent
    port = svc.ipc.bound_port
    assert svc.ipc.bound_port == port


# ---------------------------------------------------------------------- pause
def test_pause_stops_counting_for_normal_rules(service):
    svc, clock, source = service
    svc.start()
    rid = add_rule(svc)
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()
    advance(svc, clock, 30)
    assert svc.tracker.today_total(rid) == 30

    svc.pause(30)
    assert svc.snapshot().paused is True
    advance(svc, clock, 120)  # two minutes of paused ticks
    assert svc.tracker.today_total(rid) == 30  # nothing accrued

    svc.resume()
    advance(svc, clock, 40)
    # Counting again — 65 rather than 70 because of the documented session
    # contract: the interval is billed when a session is open *across* it, and
    # the pause sealed this rule's session, so the re-opening tick bills 0.
    assert svc.tracker.today_total(rid) == 65


def test_strict_rules_keep_counting_during_a_pause(service):
    svc, clock, source = service
    svc.start()
    normal = add_rule(svc)
    strict = add_rule(svc, name="League", type=RuleType.GAME, mode=Mode.STRICT,
                      executable="league of legends.exe", target="league of legends.exe",
                      action=Action.CLOSE)
    source.launch("discord.exe")
    source.launch("league of legends.exe")
    source.focus("league of legends.exe")
    svc.pause(60)
    svc.monitor.tick_once()
    advance(svc, clock, 30)
    snapshot = svc.snapshot()
    by_id = {row.rule.id: row for row in snapshot.rules}
    assert by_id[strict].used_seconds == 30  # STRICT ignores pauses
    assert by_id[normal].used_seconds == 0
    assert snapshot.pause_exempt_rules == ("League",)


def test_pause_prevents_enforcement_actions(service):
    """A paused rule that is already over its limit must not be closed."""
    svc, clock, source = service
    svc.start()
    add_rule(svc, daily_limit_seconds=60)
    controller = svc.closer.controller
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()
    advance(svc, clock, 65)
    assert controller.requests  # closed as usual at the limit
    before = len(controller.requests)

    source.launch("discord.exe")  # user relaunches
    source.focus("discord.exe")
    svc.pause(60)
    advance(svc, clock, 60)
    assert len(controller.requests) == before  # nothing new was closed
    rows = [r for r in svc.enforcement_history(limit=20)
            if (r["detail"] or "").startswith("paused by the user")]
    assert rows, "the audit trail must say the action was skipped because of a pause"


# ---------------------------------------------------------------------- rules
def test_rule_crud_pushes_to_the_bridge(service):
    svc, _, _ = service
    svc.start()
    pushes: list[str] = []
    svc.bridge.attach_broadcast(lambda message: pushes.append(message) or 1)
    rule = Rule(name="YouTube", type=RuleType.WEBSITE, target="youtube.com",
                domain="youtube.com", daily_limit_seconds=1800, action=Action.BLOCK)
    rule_id = svc.save_rule(rule)
    assert any("RULE_UPDATE" in msg for msg in pushes)

    rule.id = rule_id
    rule.daily_limit_seconds = 900
    svc.save_rule(rule)
    assert svc.db.get_rule(rule_id).daily_limit_seconds == 900

    svc.set_rule_enabled(rule_id, False)
    assert svc.db.get_rule(rule_id).enabled is False

    svc.delete_rule(rule_id)
    assert svc.db.get_rule(rule_id) is None


def test_invalid_rule_is_rejected_before_it_reaches_the_database(service):
    svc, _, _ = service
    svc.start()
    with pytest.raises(ValueError):
        svc.save_rule(Rule(name="", type=RuleType.APPLICATION, target="x.exe",
                           executable="x.exe", daily_limit_seconds=60, action=Action.CLOSE))


# ------------------------------------------------------------------- snapshot
def test_snapshot_shape_and_json_safety(service):
    import json

    svc, clock, source = service
    svc.start()
    rid = add_rule(svc)
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()
    advance(svc, clock, 45)

    snapshot = svc.snapshot()
    payload = snapshot.to_dict()
    assert json.dumps(payload)  # no unserializable values
    assert payload["running"] is True
    assert payload["monitor"]["ticks"] >= 2
    assert payload["ipc"]["running"] is True and payload["ipc"]["port"]
    rule = next(r for r in payload["rules"] if r["id"] == rid)
    assert rule["used_seconds"] == 45
    assert rule["remaining_today_seconds"] == 600 - 45
    assert 0 < rule["used_ratio"] < 1
    assert payload["storage"]["db_path"].endswith("t.db")


def test_snapshot_never_raises_even_when_stopped(tmp_path):
    svc, _, _ = build(tmp_path)
    snapshot = svc.snapshot()  # before start()
    assert snapshot.running is False
    assert snapshot.rules == []
    svc.start()
    svc.stop(backup=False)
    assert svc.snapshot().running is False  # after stop: still safe


def test_snapshot_reports_tick_errors(service):
    svc, _, _ = service
    svc.start()  # no worker thread: exactly one tick, exactly one error

    class Boom(FakeProcessSource):
        def snapshot(self):
            raise OSError("process table gone")

    boom = Boom()
    boom.clock = svc.clock
    svc.monitor._source = boom
    svc.monitor.tick_once()
    snapshot = svc.snapshot()
    assert snapshot.tick_errors == 1
    assert "process table gone" in snapshot.last_tick_error
    assert "error" in svc.snapshot().to_dict()["monitor"]["last_error"].lower()


def test_states_come_from_the_engine_not_the_demo(service):
    svc, clock, source = service
    svc.start()
    rid = add_rule(svc, daily_limit_seconds=60)
    source.launch("discord.exe")
    source.focus("discord.exe")
    svc.monitor.tick_once()
    advance(svc, clock, 70)
    row = next(r for r in svc.snapshot().rules if r.rule.id == rid)
    assert row.state == RuleState.ENFORCED
    assert row.remaining_today == 0


# ---------------------------------------------------------------- token/backup
def test_token_rotation_restarts_the_browser_link(service):
    svc, _, _ = service
    svc.start()
    first_token = svc.token
    new_token = svc.regenerate_token()
    assert new_token != first_token
    assert svc.bridge.token == new_token
    assert svc.ipc.bound_port  # link came back up
    assert svc.ipc.status()["last_error"] == ""


def test_backup_writes_a_file(service, monkeypatch, tmp_path):
    svc, _, _ = service
    svc.start()
    monkeypatch.setattr("app.service.profile_dir", lambda: tmp_path / "profile")
    target = svc.backup()
    assert target is not None and target.exists()
    assert target.name.startswith("timemanager-")

def test_default_profile_construction_does_not_call_the_db_path(tmp_path):
    """Regression: resolved_db_path is a *property*; constructing the service
    without an explicit db path (how every real user starts the app) used to
    call it with parentheses -> TypeError: 'WindowsPath' object is not
    callable. Every test passed an explicit path, so the branch never ran
    until the first real launch. A default-profile construction must work
    and produce a real path."""
    svc = AgentService(None, settings=AppSettings(db_path=""), concurrency_guard=False)
    from pathlib import Path as _Path

    assert isinstance(svc.db_path, _Path)
    assert svc.db_path.name == "timemanager.db"
