"""Agent lifecycle audit — "did the agent stop without saying so?"

Bypass vector this closes (spec §15): the user kills the agent, uses tracked
apps freely, then restarts it. Killing a process leaves no trace in the
process itself, so the trace lives in the database: every run writes a START
row when it takes over the profile and a STOP row on clean shutdown. At the
next start, a START with no matching STOP means the previous run ended
uncleanly — killed, crashed, or power-lost — and the user is told, with the
timestamp of the last known heartbeat, that limits were not enforced in
between.

Rules:
- Only the instance that owns the profile writes lifecycle rows; a second
  instance (exit 3) and read-only status runs never touch the audit.
- STOP is written only on an intentional `stop()`. SIGKILL, power loss and
  crashes therefore *cannot* fake a clean stop — they simply never write one.
- Detection is purely historical: it never blocks the new run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.clock import Clock
from app.database.db import Database

log = logging.getLogger(__name__)

#: Kinds that participate in the START/STOP pairing.
LIFECYCLE_KINDS = ("START", "STOP", "UNEXPECTED_STOP")


@dataclass(frozen=True)
class UncleanStop:
    """The previous run started but never recorded a clean stop."""

    last_start_at: str  # ISO-8601 UTC, as stored
    detail: str = ""

    @property
    def started_local(self) -> datetime:
        dt = datetime.fromisoformat(self.last_start_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()


class LifecycleAudit:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # ------------------------------------------------------------------ write
    def _now(self) -> tuple[str, float]:
        return (
            datetime.fromtimestamp(self._clock.wall(), tz=timezone.utc).isoformat(),
            self._clock.mono(),
        )

    def record_start(self, version: str) -> int:
        ts, mono = self._now()
        return self._db.log_audit(
            "START", "info", f"agent v{version} took over the profile",
            recorded_at=ts, mono=mono,
        )

    def record_stop(self, reason: str = "clean") -> int:
        ts, mono = self._now()
        return self._db.log_audit("STOP", "info", reason, recorded_at=ts, mono=mono)

    # ------------------------------------------------------------------- read
    def detect_unclean_stop(self) -> UncleanStop | None:
        """Call BEFORE `record_start()` of the new run.

        Returns the START row of the previous run when that run never wrote a
        STOP. A previous UNEXPECTED_STOP row does not reset the pairing: the
        original START stays the reference point for the gap.
        """
        row = self._db.last_audit_of(LIFECYCLE_KINDS)
        if row is None:
            return None  # first ever run: nothing to compare against
        if row["kind"] == "START":
            return UncleanStop(last_start_at=row["recorded_at"], detail=row["detail"] or "")
        return None

    def report_unclean_stop(self, finding: UncleanStop, version: str) -> None:
        """Record the finding itself, so the audit trail is self-contained."""
        ts, mono = self._now()
        self._db.log_audit(
            "UNEXPECTED_STOP", "critical",
            f"previous run (started {finding.last_start_at}) never shut down cleanly; "
            f"limits were not enforced after that point (detected by v{version})",
            recorded_at=ts, mono=mono,
        )
        log.warning(
            "Unclean stop detected: previous run started %s and never recorded a stop.",
            finding.last_start_at,
        )
