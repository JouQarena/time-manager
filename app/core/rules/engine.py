"""Rule engine: one tick = track every rule, evaluate limits, persist states.

Pure orchestration over Tracker + enforcement state machine + Database.
No threads, no Win32, no network — the Phase 3 monitor loop will call `tick()`
every `monitoring_interval` with live activity flags (foreground exe matches,
extension tab states, game-detector verdicts).

Schedule gating: a rule outside its schedule counts NOTHING (tracker sees it
as inactive) while the state machine still reports SCHEDULE_BLOCKED/DISABLED
status for the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.clock import Clock
from app.core.enforcement import state_machine as sm
from app.core.rules.models import Rule
from app.core.timeutils import local_day_str
from app.core.tracking.tracker import Activity, Tracker
from app.core.types import RuleState
from app.database.db import Database

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.security.floor import UsageFloor


@dataclass(frozen=True)
class ActivityInput:
    active: bool
    session_type: str = "FOREGROUND"  # SessionType value
    in_game_session: bool | None = None
    detector_confident: bool = True
    idle_blocked: bool = False  # informational: why nothing counted (UI/logs)


@dataclass(frozen=True)
class RuleOutcome:
    rule: Rule
    decision: sm.Decision
    today_used_seconds: int  # DB + unflushed (exact)
    session_used_seconds: int  # continuous open session (0 when idle)
    session_id: int | None
    tracked_active: bool


class RuleEngine:
    def __init__(
        self,
        db: Database,
        tracker: Tracker,
        clock: Clock,
        floor: "UsageFloor | None" = None,
    ) -> None:
        self._db = db
        self._tracker = tracker
        self._clock = clock
        #: Phase 7: usage floors for STRICT rules (None = feature off).
        self._floor = floor

    def tick(self, activities: dict[int, ActivityInput]) -> list[RuleOutcome]:
        now_local = self._clock.local_now()
        today = local_day_str(now_local)
        rules = [r for r in self._db.list_rules() if r.id is not None]

        effective: dict[int, Activity] = {}
        for rule in rules:
            assert rule.id is not None
            inp = activities.get(rule.id, ActivityInput(active=False))
            countable = (
                rule.enabled
                and inp.active
                and (rule.schedule is None or rule.schedule.is_active(now_local))
            )
            effective[rule.id] = Activity(active=countable, session_type=inp.session_type)

        infos = self._tracker.poll(effective)

        outcomes: list[RuleOutcome] = []
        for rule in rules:
            assert rule.id is not None
            inp = activities.get(rule.id, ActivityInput(active=False))
            info = infos.get(rule.id)
            session_used = info.session_seconds if info else 0
            session_id = info.session_id if info else None
            tracked = info.tracked_active if info else False
            today_used = self._tracker.today_total(rule.id, today)
            if self._floor is not None:
                # Phase 7: remember the true high-water mark, then evaluate
                # STRICT limits against max(stored, floor) so erased DB rows
                # cannot grant free time.
                self._floor.observe(rule.id, today, today_used)
                today_used = self._floor.raise_for(rule, today, today_used)

            stored = self._db.get_state(rule.id)
            if stored is None:
                stored_day, stored_state, warned = today, RuleState.NORMAL, frozenset()
            else:
                stored_day, stored_state, warned = stored

            decision = sm.tick(
                sm.TickInput(
                    rule=rule,
                    today_used_seconds=today_used,
                    session_used_seconds=session_used,
                    target_active_now=inp.active,
                    in_game_session=inp.in_game_session,
                    detector_confident=inp.detector_confident,
                    now_local=now_local,
                    stored_state=stored_state,
                    warned_thresholds=warned,
                    stored_day=stored_day,
                    today_day=today,
                )
            )
            self._db.put_state(
                rule.id, today, decision.new_state, warned | set(decision.warnings_to_fire)
            )
            outcomes.append(
                RuleOutcome(
                    rule=rule,
                    decision=decision,
                    today_used_seconds=today_used,
                    session_used_seconds=session_used,
                    session_id=session_id,
                    tracked_active=tracked,
                )
            )
        return outcomes
