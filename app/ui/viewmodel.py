"""Presentation logic for the status surface — Qt-free and fully tested.

Everything the GUI (and `--status-json`) shows is computed here from an
`AgentService.Snapshot`, so wording, colours and numbers can be unit-tested
without a display. The Qt layer in `app/ui/qt/` only paints these values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.types import Action, RuleState, RuleType
from app.i18n import tr

# --------------------------------------------------------------------- format
def format_duration(seconds: int | None) -> str:
    """`3725 -> "1h 02m"`, `95 -> "1m 35s"`, `None -> "—"` (translated)."""
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return tr("fmt.dur_hm", hours=hours, minutes=minutes)
    if minutes:
        return tr("fmt.dur_ms", minutes=minutes, secs=secs)
    return tr("fmt.dur_s", secs=secs)


def format_remaining(seconds: int | None) -> str:
    if seconds is None:
        return tr("fmt.unlimited")
    return tr("fmt.left", duration=format_duration(seconds))


def format_reset(now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tr("fmt.resets", duration=format_duration(int((midnight - now).total_seconds())))


WEEKDAYS = frozenset({"MON", "TUE", "WED", "THU", "FRI"})
WEEKENDS = frozenset({"SAT", "SUN"})
DAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def schedule_text(schedule) -> str:
    """Readable summary of a `Schedule` (or None for "always")."""
    if schedule is None:
        return tr("schedule.always")
    days = schedule.days
    if days is None or days == frozenset(DAY_ORDER):
        label = tr("schedule.every_day")
    elif days == WEEKDAYS:
        label = tr("schedule.weekdays")
    elif days == WEEKENDS:
        label = tr("schedule.weekends")
    else:
        label = "/".join(
            tr(f"day.{token}") for token in sorted(days, key=DAY_ORDER.index)
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

#: Enum member -> catalog key (resolved at call time: language can change
#: while the app is running, so module-level strings would freeze one language).
ACTION_KEYS = {
    Action.CLOSE: "action.close_app",
    Action.BLOCK: "action.block",
    Action.WAIT_FOR_SESSION_END: "action.wait_session",
    Action.WARN_ONLY: "action.warn_only",
}

TYPE_KEYS = {
    RuleType.APPLICATION: "type.app",
    RuleType.GAME: "type.game",
    RuleType.WEBSITE: "type.website",
}


def action_label(rule) -> str:
    if rule.type == RuleType.WEBSITE and rule.action == Action.BLOCK:
        return tr("action.block_site")
    if rule.type != RuleType.WEBSITE and rule.action == Action.BLOCK:
        return tr("action.prevent_launch")
    key = ACTION_KEYS.get(rule.action)
    return tr(key) if key else str(rule.action)


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
        if self.schedule != tr("schedule.always"):
            bits.append(self.schedule)
        return " · ".join(bits)


def state_text_and_role(status, paused_rule: bool = False) -> tuple[str, str]:
    """Human wording + colour role for one rule's current state."""
    rule = status.rule
    state = status.state
    if not rule.enabled:
        return tr("state.off"), ROLE_MUTED
    if paused_rule:
        return tr("state.paused"), ROLE_MUTED
    if rule.schedule is not None and state == RuleState.DISABLED:
        return tr("state.outside_schedule"), ROLE_MUTED
    if state == RuleState.ENFORCED:
        if rule.action == Action.WARN_ONLY:
            return tr("state.limit_warned"), ROLE_BAD
        if rule.type == RuleType.WEBSITE:
            return tr("state.blocked_browser"), ROLE_BAD
        if rule.action == Action.WAIT_FOR_SESSION_END:
            return tr("state.limit_after_match"), ROLE_WARN
        return tr("state.limit_closed"), ROLE_BAD
    if state == RuleState.WAITING_FOR_SESSION_END:
        return tr("state.in_match"), ROLE_WARN
    if state == RuleState.WARNING:
        return tr("state.warning", remaining=format_remaining(status.remaining_today)), ROLE_WARN
    if status.used_seconds > 0:
        return tr("state.in_use", remaining=format_remaining(status.remaining_today)), ROLE_INFO
    return tr("state.ready"), ROLE_OK


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
            type_label=tr(TYPE_KEYS.get(rule.type, "")) if rule.type in TYPE_KEYS
            else str(rule.type.value),
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
                tr("session.text", duration=format_duration(status.session_seconds))
                if status.session_seconds else tr("session.none")
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
        return ROLE_MUTED, tr("health.stopped")
    if snapshot.paused:
        return ROLE_WARN, tr("health.paused",
                             duration=format_duration(snapshot.pause_remaining_seconds))
    if snapshot.tick_errors:
        return ROLE_BAD, tr("health.errors", count=snapshot.tick_errors)
    if snapshot.degraded:
        return ROLE_WARN, tr("health.degraded")
    return ROLE_OK, tr("health.monitoring")


def status_line(snapshot) -> str:
    bits = [tr("status.tick", ticks=f"{snapshot.ticks:,}")]
    if snapshot.browsers_connected:
        bits.append(tr("status.browsers", count=snapshot.browsers_connected))
    else:
        bits.append(tr("status.no_browser"))
    if snapshot.closes:
        bits.append(tr("status.closes", count=snapshot.closes))
    if snapshot.last_sleep_gap:
        bits.append(tr("status.sleep_gap",
                       duration=format_duration(int(snapshot.last_sleep_gap))))
    if snapshot.last_tick_error:
        bits.append(tr("status.last_error", text=snapshot.last_tick_error[:60]))
    return " · ".join(bits)


def pause_banner(snapshot) -> str | None:
    if not snapshot.paused:
        return None
    text = tr("pause.banner",
              duration=format_duration(snapshot.pause_remaining_seconds))
    if snapshot.pause_until:
        text += tr("pause.until", time=snapshot.pause_until[11:16])
    if snapshot.pause_exempt_rules:
        text += tr("pause.strict",
                   names=tr("fmt.list_sep").join(snapshot.pause_exempt_rules))
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
        notices.append((ROLE_BAD, tr("sec.unclean", when=when)))

    regressed = sec.get("clock_regressed_seconds")
    if regressed:
        notices.append((ROLE_BAD, tr(
            "sec.clock", duration=format_duration(int(regressed)),
        )))

    for item in sec.get("usage_tamper", []) or []:
        notices.append((ROLE_BAD, tr(
            "sec.tamper",
            rule=item.get("rule", "A rule"),
            day=item.get("day", "today"),
            duration=format_duration(int(item.get("floor_seconds", 0))),
        )))

    silent = sec.get("extension_silent_rules", []) or []
    if silent:
        notices.append((ROLE_WARN, tr("sec.silent",
                                      rules=tr("fmt.list_sep").join(silent))))

    if sec.get("startup_repaired"):
        notices.append((ROLE_INFO, tr("sec.startup_repaired")))
    return notices


def browser_lines(snapshot) -> list[str]:
    if not snapshot.browsers_connected:
        return [tr("browser.none")]
    lines = []
    for client in snapshot.ipc_clients:
        lines.append(f"{client.get('browser', '?')} · {client.get('version', '?')}")
    if snapshot.browser_domains:
        lines.append(tr("browser.active", domains=", ".join(snapshot.browser_domains)))
    else:
        lines.append(tr("browser.connected_idle"))
    return lines


def detector_lines(snapshot) -> list[str]:
    """Game-detector panel: what is watching which games, and its health."""
    items = list(getattr(snapshot, "detectors", []) or [])
    summary = getattr(snapshot, "detector_summary", {}) or {}
    rejected = list(getattr(snapshot, "detector_rejected", []) or [])
    if not items:
        return [tr("detector.none")]

    lines: list[str] = []
    for item in items:
        name = item.get("name") or item.get("id", "?")
        parts = [f"{name} · {item.get('source', 'builtin')}"]
        if item.get("quarantined"):
            parts.append(tr("detector.quarantined"))
        elif item.get("last_detail"):
            key = ("detector.in_session" if item.get("last_in_session") else
                   "detector.no_session" if item.get("last_in_session") is False else
                   "detector.unknown")
            parts.append(tr(key, detail=item["last_detail"]))
        else:
            parts.append(tr("detector.waiting"))
        if item.get("timeouts") or item.get("errors"):
            parts.append(f"{item.get('timeouts', 0)} timeout(s), {item.get('errors', 0)} error(s)")
        lines.append(" · ".join(parts))
    if summary.get("timeout_ms"):
        lines.append(tr("detector.budget", ms=summary["timeout_ms"],
                        quarantined=summary.get("quarantined", 0)))
    for failure in rejected:
        lines.append(tr("detector.rejected", origin=failure.get("origin", "?"),
                        reason=failure.get("reason", "")))
    return lines


def link_lines(snapshot) -> list[str]:
    if not snapshot.ipc_running:
        return [snapshot.ipc_last_error or tr("link.unavailable")]
    return [
        tr("link.listening", port=snapshot.ipc_port),
        tr("link.messages", in_count=f"{snapshot.ipc_messages_in:,}",
           out_count=f"{snapshot.ipc_messages_out:,}")
        + (tr("link.rate_limited", count=snapshot.ipc_rate_limited)
           if snapshot.ipc_rate_limited else ""),
    ]


#: Audit-log action strings (what the engine recorded) -> catalog keys.
AUDIT_ACTION_KEYS = {
    "CLOSE_APP": "audit.close_app",
    "BLOCK_WEBSITE": "audit.block_website",
    "WAIT_FOR_SESSION_END": "audit.wait_session",
    "WARN": "audit.warn",
    "NOTIFY_ONLY": "audit.notify",
}


def timeline(records: list[dict], limit: int = 40) -> list[str]:
    """Enforcement audit rows -> readable one-liners (newest first)."""
    out = []
    for row in records[:limit]:
        created = str(row.get("created_at", ""))
        clock = created[11:19] if len(created) >= 19 else created
        outcome = str(row.get("outcome", ""))
        detail = str(row.get("detail") or "")
        raw_action = str(row.get("action", ""))
        action_key = AUDIT_ACTION_KEYS.get(raw_action)
        action = tr(action_key) if action_key else raw_action
        out.append(
            f"{clock}  {action}  {outcome}"
            + (f"  ({detail})" if detail else "")
        )
    return out


def window_title(snapshot) -> str:
    _role, text = health(snapshot)
    return tr("title.window", state=text)
