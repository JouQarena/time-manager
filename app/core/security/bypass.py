"""Runtime bypass signals the agent can actually see.

Bypass vector this closes (spec §15: "restart the browser"): website rules
are enforced *through* the browser extension. A user can defeat that by
disabling/uninstalling the extension, browsing from a profile that never had
it, or restarting the browser and never letting it reconnect. The agent
cannot force a browser to keep an extension — but it can notice the hole and
say so loudly, instead of silently counting nothing:

    STRICT website rule enabled
    AND a supported browser process is running
    AND no extension connection for `silence_seconds`
    -> EXTENSION_SILENT alert (audit once per episode + notification,
       re-notified on a long cooldown while the hole stays open).

When a connection returns, the episode is closed with an EXTENSION_RESTORED
audit row. Alerts are advisory for NORMAL rules and *actionable* for STRICT
ones; the dashboard shows both.

This module never decides enforcement — it only produces `BypassAlert`s for
the monitor loop to surface. That keeps the enforcement path in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.clock import Clock
from app.core.monitoring.processes import SystemSnapshot
from app.core.rules.models import Rule
from app.core.types import Mode, RuleType
from app.database.db import Database

log = logging.getLogger(__name__)

#: Browsers the extension supports (Phase 4 decision: Chrome + Edge first).
SUPPORTED_BROWSER_EXES = ("chrome.exe", "msedge.exe")


@dataclass(frozen=True)
class BypassAlert:
    kind: str  # EXTENSION_SILENT
    rule_id: int
    rule_name: str
    message: str


class BypassWatch:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        notifier=None,
        *,
        silence_seconds: float = 60.0,
        renotify_seconds: float = 900.0,
    ) -> None:
        self._db = db
        self._clock = clock
        self.notifier = notifier
        self.silence_seconds = float(silence_seconds)
        self.renotify_seconds = float(renotify_seconds)
        #: rule_id -> mono when continuous silence began
        self._silent_since: dict[int, float] = {}
        #: rules currently in a confirmed (>= silence_seconds) silent episode
        self._episode_open: set[int] = set()
        #: rule_id -> mono of the last user notification about it
        self._last_notified: dict[int, float] = {}
        self._rule_names: dict[int, str] = {}

    # ------------------------------------------------------------------- API
    @property
    def silent_rule_names(self) -> tuple[str, ...]:
        """Names of rules in an open silent episode (for the UI)."""
        return tuple(self._rule_names[r] for r in sorted(self._episode_open))

    def check(
        self,
        rules: list[Rule],
        snapshot: SystemSnapshot,
        browsers_connected: int,
    ) -> list[BypassAlert]:
        """One tick of bypass observation. Cheap; never raises."""
        now = self._clock.mono()
        strict_web = [
            r for r in rules
            if r.enabled and r.mode == Mode.STRICT and r.type == RuleType.WEBSITE
            and r.id is not None
        ]
        for r in strict_web:
            self._rule_names[r.id if r.id is not None else -1] = r.name

        browser_running = any(snapshot.has(exe) for exe in SUPPORTED_BROWSER_EXES)
        silent_now = bool(strict_web) and browser_running and browsers_connected == 0

        alerts: list[BypassAlert] = []
        if silent_now:
            for rule in strict_web:
                rid = rule.id
                assert rid is not None
                start = self._silent_since.setdefault(rid, now)
                if now - start < self.silence_seconds:
                    continue  # still inside the reconnect grace window
                if rid not in self._episode_open:
                    self._episode_open.add(rid)
                    message = (
                        f"{rule.name}: no browser extension is connected while "
                        f"{'/'.join(SUPPORTED_BROWSER_EXES)} is running — this "
                        "website limit cannot be enforced right now."
                    )
                    self._audit("EXTENSION_SILENT", "warning", f"rule={rule.name!r}")
                    self._notify(rid, message, urgent=True, now=now)
                    alerts.append(BypassAlert("EXTENSION_SILENT", rid, rule.name, message))
                elif now - self._last_notified.get(rid, now) >= self.renotify_seconds:
                    message = (
                        f"{rule.name}: still no browser extension connected — "
                        "the website limit remains unenforced."
                    )
                    self._notify(rid, message, urgent=False, now=now)
                    alerts.append(BypassAlert("EXTENSION_SILENT", rid, rule.name, message))
        else:
            # Silence lifted (browser closed, extension back, or rules changed):
            # close any open episodes with an audit row each.
            for rid in sorted(self._episode_open):
                self._audit(
                    "EXTENSION_RESTORED", "info",
                    f"rule={self._rule_names.get(rid, rid)!r}",
                )
            self._episode_open.clear()
            self._silent_since.clear()
        return alerts

    # -------------------------------------------------------------- plumbing
    def _audit(self, kind: str, severity: str, detail: str) -> None:
        try:
            self._db.log_audit(kind, severity, detail)
        except Exception:  # noqa: BLE001 - observation must never break ticking
            log.exception("Could not write %s audit row.", kind)

    def _notify(self, rid: int, message: str, *, urgent: bool, now: float) -> None:
        # Record the attempt even if delivery fails, so we never spam.
        self._last_notified[rid] = now
        if self.notifier is None:
            return
        try:
            self.notifier.notify("Time Manager — bypass warning", message, urgent=urgent)
        except Exception:  # noqa: BLE001
            log.warning("Bypass notification failed.", exc_info=True)
