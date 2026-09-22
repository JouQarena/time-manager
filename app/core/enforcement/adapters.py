"""Bridges core decisions -> user-facing side effects.

Two adapters, both behind small protocols so the whole enforcement path is
testable without touching a real desktop:

* `Notifier`      — warnings/limit notifications (Phase 7 wires the tray).
* `EnforcementExecutor` — turns `RuleOutcome`s + a snapshot into concrete
  actions: notifications, process closes (via `KillPolicy` + `ProcessCloser`),
  and website blocks pushed to connected browsers (Phase 4). With no browser
  connected the block is recorded as DEFERRED, so nothing silently pretends to
  have happened.

Every action attempt is written to `enforcement_log` (schema v2) with its
outcome, so the user can always audit what the agent did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Protocol

from app.core.clock import Clock
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.policy import GrandpaStore, KillPolicy
from app.core.enforcement.state_machine import Decision
from app.core.monitoring.processes import SystemSnapshot
from app.core.rules.engine import RuleOutcome
from app.core.rules.models import Rule
from app.core.types import Mode, RuleState
from app.database.db import Database

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, title: str, message: str, *, urgent: bool = False) -> None: ...


@dataclass
class RecordingNotifier:
    """Test/dev notifier that keeps everything in memory."""

    messages: list[tuple[str, str, bool]] = field(default_factory=list)

    def notify(self, title: str, message: str, *, urgent: bool = False) -> None:
        self.messages.append((title, message, urgent))

    def drain(self) -> list[tuple[str, str, bool]]:
        out, self.messages = self.messages, []
        return out


@dataclass(frozen=True)
class EnforcementRecord:
    rule_id: int
    rule_name: str
    action: str
    outcome: str  # EXECUTED|SKIPPED|DEFERRED|PROTECTED|FAILED
    detail: str


class EnforcementExecutor:
    def __init__(
        self,
        db: Database,
        closer: ProcessCloser,
        clock: Clock,
        *,
        policy: KillPolicy | None = None,
        notifier: Notifier | None = None,
        browser_sink: Callable[[Rule, Decision], bool] | None = None,
    ) -> None:
        self._db = db
        self._closer = closer
        self._clock = clock
        self._policy = policy or KillPolicy()
        self._notifier = notifier or RecordingNotifier()
        self._browser_sink = browser_sink
        self._notified: set[str] = set()  # dedupe keys, day-scoped
        self._noop_signature: dict[int, tuple[str, str]] = {}
        #: Optional Phase 5 hook: `callable(rule) -> True` when a pause is
        #: suppressing this rule (STRICT rules are never suppressed).
        self.pause_provider = None

    # ------------------------------------------------------------------ tick
    def handle(
        self, outcomes: list[RuleOutcome], snapshot: SystemSnapshot, day: str
    ) -> list[EnforcementRecord]:
        records: list[EnforcementRecord] = []
        for outcome in outcomes:
            records.extend(self._handle_one(outcome, snapshot, day))
        return records

    def _handle_one(
        self, outcome: RuleOutcome, snapshot: SystemSnapshot, day: str
    ) -> list[EnforcementRecord]:
        rule, decision = outcome.rule, outcome.decision
        assert rule.id is not None
        records: list[EnforcementRecord] = []

        for threshold in decision.warnings_to_fire:
            self._notify_once(
                f"{rule.id}:warn:{threshold}",
                f"{rule.name}: {threshold // 60} min left" if threshold >= 60 else f"{rule.name}",
                decision.user_message or f"{rule.name}: {threshold}s remaining.",
            )

        if decision.new_state != RuleState.ENFORCED or not decision.action_to_execute:
            return records

        action = decision.action_to_execute

        if self.pause_provider is not None and self.pause_provider(rule):
            # Paused: counting already stopped, so do not close or block either —
            # but say so in the audit trail (once) instead of silently skipping.
            records.append(self._log_once(
                rule, day, action, "SKIPPED", "paused by the user",
            ))
            return records

        if action == "NOTIFY_ONLY":
            self._notify_once(
                f"{rule.id}:enforced", f"{rule.name}: limit reached",
                decision.user_message or f"{rule.name}: limit reached.", urgent=True,
            )
            records.append(self._log(rule, day, action, "EXECUTED", "warning only"))
            return records

        if action == "BLOCK_WEBSITE":
            # One push per rule per day is enough: the extension holds the
            # blocked state, and re-pushing every tick would bury the log.
            if self._browser_sink is None:
                records.append(self._log_once(
                    rule, day, action, "DEFERRED",
                    "no browser extension connected",
                ))
            else:
                ok = bool(self._browser_sink(rule, decision))
                records.append(self._log_once(
                    rule, day, action, "EXECUTED" if ok else "FAILED",
                    "rule pushed to browser",
                ))
            return records

        if action in ("CLOSE_APP", "PREVENT_LAUNCH"):
            records.extend(self._enforce_processes(rule, decision, action, snapshot, day))
            return records

        records.append(self._log(rule, day, action, "SKIPPED", "unknown action"))
        return records

    # ---------------------------------------------------------- process work
    def _enforce_processes(
        self, rule: Rule, decision: Decision, action: str, snapshot: SystemSnapshot, day: str
    ) -> list[EnforcementRecord]:
        assert rule.id is not None
        matched = snapshot.matched_pids(rule.executables)
        if not matched:
            # Limit is exceeded but the target is not running — nothing to do.
            # (STRICT rules act again the moment it reappears.) Logged once per
            # enforcement episode, not once per tick.
            return [self._log_once(
                rule, day, action, "SKIPPED", "target not running"
            )]

        # Grandfathering is a NORMAL/BLOCK idea only: "leave the instance the
        # user already had open alone, but allow no new launches". CLOSE means
        # close, and STRICT means no leniency — both act on every match.
        exempting = rule.mode == Mode.NORMAL and action == "PREVENT_LAUNCH"
        store = GrandpaStore(self._db, day)
        if exempting:
            grandfathered = store.load(rule.id)
            first = grandfathered is None
            if first:
                grandfathered = frozenset()
        else:
            grandfathered, first = frozenset(), False
            store.clear(rule.id)  # drop any earlier exemption for this rule

        kill = self._policy.decide(
            rule=rule, action=action, matched=matched,
            grandfathered=grandfathered, first_enforcement=first,
        )
        if exempting and first and kill.grandfathered:
            store.save(rule.id, frozenset(kill.grandfathered))

        records: list[EnforcementRecord] = []
        if kill.pids_to_close:
            started = self._closer.request(
                rule.id, rule.name, kill.pids_to_close, self._clock.mono()
            )
            if started:
                # New close requests only: a pending close escalates on its own
                # timeline in ProcessCloser.poll(), no need to re-log each tick.
                records.append(self._log(rule, day, action, "EXECUTED",
                                         kill.detail() or "no candidates"))
            else:
                records.append(self._log_once(
                    rule, day, action, "EXECUTED",
                    f"{kill.detail()}; already being closed (grace period running)",
                ))
            self._notify_once(
                f"{rule.id}:enforced", f"{rule.name}: limit reached",
                decision.user_message or f"{rule.name}: limit reached. Closing it now.",
                urgent=True,
            )
        elif kill.grandfathered:
            records.append(self._log(
                rule, day, action, "SKIPPED",
                f"{kill.detail()}; already-running instance left alone (NORMAL mode)",
            ))
        if kill.skipped:
            records.append(self._log(
                rule, day, action, "PROTECTED",
                "; ".join(f"{e}:{p}({r})" for e, p, r in kill.skipped),
            ))
        if not records:
            records.append(self._log_once(rule, day, action, "SKIPPED", "nothing to do"))
        return records

    # ---------------------------------------------------------------- helpers
    def _log(self, rule: Rule, day: str, action: str, outcome: str, detail: str) -> EnforcementRecord:
        assert rule.id is not None
        self._db.log_enforcement(rule.id, day, action, rule.mode.value, outcome, detail)
        if outcome == "EXECUTED":
            log.info("Enforced %s (%s): %s", rule.name, action, detail)
        else:
            log.info("Enforcement %s for %s (%s): %s", outcome, rule.name, action, detail)
        return EnforcementRecord(rule.id, rule.name, action, outcome, detail)

    def _log_once(
        self, rule: Rule, day: str, action: str, outcome: str, detail: str
    ) -> EnforcementRecord:
        """Log a no-op only when it is news.

        An exceeded rule ticks every second; writing "target not running" 3600
        times a day would bury the actions that actually happened. The audit
        trail records *changes*, not heartbeats.
        """
        assert rule.id is not None
        signature = (outcome, detail)
        if self._noop_signature.get(rule.id) == signature:
            return EnforcementRecord(rule.id, rule.name, action, outcome, detail)
        self._noop_signature[rule.id] = signature
        return self._log(rule, day, action, outcome, detail)

    def _notify_once(self, key: str, title: str, message: str, *, urgent: bool = False) -> bool:
        if key in self._notified:
            return False
        self._notified.add(key)
        try:
            self._notifier.notify(title, message, urgent=urgent)
        except Exception:  # noqa: BLE001 - a broken notifier must not stop enforcement
            log.warning("Notifier failed for %s.", title, exc_info=True)
        return True

    def set_browser_sink(self, sink: Callable[[Rule, Decision], bool] | None) -> None:
        """Late binding for the browser link (Phase 4 starts after this is built)."""
        self._browser_sink = sink

    def reset_day(self) -> None:
        """Called on local-day rollover so notifications can fire again."""
        self._notified.clear()
        self._noop_signature.clear()
