"""Phase 7: agent lifecycle audit — unclean-stop detection (schema v3)."""

import pytest

from app.core.clock import FakeClock
from app.core.security.lifecycle import LifecycleAudit
from app.database.db import Database, SCHEMA_VERSION


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    clock = FakeClock()
    yield db, clock
    db.close()


def test_schema_v3_creates_agent_audit(env):
    db, _ = env
    assert SCHEMA_VERSION == 3
    row = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_audit'"
    ).fetchone()
    assert row is not None


def test_log_audit_validates_severity(env):
    db, _ = env
    with pytest.raises(ValueError):
        db.log_audit("START", "catastrophic", "nope")


def test_first_run_has_no_finding(env):
    db, clock = env
    audit = LifecycleAudit(db, clock)
    assert audit.detect_unclean_stop() is None
    audit.record_start("0.7.0")
    audit.record_stop("clean shutdown")
    # Clean stop written -> the next run sees no problem.
    assert LifecycleAudit(db, clock).detect_unclean_stop() is None


def test_unclean_stop_detected_after_kill(env):
    db, clock = env
    audit = LifecycleAudit(db, clock)
    audit.record_start("0.7.0")
    clock.advance(3600)
    # ...the agent is killed: no record_stop() ever happens.

    next_run = LifecycleAudit(db, clock)
    finding = next_run.detect_unclean_stop()
    assert finding is not None
    assert finding.last_start_at  # ISO timestamp of the killed run's START
    assert finding.started_local is not None

    next_run.report_unclean_stop(finding, "0.7.0")
    rows = db.list_audit(kind="UNEXPECTED_STOP")
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"
    assert "never shut down cleanly" in rows[0]["detail"]


def test_detection_points_at_original_start_until_new_start(env):
    db, clock = env
    first = LifecycleAudit(db, clock)
    first.record_start("0.7.0")  # killed shortly after

    second = LifecycleAudit(db, clock)
    finding = second.detect_unclean_stop()
    assert finding is not None
    second.report_unclean_stop(finding, "0.7.0")
    # The UNEXPECTED_STOP row consumes the finding: re-running detection must
    # NOT report the same kill again (no duplicate alerts across restarts).
    assert second.detect_unclean_stop() is None
    second.record_start("0.7.0")  # this run claims the profile...
    # ...and if IT is killed, the next start detects it again.
    assert LifecycleAudit(db, clock).detect_unclean_stop() is not None
    LifecycleAudit(db, clock).record_stop("clean shutdown")
    assert LifecycleAudit(db, clock).detect_unclean_stop() is None


def test_audit_rows_keep_order_and_kind(env):
    db, clock = env
    audit = LifecycleAudit(db, clock)
    audit.record_start("0.7.0")
    db.log_audit("USAGE_TAMPERED", "critical", "rule='X' day=2026-09-21")
    audit.record_stop("clean shutdown")
    rows = db.list_audit(limit=10)
    kinds = [r["kind"] for r in rows]  # newest first
    assert kinds == ["STOP", "USAGE_TAMPERED", "START"]
    assert db.last_audit_of(("START", "STOP", "UNEXPECTED_STOP"))["kind"] == "STOP"
