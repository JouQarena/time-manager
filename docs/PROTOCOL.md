# Browser ↔ Desktop IPC protocol (v1)

**Status: implemented (Phase 4).** Agent side: `app/ipc/server.py` +
`app/ipc/bridge.py`; extension side: `browser-extension/`. Verified end to end
by `python -m app.main --ipc-selftest` and `tests/test_ipc_server.py`.

Transport: **WebSocket server on the agent**, bound to `127.0.0.1` only
(default port `17846`, configurable). No TLS (loopback); authentication via a
per-machine secret token (`python -m app.main --show-token`, stored in
`%APPDATA%\TimeManager\agent_token`) pasted into the extension's Options page.
`--regen-token` invalidates the old token. (Future: optional `wss://` with a
local CA — not needed for v1 on loopback.)

## Connection lifecycle

1. Extension opens `ws://127.0.0.1:17846/`.
2. Extension MUST send `HELLO` within 5 s or the server closes the socket.
3. Server validates token (constant-time compare). Invalid → `ERROR` + close.
4. Valid → server replies `WELCOME` (includes agent version, active website
   rules as **domain + remaining seconds only**).
5. Steady state: extension → `TAB_ACTIVITY` on every change + `HEARTBEAT`
   every 5 s; server → `BLOCK_DECISION` / `RULE_UPDATE` / `PING`.
6. Extension answers `PING` with `PONG`. Three missed heartbeats (15 s) → agent
   treats that browser source as stale and pauses website timers for it.

Implementation notes (behaviour the extension can rely on):

* **Liveness comes from heartbeats, not tab reports.** A user can watch one tab
  for an hour without a second `TAB_ACTIVITY`; the 5 s `HEARTBEAT` is what keeps
  that tab "live". The agent drops a browser's state 15 s after the last sign of
  life (heartbeat *or* tab report).
* The agent pushes `PING` every 5 s and disconnects a client that has been
  silent for 15 s.
* `RULE_UPDATE` is sent at most once every 5 s, and only when the payload
  actually changed (so the popup's numbers stay fresh while a site is in use,
  without chatty traffic when nothing is happening).
* `BLOCK_DECISION` is pushed on *transitions* (unblocked → blocked, and back at
  local midnight), never once per tick. Idempotent on the agent side: a rule
  that stays exceeded does not re-push.
* Messages may carry extra keys (`used_seconds`, `limit_seconds`, `schedule`,
  `reason`); clients must ignore what they do not understand, and
  `domain`/`blocked`/`remaining_seconds` are the compatibility contract.

Multiple simultaneous clients are allowed (Chrome + Edge, several windows);
each connection is keyed by `(browser, browser_id)` where `browser_id` is a
random UUID the extension generates once and persists in `chrome.storage`.

## Extension → Agent messages

### `HELLO`
```json
{"type":"HELLO","browser":"chrome","browser_id":"uuid4","version":"1.0.0","token":"<64-hex>","timestamp":1726900000000}
```
- `browser`: `chrome|edge|firefox`. `timestamp`: ms epoch, informational only.

### `TAB_ACTIVITY` (send on: tab switch, URL change, window focus change)
```json
{"type":"TAB_ACTIVITY","browser":"chrome","browser_id":"uuid4","tab_id":123,
 "domain":"youtube.com","active":true,"window_focused":true,
 "audible":false,"timestamp":1726900000000}
```
- `domain`: extension-normalized (lowercase, strip port, punycode-safe; see below).
- `active`: tab is the selected tab in its window.
- `window_focused`: `chrome.windows.getLastFocused(focused==true)` for that window.
- Timer counts **only** when `active && window_focused` AND the agent still
  considers the heartbeat fresh AND the browser process is the foreground app
  (defense-in-depth; the agent cross-checks `chrome.exe` foreground state).

### `HEARTBEAT`
```json
{"type":"HEARTBEAT","browser":"chrome","browser_id":"uuid4","timestamp":1726900000000}
```

### `BLOCK_QUERY` (extension asks "is this domain blocked right now?")
```json
{"type":"BLOCK_QUERY","browser":"chrome","browser_id":"uuid4","domain":"youtube.com","timestamp":1726900000000}
```

### `PONG`
```json
{"type":"PONG","browser_id":"uuid4","timestamp":1726900000000}
```

## Agent → Extension messages

### `WELCOME`
```json
{"type":"WELCOME","agent_version":"1.0.0","server_time":1726900000000,
 "website_rules":[{"domain":"youtube.com","remaining_seconds":840,"blocked":false}]}
```

### `BLOCK_DECISION` (pushed on rule change / limit hit)
```json
{"type":"BLOCK_DECISION","domain":"youtube.com","blocked":true,
 "reason":"DAILY_LIMIT_REACHED","reset_at":"2026-09-22T00:00:00+03:00",
 "message":"Daily limit reached. You have used your allowed time for YouTube today."}
```
- `reason`: `DAILY_LIMIT_REACHED|SESSION_LIMIT_REACHED|SCHEDULE_BLOCKED|MANUAL|UNBLOCKED`.

### `RULE_UPDATE` (full small snapshot when rules change)
```json
{"type":"RULE_UPDATE","website_rules":[{"domain":"youtube.com","remaining_seconds":0,"blocked":true}]}
```

### `PING` / `ERROR`
```json
{"type":"PING","server_time":1726900000000}
{"type":"ERROR","code":"AUTH_FAILED","message":"Invalid token."}
```
Error codes: `AUTH_FAILED|BAD_MESSAGE|RATE_LIMITED|SERVER_ERROR`.

## Validation rules (agent side, `app/ipc/protocol.py`)

- JSON object ≤ 8 KiB; unknown `type` → `ERROR{BAD_MESSAGE}`, no crash.
- Every field type/range checked (`domain` ≤ 253 chars, valid labels;
  `tab_id` int ≥ 0; booleans real booleans).
- Rate limit: ≤ 30 msgs/10 s per connection; excess → `ERROR{RATE_LIMITED}`.
- Extension `timestamp` is **never** used for accounting — the agent stamps
  `received_at` (wall) + `monotonic_at` on receipt.
- The extension can only query/report; there is deliberately **no**
  `EXECUTE_*` / `CLOSE_APP` / `SET_RULE` message type.

## Domain normalization (shared rule, implemented on both sides)

1. Lowercase; strip leading/trailing dots and whitespace; strip port.
2. IDNA-encode (`münchen.de` → `xn--mnchen-3ya.de`) for comparison.
3. Matching: rule `youtube.com` matches `youtube.com`, `www.youtube.com`,
   `m.youtube.com`, `music.youtube.com` — i.e. exact or `*.youtube.com` —
   but NOT `fakeyoutube.com` nor `youtube.com.evil.com`. A rule for an
   explicit subdomain (`music.youtube.com`) matches only it and below it.
4. IP literals and `chrome://`, `edge://`, `about:` URLs never match website
   rules and are never sent (extension sends `domain:""` + `active:false`
   for those, or simply omits the event).

## Why WebSocket (not nativeMessaging / polling)

- Extensions can open loopback WebSockets with just the `tabs` permission —
  nobys, no registry keys, works unpacked AND from the Web Store.
- Push semantics: the agent can push `BLOCK_DECISION` instantly instead of
  the extension polling.
- `nativeMessaging` would need registry-installed host manifests per browser —
  heavier install story, kept as a possible Phase 7 hardening addition.
