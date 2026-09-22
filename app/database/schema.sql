-- Time Manager SQLite schema, version 1.
-- Canonical DDL; applied by app/database/db.py (fresh create or migration).

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    type                 TEXT NOT NULL CHECK (type IN ('APPLICATION','GAME','WEBSITE')),
    target               TEXT NOT NULL,
    executable           TEXT NULL,
    extra_executables    TEXT NOT NULL DEFAULT '[]',
    domain               TEXT NULL,
    daily_limit_seconds  INTEGER NULL CHECK (daily_limit_seconds IS NULL OR (daily_limit_seconds > 0 AND daily_limit_seconds <= 86400)),
    session_limit_seconds INTEGER NULL CHECK (session_limit_seconds IS NULL OR (session_limit_seconds > 0 AND session_limit_seconds <= 86400)),
    warning_seconds      TEXT NOT NULL DEFAULT '[600,300,60]',
    action               TEXT NOT NULL CHECK (action IN ('BLOCK','CLOSE','WAIT_FOR_SESSION_END','WARN_ONLY')),
    mode                 TEXT NOT NULL CHECK (mode IN ('NORMAL','STRICT')),
    schedule             TEXT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (daily_limit_seconds IS NOT NULL OR session_limit_seconds IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_rules_type ON rules(type);
CREATE INDEX IF NOT EXISTS idx_rules_enabled ON rules(enabled);

CREATE TABLE IF NOT EXISTS usage_sessions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id               INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    started_at            TEXT NOT NULL,
    ended_at              TEXT NULL,
    duration_seconds      INTEGER NOT NULL DEFAULT 0,
    monotonic_start       REAL NULL,
    session_type          TEXT NOT NULL DEFAULT 'FOREGROUND'
                          CHECK (session_type IN ('FOREGROUND','WEBSITE','GAME')),
    completed             INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0,1)),
    created_from_recovery INTEGER NOT NULL DEFAULT 0 CHECK (created_from_recovery IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_sessions_rule_start ON usage_sessions(rule_id, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON usage_sessions(completed) WHERE completed = 0;

CREATE TABLE IF NOT EXISTS daily_usage (
    rule_id       INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    day           TEXT NOT NULL, -- local YYYY-MM-DD
    total_seconds INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (rule_id, day)
);

CREATE TABLE IF NOT EXISTS enforcement_state (
    rule_id           INTEGER PRIMARY KEY REFERENCES rules(id) ON DELETE CASCADE,
    day               TEXT NOT NULL,
    state             TEXT NOT NULL CHECK (state IN ('NORMAL','WARNING','LIMIT_REACHED','WAITING_FOR_SESSION_END','ENFORCED','DISABLED')),
    warned_thresholds TEXT NOT NULL DEFAULT '[]',
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS browser_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    browser      TEXT NOT NULL,
    domain       TEXT NOT NULL,
    active       INTEGER NOT NULL CHECK (active IN (0,1)),
    received_at  TEXT NOT NULL,
    monotonic_at REAL NOT NULL,
    processed    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_browser_domain_time ON browser_events(domain, received_at);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL, -- JSON-encoded
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    monotonic   REAL NOT NULL,
    note        TEXT NULL
);

-- v2: audit trail for every enforcement action the agent took (or declined).
CREATE TABLE IF NOT EXISTS enforcement_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id      INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,      -- local YYYY-MM-DD
    action       TEXT NOT NULL,      -- CLOSE_APP|PREVENT_LAUNCH|BLOCK_WEBSITE|NOTIFY_ONLY
    mode         TEXT NOT NULL,      -- NORMAL|STRICT
    outcome      TEXT NOT NULL,      -- EXECUTED|SKIPPED|DEFERRED|FAILED|PROTECTED
    detail       TEXT NULL,          -- e.g. "pids=1234,5678" / "target not running"
    created_at   TEXT NOT NULL,
    monotonic_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enforcement_rule_day
    ON enforcement_log(rule_id, day);

-- v3 (Phase 7): agent lifecycle + anti-bypass audit trail.
-- One row per security-relevant event: agent start/stop, unclean stops,
-- usage-floor tamper findings, startup repairs, watchdog changes,
-- extension-silence episodes, clock regressions. The user can always read
-- what the agent noticed and why (Settings -> audit / --status-json).
CREATE TABLE IF NOT EXISTS agent_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,   -- wall time, local offset ISO-8601
    monotonic   REAL NOT NULL,
    kind        TEXT NOT NULL,   -- START|STOP|UNEXPECTED_STOP|USAGE_TAMPERED|
                                 -- STARTUP_REPAIRED|WATCHDOG_INSTALLED|
                                 -- WATCHDOG_REMOVED|EXTENSION_SILENT|
                                 -- EXTENSION_RESTORED|CLOCK_REGRESSED
    severity    TEXT NOT NULL DEFAULT 'info'
                CHECK (severity IN ('info','warning','critical')),
    detail      TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_kind ON agent_audit(kind);
CREATE INDEX IF NOT EXISTS idx_agent_audit_time ON agent_audit(recorded_at);
