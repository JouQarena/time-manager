"""AgentBridge — glue between the IPC server, the database and the monitor.

Everything the browser needs to know is derived here from the *same* data the
enforcement engine uses (`enforcement_state`, `daily_usage`, rule schedules), so
the extension and the desktop can never disagree about what is blocked:

* `block_decision(domain)` is a read-only, sub-second-fresh answer for
  `BLOCK_QUERY` (the extension asks before the monitor's next tick).
* `on_tick(...)` is the authoritative path: after each monitor tick the bridge
  pushes `BLOCK_DECISION` on every blocked/unblocked transition and a throttled
  `RULE_UPDATE` with fresh remaining seconds for the popup.
* `browser_sink` is what `EnforcementExecutor` calls when a website rule
  reaches ENFORCED — it returns True only if the message actually went out,
  which is what lands in the `enforcement_log` as EXECUTED vs DEFERRED.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import __version__ as AGENT_VERSION
from app.core.clock import Clock
from app.core.rules.models import Rule
from app.core.rules.matcher import domain_matches
from app.core.timeutils import local_day_str
from app.core.types import LimitReason, RuleState, RuleType
from app.database.db import Database
from app.ipc import protocol
from app.ipc.browser_state import BrowserStateStore

log = logging.getLogger(__name__)

#: Cap for the "call me back in N seconds" hint sent to the extension.
MIN_REMAINING_POLL_SECONDS = 2.0
RULE_PUSH_MIN_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class BlockDecision:
    domain: str
    blocked: bool
    reason: str  # protocol.BLOCK_REASONS
    reset_at: str
    message: str
    remaining_seconds: int | None = None

    def to_message(self) -> str:
        return protocol.block_decision(
            domain=self.domain, blocked=self.blocked, reason=self.reason,
            reset_at=self.reset_at, message=self.message,
        )


def _midnight_after(now: datetime) -> str:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


class AgentBridge:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        token: str,
        *,
        tracker: object | None = None,
        broadcast: object | None = None,
        agent_version: str = AGENT_VERSION,
    ) -> None:
        self.db = db
        self.clock = clock
        self.token = token
        self.tracker = tracker
        self.browsers = BrowserStateStore()
        self._broadcast = broadcast  # set by IpcServer once it exists
        self.agent_version = agent_version
        self._last_rules_hash: str | None = None
        self._last_rules_push_mono: float = -1e9
        self._last_blocked: dict[str, bool] = {}

    def attach_broadcast(self, broadcast) -> None:
        self._broadcast = broadcast

    # ------------------------------------------------------------------ rules
    def website_rules(self) -> list[Rule]:
        return [r for r in self.db.list_rules() if r.type == RuleType.WEBSITE and r.id is not None]

    def _used_seconds(self, rule: Rule) -> int:
        assert rule.id is not None
        day = local_day_str(self.clock.local_now())
        if self.tracker is not None and hasattr(self.tracker, "today_total"):
            return int(self.tracker.today_total(rule.id, day))  # type: ignore[attr-defined]
        return int(self.db.get_daily(rule.id, day))

    def rule_status(self, rule: Rule, now: datetime | None = None) -> dict:
        """Single source of truth for "is this website rule blocking, and why".

        Both the `BLOCK_QUERY` reply and the `WELCOME`/`RULE_UPDATE` payload are
        derived from this, so the extension's pre-emptive check and the agent's
        push can never disagree.
        """
        assert rule.id is not None
        now = now or self.clock.local_now()
        day = local_day_str(now)
        used = self._used_seconds(rule)
        remaining = (
            None if rule.daily_limit_seconds is None
            else max(0, rule.daily_limit_seconds - used)
        )

        reason = "UNBLOCKED"
        blocked = False
        if not rule.enabled:
            reason = "UNBLOCKED"
        elif rule.schedule is not None and not rule.schedule.is_active(now):
            blocked, reason = True, "SCHEDULE_BLOCKED"
        else:
            stored = self.db.get_state(rule.id)
            enforced_today = bool(
                stored and stored[0] == day and stored[1] == RuleState.ENFORCED
            )
            # The engine's ENFORCED verdict outranks the counter: it is the
            # same signal the desktop UI shows.
            if enforced_today or (remaining == 0 and remaining is not None):
                blocked, reason = True, "DAILY_LIMIT_REACHED"

        return {
            "domain": rule.domain,
            "blocked": bool(blocked),
            "reason": reason,
            "remaining_seconds": remaining,
            "used_seconds": used,
            "limit_seconds": rule.daily_limit_seconds,
            "schedule": rule.schedule.to_json() if rule.schedule else None,
        }

    def rule_payload(self) -> list[dict]:
        """`website_rules` for WELCOME/RULE_UPDATE (domain + numbers only).

        Deliberately minimal: no rule ids beyond what the UI needs, no
        executable paths, no other rules — the extension only ever learns about
        the websites it must block. Extra keys are optional additions to v1
        (docs/PROTOCOL.md); `domain`/`blocked`/`remaining_seconds` are the
        contract the extension can rely on.
        """
        return [self.rule_status(rule) for rule in self.website_rules()]

    # ------------------------------------------------------- browser lifecycle
    #: All browser state is stamped with THIS agent's clock (never the
    #: extension's `timestamp`, never a second clock source) so tracking,
    #: freshness and the monitor all agree.
    def on_connect(self, browser: str, browser_id: str) -> None:
        self.browsers.connect(browser, browser_id, self.clock.mono())

    def on_disconnect(self, browser: str, browser_id: str) -> None:
        self.browsers.disconnect(browser, browser_id)

    def on_tab_activity(
        self, browser: str, browser_id: str, *, tab_id: int, domain: str,
        active: bool, window_focused: bool, audible: bool,
    ):
        return self.browsers.update(
            browser, browser_id, tab_id=tab_id, domain=domain, active=active,
            window_focused=window_focused, audible=audible, mono=self.clock.mono(),
        )

    def on_heartbeat(self, browser: str, browser_id: str) -> None:
        self.browsers.touch(browser, browser_id, self.clock.mono())

    # -------------------------------------------------------------- decisions
    def match_rule(self, domain: str) -> Rule | None:
        if not domain:
            return None
        for rule in self.website_rules():
            if rule.domain is not None and domain_matches(rule.domain, domain):
                return rule
        return None

    def block_decision(self, domain: str) -> BlockDecision:
        """Advisory, read-only answer for BLOCK_QUERY (never writes state)."""
        now = self.clock.local_now()
        rule = self.match_rule(domain)
        if rule is None or rule.id is None:
            return BlockDecision(domain, False, "UNBLOCKED", "", "")
        if not rule.enabled:
            return BlockDecision(domain, False, "UNBLOCKED", "", "", None)

        status = self.rule_status(rule, now)
        if status["blocked"]:
            reason = str(status["reason"])
            message = (
                f"{rule.name} is outside its allowed schedule."
                if reason == "SCHEDULE_BLOCKED"
                else f"Daily limit reached. You have used your allowed time for "
                     f"{rule.name} today."
            )
            return BlockDecision(
                domain, True, reason, _midnight_after(now), message,
                int(status["remaining_seconds"] or 0),
            )

        # A session limit can still bite while the daily budget is intact.
        session_limit = rule.session_limit_seconds
        if session_limit is not None and self.tracker is not None and hasattr(
            self.tracker, "session_seconds"
        ):
            session_used = int(self.tracker.session_seconds(rule.id))  # type: ignore[attr-defined]
            if session_used >= session_limit:
                return BlockDecision(
                    domain, True, "SESSION_LIMIT_REACHED", "",
                    f"Session limit reached for {rule.name}. Take a break.", 0,
                )

        remaining = status["remaining_seconds"]
        return BlockDecision(
            domain, False, "UNBLOCKED", "", "",
            None if remaining is None else int(remaining),
        )

    # ------------------------------------------------------------- from engine
    def browser_sink(self, rule: Rule, decision) -> bool:
        """`EnforcementExecutor` hook: push a block to every live browser.

        Called on every tick while the rule stays ENFORCED, so it must be
        idempotent: the extension holds the blocked state once told, and
        re-sending the same decision each second would be pure noise (the
        `enforcement_log` records the first push and nothing after it).
        """
        if rule.domain is None:
            return False
        if self._last_blocked.get(rule.domain) is True:
            return True  # already pushed; the browser is holding the block
        payload = BlockDecision(
            domain=rule.domain,
            blocked=True,
            reason=self._reason_for(decision),
            reset_at=_midnight_after(self.clock.local_now()),
            message=decision.user_message or f"{rule.name}: limit reached.",
            remaining_seconds=0,
        )
        return self.push(payload)

    @staticmethod
    def _reason_for(decision) -> str:
        reason = getattr(decision, "reason", LimitReason.NONE)
        if reason == LimitReason.SESSION_LIMIT_REACHED:
            return "SESSION_LIMIT_REACHED"
        if reason == LimitReason.SCHEDULE_BLOCKED:
            return "SCHEDULE_BLOCKED"
        if reason == LimitReason.MANUAL:
            return "MANUAL"
        return "DAILY_LIMIT_REACHED"

    def push(self, decision: BlockDecision) -> bool:
        if self._broadcast is None:
            return False
        sent = self._broadcast(decision.to_message())
        if sent:
            self._last_blocked[decision.domain] = decision.blocked
            log.info("Pushed %s for %s to %s browser(s).",
                     decision.reason, decision.domain, sent)
        return bool(sent)

    # ---------------------------------------------------------------- tick tap
    def on_tick(self, outcomes=None) -> dict:
        """Called by the monitor after each tick.

        * pushes BLOCK_DECISION on blocked/unblocked transitions,
        * pushes a throttled RULE_UPDATE when the payload actually changed,
        * prunes stale browser/tab state.
        """
        now_mono = self.clock.mono()
        self.browsers.prune(now_mono)

        # Blocked/unblocked transitions are computed from rule status (which
        # already folds in the engine's ENFORCED verdict), so a transition is
        # pushed on the tick it happens whether or not outcomes were handed in —
        # including the midnight unblock, when nothing else has changed yet.
        pushed_blocks = 0
        for rule in self.website_rules():
            if rule.domain is None:
                continue
            blocked = bool(self.rule_status(rule)["blocked"])
            previous = self._last_blocked.get(rule.domain)
            if previous is None:
                if blocked and self.push(self.block_decision(rule.domain)):
                    pushed_blocks += 1  # first sight of a blocked rule
            elif previous != blocked:
                if self.push(self.block_decision(rule.domain)):
                    pushed_blocks += 1
            self._last_blocked[rule.domain] = blocked

        payload = self.rule_payload()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        if digest != self._last_rules_hash and (
            now_mono - self._last_rules_push_mono >= RULE_PUSH_MIN_INTERVAL_SECONDS
        ):
            if self._broadcast is not None and self._broadcast(protocol.rule_update(payload)):
                self._last_rules_hash = digest
                self._last_rules_push_mono = now_mono
        return {"block_pushes": pushed_blocks, "rules_pushed": digest != self._last_rules_hash}

    # ------------------------------------------------------------------ helpers
    def web_state(self) -> dict[str, float] | None:
        return self.browsers.web_state(self.clock.mono())

    def status(self) -> dict:
        return {
            "agent_version": self.agent_version,
            "port": getattr(self._broadcast, "__self__", None) and getattr(
                getattr(self._broadcast, "__self__", None), "bound_port", None
            ),
            **self.browsers.summary(),
            "website_rules": len(self.website_rules()),
        }
