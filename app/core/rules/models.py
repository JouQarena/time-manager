"""Rule dataclass + validation.

A Rule is the single source of truth for "what is limited and how".
Validation runs at creation/edit time; the DB layer re-validates on load so
a hand-edited database can never inject a nonsense rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.core.rules.matcher import normalize_domain, normalize_executable
from app.core.scheduling.schedule import Schedule, parse_schedule
from app.core.types import Action, Mode, RuleType


@dataclass
class Rule:
    name: str
    type: RuleType
    target: str  # display target as typed by the user
    action: Action
    mode: Mode = Mode.NORMAL
    executable: str | None = None  # normalized, e.g. "discord.exe"
    extra_executables: tuple[str, ...] = ()  # e.g. LoL launcher+game exes
    domain: str | None = None  # normalized, e.g. "youtube.com"
    daily_limit_seconds: int | None = None
    session_limit_seconds: int | None = None
    warning_seconds: tuple[int, ...] = (600, 300, 60)
    schedule: Schedule | None = None  # None = always active
    enabled: bool = True
    id: int | None = None

    def __post_init__(self) -> None:
        # Normalize enums from raw strings (DB rows, JSON).
        if isinstance(self.type, str):
            self.type = RuleType(self.type)
        if isinstance(self.action, str):
            self.action = Action(self.action)
        if isinstance(self.mode, str):
            self.mode = Mode(self.mode)
        if isinstance(self.schedule, (str, dict)) or self.schedule is not None and not isinstance(
            self.schedule, Schedule
        ):
            self.schedule = parse_schedule(self.schedule)  # type: ignore[arg-type]
        self.validate()

    # ------------------------------------------------------------------ API
    def validate(self) -> None:
        """Raise ValueError describing the first problem found."""
        if not self.name or not self.name.strip():
            raise ValueError("Rule name must not be empty.")
        if len(self.name) > 120:
            raise ValueError("Rule name too long (max 120 chars).")

        if self.type in (RuleType.APPLICATION, RuleType.GAME):
            if not self.executable:
                raise ValueError(f"{self.type.value} rules require an executable.")
            self.executable = normalize_executable(self.executable)
            self.extra_executables = tuple(
                normalize_executable(e) for e in self.extra_executables
            )
            if self.domain is not None:
                raise ValueError("Application/game rules must not carry a domain.")
            if self.type == RuleType.GAME and self.action not in (
                Action.WAIT_FOR_SESSION_END,
                Action.CLOSE,
                Action.BLOCK,
                Action.WARN_ONLY,
            ):
                raise ValueError("Invalid action for game rule.")
        elif self.type == RuleType.WEBSITE:
            if not self.domain:
                raise ValueError("Website rules require a domain.")
            self.domain = normalize_domain(self.domain)
            if self.executable is not None:
                raise ValueError("Website rules must not carry an executable.")
            if self.action == Action.CLOSE:
                raise ValueError("Website rules cannot use CLOSE (use BLOCK).")
        else:  # pragma: no cover - enum exhaustiveness
            raise ValueError(f"Unknown rule type: {self.type}")

        for label, value in (
            ("daily_limit_seconds", self.daily_limit_seconds),
            ("session_limit_seconds", self.session_limit_seconds),
        ):
            if value is not None and (value <= 0 or value > 24 * 3600):
                raise ValueError(f"{label} must be 1..86400 seconds or None.")
        if self.daily_limit_seconds is None and self.session_limit_seconds is None:
            raise ValueError("At least one of daily/session limit must be set.")

        cleaned: list[int] = []
        for w in self.warning_seconds or ():
            w = int(w)
            if w <= 0 or w > 24 * 3600:
                raise ValueError("warning_seconds entries must be 1..86400.")
            if w not in cleaned:
                cleaned.append(w)
        self.warning_seconds = tuple(sorted(cleaned, reverse=True))

    @property
    def executables(self) -> tuple[str, ...]:
        """Primary + extra exes (apps/games); empty for websites."""
        if self.type == RuleType.WEBSITE:
            return ()
        assert self.executable is not None
        return (self.executable, *self.extra_executables)

    # ------------------------------------------------------------ ser/deser
    def to_row(self) -> dict:
        """Serialize to a DB-row dict (matches `rules` columns)."""
        return {
            "id": self.id,
            "name": self.name.strip(),
            "type": self.type.value,
            "target": self.target.strip(),
            "executable": self.executable,
            "extra_executables": json.dumps(list(self.extra_executables)),
            "domain": self.domain,
            "daily_limit_seconds": self.daily_limit_seconds,
            "session_limit_seconds": self.session_limit_seconds,
            "warning_seconds": json.dumps(list(self.warning_seconds)),
            "action": self.action.value,
            "mode": self.mode.value,
            "schedule": self.schedule.to_json() if self.schedule else None,
            "enabled": 1 if self.enabled else 0,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Rule":
        """Build a validated Rule from a DB row (sqlite3.Row or dict)."""
        d = dict(row)
        return cls(
            id=d.get("id"),
            name=d["name"],
            type=d["type"],
            target=d["target"],
            action=d["action"],
            mode=d.get("mode", Mode.NORMAL.value),
            executable=d.get("executable"),
            extra_executables=tuple(json.loads(d.get("extra_executables") or "[]")),
            domain=d.get("domain"),
            daily_limit_seconds=d.get("daily_limit_seconds"),
            session_limit_seconds=d.get("session_limit_seconds"),
            warning_seconds=tuple(json.loads(d.get("warning_seconds") or "[]")),
            schedule=parse_schedule(d.get("schedule")),
            enabled=bool(d.get("enabled", 1)),
        )
