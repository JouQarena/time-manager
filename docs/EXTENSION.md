# The browser extension — how it works

Companion to **[INSTALL-EXTENSION.md](INSTALL-EXTENSION.md)** (the install
walkthrough). This one explains what the extension actually does — the parts
worth knowing when something behaves oddly, or when you are changing the code.
It follows `browser-extension/background/service-worker.js` line for line.

```
┌─ Chrome / Edge ─────────────────────┐        ┌─ desktop agent (Python) ────────┐
│ service worker                      │        │  WS server 127.0.0.1:17846      │
│   HELLO ────────────────────────────┼───────►│  token check → WELCOME          │
│   TAB_ACTIVITY (domain, focus) ─────┼───────►│  tracker → daily_usage          │
│   HEARTBEAT every 5 s ──────────────┼───────►│  keep-alive + staleness clock   │
│   ◄──── RULE_UPDATE / BLOCK_DECISION│        │  rules engine + enforcement     │
│ content script ◄── DECISION         │        │                                 │
└─────────────────────────────────────┘        └─────────────────────────────────┘
```

## Four jobs, in the order the worker does them

1. **Keep one authenticated WebSocket alive** to `ws://127.0.0.1:<port>/`.
2. **Report the active tab's domain + focus state** on every change.
3. **Enforce the agent's `BLOCK_DECISION` answers** inside pages.
4. **Never lie**: if the agent is unreachable, nothing is counted and the badge
   says so.

## Connection & handshake

| Step | Extension | Agent |
| --- | --- | --- |
| 1 | reads token/port from `chrome.storage.local`, opens the socket | accepts |
| 2 | sends `HELLO{browser, browser_id, token}` | verifies the token |
| 3 | starts the 5 s heartbeat | answers `WELCOME{agent_version, website_rules}` |
| 4 | badge clears, rules cached, current tab announced | starts counting website time for that client |

Failure modes are explicit, never silent:

- **Wrong token** → `ERROR{code: AUTH_FAILED}` and the worker *closes the
  socket without retrying* (a bad token must not hammer the agent). Options page
  shows `AUTH_FAILED: …`; fix it by re-pasting the token.
- **Right port, wrong service** → nothing answers `HELLO` within 5 s, so the
  worker closes and retries: "Agent did not reply to HELLO (wrong token or wrong
  port?)". Usually a port clash.
- **Agent not running** → the socket never opens; reconnect backs off
  **1 s → 30 s** (`RECONNECT_MIN_MS`/`RECONNECT_MAX_MS`), plus a 1-minute alarm
  as a safety net that survives worker eviction.

## MV3 lifetime: why the heartbeat is also the keep-alive

A Manifest V3 service worker is normally killed after ~30 s idle. Since Chrome
116 (the manifest's `minimum_chrome_version`) *activity on a WebSocket resets
that timer* — so the 5 s `HEARTBEAT` doubles as the worker's keep-alive. There
is no persistent background page and nothing to "run in the background" in the
old sense.

That has one visible consequence, and the design leans into it rather than
papering over it: if the worker is evicted anyway (laptop sleep, extension
reload, browser update), the agent stops counting website time after **15 s**
(`website_stale_seconds`) — *failing closed, never inventing usage* — and
counting resumes when the worker reconnects on the next tab event, alarm, or
browser restart.

## What gets reported (and what never does)

`reportTabState()` runs on tab activation, tab close, tab URL/status change,
window focus change, and once at `WELCOME`. Each report is
`TAB_ACTIVITY{browser, browser_id, tab_id, domain, active, window_focused,
audible}`.

- The **domain only** — derived by `shared/normalize.js`, which is parity-tested
  against the Python `normalize_domain` on a shared fixture list. No paths, no
  query strings, no titles, no page content, ever.
- **Untrackable targets return an empty domain**: `chrome://`, `about:`,
  `file:`, `localhost`, raw IPs. Those are never counted.
- Time accrues **only while the tab is active and its window is focused**.
  Background tabs and minimised windows contribute nothing — this is enforced
  on the agent side too, so a misbehaving extension cannot inflate usage.
- `audible` is reported so the agent can see background media; it does not
  create usage on its own.

## Blocking flow

The agent decides; the extension paints. There are three message paths:

| Message | When | Effect |
| --- | --- | --- |
| `RULE_UPDATE` | rules or limits changed | replaces the cached rules; re-pushes decisions to every open tab |
| `BLOCK_DECISION` | account state changed for one domain | caches it and messages the matching tabs |
| `PING` | the agent checking liveness | replies `PONG` |

`content/block-overlay.js` renders the block page **locally** in a shadow DOM
(so site CSS cannot restyle or hide it), freezes background media, and shows the
reason plus a live countdown to the reset. Unblocking is symmetric: a fresh
decision with `blocked: false` removes the overlay from any open tab.

The extension can only *render* a block — it never proxies traffic, never uses
`declarativeNetRequest`, and never blocks anything on its own authority. Its
`permissions` are `["tabs", "storage", "alarms"]` with `host_permissions: []`.

## The badge, and telling the truth

`broadcastBadge()` is the extension's honesty mechanism:

| Badge | State |
| --- | --- |
| `!` | not connected (disconnected / connecting / error / disabled) |
| *(blank)* | connected, current tab not blocked |
| `⛔` | connected, current tab blocked |

The hover title carries `connected to the agent (v… )` or the failure reason.
`!` matters: a user who *thinks* limits are running while the agent is down is
the real failure case, so the extension makes that state loud.

## Where its state lives

| Key (`chrome.storage.local`) | Meaning |
| --- | --- |
| `agentToken` | the pairing token — this profile only, never synced |
| `agentPort` / `agentHost` | where the agent listens (default `17846` / `127.0.0.1`) |
| `enabled` | the "Report browsing activity" switch |
| `browserId` | a random UUID, one per browser profile — the agent's client identity |
| `lastConnectedAt` | last successful `WELCOME` |

Changing token/port/host/enabled in Options clears cached decisions and forces
a reconnect immediately. Everything in RAM (socket, rules, decisions, counters)
is rebuilt on each worker start — that is why `GET_STATUS` may briefly show
zeros right after a sleep.

## Debugging it

1. **Popup** — status, agent version, this tab's domain, per-rule usage.
2. **Options ▸ Status** — connection state, agent version, browser id, message
   counters. Also its own page: **Settings ▸ Guide**, opened automatically on a
   fresh install.
3. **Service worker console** — `chrome://extensions` → *Time Manager
   Companion* → **service worker**. The worker logs under `[time-manager]`
   (`socket open; sending HELLO`, reconnect attempts). This is where an
   eviction/reconnect loop becomes visible.
4. **Agent side** — `TimeManager.exe --status` (`N browser(s) connected`) and
   the dashboard panels *Browser extension* / *Agent link*.
5. **End to end without a browser** — `python -m app.main --ipc-selftest`
   drives the real WebSocket protocol and asserts the replies.

## Failure-mode cheat sheet

| What you see | Cause | Fix |
| --- | --- | --- |
| `!` badge, *not reachable* | agent not running / sleeping | start `TimeManagerTray.exe` |
| `!` badge, *AUTH_FAILED* | token wrong or regenerated | re-copy and re-paste; re-pair every browser |
| *did not reply to HELLO* | something else on that port | change the port in the app, mirror it in Options |
| connected, but dashboard says *no browser connected* | that profile was never paired | pair each browser/profile separately |
| counting stops while the laptop was asleep | worker evicted, agent fails closed after 15 s | nothing to do — it reconnects; use the watchdog in STRICT setups |
| site stays blocked after pausing/editing the rule | open tab has not received the new decision yet | reload the tab |
| unpacked extension disabled itself after a Chrome update | Chrome flags stale unpacked extensions | `chrome://extensions` → **Reload** |

## Related documents

- **[INSTALL-EXTENSION.md](INSTALL-EXTENSION.md)** — user-facing install + pairing.
- **[PROTOCOL.md](PROTOCOL.md)** — the message schema in full.
- **[SECURITY-PRIVACY.md](SECURITY-PRIVACY.md)** — what is stored and what never leaves.
- **[LIMITATIONS.md](LIMITATIONS.md)** — the honest list, including what a
  browser extension fundamentally cannot see.
