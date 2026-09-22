"""Explicit enforcement state machine (pure logic, no I/O).

See docs/STATE_MACHINE.md. The monitor loop calls `tick()` once per rule per
interval with a `TickInput`; the machine returns a `Decision` describing the
new state, newly-fired warnings, and exactly one action for the adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.rules.models import Rule
from app.core.timeutils import format_duration
from app.core.types import Action, LimitReason, RuleState


@dataclass(frozen=True)
class TickInput:
    rule: Rule
    today_used_seconds: int  # incl. open-session heartbeat
    session_used_seconds: int  # current continuous session
    target_active_now: bool
    in_game_session: bool | None  # None = n/a or unknown
    detector_confident: bool = True
    now_local: datetime | None = None
    stored_state: RuleState = RuleState.NORMAL
    warned_thresholds: frozenset[int] = frozenset()
    stored_day: str | None = None  # local YYYY-MM-DD the stored state belongs to
    today_day: str | None = None  # current local YYYY-MM-DD


@dataclass(frozen=True)
class Decision:
    new_state: RuleState
    warnings_to_fire: tuple[int, ...] = ()
    action_to_execute: str | None = None  # CLOSE_APP|BLOCK_WEBSITE|PREVENT_LAUNCH|NOTIFY_ONLY
    reason: LimitReason = LimitReason.NONE
    user_message: str = ""
    remaining_today: int | None = None
    remaining_session: int | None = None


def _remaining(limit: int | None, used: int) -> int | None:
    return None if limit is None else max(0, limit - used)


def tick(inp: TickInput) -> Decision:
    rule = inp.rule
    now = inp.now_local or datetime.now().astimezone()

    # --- Day rollover / disabled / schedule ---------------------------------
    if inp.stored_day is not None and inp.today_day is not None and inp.stored_day != inp.today_day:
        stored_state: RuleState = RuleState.NORMAL
        warned: frozenset[int] = frozenset()
    else:
        stored_state = inp.stored_state
        warned = inp.warned_thresholds

    if not rule.enabled:
        return Decision(new_state=RuleState.DISABLED, reason=LimitReason.NONE)
    if rule.schedule is not None and not rule.schedule.is_active(now):
        # Outside schedule: enforced as blocked (tracking paused by caller).
        if stored_state == RuleState.ENFORCED:
            return Decision(new_state=RuleState.ENFORCED, reason=LimitReason.SCHEDULE_BLOCKED)
        return Decision(
            new_state=RuleState.DISABLED,
            reason=LimitReason.SCHEDULE_BLOCKED,
            user_message=f"{rule.name} is outside its allowed schedule.",
        )

    remaining_today = _remaining(rule.daily_limit_seconds, inp.today_used_seconds)
    remaining_session = _remaining(rule.session_limit_seconds, inp.session_used_seconds)

    # --- Limit reached? ------------------------------------------------------
    reason = LimitReason.NONE
    if rule.daily_limit_seconds is not None and inp.today_used_seconds >= rule.daily_limit_seconds:
        reason = LimitReason.DAILY_LIMIT_REACHED
    elif rule.session_limit_seconds is not None and inp.session_used_seconds >= rule.session_limit_seconds:
        reason = LimitReason.SESSION_LIMIT_REACHED

    if reason != LimitReason.NONE:
        return _resolve_limit(rule, stored_state, reason, remaining_today, remaining_session, inp)

    # --- Warnings (daily and session horizons, fire once per threshold) ------
    fire: list[int] = []
    for horizon in (remaining_today, remaining_session):
        if horizon is None:
            continue
        for w in rule.warning_seconds:
            if horizon <= w and w not in warned and w not in fire:
                fire.append(w)
    fire.sort(reverse=True)
    if fire:
        w_txt = ", ".join(format_duration(w) for w in fire)
        return Decision(
            new_state=RuleState.WARNING,
            warnings_to_fire=tuple(fire),
            reason=LimitReason.NONE,
            user_message=f"{rule.name}: {w_txt} remaining.",
            remaining_today=remaining_today,
            remaining_session=remaining_session,
        )

    # --- Stable states --------------------------------------------------------
    # Reaching here means the rule is enabled and inside its schedule (checked
    # above), so a stale DISABLED (left from an earlier out-of-schedule tick)
    # must clear back to NORMAL/WARNING instead of sticking forever.
    if stored_state in (RuleState.WARNING, RuleState.NORMAL, RuleState.DISABLED):
        # Stay WARNING once warned (until reset) so the UI can show it.
        state = RuleState.WARNING if warned else RuleState.NORMAL
        return Decision(
            new_state=state, reason=LimitReason.NONE,
            remaining_today=remaining_today, remaining_session=remaining_session,
        )
    if stored_state == RuleState.WAITING_FOR_SESSION_END:
        # Limit was hit earlier but usage now reads below limit (e.g. clock
        # wobble): re-resolve rather than silently clearing.
        return _resolve_limit(
            rule, stored_state, LimitReason.DAILY_LIMIT_REACHED,
            remaining_today, remaining_session, inp,
        )
    return Decision(
        new_state=stored_state, reason=LimitReason.NONE,
        remaining_today=remaining_today, remaining_session=remaining_session,
    )


def _resolve_limit(
    rule: Rule,
    stored_state: RuleState,
    reason: LimitReason,
    remaining_today: int | None,
    remaining_session: int | None,
    inp: TickInput,
) -> Decision:
    base = dict(remaining_today=remaining_today, remaining_session=remaining_session, reason=reason)
    limit_word = "Daily" if reason == LimitReason.DAILY_LIMIT_REACHED else "Session"

    if rule.action == Action.WARN_ONLY:
        return Decision(
            new_state=RuleState.ENFORCED, action_to_execute="NOTIFY_ONLY",
            user_message=f"{rule.name}: {limit_word.lower()} limit reached.", **base,  # type: ignore[arg-type]
        )

    if rule.action == Action.WAIT_FOR_SESSION_END:
        if inp.in_game_session and inp.detector_confident:
            if stored_state == RuleState.WAITING_FOR_SESSION_END:
                return Decision(new_state=RuleState.WAITING_FOR_SESSION_END, **base)  # type: ignore[arg-type]
            return Decision(
                new_state=RuleState.WAITING_FOR_SESSION_END,
                user_message=(
                    f"{rule.name}: {limit_word.lower()} limit reached. "
                    "Current match detected — it will be allowed to finish."
                ),
                **base,  # type: ignore[arg-type]
            )
        if inp.in_game_session is None or not inp.detector_confident:
            # FAIL-SAFE: unknown state -> wait, never kill.
            return Decision(
                new_state=RuleState.WAITING_FOR_SESSION_END,
                user_message=(
                    f"{rule.name}: game state could not be verified. "
                    "Waiting for a safe session-end state."
                ),
                **base,  # type: ignore[arg-type]
            )
        # Confidently NOT in a session -> enforce now.
        action = "BLOCK_WEBSITE" if rule.type.value == "WEBSITE" else "CLOSE_APP"
        extra = " Match ended. " if stored_state == RuleState.WAITING_FOR_SESSION_END else " "
        return Decision(
            new_state=RuleState.ENFORCED, action_to_execute=action,
            user_message=f"{rule.name}:{extra}{limit_word.lower()} limit reached. Enforced.",
            **base,  # type: ignore[arg-type]
        )

    # BLOCK / CLOSE: immediate.
    action = "BLOCK_WEBSITE" if rule.type.value == "WEBSITE" else "CLOSE_APP"
    if rule.action == Action.BLOCK and rule.type.value != "WEBSITE":
        action = "PREVENT_LAUNCH"  # app/game block = relaunch prevention
    return Decision(
        new_state=RuleState.ENFORCED, action_to_execute=action,
        user_message=f"{rule.name}: {limit_word.lower()} limit reached.",
        **base,  # type: ignore[arg-type]
    )
