"""Centralized SQLite access. ALL SQL lives here (plus schema.sql).

Usage:
    db = Database(path)   # creates + migrates on connect
    rule_id = db.add_rule(rule)
    db.upsert_daily(rule_id, "2026-09-21", 60)
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from pathlib import Path

from app.core.rules.models import Rule
from app.core.timeutils import monotonic, utcnow_iso
from app.core.types import RuleState

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
_SCHEMA_SQL = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")

DEFAULT_SETTINGS: dict[str, object] = {
    "monitoring_interval": 1.0,
    "default_warning_seconds": [600, 300, 60],
    "website_grace_seconds": 3,
    "website_stale_seconds": 15,
    "strict_relaunch_guard_seconds": 300,
    "theme": "system",
    "language": "en",
    "launch_at_startup": False,
    "start_minimized": False,
    "notifications_enabled": True,
    "heartbeat_persist_seconds": 5,
    # --- Phase 3: monitoring & enforcement ---------------------------------
    # Count an app's time only while it is in the foreground (plus a short
    # grace for alt-tabbing). False = "running counts", for exotic setups.
    "enforce_foreground_only": True,
    "background_grace_seconds": 5,
    # Ignore idle time (walked away / watching a movie) after N seconds of no
    # user input. 0 = disabled (count everything).
    "idle_grace_seconds": 0,
    # CLOSE escalation: post WM_CLOSE, wait, then force-terminate.
    "graceful_close_timeout_seconds": 6,
    # Website accounting comes from the extension (Phase 4); grace/stale
    # windows used to debounce tab switching.
    "website_grace_seconds": 3,
    "website_stale_seconds": 15,
    # Extra exes that must never be terminated, on top of the built-in list.
    "protected_processes": [],
    # Correlate detector verdicts with enforcement.
    "game_detector_enabled": True,
    # --- Phase 6: game detectors -------------------------------------------
    # After the match process disappears, wait this long before trusting
    # "no match is running" (a reconnect attempt looks exactly like this).
    "game_settle_seconds": 30,
    # Budget for one detector probe; slower than this -> unknown -> WAIT.
    "game_probe_timeout_ms": 300,
    # Quarantine a detector after this many consecutive failures (fail-safe).
    "game_plugin_max_failures": 5,
    # Local, read-only evidence (never leaves the machine).
    "game_live_api_enabled": True,
    "game_live_api_port": 2999,
    "game_log_scan_enabled": True,
    "game_log_fresh_seconds": 90,
    # Explicit log locations; [] = auto-detect the standard Riot paths.
    "game_log_dirs": [],
    # Drop-in detector plugins; "" = <profile>/detectors
    "detector_plugins_dir": "",
    # --- Phase 4: browser extension link ------------------------------------
    "ipc_port": 17846,
    "ipc_rate_limit_count": 30,
    "ipc_rate_limit_window": 10.0,
    # Tab events only count while a browser owns the foreground window.
    "require_browser_foreground": True,
}


class DatabaseError(RuntimeError):
    pass


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> "Database":
        with self._lock:
            if self._conn is not None:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # check_same_thread=False: the monitor loop ticks on a worker
                # thread while the CLI/UI thread reads status. Every access in
                # this class is serialized by self._lock, so this is safe and
                # is what SQLite documents as the correct pattern for a shared
                # connection.
                conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            except sqlite3.Error as exc:
                raise DatabaseError(f"Cannot open database {self.path}: {exc}") from exc
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
            self._migrate()
            self._seed_settings()
            return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                finally:
                    self._conn.close()
                    self._conn = None

    def __enter__(self) -> "Database":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("Database not connected; call .connect() first.")
        return self._conn

    # -------------------------------------------------------------- migrate
    def _migrate(self) -> None:
        assert self._conn is not None
        self._conn.executescript(_SCHEMA_SQL)
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] if row and row["v"] is not None else 0
        if current < SCHEMA_VERSION:
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_version(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, utcnow_iso()),
            )
        self._conn.commit()

    def _seed_settings(self) -> None:
        assert self._conn is not None
        for key, value in DEFAULT_SETTINGS.items():
            self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
                (key, json.dumps(value), utcnow_iso()),
            )
        self._conn.commit()

    # ----------------------------------------------------------------- rules
    def add_rule(self, rule: Rule) -> int:
        rule.validate()
        with self._lock:
            row = rule.to_row()
            cur = self.conn.execute(
                """INSERT INTO rules(name,type,target,executable,extra_executables,domain,
                   daily_limit_seconds,session_limit_seconds,warning_seconds,action,mode,
                   schedule,enabled,created_at,updated_at)
                   VALUES(:name,:type,:target,:executable,:extra_executables,:domain,
                   :daily_limit_seconds,:session_limit_seconds,:warning_seconds,:action,:mode,
                   :schedule,:enabled,:now,:now)""",
                {**row, "now": utcnow_iso()},
            )
            self.conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def update_rule(self, rule: Rule) -> None:
        if rule.id is None:
            raise DatabaseError("Cannot update a rule without id.")
        rule.validate()
        with self._lock:
            row = rule.to_row()
            cur = self.conn.execute(
                """UPDATE rules SET name=:name,type=:type,target=:target,executable=:executable,
                   extra_executables=:extra_executables,domain=:domain,
                   daily_limit_seconds=:daily_limit_seconds,session_limit_seconds=:session_limit_seconds,
                   warning_seconds=:warning_seconds,action=:action,mode=:mode,schedule=:schedule,
                   enabled=:enabled,updated_at=:now WHERE id=:id""",
                {**row, "now": utcnow_iso()},
            )
            self.conn.commit()
            if cur.rowcount == 0:
                raise DatabaseError(f"No rule with id={rule.id}.")

    def delete_rule(self, rule_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            self.conn.commit()

    def get_rule(self, rule_id: int) -> Rule | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
            return Rule.from_row(row) if row else None

    def list_rules(self, *, enabled_only: bool = False) -> list[Rule]:
        with self._lock:
            sql = "SELECT * FROM rules"
            if enabled_only:
                sql += " WHERE enabled=1"
            sql += " ORDER BY name"
            return [Rule.from_row(r) for r in self.conn.execute(sql).fetchall()]

    # ---------------------------------------------------------------- sessions
    def open_session(
        self, rule_id: int, session_type: str = "FOREGROUND", started_at: str | None = None
    ) -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO usage_sessions(rule_id,started_at,monotonic_start,session_type,completed)
                   VALUES(?,?,?,?,0)""",
                (rule_id, started_at or utcnow_iso(), monotonic(), session_type),
            )
            self.conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def heartbeat_session(self, session_id: int, duration_seconds: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE usage_sessions SET duration_seconds=? WHERE id=? AND completed=0",
                (max(0, int(duration_seconds)), session_id),
            )
            self.conn.commit()

    def close_session(self, session_id: int, duration_seconds: int) -> None:
        """Idempotent close: second call is a no-op (no double counting)."""
        with self._lock:
            self.conn.execute(
                """UPDATE usage_sessions SET ended_at=?, duration_seconds=?, completed=1
                   WHERE id=? AND completed=0""",
                (utcnow_iso(), max(0, int(duration_seconds)), session_id),
            )
            self.conn.commit()

    def open_sessions(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM usage_sessions WHERE completed=0 ORDER BY started_at"
            ).fetchall()

    def recover_open_sessions(self, cutoff_iso: str | None = None) -> int:
        """Crash recovery: close rows left open by an unclean shutdown.

        Duration is capped at already-heartbeated value (never grows during
        recovery, so no double count). Returns number of rows recovered.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM usage_sessions WHERE completed=0"
            ).fetchall()
            ended = cutoff_iso or utcnow_iso()
            for r in rows:
                self.conn.execute(
                    """UPDATE usage_sessions SET ended_at=?, completed=1,
                       created_from_recovery=1 WHERE id=? AND completed=0""",
                    (ended, r["id"]),
                )
            self.conn.commit()
            return len(rows)

    # ------------------------------------------------------------ daily usage
    def add_daily(self, rule_id: int, day: str, seconds: int) -> int:
        """Increment today's aggregate; returns the new total."""
        with self._lock:
            self.conn.execute(
                """INSERT INTO daily_usage(rule_id,day,total_seconds,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(rule_id,day) DO UPDATE SET
                   total_seconds=daily_usage.total_seconds+excluded.total_seconds,
                   updated_at=excluded.updated_at""",
                (rule_id, day, max(0, int(seconds)), utcnow_iso()),
            )
            row = self.conn.execute(
                "SELECT total_seconds FROM daily_usage WHERE rule_id=? AND day=?",
                (rule_id, day),
            ).fetchone()
            self.conn.commit()
            return int(row["total_seconds"]) if row else 0

    def get_daily(self, rule_id: int, day: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT total_seconds FROM daily_usage WHERE rule_id=? AND day=?",
                (rule_id, day),
            ).fetchone()
            return int(row["total_seconds"]) if row else 0

    def rebuild_daily(self, rule_id: int, day: str) -> int:
        """Recompute one aggregate from sessions (self-heal)."""
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(duration_seconds),0) AS t FROM usage_sessions
                   WHERE rule_id=? AND substr(started_at,1,10)=substr(?,1,10)""",
                (rule_id, day),
            ).fetchone()
            # NOTE: sessions store UTC; day keys are local. Exact rebuild needs
            # local-day attribution at write time (Phase 2 tracker splits
            # midnight-spanning sessions). This is a coarse fallback.
            total = int(row["t"]) if row else 0
            self.conn.execute(
                """INSERT INTO daily_usage(rule_id,day,total_seconds,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(rule_id,day) DO UPDATE SET
                   total_seconds=excluded.total_seconds, updated_at=excluded.updated_at""",
                (rule_id, day, total, utcnow_iso()),
            )
            self.conn.commit()
            return total

    # ------------------------------------------------------------------ state
    def get_state(self, rule_id: int) -> tuple[str, RuleState, frozenset[int]] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT day,state,warned_thresholds FROM enforcement_state WHERE rule_id=?",
                (rule_id,),
            ).fetchone()
            if not row:
                return None
            return (
                row["day"],
                RuleState(row["state"]),
                frozenset(json.loads(row["warned_thresholds"] or "[]")),
            )

    def put_state(
        self, rule_id: int, day: str, state: RuleState, warned: frozenset[int] | set[int]
    ) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO enforcement_state(rule_id,day,state,warned_thresholds,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET day=excluded.day,
                   state=excluded.state, warned_thresholds=excluded.warned_thresholds,
                   updated_at=excluded.updated_at""",
                (rule_id, day, state.value, json.dumps(sorted(warned)), utcnow_iso()),
            )
            self.conn.commit()

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: object = None) -> object:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: object) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, json.dumps(value), utcnow_iso()),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ misc
    def log_browser_event(self, browser: str, domain: str, active: bool) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO browser_events(browser,domain,active,received_at,monotonic_at)
                   VALUES(?,?,?,?,?)""",
                (browser, domain, 1 if active else 0, utcnow_iso(), monotonic()),
            )
            self.conn.commit()

    def log_clock(self, note: str | None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO clock_log(recorded_at,monotonic,note) VALUES(?,?,?)",
                (utcnow_iso(), monotonic(), note),
            )
            self.conn.commit()

    # ------------------------------------------------------ enforcement audit
    def log_enforcement(
        self,
        rule_id: int,
        day: str,
        action: str,
        mode: str,
        outcome: str,
        detail: str | None = None,
        created_at: str | None = None,
    ) -> int:
        """Record one enforcement action/attempt (see schema.sql v2).

        `created_at` may be supplied for imports, demos and tests that run on a
        fake clock; production callers omit it and get the real wall clock.
        """
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO enforcement_log(rule_id,day,action,mode,outcome,detail,
                   created_at,monotonic_at) VALUES(?,?,?,?,?,?,?,?)""",
                (rule_id, day, action, mode, outcome, detail,
                 created_at or utcnow_iso(), monotonic()),
            )
            self.conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def list_enforcement(self, day: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        with self._lock:
            if day is None:
                return self.conn.execute(
                    "SELECT * FROM enforcement_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return self.conn.execute(
                """SELECT * FROM enforcement_log WHERE day=? ORDER BY id DESC LIMIT ?""",
                (day, limit),
            ).fetchall()

    # ------------------------------------------------------------ agent audit
    def log_audit(
        self,
        kind: str,
        severity: str,
        detail: str | None = None,
        *,
        recorded_at: str | None = None,
        mono: float | None = None,
    ) -> int:
        """Record one security/lifecycle event (schema v3, Phase 7).

        Timestamps default to the real clock; tests and fake-clock callers may
        supply `recorded_at`/`mono` so audit rows line up with simulated time.
        """
        if severity not in ("info", "warning", "critical"):
            raise ValueError(f"unknown audit severity: {severity!r}")
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO agent_audit(recorded_at,monotonic,kind,severity,detail)
                   VALUES(?,?,?,?,?)""",
                (recorded_at or utcnow_iso(),
                 monotonic() if mono is None else mono,
                 kind, severity, detail),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def list_audit(self, limit: int = 200, kind: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if kind is None:
                return self.conn.execute(
                    "SELECT * FROM agent_audit ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return self.conn.execute(
                "SELECT * FROM agent_audit WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            ).fetchall()

    def last_audit_of(self, kinds: tuple[str, ...]) -> sqlite3.Row | None:
        """Most recent audit row whose kind is in `kinds` (lifecycle lookups)."""
        marks = ",".join("?" for _ in kinds)
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM agent_audit WHERE kind IN ({marks}) "  # noqa: S608
                "ORDER BY id DESC LIMIT 1",
                tuple(kinds),
            ).fetchone()


    def backup(self, dest_dir: str | Path, keep: int = 7) -> Path | None:
        """Copy the DB file to `dest_dir` (call when idle); prune old copies."""
        with self._lock:
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                return None
            # Checkpoint WAL so the backup is self-contained.
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                log.warning("WAL checkpoint failed; backing up anyway.", exc_info=True)
            stamp = utcnow_iso().replace(":", "-").split(".")[0]
            target = dest / f"timemanager-{stamp}.db"
            shutil.copy2(self.path, target)
            copies = sorted(dest.glob("timemanager-*.db"))
            for old in copies[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return target
