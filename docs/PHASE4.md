# Phase 4 — Browser Extension Link (COMPLETE, live-browser pass outstanding)

## Goal
Close the loop on website rules: an authenticated loopback WebSocket inside the
agent, plus a real MV3 extension that reports the active tab, counts only when
you are actually looking at a page, and blocks sites locally when a limit is
reached.

## What was built

| Area | File | Notes |
|---|---|---|
| Server | `app/ipc/server.py` | websockets 13 & 17 compatible (`asyncio.server` or legacy `serve`), loopback bind, HELLO timeout, token auth, rate limit, PING/PONG watchdog, thread-safe `broadcast()` |
| Browser state | `app/ipc/browser_state.py` | per-browser current tab + heartbeat freshness; `None` vs `{}` distinction |
| Agent bridge | `app/ipc/bridge.py` | rule payloads, `block_decision`, transition pushes, `web_state` for the monitor |
| Monitor wiring | `app/core/monitoring/monitor.py` | `set_web_state_provider()` + `set_tick_hook()` |
| Foreground cross-check | `app/core/monitoring/activity.py` | extension events only count while a browser owns the foreground window |
| CLI | `app/main.py` | `--show-token`, `--regen-token`, `--ipc-selftest`, `--monitor` now serves the link |
| Extension | `browser-extension/**` | MV3 service worker, content-script block page, popup, options, generated icons |
| Tests | `tests/test_ipc_{browser_state,bridge,server,selftest}.py`, `tests/test_extension_assets.py` | 65 new tests (real sockets + JS↔Python parity), plus 3 added to the Phase 3 activity suite |

## Security posture (unchanged promises)

* **Loopback only.** `127.0.0.1`, never `0.0.0.0`; no TLS needed because nothing
  leaves the machine.
* **Token auth.** 64-hex token from `%APPDATA%\TimeManager\agent_token`
  (`--show-token` to read it), constant-time compare, `ERROR{AUTH_FAILED}` +
  close on mismatch. `--regen-token` invalidates the old one.
* **Tiny inbound vocabulary.** `HELLO`, `TAB_ACTIVITY`, `HEARTBEAT`,
  `BLOCK_QUERY`, `PONG`. There is deliberately no remote-execute/close/rule-edit
  message, so a compromised page cannot drive the agent. Junk messages produce
  `ERROR{BAD_MESSAGE}` and are dropped — never a crash.
* **8 KiB cap** per message, **30 msgs / 10 s** per connection (configurable via
  `ipc_rate_limit_count` / `ipc_rate_limit_window`), HELLO required within 5 s.
* **Extension permissions stay minimal:** `tabs`, `storage`, `alarms`, no
  `host_permissions`, no remote code, no network calls except the loopback
  socket (asserted by `tests/test_extension_assets.py`).
* **The extension is untrusted input.** Timestamps are ignored for accounting,
  domains are re-validated with the agent's own normalizer, and a browser that
  stops heartbeating stops being counted.

## Behaviour decisions

**What counts as website time.** `tab.active && tab.window_focused` in the
extension **and** a browser is the OS foreground app (`chrome.exe`, `msedge.exe`,
`firefox.exe`, `brave.exe`, …) **and** the extension connection is live. Missing
signals never count. Three independent gates, each covered by tests.

**Liveness = heartbeats, not tab reports.** Someone watching one YouTube tab for
an hour sends a single `TAB_ACTIVITY`; the 5 s `HEARTBEAT` is what keeps the tab
"live". Miss three beats (15 s) and the agent stops counting that browser —
failing closed, never inventing usage. `web_state()` returns `None` (no
extension at all) vs `{}` (connected, nothing web in focus) so the UI/logs can
tell those apart.

**One truth for blocks.** `BLOCK_QUERY` replies and `WELCOME`/`RULE_UPDATE`
payloads are both derived from `AgentBridge.rule_status()`, which folds in the
tracker total, the engine's `ENFORCED` verdict and the schedule — so the
pre-emptive check and the pushed decision cannot disagree. Blocks are pushed
once per transition (not per tick) and un-blocks are pushed at local midnight.

**DEFERRED vs EXECUTED.** With a browser connected, a website limit lands in
`enforcement_log` as `EXECUTED` ("rule pushed to browser"). With nobody
connected it stays `DEFERRED` — the desktop still records that the limit was
reached, and the extension picks the block up on its next `BLOCK_QUERY`.

**The block page** is rendered by a content script at `document_start` in a
closed Shadow DOM (site CSS cannot restyle or remove it), calls `window.stop()`
and pauses media, shows the reason, the domain and a live countdown to midnight,
and re-checks on `popstate`/`hashchange` for SPAs.

## Verification

Automated here (Linux, Python 3.13, websockets 17.1, Node 20):
- `python -m pytest -q` → **249 passed** (Phases 1–3: 181, Phase 4: 68).
- `python -m app.main --ipc-selftest` → 13/13 steps over a real socket:
  wrong token rejected; `HELLO`→`WELCOME`; tab report counted; limit pushes
  `BLOCK_DECISION`; `BLOCK_QUERY` answered; unrelated domain free; PING/PONG;
  rate limiter rejects a flood; disconnect stops counting.
- `python -m app.main --simulate` → the full Phase 3 story plus
  `BLOCK_WEBSITE:EXECUTED` and "1 push(es)" to the simulated extension.
- `python -m compileall -q app` → clean; every extension `.js` passes
  `node --check`; `normalize.js` and `domainMatches` agree with Python on a
  shared fixture list (including IDNA, IP literals, lookalike domains).

### Manual pass — Chrome/Edge (required)

1. `python -m app.main --init-db`, add a website rule with a *small* limit
   (e.g. `youtube.com`, 2 minutes, BLOCK) — `--add-sample` gives you a 45-minute
   YouTube rule to start from.
2. `python -m app.main --show-token` → copy the token.
3. Chrome/Edge → `chrome://extensions` → enable **Developer mode** →
   **Load unpacked** → pick `browser-extension/`.
4. Open the extension's **Options**, paste the token, press **Save & test**.
   The badge should switch from “!” to blank and show “Connected to agent v0.4.0”.
5. Run `python -m app.main --monitor`. Checks:
   - [ ] Badge is blank; popup lists the rule with remaining time; “This tab”
         reads *Counting towards your limit* while YouTube is focused.
   - [ ] Alt-tab to another app → popup shows the agent connection but the
         tab stops counting (foreground gate). Verify with `--status` before/after.
   - [ ] Reach the limit → the page is replaced by the block screen with a
         countdown; `enforcement_log` shows `BLOCK_WEBSITE / EXECUTED`.
   - [ ] In a second window, open the same site: it blocks immediately
         (cached decision + `BLOCK_QUERY` on load).
   - [ ] Stop the agent (`Ctrl-C`): the popup/options show “Agent not reachable”,
         pages go back to normal, nothing is counted. Restart it: reconnect
         happens within a few seconds without touching the browser.
   - [ ] Paste a wrong token in Options: you get an explicit auth error and the
         badge shows “!” (no reconnect storm).
   - [ ] Suspend the machine for a minute: after resume, usage does not jump
         (heartbeats stopped while asleep) and the agent logs the gap.
   - [ ] Check the service worker console for errors after 30+ minutes
         (MV3 eviction): the badge should recover to connected on the next tab
         switch, and the agent should show no ghost counting.

Known limitations (also in `docs/LIMITATIONS.md`): Chrome may suspend the MV3
worker while idle — counting pauses (never over-counts) until it returns; the
extension cannot see other Chrome profiles' windows; blocking happens after the
page starts loading (a few hundred ms), it is not a network-level filter.

## Phase 5 preview (IPC hardening + status surface)
`--ipc-status` JSON for tooling, message-size/rate telemetry in the UI, a
"connected browsers" panel, `regenerate token` from the GUI, optional
`nativeMessaging` host as a second transport, and a loopback-only connection
audit log.
