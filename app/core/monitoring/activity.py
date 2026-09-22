"""Snapshot -> per-rule activity flags (the monitor's brain, still pure logic).

Counting decisions made here (all documented + tested):

* **Foreground counts, background does not.** Being *installed and running* is
  not usage; Discord idling in the tray is not "Discord time". Foreground
  matches count immediately.
* **A short grace after losing focus** keeps rapid alt-tabbing from creating
  accounting holes (and matches how people use a browser+app pair).
* **STRICT mode does not change what counts** — it changes how hard
  enforcement hits (see policy.py). One knob, one meaning.
* **Idle gate (opt-in, default off):** after `idle_grace_seconds` without user
  input nothing is counted, so a paused movie or a coffee break is not billed
  as screen time.
* **Games** additionally carry a detector verdict; unknown state is passed
  through as "unknown" and the state machine waits (never kills).
* **Websites** need the extension's tab events (Phase 4, `app/ipc/`). Without
  a connected extension a website rule resolves to inactive with an explicit
  reason, never a guess. Defense in depth: the events are only trusted while a
  browser is genuinely the foreground application (`require_browser_foreground`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.enforcement.game_guard import GameSessionGuard
from app.core.monitoring.processes import SystemSnapshot
from app.core.rules.engine import ActivityInput
from app.core.rules.matcher import executable_matches, domain_matches
from app.core.rules.models import Rule
from app.core.types import RuleType, SessionType

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityPolicy:
    enforce_foreground_only: bool = True
    background_grace_seconds: int = 5
    idle_grace_seconds: int = 0  # 0 = disabled
    website_grace_seconds: int = 3
    website_stale_seconds: int = 15
    # Defense in depth (docs/PROTOCOL.md): extension tab events only count
    # while a browser is genuinely the foreground application, so a buggy or
    # hostile page cannot keep a website timer running in the background.
    require_browser_foreground: bool = True
    browser_executables: tuple[str, ...] = (
        "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
        "opera.exe", "vivaldi.exe", "chrome_proxy.exe", "msedgewebview2.exe",
    )


@dataclass(frozen=True)
class ResolvedActivity:
    activities: dict[int, ActivityInput]
    detail: dict[int, str]
    foreground_exe: str | None = None
    idle_seconds: float | None = None
    idle_blocked: bool = False

    def active_ids(self) -> list[int]:
        return [rid for rid, a in self.activities.items() if a.active]


@dataclass
class ActivityResolver:
    guard: GameSessionGuard | None = None
    policy: ActivityPolicy = field(default_factory=ActivityPolicy)

    def __post_init__(self) -> None:
        self._last_foreground: dict[str, float] = {}
        self._web_first_seen: dict[str, float] = {}

    def resolve(
        self,
        rules: list[Rule],
        snapshot: SystemSnapshot,
        *,
        web_active: dict[str, float] | None = None,
    ) -> ResolvedActivity:
        now = snapshot.captured_mono
        idle = snapshot.idle_seconds
        idle_blocked = bool(
            self.policy.idle_grace_seconds > 0
            and idle is not None
            and idle >= self.policy.idle_grace_seconds
        )
        fg_exe = snapshot.foreground_exe()
        browser_foreground = (
            not self.policy.require_browser_foreground
            or (fg_exe is not None
                and executable_matches(self.policy.browser_executables, fg_exe))
        )
        activities: dict[int, ActivityInput] = {}
        details: dict[int, str] = {}

        for rule in rules:
            assert rule.id is not None
            if not rule.enabled:
                activities[rule.id] = ActivityInput(active=False, idle_blocked=idle_blocked)
                details[rule.id] = "rule disabled"
                continue

            if rule.type == RuleType.WEBSITE:
                if not browser_foreground:
                    active = False
                    detail = (
                        f"no browser in the foreground (foreground={fg_exe or 'unknown'})"
                    )
                else:
                    active, detail = self._website_active(rule, web_active, now)
                if idle_blocked and active:
                    # A tab left open while nobody touches the keyboard is not
                    # usage (same rule as for apps).
                    active = False
                    detail = f"idle >= {self.policy.idle_grace_seconds}s (not counted)"
                activities[rule.id] = ActivityInput(
                    active=active, session_type=SessionType.WEBSITE.value,
                    idle_blocked=idle_blocked,
                )
                details[rule.id] = detail
                continue

            active, detail = self._process_active(rule, snapshot, fg_exe, idle_blocked, now)
            in_session, confident, gdetail = (None, True, "")
            if rule.type == RuleType.GAME and self.guard is not None:
                verdict = self.guard.for_rule(rule, snapshot)
                in_session, confident, gdetail = verdict.in_session, verdict.confident, verdict.detail
            stype = SessionType.GAME if rule.type == RuleType.GAME else SessionType.FOREGROUND
            activities[rule.id] = ActivityInput(
                active=active,
                session_type=stype.value,
                in_game_session=in_session,
                detector_confident=confident,
                idle_blocked=idle_blocked,
            )
            details[rule.id] = f"{detail}{' | ' + gdetail if gdetail else ''}"

        return ResolvedActivity(
            activities=activities,
            detail=details,
            foreground_exe=fg_exe,
            idle_seconds=idle,
            idle_blocked=idle_blocked,
        )

    # ------------------------------------------------------------------ apps
    def _process_active(
        self, rule: Rule, snapshot: SystemSnapshot, fg_exe: str | None,
        idle_blocked: bool, now: float,
    ) -> tuple[bool, str]:
        exes = rule.executables
        for exe in exes:
            if fg_exe is not None and executable_matches((exe,), fg_exe):
                self._last_foreground[exe] = now
                if idle_blocked:
                    return False, f"foreground but idle >= {self.policy.idle_grace_seconds}s"
                return True, "foreground"
        if not any(snapshot.has(exe) for exe in exes):
            return False, "not running"
        if idle_blocked:
            return False, f"running but idle >= {self.policy.idle_grace_seconds}s"
        if not self.policy.enforce_foreground_only:
            return True, "running (foreground-only counting disabled)"
        last = max((self._last_foreground.get(e, float("-inf")) for e in exes), default=float("-inf"))
        if now - last <= self.policy.background_grace_seconds:
            return True, f"background within {self.policy.background_grace_seconds}s grace"
        return False, "running in background (not counted)"

    # -------------------------------------------------------------- websites
    def _website_active(
        self, rule: Rule, web_active: dict[str, float] | None, now: float
    ) -> tuple[bool, str]:
        if web_active is None:
            return False, "no browser extension connected"
        assert rule.domain is not None
        newest = max(
            (ts for host, ts in web_active.items()
             if domain_matches(rule.domain, host) and now - ts <= self.policy.website_stale_seconds),
            default=None,
        )
        if newest is None:
            self._web_first_seen.pop(rule.domain, None)
            return False, "no active tab for this domain"
        first = self._web_first_seen.setdefault(rule.domain, now)
        if now - first < self.policy.website_grace_seconds:
            return False, f"debounce ({self.policy.website_grace_seconds}s) not elapsed"
        return True, "active tab"
