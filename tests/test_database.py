import pytest

from app.core.rules.models import Rule
from app.core.types import RuleState
from app.database.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db").connect()
    yield d
    d.close()


def sample_rule():
    return Rule(name="Discord", type="APPLICATION", target="discord.exe",
                executable="discord.exe", daily_limit_seconds=3600, action="CLOSE")


def test_rule_crud(db):
    rid = db.add_rule(sample_rule())
    assert db.get_rule(rid).name == "Discord"
    assert len(db.list_rules()) == 1
    r = db.get_rule(rid)
    r.name = "Discord 2"
    db.update_rule(r)
    assert db.get_rule(rid).name == "Discord 2"
    db.delete_rule(rid)
    assert db.get_rule(rid) is None


def test_daily_aggregate_persists(db):
    rid = db.add_rule(sample_rule())
    assert db.get_daily(rid, "2026-09-21") == 0
    assert db.add_daily(rid, "2026-09-21", 60) == 60
    assert db.add_daily(rid, "2026-09-21", 30) == 90
    assert db.get_daily(rid, "2026-09-22") == 0  # different local day


def test_session_lifecycle_idempotent(db):
    rid = db.add_rule(sample_rule())
    sid = db.open_session(rid)
    db.heartbeat_session(sid, 25)
    assert len(db.open_sessions()) == 1
    db.close_session(sid, 30)
    db.close_session(sid, 999)  # second close must NOT overwrite
    assert db.open_sessions() == []
    row = db.conn.execute("SELECT duration_seconds, completed FROM usage_sessions WHERE id=?",
                          (sid,)).fetchone()
    assert (row["duration_seconds"], row["completed"]) == (30, 1)


def test_crash_recovery_marks_once(db):
    rid = db.add_rule(sample_rule())
    sid = db.open_session(rid)
    db.heartbeat_session(sid, 41)
    assert db.recover_open_sessions() == 1
    assert db.recover_open_sessions() == 0  # idempotent
    row = db.conn.execute(
        "SELECT duration_seconds, completed, created_from_recovery FROM usage_sessions WHERE id=?",
        (sid,)).fetchone()
    assert (row["duration_seconds"], row["completed"], row["created_from_recovery"]) == (41, 1, 1)


def test_state_and_settings(db):
    rid = db.add_rule(sample_rule())
    assert db.get_state(rid) is None
    db.put_state(rid, "2026-09-21", RuleState.WARNING, frozenset({600}))
    day, state, warned = db.get_state(rid)
    assert (day, state, warned) == ("2026-09-21", RuleState.WARNING, frozenset({600}))
    assert db.get_setting("monitoring_interval") == 1.0
    db.set_setting("theme", "dark")
    assert db.get_setting("theme") == "dark"
    db.log_browser_event("chrome", "youtube.com", True)
    db.log_clock("test")


def test_schema_v2_enforcement_log(db):
    rid = db.add_rule(sample_rule())
    a = db.log_enforcement(rid, "2026-09-21", "CLOSE_APP", "NORMAL", "EXECUTED", "pids=42")
    b = db.log_enforcement(rid, "2026-09-22", "CLOSE_APP", "STRICT", "FAILED", "denied")
    assert (a, b) == (1, 2)
    rows = db.list_enforcement("2026-09-21")
    assert len(rows) == 1 and rows[0]["detail"] == "pids=42"
    assert rows[0]["mode"] == "NORMAL" and rows[0]["created_at"]
    assert len(db.list_enforcement()) == 2  # no day filter
    assert db.list_enforcement("2026-01-01") == []


def test_enforcement_log_follows_rule_deletion(db):
    rid = db.add_rule(sample_rule())
    db.log_enforcement(rid, "2026-09-21", "CLOSE_APP", "NORMAL", "EXECUTED", "")
    db.delete_rule(rid)
    assert db.list_enforcement() == []  # ON DELETE CASCADE, no orphans


def test_new_phase3_settings_have_defaults(db):
    assert db.get_setting("enforce_foreground_only") is True
    assert db.get_setting("idle_grace_seconds") == 0
    assert db.get_setting("graceful_close_timeout_seconds") == 6
    assert db.get_setting("protected_processes") == []


def test_connection_is_usable_from_another_thread(db):
    """The monitor loop ticks on a worker thread (Phase 3)."""
    import threading

    errors: list[str] = []

    def read_in_thread():
        try:
            db.get_setting("monitoring_interval")
            db.list_rules()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    t = threading.Thread(target=read_in_thread)
    t.start()
    t.join(timeout=5)
    assert errors == []
