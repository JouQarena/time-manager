# Enforcement state machine

One machine instance per rule per local day. Pure logic
(`app/core/enforcement/state_machine.py`) — no processes, no network, no DB.
The monitoring loop feeds it events; it emits *decisions* the adapters execute.

## States

| State | Meaning |
|---|---|
| `NORMAL` | Within limits, no warning fired yet |
| `WARNING` | At least one warning threshold crossed, limit not yet reached |
| `LIMIT_REACHED` | Transient: limit just hit, action not yet chosen (single tick) |
| `WAITING_FOR_SESSION_END` | Limit hit, `WAIT_FOR_SESSION_END`, game session still active — do NOT kill |
| `ENFORCED` | Terminal for the day: blocked/closed/warned per action |
| `DISABLED` | Rule disabled or outside schedule (tracking paused) |

## Diagram

```
        ┌──────────┐  warn threshold   ┌──────────┐  limit hit   ┌───────────────┐
        │  NORMAL  │ ─────────────────▶│ WARNING  │ ───────────▶│ LIMIT_REACHED │
        └──────────┘                   └──────────┘             └───────────────┘
              │                           │  ▲                        │
              │ limit hit (no warn cfg)   │  │ refill/new day         │ decide(action, in_session)
              │                           │  │                        ▼
              │                           │  │            ┌────────────────────────┐
              │                           └──┘            │ action dispatch:       │
              │                                           │ WARN_ONLY → ENFORCED   │
              ▼                                           │ BLOCK/CLOSE, no game   │
        ┌───────────────┐                                 │   → ENFORCED           │
        │ LIMIT_REACHED │                                 │ WAIT_FOR_SESSION_END   │
        └───────────────┘                                 │   + in_session → WAIT… │
                                                          └────────────────────────┘
                                                                     │
                                        ┌────────────────────────────┼───────────────────┐
                                        ▼                            ▼                   ▼
                                 ┌────────────┐            ┌───────────────────┐  ┌────────────┐
                                 │  ENFORCED  │◀──────────│ WAITING_FOR_      │  │  ENFORCED  │
                                 │ (warn_only)│ session    │ SESSION_END       │  │(block/close│
                                 └────────────┘ ended      └───────────────────┘  │ immediately│
                                                        ▲    │ ▲                  └────────────┘
                                                        │    │ │ detector unknown:
                                                        │    │ │ stay (fail-safe),
                                                    new │    │ │ re-poll
                                                    day │    │ │                 any state ──disable──▶ DISABLED
                                                  reset │    │ │                 DISABLED ──enable──▶ NORMAL
                                                        │    ▼ │                 any state ──new day──▶ NORMAL
                                                        │  ENFORCED
                                                        │  (post-game block)
                                                        ▼
```

## Events (inputs per tick)

```python
TickInput(
    rule,                    # limits, action, mode, warnings, schedule
    today_used_seconds,      # from daily_usage (+ open session heartbeat)
    session_used_seconds,    # current continuous session
    target_active_now,       # foreground / tab-active right now
    in_game_session,         # None=unknown/not-a-game, True/False
    detector_confident,      # False → fail-safe: never kill
    now_local,               # datetime for schedule + reset checks
    stored_state,            # persisted EnforcementState row
    warned_thresholds,       # already-fired warning thresholds
)
```

## Decisions (outputs)

```python
Decision(
    new_state,               # state to persist
    warnings_to_fire,        # e.g. [300, 60] newly crossed thresholds
    action_to_execute,       # None | CLOSE_APP | BLOCK_WEBSITE | PREVENT_LAUNCH | NOTIFY_ONLY
    reason,                  # DAILY_LIMIT_REACHED | SESSION_LIMIT_REACHED | SCHEDULE_BLOCKED | ...
    user_message,            # ready-to-display notification text
)
```

Key rules:
- Warnings fire **once per threshold per day** (persisted in `warned_thresholds`).
- `LIMIT_REACHED` never persists across ticks — the dispatcher resolves it
  immediately to `WAITING_FOR_SESSION_END` or `ENFORCED`.
- Games with unknown detector state stay in `WAITING_FOR_SESSION_END`
  indefinitely rather than killing a possible live match. Since Phase 6 that
  "unknown" is produced deliberately by `DetectorHost` for every plugin failure
  mode (crash, hang, quarantine, bad return value) — see `docs/PHASE6.md`.
- `STRICT` does not change the state graph — it changes what `ENFORCED` means
  to the executor (relaunch-guard window, no "pause monitoring" without
  confirmation, startup repair).
- Midnight (local day change) resets every machine to `NORMAL`.
