"""GameSessionGuard: the fail-safe rules around killing a game."""

import pytest

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict
from app.core.detection.registry import DetectorRegistry
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.models import Rule
from app.core.types import Action, RuleType


class StubDetector(GameSessionDetector):
    detector_id = "stub"
    display_name = "Stub"
    known_executables = ("league of legends.exe",)

    def __init__(self, verdict=None, raises=False):
        self._verdict = verdict or SessionVerdict(in_session=False, confidence=1.0)
        self.raises = raises
        self.calls = 0

    def probe(self, snapshot):
        self.calls += 1
        if self.raises:
            raise RuntimeError("plugin exploded")
        return self._verdict


def snapshot(procs=(("league of legends.exe", 10),), fg=10):
    return SystemSnapshot(
        processes=tuple(ProcessInfo(pid=p, exe=e) for e, p in procs),
        foreground_pid=fg, captured_mono=1.0,
    )


def game_rule():
    r = Rule(name="LoL", type=RuleType.GAME, target="league of legends.exe",
             executable="league of legends.exe", daily_limit_seconds=600,
             action=Action.WAIT_FOR_SESSION_END)
    r.id = 1
    return r


def app_rule():
    r = Rule(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
             executable="discord.exe", daily_limit_seconds=600, action=Action.CLOSE)
    r.id = 2
    return r


def guard_with(detector):
    reg = DetectorRegistry()
    if detector is not None:
        reg.register(detector)
    return GameSessionGuard(reg)


def test_non_game_rule_is_not_a_session_question():
    v = guard_with(StubDetector()).for_rule(app_rule(), snapshot())
    assert v.in_session is None and v.confident is True


def test_game_not_running_is_confidently_not_in_session():
    detector = StubDetector()
    v = guard_with(detector).for_rule(game_rule(), snapshot(procs=()))
    assert v.in_session is False and v.confident is True
    assert detector.calls == 0  # no detector call when the game is not running


def test_confident_match():
    detector = StubDetector(SessionVerdict(in_session=True, confidence=1.0, detail="in game"))
    v = guard_with(detector).for_rule(game_rule(), snapshot())
    assert v.in_session is True and v.confident is True and v.detail == "in game"


def test_low_confidence_match_is_not_confident():
    """Claims a session but unsure -> enforcement must WAIT, never kill."""
    v = guard_with(StubDetector(SessionVerdict(in_session=True, confidence=0.6))).for_rule(
        game_rule(), snapshot())
    assert v.in_session is True and v.confident is False


def test_low_confidence_not_in_match_becomes_unknown():
    """Cannot rule a match out -> unknown -> WAIT."""
    v = guard_with(StubDetector(SessionVerdict(in_session=False, confidence=0.3))).for_rule(
        game_rule(), snapshot())
    assert v.in_session is None and v.confident is False


def test_detector_exception_is_unknown_not_a_kill():
    """A raising detector can never produce a kill: no confident verdict.

    Phase 8 uses the host's UNKNOWN convention (`in_session=True, confidence
    0.0`) for exceptions; what matters for safety is `confident=False` -> the
    state machine WAITs.
    """
    v = guard_with(StubDetector(raises=True)).for_rule(game_rule(), snapshot())
    assert v.in_session is True and v.confident is False
    assert "detector error" in v.detail


def test_no_detector_registered_enforces_on_the_game_clock():
    v = guard_with(None).for_rule(game_rule(), snapshot())
    assert v.in_session is False and v.confident is True
    assert "no detector" in v.detail


def test_detector_disabled_by_setting():
    v = GameSessionGuard(DetectorRegistry(), enabled=False).for_rule(game_rule(), snapshot())
    assert v.in_session is None and v.confident is False


def test_probe_carries_snapshot_facts():
    detector = StubDetector()
    guard = guard_with(detector)
    probe = guard.probe_for(game_rule(), snapshot(fg=10))
    assert "league of legends.exe" in probe.running_exes
    assert probe.foreground_exe == "league of legends.exe"
    assert probe.hints and probe.hints["pids"]["league of legends.exe"] == 10


# ------------------------------------------------------- Phase 8: shared exes
def test_shared_process_candidates_merge_fail_safe():
    """TFT + LoL share the match process: when EITHER detector waits, the
    merged verdict waits; only all-decisive-not-in-session may enforce."""
    from datetime import datetime

    from app.core.clock import FakeClock
    from app.core.detection.games.league_of_legends import LeagueOfLegendsDetector
    from app.core.detection.games.teamfight_tactics import TftDetector

    class FakeApi:
        def __init__(self, mode):
            self._mode = mode

        def probe(self):
            return {"gameData": {"gameMode": self._mode}}

    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 20, 0))
    registry = DetectorRegistry()
    registry.register(LeagueOfLegendsDetector(clock=clock.mono))
    registry.register(TftDetector(live_api=FakeApi("CLASSIC"), clock=clock.mono))

    tft_rule = Rule(
        name="TFT", type=RuleType.GAME, target="leagueclientux.exe",
        executable="leagueclientux.exe", extra_executables=("league of legends.exe",),
        daily_limit_seconds=3600, action=Action.WAIT_FOR_SESSION_END,
    )
    probe = SessionProbe(running_exes=frozenset({"league of legends.exe", "leagueclientux.exe"}))
    client_probe = SessionProbe(running_exes=frozenset({"leagueclientux.exe"}))

    # Match process running an LoL game: LoL says in-session 1.0, TFT says
    # in-session 0.5 -> merged must WAIT (confidently: one candidate has
    # decisive proof of a live session).
    v = GameSessionGuard(registry).for_rule(tft_rule, snapshot())
    assert v.in_session is True and v.confident is True

    # Process gone, client settled: BOTH decisive not-in-session -> enforce.
    lol_det = registry.get("league_of_legends")
    tft_det = registry.get("teamfight_tactics")
    lol_det.probe(client_probe)
    tft_det.probe(client_probe)
    clock.advance(31)
    lol_det.probe(client_probe)
    tft_det.probe(client_probe)
    settled = SystemSnapshot(
        processes=(ProcessInfo(pid=7, exe="leagueclientux.exe"),),
        foreground_pid=7, captured_mono=clock.mono())
    v2 = GameSessionGuard(registry).for_rule(tft_rule, settled)
    assert v2.in_session is False and v2.confident is True


def test_one_uncertain_candidate_blocks_enforcement():
    """If any candidate cannot rule a session out, the merge must WAIT."""
    class Doubtful(GameSessionDetector):
        detector_id = "doubtful"
        display_name = "Doubtful"
        known_executables = ("league of legends.exe",)

        def probe(self, snapshot):
            return SessionVerdict(in_session=False, confidence=0.4,
                                  detail="cannot rule out")

    class Sure(GameSessionDetector):
        detector_id = "sure"
        display_name = "Sure"
        known_executables = ("league of legends.exe",)

        def probe(self, snapshot):
            return SessionVerdict(in_session=False, confidence=1.0, detail="certain no")

    registry = DetectorRegistry()
    registry.register(Doubtful())
    registry.register(Sure())
    v = GameSessionGuard(registry).for_rule(game_rule(), snapshot())
    # (None, not confident) is the guard's established unknown shape -> WAIT.
    assert v.in_session is None and v.confident is False
    assert "cannot rule out" in v.detail
