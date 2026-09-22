"""Game-session correlation: turns detector plugins into the state machine's
`in_game_session` / `detector_confident` inputs.

THE fail-safe rule of this project lives here: if a detector cannot tell us
something with full confidence — it raised, timed out, or reported
`confidence < 1.0` — the answer is `(None, False)`, which makes the state
machine WAIT for session end instead of terminating a match in progress.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.detection.base import SessionProbe, SessionVerdict
from app.core.detection.registry import DetectorRegistry
from app.core.monitoring.processes import SystemSnapshot
from app.core.rules.models import Rule
from app.core.types import RuleType

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GameVerdict:
    in_session: bool | None  # None = unknown (n/a for non-game rules)
    confident: bool
    detail: str = ""

    @property
    def unknown(self) -> bool:
        return self.in_session is None or not self.confident


class GameSessionGuard:
    def __init__(self, registry: DetectorRegistry | None = None, *, enabled: bool = True) -> None:
        self._registry = registry or DetectorRegistry()
        self.enabled = enabled

    # ------------------------------------------------------------------ probe
    def probe_for(self, rule: Rule, snapshot: SystemSnapshot) -> SessionProbe:
        return SessionProbe(
            running_exes=snapshot.exes(),
            foreground_exe=snapshot.foreground_exe(),
            hints={"pids": {p.exe: p.pid for p in snapshot.processes}},
        )

    def for_rule(self, rule: Rule, snapshot: SystemSnapshot) -> GameVerdict:
        """Verdict for one rule. Never raises, never guesses.

        Several detectors may claim the same processes (League of Legends and
        Teamfight Tactics share the match process). Probing all candidates and
        merging fail-safe keeps the invariant: a verdict that would allow
        enforcement needs EVERY candidate to say "not in session, certain";
        any doubt on any detector means WAIT.
        """
        if rule.type != RuleType.GAME:
            return GameVerdict(in_session=None, confident=True, detail="not a game rule")
        if not self.enabled:
            return GameVerdict(in_session=None, confident=False, detail="game detector disabled")
        if not any(snapshot.has(exe) for exe in rule.executables):
            return GameVerdict(in_session=False, confident=True, detail="game not running")

        candidates = self._registry.candidates_for(rule.executables)
        if not candidates:
            # No plugin knows this game: we can never claim a live session, so
            # a limit is enforced on the game's own clock (documented).
            return GameVerdict(in_session=False, confident=True, detail="no detector registered")

        verdicts: list[SessionVerdict] = []
        for detector in candidates:
            try:
                verdicts.append(detector.probe(self.probe_for(rule, snapshot)))
            except Exception as exc:  # noqa: BLE001 - plugin code must never break us
                log.warning("Detector %s raised; treating session state as unknown.",
                            detector.detector_id, exc_info=True)
                verdicts.append(SessionVerdict(
                    in_session=True, confidence=0.0, detail=f"detector error: {exc!r}",
                ))

        details = [v.detail for v in verdicts if v.detail]
        joined = "; ".join(dict.fromkeys(details))[:300]
        if any(v.in_session and v.confidence >= 1.0 for v in verdicts):
            return GameVerdict(in_session=True, confident=True, detail=joined)
        if any(v.confidence < 1.0 for v in verdicts):
            # Any uncertainty (from any candidate) means WAIT, never kill.
            in_session = any(v.in_session for v in verdicts)
            return GameVerdict(in_session=True if in_session else None, confident=False,
                               detail=joined)
        return GameVerdict(in_session=False, confident=True, detail=joined)
