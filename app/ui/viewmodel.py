"""Presentation logic for the status surface — Qt-free and fully tested.

Everything the GUI (and `--status-json`) shows is computed here from an
`AgentService.Snapshot`, so wording, colours and numbers can be unit-tested
without a display. The Qt layer in `app/ui/qt/` only paints these values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.types import Action, RuleState, RuleType

# --------------------------------------------------------------------- format
def format_duration(seconds: int | None) -> str:
    """`3725 -> "1h 02m"`, `95 -> "1m 35s"`, `None -> "—"`."""
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_remaining(seconds: int | None) -> str:
    return "unlimited" if seconds is None else f"{format_duration(seconds)} left"


def format_reset(now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return f"resets at midnight ({format_duration(int((midnight - now).total_seconds()))})"


WEEKDAYS = frozenset({"MON", "TUE", "WED", "THU", "FRI"})
WEEKENDS = frozenset({"SAT", "SUN"})
DAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def schedule_text(schedule) -> str:
    """Readable summary of a `Schedule` (or None for "always")."""
    if schedule is None:
        return "Always"
    days = schedule.days
    if days is None or days == frozenset(DAY_ORDER):
        label = "Every day"
    elif days == WEEKDAYS:
        label = "Mon–Fri"
    elif days == WEEKENDS:
        label = "Sat–Sun"
    else:
        label = "/".join(
            token.title() for token in sorted(days, key=DAY_ORDER.index)
        )
    spans = ", ".join(
        f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        for start, end in schedule.windows
    )
    return f"{label} {spans}".strip()


# ---------------------------------------------------------------------- roles
#: Colour roles the Qt theme maps to actual colours (kept as strings so the
#: viewmodel has no Qt dependency).
ROLE_OK = "ok"
ROLE_WARN = "warn"
ROLE_BAD = "bad"
ROLE_INFO = "info"
ROLE_MUTED = "muted"

ACTION_LABELS = {
    Action.CLOSE: "Close app",
    Action.BLOCK: "Block",
    Action.WAIT_FOR_SESSION_END: "Wait for match to end",
    Action.WARN_ONLY: "Warn only",
}

TYPE_LABELS = {
    RuleType.APPLICATION: "App",
    RuleType.GAME: "Game",
    RuleType.WEBSITE: "Website",
}


def action_label(rule) -> str:
    if rule.type == RuleType.WEBSITE and rule.action == Action.BLOCK:
        return "Block site"
    if rule.type != RuleType.WEBSITE and rule.action == Action.BLOCK:
        return "Prevent launch"
    return ACTION_LABELS.get(rule.action, str(rule.action))


@dataclass(frozen=True)
class RuleRow:
    rule_id: int
    name: str
    type_label: str
    target: str
    mode: str
    action: str
    schedule: str
    enabled: bool
    state: RuleState
    state_text: str
    state_role: str
    used_text: str
    limit_text: str
    remaining_text: str
    session_text: str
    progress: float  # 0..1 (daily)
    is_website: bool
    paused: bool = False

    @property
    def subtitle(self) -> str:
        bits = [self.type_label, self.target, self.action, self.mode]
        if self.schedule != "Always":
            bits.append(self.schedule)
        return " · ".join(bits)


def state_text_and_role(status, paused_rule: bool = False) -> tuple[str, str]:
    """Human wording + colour role for one rule's current state."""
    rule = status.rule
    state = status.state
    if not rule.enabled:
        return "Off", ROLE_MUTED
    if paused_rule:
        return "Paused (not counting)", ROLE_MUTED
    if rule.schedule is not None and state == RuleState.DISABLED:
        return "Outside schedule", ROLE_MUTED
    if state == RuleState.ENFORCED:
        if rule.action == Action.WARN_ONLY:
            return "Limit reached (warned)", ROLE_BAD
        if rule.type == RuleType.WEBSITE:
            return "Blocked in browser", ROLE_BAD
        if rule.action == Action.WAIT_FOR_SESSION_END:
            return "Limit reached — closes after the match", ROLE_WARN
        return "Limit reached — closed", ROLE_BAD
    if state == RuleState.WAITING_FOR_SESSION_END:
        return "In a match — waits for it to end", ROLE_WARN
    if state == RuleState.WARNING:
        return f"Warning — {format_remaining(status.remaining_today)}", ROLE_WARN
    if status.used_seconds > 0:
        return f"In use — {format_remaining(status.remaining_today)}", ROLE_INFO
    return "Ready", ROLE_OK


def rule_rows(snapshot, now: datetime | None = None) -> list[RuleRow]:
    rows: list[RuleRow] = []
    paused = snapshot.paused
    exempt = set(snapshot.pause_exempt_rules)
    for status in snapshot.rules:
        rule = status.rule
        paused_rule = paused and rule.name not in exempt
        text, role = state_text_and_role(status, paused_rule)
        rows.append(RuleRow(
            rule_id=rule.id or 0,
            name=rule.name,
            type_label=TYPE_LABELS.get(rule.type, str(rule.type.value)),
            target=rule.target,
            mode=rule.mode.value.title(),
            action=action_label(rule),
            schedule=schedule_text(rule.schedule),
            enabled=rule.enabled,
            state=status.state,
            state_text=text,
            state_role=ROLE_MUTED if paused_rule else role,
            used_text=format_duration(status.used_seconds),
            limit_text=format_duration(rule.daily_limit_seconds),
            remaining_text=format_remaining(status.remaining_today),
            session_text=(
                f"session {format_duration(status.session_seconds)}"
                if status.session_seconds else "no open session"
            ),
            progress=status.used_ratio,
            is_website=rule.type == RuleType.WEBSITE,
            paused=paused_rule,
        ))
    rows.sort(key=lambda row: (not row.enabled, -row.progress, row.name.lower()))
    return rows


# --------------------------------------------------------------------- header
def health(snapshot) -> tuple[str, str]:
    """(role, text) for the main status chip."""
    if not snapshot.running:
        return ROLE_MUTED, "Stopped"
    if snapshot.paused:
        return ROLE_WARN, f"Paused — {format_duration(snapshot.pause_remaining_seconds)} left"
    if snapshot.tick_errors:
        return ROLE_BAD, f"Running with {snapshot.tick_errors} monitor error(s)"
    if snapshot.degraded:
        return ROLE_WARN, "Running (limited OS support)"
    return ROLE_OK, "Monitoring"


def status_line(snapshot) -> str:
    bits = [f"Tick {snapshot.ticks:,}"]
    if snapshot.browsers_connected:
        bits.append(f"{snapshot.browsers_connected} browser(s) connected")
    else:
        bits.append("no browser connected")
    if snapshot.closes:
        bits.append(f"{snapshot.closes} app close(s) today")
    if snapshot.last_sleep_gap:
        bits.append(f"last sleep gap {format_duration(int(snapshot.last_sleep_gap))}")
    if snapshot.last_tick_error:
        bits.append(f"last error: {snapshot.last_tick_error[:60]}")
    return " · ".join(bits)


def pause_banner(snapshot) -> str | None:
    if not snapshot.paused:
        return None
    text = f"Tracking paused for {format_duration(snapshot.pause_remaining_seconds)}"
    if snapshot.pause_until:
        text += f" (until {snapshot.pause_until[11:16]})"
    if snapshot.pause_exempt_rules:
        text += " — strict rules keep running: " + ", ".join(snapshot.pause_exempt_rules)
    return text


def security_notices(snapshot) -> list[tuple[str, str]]:
    """Phase 7: anti-bypass findings the user must see, as (role, text).

    Ordered worst-first. Empty list = nothing to report (the normal case).
    """
    sec = getattr(snapshot, "security", None) or {}
    notices: list[tuple[str, str]] = []

    unclean = sec.get("unclean_stop")
    if unclean:
        when = unclean.get("local") or unclean.get("last_start_at") or "earlier"
        notices.append((
            ROLE_BAD,
            f"Time Manager stopped unexpectedly (last run started {when}) — "
            "limits were not enforced until this start.",
        ))

    regressed = sec.get("clock_regressed_seconds")
    if regressed:
        notices.append((
            ROLE_BAD,
            f"The system clock is {format_duration(int(regressed))} behind the last "
            "time the agent ran — daily limits may have been rolled back.",
        ))

    for item in sec.get("usage_tamper", []) or []:
        notices.append((
            ROLE_BAD,
            f"{item.get('rule', 'A rule')}: recorded usage for {item.get('day', 'today')} "
            f"was erased or rolled back — the limit counts from the remembered "
            f"{format_duration(int(item.get('floor_seconds', 0)))} instead.",
        ))

    silent = sec.get("extension_silent_rules", []) or []
    if silent:
        notices.append((
            ROLE_WARN,
            f"No browser extension connected — website limits are NOT enforced "
            f"for: {', '.join(silent)}.",
        ))

    if sec.get("startup_repaired"):
        notices.append((
            ROLE_INFO,
            "Windows startup was re-enabled because STRICT rules are active.",
        ))
    return notices


def browser_lines(snapshot) -> list[str]:
    if not snapshot.browsers_connected:
        return ["No browser connected — open the extension options and paste the token "
                "(python -m app.main --show-token)."]
    lines = []
    for client in snapshot.ipc_clients:
        lines.append(f"{client.get('browser', '?')} · {client.get('version', '?')}")
    if snapshot.browser_domains:
        lines.append("Active: " + ", ".join(snapshot.browser_domains))
    else:
        lines.append("Connected — no tracked site in focus")
    return lines


def detector_lines(snapshot) -> list[str]:
    """Game-detector panel: what is watching which games, and its health."""
    items = list(getattr(snapshot, "detectors", []) or [])
    summary = getattr(snapshot, "detector_summary", {}) or {}
    rejected = list(getattr(snapshot, "detector_rejected", []) or [])
    if not items:
        return ["No game detectors loaded — games count like any other app."]

    lines: list[str] = []
    for item in items:
        name = item.get("name") or item.get("id", "?")
        parts = [f"{name} · {item.get('source', 'builtin')}"]
        if item.get("quarantined"):
            parts.append("quarantined — matches will not be closed")
        elif item.get("last_detail"):
            verdict = "in session" if item.get("last_in_session") else (
                "no session" if item.get("last_in_session") is False else "unknown")
            parts.append(f"{verdict}: {item['last_detail']}")
        else:
            parts.append("waiting for a game")
        if item.get("timeouts") or item.get("errors"):
            parts.append(f"{item.get('timeouts', 0)} timeout(s), {item.get('errors', 0)} error(s)")
        lines.append(" · ".join(parts))
    if summary.get("timeout_ms"):
        lines.append(
            f"Budget {summary['timeout_ms']} ms per probe; "
            f"{summary.get('quarantined', 0)} quarantined"
        )
    for failure in rejected:
        lines.append(f"Rejected plugin: {failure.get('origin', '?')} — {failure.get('reason', '')}")
    return lines


def link_lines(snapshot) -> list[str]:
    if not snapshot.ipc_running:
        return [snapshot.ipc_last_error or "Browser link unavailable"]
    return [
        f"Listening on 127.0.0.1:{snapshot.ipc_port}",
        f"in {snapshot.ipc_messages_in:,} / out {snapshot.ipc_messages_out:,} messages"
        + (f", {snapshot.ipc_rate_limited} rate-limited" if snapshot.ipc_rate_limited else ""),
    ]


#: Audit-log action strings (what the engine recorded) -> words for humans.
AUDIT_ACTION_TEXT = {
    "CLOSE_APP": "Close app",
    "BLOCK_WEBSITE": "Block site",
    "WAIT_FOR_SESSION_END": "Wait for match",
    "WARN": "Warning",
    "NOTIFY_ONLY": "Notified",
}


def timeline(records: list[dict], limit: int = 40) -> list[str]:
    """Enforcement audit rows -> readable one-liners (newest first)."""
    out = []
    for row in records[:limit]:
        created = str(row.get("created_at", ""))
        clock = created[11:19] if len(created) >= 19 else created
        outcome = str(row.get("outcome", ""))
        detail = str(row.get("detail") or "")
        action = AUDIT_ACTION_TEXT.get(str(row.get("action", "")), str(row.get("action", "")))
        out.append(
            f"{clock}  {action}  {outcome}"
            + (f"  ({detail})" if detail else "")
        )
    return out


def window_title(snapshot) -> str:
    role, text = health(snapshot)
    return f"Time Manager — {text}"
