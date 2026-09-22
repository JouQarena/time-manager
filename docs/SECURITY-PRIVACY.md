# Security & Privacy

Everything Time Manager knows stays on your machine. This document is the
single place that says exactly what the app stores, what it sends (nothing),
where its security boundaries are, and how to verify all of it yourself.

Status: **v1.0.0**. Companion reading: `docs/LIMITATIONS.md` (honest
trade-offs) and `docs/PHASE7.md` (anti-bypass threat model).

---

## 1. Privacy: what is stored, and where

| Data | Stored in | Contents |
| --- | --- | --- |
| Rules | `%APPDATA%\TimeManager\timemanager.db` → `rules` | the names/exes/domains **you typed** |
| Usage | `usage_sessions`, `daily_usage` | seconds per rule per local day; session start/end |
| Enforcement history | `enforcement_log` | what the agent closed/blocked/declined and why |
| Security audit | `agent_audit` | starts/stops, tamper findings, watchdog changes |
| Clock events | `clock_log` | sleep/suspend and clock-skew notes |
| Settings | `config.json` + `settings` table | your preferences |
| Pairing token | `agent_token` | 32 random bytes hex; local IPC authentication only |
| Usage floor | `timemanager.floor.json` (next to the DB) | per-day high-water marks for STRICT rules |
| Backups | `%APPDATA%\TimeManager\backups\` | DB copies, pruned to the last 7 |
| Agent log (packaged builds) | `%APPDATA%\TimeManager\logs\agent.log` | 3 × 1 MB rotating diagnostics |

**Never stored, anywhere:** full URLs, page contents, browsing history,
keystrokes, screenshots, window contents, file contents, personal data of any
kind. Website tracking sees **domains only** (`youtube.com`), never paths,
queries or titles (app window titles are read for nothing by default; debug
title logging does not exist — the design never needed it).

## 2. Privacy: what leaves the machine

**Nothing.**

- No cloud, no accounts, no telemetry, no crash reporting, no update phone-
  home. The core agent makes **zero network connections**; the only network-
  adjacent code is a *server* listening on `127.0.0.1` for your own browsers.
- The browser extension connects **only** to `ws://127.0.0.1:<port>` with your
  token and makes no other request of any kind.
- You can verify all of this rather than trust it:
  - the source is the spec — grep for `urlopen|socket|requests|http` in
    `app/`: the only outbound path is the optional Riot Live Client API probe
    (`https://127.0.0.1:2999`), also loopback, also read-only;
  - firewall the agent completely and everything keeps working (offline-first
    is a design requirement, not a slogan);
  - the extension's requested permissions are minimal (`tabs`, `storage`,
    `alarms`) — inspect `browser-extension/manifest.json`;
  - Wireshark/netstat will show the agent holding a loopback listener and
    nothing else.

**Deleting your data:** quit the agent and delete `%APPDATA%\TimeManager`
(that is every table above, the token, the floor, backups and logs).
Uninstalling the app deliberately preserves that folder — documented, not
hidden.

## 3. Security boundaries

| Boundary | Enforcement |
| --- | --- |
| Extension → agent | WebSocket on `127.0.0.1` only; first message must authenticate with the per-machine token (constant-time compare); strict per-field schema validation; exactly five inbound message types; per-connection rate limiting; agent-side timestamps (browser timestamps are never trusted for accounting); a malformed or hostile message can fail the connection, never the agent. No inbound message can execute anything — the protocol has no such verb. |
| Other local processes | The token lives in the user profile (`agent_token`, 0600-equivalent); anything running *as you* could read it — that is the documented ceiling of user-level software (see §4). Rotate it from Settings if a local tool ever read it. |
| Agent → OS | Process control is constrained twice: `KillPolicy` refuses protected system processes, the agent's own PID/image, and PIDs ≤ 4; the OS-facing `ProcessController` independently re-refuses the same. Closing is graceful-first (WM_CLOSE), forced only after a timeout, only for a rule's listed exes. |
| Detector plugins | Third-party detector code runs inside a supervised host: 300 ms probe budget, failures counted, quarantine after repeated failures — a crashing, hanging or lying plugin degrades to "unknown → wait", it can never cause a kill or crash the agent. |
| Self | The agent is transparent by design: everything it does lands in `enforcement_log`/`agent_audit` and is visible in the dashboard, `--status` and `--security`. There is no hidden persistence, no self-defense against Task Manager, no driver, no service. |

## 4. Anti-bypass: what is protected, and the honest ceiling

Phase 7 (`docs/PHASE7.md` has the full threat-model table) closes the basic
vectors: a killed agent is reported at the next start (`UNEXPECTED_STOP`),
STRICT usage survives database tampering (floors live outside the DB), STRICT
rules re-arm their autostart entry, the extension going silent while a STRICT
website rule is armed raises a visible alert, clock rollbacks at startup are
detected against the last-seen time, and an opt-in watchdog task revives a
killed agent within about a minute.

**The ceiling, stated plainly:** this is a self-control tool, not parental-
control malware. An administrator can always win eventually — kill the agent,
delete the watchdog task, delete the profile folder, and never start it again.
Every one of those steps is *visible* (audit trail, Task Scheduler, the
folder), and the design goal is that bypassing costs more friction than it
saves, not that it is impossible. What the project will never do: kernel
drivers, rootkit/stealth techniques, credential theft, AV interference,
hidden remote control.

## 5. Data flows (the complete list)

1. **App monitoring:** psutil enumerates process names; the foreground exe is
   read via Win32. Matched against your rules; counted in seconds.
2. **Website monitoring:** the extension reports the *active tab's* domain +
   focus state, only while you enable it, only to your local agent.
3. **Game detection (League/VALORANT/R.E.P.O./TFT):** process table; optional
   local evidence — Riot's Live Client API on `127.0.0.1:2999` (loopback,
   read-only) and log-file freshness under your own user profile.
4. **Enforcement:** close/block/wait decisions → local actions + audit rows.

No step in any flow touches a network beyond loopback.

## 6. Reporting problems

This is a personal open project: file an issue with the version
(`TimeManager.exe --version`) and, for enforcement bugs, the relevant rows
from `--security` and the dashboard's *Recent enforcement* panel — they are
designed to be complete enough to explain any action after the fact. Please
do not paste your `agent_token` anywhere; rotate it instead.
