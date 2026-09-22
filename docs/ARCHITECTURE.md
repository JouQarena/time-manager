# Architecture

## 1. System overview

```
┌──────────────────────────────────────────────────────────────┐
│                      DESKTOP AGENT (Python)                   │
│                                                               │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────┐  │
│  │ Monitor  │─▶│ Tracker  │─▶│ RuleEngine│─▶│  Enforcement │  │
│  │ (psutil  │  │ (sessions│  │ (limits,  │  │ (close/block/│  │
│  │ +Win32)  │  │ +daily)  │  │ warnings) │  │  wait-game)  │  │
│  └──────────┘  └──────────┘  └───────────┘  └──────────────┘  │
│        │             │              │               │          │
│        └─────────────┴──────┬───────┴───────────────┘          │
│                             ▼                                │
│                    ┌─────────────────┐    ┌──────────────┐    │
│                    │ SQLite Database │    │ IPC Server   │    │
│                    │ (WAL, sessions, │◀──▶│ (ws://127.0. │    │
│                    │ daily, state)   │    │ 0.0.1:port)  │    │
│                    └─────────────────┘    └──────────────┘    │
│        ▲                                     ▲                │
│  ┌─────┴────┐   ┌──────────────┐        ┌────┴──────┐        │
│  │ Tray/GUI │   │ PauseControl │        │ Game      │        │
│  │ (PySide6)│   │ + Instance   │        │ Detectors │        │
│  │          │   │   Lock       │        │ (plugins) │        │
│  └──────────┘   └──────────────┘        └───────────┘        │
│        all wired together by app/service.py (AgentService)   │
└──────────────────────────────────────────────────────────────┘
                               │ authenticated local WS
                               ▼
               ┌────────────────────────────────┐
               │  BROWSER EXTENSION (MV3)        │
               │  background service worker:    │
               │   active tab + focus tracking  │
               │   heartbeat, block page        │
               │  content script: overlay block │
               │  popup/options: status + token │
               └────────────────────────────────┘
```

## 2. Technology decisions

| Area | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Match spec; huge Windows ecosystem; fast iteration |
| GUI | PySide6 (Qt 6) | Spec choice; native look, tray, notifications, dark/light themes; LGPL-friendly vs PyQt |
| DB | SQLite (WAL) | Spec choice; zero-config, crash-safe with WAL + periodic checkpoints/backups; single-file |
| Process monitor | `psutil` + `pywin32` | `psutil` for portable process listing; `pywin32` (`GetForegroundWindow`, `GetWindowThreadProcessId`, power/suspend events) for what `psutil` cannot do |
| IPC | Local WebSocket (`websockets` lib) on `127.0.0.1` + secret token | Browser extensions **cannot** use named pipes; WebSocket is the only practical browser→desktop channel. Bound to loopback only, first message must authenticate, strict schema validation, agent never executes arbitrary commands |
| Startup | Registry `HKCU\...\Run` (+ optional Task Scheduler for strict mode) | Registry Run is transparent, user-visible, easy to uninstall; Task Scheduler entry (documented, named) as opt-in for "restart if killed" in strict mode |
| Packaging | PyInstaller | Spec choice; single-folder build, code-signing friendly |
| Extension | Manifest V3, no framework, shared `chrome.*` APIs | Works in Chrome + Edge today; Firefox (MV3-compatible `browser.*` with polyfill) later |

**Rejected alternatives:**
- Named pipes / TCP-sockets-only IPC: unreachable from extensions → rejected.
- Electron/Tauri desktop: unnecessary runtime weight; Python+Qt already covers it.
- ORM (SQLAlchemy): overkill; a small centralized `Database` class with
  hand-written SQL keeps the schema explicit and reviewable.

## 3. Module responsibilities

| Module | Owns | Must NOT do |
|---|---|---|
| `app/config` | Load/save settings, paths, token mgmt | Talk to DB or network |
| `app/core/types` | Shared enums (`RuleType`, `Action`, `Mode`, `RuleState`) | Business logic |
| `app/core/timeutils` | Local-day calc, monotonic durations, formatting | I/O |
| `app/core/rules` | `Rule` dataclass + validation; domain/exe matching | DB access (takes plain args) |
| `app/core/scheduling` | `Schedule` parsing + `is_within_schedule()` | Anything else |
| `app/core/tracking` | Session open/close, daily aggregation, recovery (Phase 2) | Kill processes |
| `app/core/enforcement` | Pure state machine `NORMAL→WARNING→LIMIT_REACHED→…` | Touch processes/network directly (emits *decisions*; `windows/` executes) |
| `app/core/monitoring` | Process snapshots, foreground PID (Phase 3) | Enforce |
| `app/core/detection` | `GameSessionDetector` ABC + registry (candidates for shared-process games), `DetectorHost` (probe budget, quarantine), plugin loader, built-in detectors for League of Legends, VALORANT, R.E.P.O. and TFT + local signal providers | Guess: fail-safe to "unknown → don't kill" |
| `app/core/security` | Phase 7 anti-bypass: `LifecycleAudit` (START/STOP pairing), `UsageFloor` (per-day high-water marks outside the DB), `BypassWatch` (silent-extension episodes) | Enforce (it only observes, records and reports; enforcement stays in `core/enforcement`) |
| `app/database` | ALL SQL; migrations; backups; crash recovery helpers | Business rules |
| `app/ipc` | Protocol validation, WS server, `AgentBridge` (rule status ↔ extension) | Trust extension timestamps for accounting |
| `app/windows` | Win32 calls: close app, notifications, startup, power events | Decide *policy* |
| `app/service` | `AgentService`: one owner of lock + DB + monitor + IPC + pause; exposes `snapshot()` and rule CRUD | Render anything (no Qt, no strings) |
| `app/ui/viewmodel` | Every word/colour role the user reads; `RuleRow` projection | Import Qt |
| `app/ui/qt` | Dashboard, rule editor, settings, tray, notifications, screenshots | Open sockets or touch SQL (all state comes from `AgentService`) |

Dependency direction: `ui/qt → ui/viewmodel → service → core → database`.
`windows/` and `ipc/` are *adapters* called by core services, never the reverse.
The service is the only object that knows the whole system; the GUI is a
consumer of `Snapshot` and a producer of validated `Rule` objects.

## 4. Threading / event-loop model (implemented, Phases 2–5)

- One `QApplication` main (GUI) thread; the dashboard polls
  `AgentService.snapshot()` on a 1 s `QTimer` and the timeline every 5 s.
- One monitor worker **thread** (plain `threading.Thread`, started by
  `MonitorLoop.start()`, so it works head-less too): every
  `monitoring_interval` (default 1 s): snapshot processes + foreground window →
  tracker tick → rule engine → enforcement decisions → push to bridge/tray.
- `AgentService.snapshot()` is guarded by an RLock: the GUI thread reads state
  while the monitor thread writes it, and neither ever sees a half-built row.
- Qt widgets are touched **only** from the GUI thread (the notifier and tray
  calls are the only cross-thread touch points, and they are queued/short).
- One asyncio WS server thread for the extension (token auth, validated
  messages only). Extension events are timestamped **on receipt** by the agent
  (monotonic + wall), never trusted blindly.
- SQLite writes serialized through a single `Database` connection owner
  (WAL mode allows concurrent readers). All writes use short transactions;
  usage persisted incrementally (heartbeat update every N seconds + on events),
  never "one row per second".

## 5. Data flow examples

**Desktop app tick (Phase 3):**
`psutil + GetForegroundWindow → foreground PID/exe → match rules (exe) →
tracker.add_heartbeat(rule, now) → rule engine (daily/session remaining,
warnings) → enforcement state machine → maybe CLOSE + STRICT relaunch-guard.`

**Website tick (Phase 4):**
`extension TAB_ACTIVITY(domain, active, window_focused) → IPC validate →
agent checks: browser proc alive? focus flag fresh (<5 s)? domain matches rule?
→ tracker heartbeat → same rule engine → BLOCK_DECISION → extension block page.`

**Game limit during match (Phase 6, implemented):**
`LIMIT_REACHED + action==WAIT_FOR_SESSION_END → GameSessionGuard →
DetectorHost → LeagueOfLegendsDetector (process table + local signals, inside a
300 ms budget) → verdict/confidence → in session (or unknown) → stay
WAITING_FOR_SESSION_END and re-poll : confident "no match" → BLOCK/CLOSE.
A detector error, timeout, quarantine or plugin rejection is *unknown* — it can
only delay enforcement, never cause it (see docs/PHASE6.md).`

## 6. Security boundaries

1. Extension is **untrusted input**: schema-validated, rate-limited, token-gated.
2. Only 5 inbound message types exist; no generic command execution.
3. Token: 32 random bytes hex, file `0600`-equivalent (user profile dir),
   regenerable from Settings; extension options page to paste it.
4. Clock: durations from `time.monotonic()`; calendar day from local wall clock
   with skew/sleep detection (see `docs/LIMITATIONS.md`).
5. Strict mode: relaunch-guard + startup repair + "unexpected shutdown"
   detection — all transparent, listed in Settings, uninstallable. No stealth.
6. Phase 7 hardening (`app/core/security`, `app/windows/watchdog.py`):
   lifecycle audit in `agent_audit` (schema v3) — a killed run is reported by
   the next one; per-day usage floors in `timemanager.floor.json` (a sibling
   of the DB, outside it) so STRICT rules evaluate `max(stored, floor)` after
   tampering; opt-in Task Scheduler watchdog that reruns the normal
   `--monitor` entry point every minute; `EXTENSION_SILENT` episodes when a
   STRICT website rule is armed while a supported browser runs unpaired.
   Findings surface on the dashboard, in `--security`, and in the `security`
   section of `--status-json`. Everything is visible and audited; the layer
   only observes and reports — enforcement decisions stay in
   `core/enforcement` (see `docs/PHASE7.md` for the threat-model table and
   the honest limits).

## 7. Privacy

Local-only by design. Stored: rule targets (exe names/domains you entered),
aggregated seconds per day, session start/end, enforcement states, settings.
Never stored/transmitted: full URLs, page contents, keystrokes, file contents.
Extension sends domain-only. Nothing leaves `127.0.0.1`.
