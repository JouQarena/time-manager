# Database schema

SQLite, WAL mode, single file: `%APPDATA%\TimeManager\timemanager.db`.
All access centralized in `app/database/db.py`. Migrations are additive and
versioned in `schema_version`. Canonical DDL lives in `app/database/schema.sql`
(current version: **3**). Usage floors (Phase 7) are deliberately *not* in
this database — see `docs/PHASE7.md`.

## Tables

### `schema_version`
| col | type | notes |
|---|---|---|
| version | INTEGER PK | current = 3 |
| applied_at | TEXT | ISO-8601 UTC |

### `rules`
| col | type | notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| name | TEXT NOT NULL | display name, e.g. "League of Legends" |
| type | TEXT NOT NULL | `APPLICATION` \| `GAME` \| `WEBSITE` |
| target | TEXT NOT NULL | human target: exe display or domain as typed |
| executable | TEXT NULL | normalized exe, e.g. `leagueclient.exe` (apps/games) |
| domain | TEXT NULL | normalized domain, e.g. `youtube.com` (websites) |
| daily_limit_seconds | INTEGER NULL | NULL = no daily cap |
| session_limit_seconds | INTEGER NULL | NULL = no session cap |
| warning_seconds | TEXT NOT NULL DEFAULT `'[600,300,60]'` | JSON int array, seconds-before-limit |
| action | TEXT NOT NULL | `BLOCK` \| `CLOSE` \| `WAIT_FOR_SESSION_END` \| `WARN_ONLY` |
| mode | TEXT NOT NULL | `NORMAL` \| `STRICT` |
| schedule | TEXT NULL | JSON `Schedule`, NULL = always active |
| enabled | INTEGER NOT NULL DEFAULT 1 | 0/1 |
| created_at / updated_at | TEXT NOT NULL | ISO-8601 UTC |

Constraints: `CHECK(type IN (...))`, `CHECK(action IN (...))`,
`CHECK(mode IN (...))`; apps/games require `executable`, websites require
`domain` (enforced in Python validation + a trigger-free app-level check to
keep SQLite portable).

### `usage_sessions`
One row per continuous active-use session (NOT one row per second).
| col | type | notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| rule_id | INTEGER NOT NULL FK→rules(id) ON DELETE CASCADE | |
| started_at | TEXT NOT NULL | ISO-8601 UTC (wall) |
| ended_at | TEXT NULL | NULL = still open |
| duration_seconds | INTEGER NOT NULL DEFAULT 0 | updated by heartbeat; final on close |
| monotonic_start | REAL NULL | `time.monotonic()` at open (duration truth) |
| session_type | TEXT NOT NULL DEFAULT `'FOREGROUND'` | `FOREGROUND` \| `WEBSITE` \| `GAME` |
| completed | INTEGER NOT NULL DEFAULT 0 | 1 when cleanly closed |
| created_from_recovery | INTEGER NOT NULL DEFAULT 0 | 1 if rebuilt after crash |

Index: `(rule_id, started_at)`.

### `daily_usage`
Aggregate cache, PK `(rule_id, day)`. Rebuildable from `usage_sessions`.
| col | type | notes |
|---|---|---|
| rule_id | INTEGER NOT NULL FK | |
| day | TEXT NOT NULL | local calendar day `YYYY-MM-DD` |
| total_seconds | INTEGER NOT NULL DEFAULT 0 | |
| updated_at | TEXT NOT NULL | |

### `enforcement_state`
One row per rule per local day (upserted on tick).
| col | type | notes |
|---|---|---|
| rule_id | INTEGER PK FK | current-day state only; reset on day change |
| day | TEXT NOT NULL | local day the state belongs to |
| state | TEXT NOT NULL | `NORMAL` \| `WARNING` \| `LIMIT_REACHED` \| `WAITING_FOR_SESSION_END` \| `ENFORCED` |
| warned_thresholds | TEXT NOT NULL DEFAULT `'[]'` | JSON array of fired warning thresholds (no spam) |
| updated_at | TEXT NOT NULL | |

### `browser_events`
Short-lived audit/debounce log (pruned, e.g. keep 7 days).
| col | type | notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| browser | TEXT NOT NULL | `chrome` \| `edge` \| `firefox` |
| domain | TEXT NOT NULL | normalized |
| active | INTEGER NOT NULL | 0/1 (tab active AND window focused, per extension) |
| received_at | TEXT NOT NULL | **agent** wall receipt time (source of truth) |
| monotonic_at | REAL NOT NULL | agent monotonic receipt time |
| processed | INTEGER NOT NULL DEFAULT 0 | |

### `settings`
| col | type | notes |
|---|---|---|
| key | TEXT PK | |
| value | TEXT NOT NULL | JSON-encoded |
| updated_at | TEXT NOT NULL | |

Seeded keys: `monitoring_interval`, `default_warning_seconds`,
`website_grace_seconds`, `strict_relaunch_guard_seconds`, `theme`, `language`,
`launch_at_startup`, `start_minimized`, `notifications_enabled`.

### `clock_log`
Append-only (pruned) for sleep/skew detection.
| col | type | notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| recorded_at | TEXT NOT NULL | wall |
| monotonic | REAL NOT NULL | monotonic at that wall time |
| note | TEXT NULL | e.g. `suspend suspected`, `clock jumped +300s` |

## Write discipline

- No per-second rows. Heartbeats `UPDATE usage_sessions SET duration_seconds=…`
  on the open row + `INSERT … ON CONFLICT DO UPDATE` on `daily_usage`.
- Every state-changing tick runs in one transaction.
- Backup: copy DB file (SQLite backup API) to `backups/` on startup if last
  backup > 24 h, keep last 7. `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`.
- Crash recovery (Phase 2): on startup, any `usage_sessions` with
  `completed=0` are closed using `clock_log`/mtime heuristics and marked
  `created_from_recovery` where the end time was inferred — never double-count:
  recovery is idempotent (completed flips to 1 exactly once).

## Schema diagram

```
rules 1──∞ usage_sessions
rules 1──∞ daily_usage (per local day)
rules 1──1 enforcement_state (current day)
rules 1──∞ enforcement_log (audit trail)
agent_audit (security lifecycle log)
settings (k/v)   browser_events (log)   clock_log (log)
```
