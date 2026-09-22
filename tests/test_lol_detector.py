"""LeagueOfLegendsDetector: the verdict table, signal by signal.

The rule under test: only a *fully confident* "no match" may let the engine
act; everything else — reconnect windows, champ select, an API that answers
unexpectedly, a dead signal — keeps waiting.
"""

from datetime import datetime

import pytest

from app.core.clock import FakeClock
from app.core.detection.base import SessionProbe, SessionVerdict
from app.core.detection.games.league_of_legends import LeagueOfLegendsDetector
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.detection.registry import DetectorRegistry
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.models import Rule
from app.core.types import Action, RuleType

GAME = "league of legends.exe"
CLIENT = "leagueclientux.exe"


class FakeLiveApi:
    def __init__(self, data=None):
        self.data = data
        self.calls = 0

    def probe(self):
        self.calls += 1
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class FakeLogs:
    def __init__(self, fresh=None):
        self.fresh = fresh

    def is_fresh(self):
        if isinstance(self.fresh, Exception):
            raise self.fresh
        return self.fresh


class FakePhase:
    def __init__(self, phase=None):
        self.phase_value = phase

    def phase(self):
        if isinstance(self.phase_value, Exception):
            raise self.phase_value
        return self.phase_value

    def is_session_phase(self):
        from app.core.detection.games.signals import SESSION_PHASES

        phase = self.phase()
        if phase is None:
            return None
        return phase in SESSION_PHASES


def probe(*exes):
    return SessionProbe(running_exes=frozenset(exes), hints={})


def detector(**kw):
    clock = kw.pop("clock", None) or FakeClock()
    return LeagueOfLegendsDetector(clock=clock.mono, **kw), clock


def game_rule():
    """A realistic LoL rule: the client *and* the match process.

    (Executable matching is exact, so the rule has to list both — the guard
    only consults a detector while one of the rule's processes is running.)
    """
    rule = Rule(name="LoL", type=RuleType.GAME, target=CLIENT, executable=CLIENT,
                extra_executables=(GAME,), daily_limit_seconds=600,
                action=Action.WAIT_FOR_SESSION_END)
    rule.id = 1
    return rule


# --------------------------------------------------------------- decisive rows
def test_no_riot_processes_is_decisively_not_a_session():
    det, _ = detector()
    verdict = det.probe(probe())
    assert (verdict.in_session, verdict.confidence) == (False, 1.0)
    assert "not running" in verdict.detail


def test_match_process_running_is_decisively_a_session():
    det, _ = detector()
    verdict = det.probe(probe(GAME, CLIENT))
    assert (verdict.in_session, verdict.confidence) == (True, 1.0)
    assert "match process is running" in verdict.detail


def test_launcher_alone_is_decisively_not_a_session():
    det, clock = detector()
    det.probe(probe(GAME, CLIENT))          # a match was running...
    clock.advance(60)                        # ...and ended a minute ago
    verdict = det.probe(probe(CLIENT))
    assert (verdict.in_session, verdict.confidence) == (False, 1.0)
    assert "launcher only" in verdict.detail


def test_a_fresh_detector_that_never_saw_a_match_trusts_the_process_table():
    det, _ = detector()
    verdict = det.probe(probe(CLIENT))
    assert (verdict.in_session, verdict.confidence) == (False, 1.0)


# ------------------------------------------------------------- uncertain rows
def test_reconnect_window_is_unknown():
    det, clock = detector(settle_seconds=30)
    det.probe(probe(GAME, CLIENT))
    for step in (5, 10, 14):  # 5s, 15s, 29s after the match process vanished
        clock.advance(step)
        verdict = det.probe(probe(CLIENT))
        assert verdict.in_session is True and verdict.confidence == 0.0
        assert "reconnect window" in verdict.detail
    clock.advance(2)  # 31s: settled, safe to act
    assert det.probe(probe(CLIENT)).confidence == 1.0


def test_settle_window_can_be_disabled():
    det, clock = detector(settle_seconds=0)
    det.probe(probe(GAME, CLIENT))
    clock.advance(1)
    assert det.probe(probe(CLIENT)).confidence == 1.0


def test_live_api_answer_without_a_visible_process_keeps_waiting():
    det, _ = detector(live_api=FakeLiveApi({"gameData": {"gameTime": 120.0}}))
    verdict = det.probe(probe(CLIENT))
    assert (verdict.in_session, verdict.confidence) == (True, 0.5)
    assert "live game data" in verdict.detail


def test_champ_select_phase_keeps_waiting():
    det, _ = detector(phase_reader=FakePhase("champselect"))
    verdict = det.probe(probe(CLIENT))
    assert (verdict.in_session, verdict.confidence) == (True, 0.5)
    assert "champselect" in verdict.detail


def test_non_session_phase_does_not_block_enforcement():
    det, _ = detector(phase_reader=FakePhase("lobby"))
    assert det.probe(probe(CLIENT)).confidence == 1.0


def test_fresh_game_logs_keep_waiting():
    det, _ = detector(log_watcher=FakeLogs(True))
    verdict = det.probe(probe(CLIENT))
    assert (verdict.in_session, verdict.confidence) == (True, 0.5)
    assert "logs" in verdict.detail


def test_stale_logs_and_absent_logs_do_not_block():
    for fresh in (False, None):
        det, _ = detector(log_watcher=FakeLogs(fresh))
        assert det.probe(probe(CLIENT)).confidence == 1.0


# ------------------------------------------------------------- degraded signals
def test_every_signal_failing_still_answers_from_the_process_table():
    det, _ = detector(
        live_api=FakeLiveApi(OSError("refused")),
        log_watcher=FakeLogs(OSError("permission denied")),
        phase_reader=FakePhase(OSError("no log")),
    )
    assert det.probe(probe(GAME)).in_session is True       # ground truth wins
    det.probe(probe(CLIENT))                               # enter the settle window
    det._last_game_seen = None                             # (as if never seen)
    assert det.probe(probe(CLIENT)).confidence == 1.0


def test_signals_are_only_used_while_the_client_is_running():
    api = FakeLiveApi({"gameData": {}})
    det, _ = detector(live_api=api)
    det.probe(probe())                                     # nothing running
    assert api.calls == 0, "no Riot process: do not even ask the local API"


def test_disabling_signals_is_respected():
    api = FakeLiveApi({"gameData": {"gameTime": 1.0}})
    det, _ = detector(live_api=api, live_api_enabled=False)
    assert det.probe(probe(CLIENT)).confidence == 1.0
    assert api.calls == 0


def test_evidence_is_reported_on_the_in_session_verdict():
    det, _ = detector(live_api=FakeLiveApi({"gameData": {"gameTime": 300.0}}),
                      log_watcher=FakeLogs(True))
    verdict = det.probe(probe(GAME))
    assert "live game data" in verdict.detail and "game logs active" in verdict.detail


def test_probe_never_raises_even_with_hostile_signals():
    class Hostile:
        def __getattr__(self, item):
            raise RuntimeError("hostile signal")

    det, _ = detector(live_api=Hostile(), log_watcher=Hostile(), phase_reader=Hostile())
    for exes in ((), (GAME,), (CLIENT,)):
        verdict = det.probe(probe(*exes))
        assert isinstance(verdict, SessionVerdict)


def test_detector_retains_the_last_verdict_for_troubleshooting():
    det, _ = detector()
    det.probe(probe(GAME))
    described = det.describe()
    assert described["last_verdict"]["in_session"] is True
    det.probe(probe())
    assert det.describe()["last_verdict"]["in_session"] is False
    assert described["id"] == "league_of_legends"
    assert described["settle_seconds"] == 30.0


# ----------------------------------------------------------------- integration
def test_guard_and_state_machine_agree_end_to_end():
    """Mid-match limit -> WAIT; reconnect -> still WAIT; lobby -> ENFORCED."""
    from app.core.enforcement.state_machine import TickInput, tick
    from app.core.types import RuleState

    registry = DetectorRegistry()
    det, clock = detector(settle_seconds=30)
    registry.register(det)
    guard = GameSessionGuard(registry)

    rule = game_rule()
    snapshot = SystemSnapshot(
        processes=(ProcessInfo(pid=1, exe=GAME), ProcessInfo(pid=2, exe=CLIENT)),
        foreground_pid=1, captured_mono=clock.mono(),
    )
    verdict = guard.for_rule(rule, snapshot)
    assert (verdict.in_session, verdict.confident) == (True, True)
    decision = tick(TickInput(
        rule=rule, today_used_seconds=700, session_used_seconds=700,
        target_active_now=True, in_game_session=verdict.in_session,
        detector_confident=verdict.confident,
        now_local=datetime(2026, 9, 21, 20, 0), stored_state=RuleState.NORMAL,
    ))
    assert decision.new_state == RuleState.WAITING_FOR_SESSION_END
    assert decision.action_to_execute is None

    # Match over: the process disappears, we are inside the settle window.
    clock.advance(10)
    snapshot = SystemSnapshot(processes=(ProcessInfo(pid=2, exe=CLIENT),),
                              foreground_pid=2, captured_mono=clock.mono())
    verdict = guard.for_rule(rule, snapshot)
    assert verdict.confident is False
    decision = tick(TickInput(
        rule=rule, today_used_seconds=700, session_used_seconds=700,
        target_active_now=True, in_game_session=verdict.in_session,
        detector_confident=verdict.confident,
        now_local=datetime(2026, 9, 21, 20, 1),
        stored_state=RuleState.WAITING_FOR_SESSION_END,
    ))
    assert decision.new_state == RuleState.WAITING_FOR_SESSION_END
    assert decision.action_to_execute is None, "a reconnect window must never be closed"

    # Settled lobby: the engine may finally act.
    clock.advance(40)
    snapshot = SystemSnapshot(processes=(ProcessInfo(pid=2, exe=CLIENT),),
                              foreground_pid=2, captured_mono=clock.mono())
    verdict = guard.for_rule(rule, snapshot)
    assert (verdict.in_session, verdict.confident) == (False, True)
    decision = tick(TickInput(
        rule=rule, today_used_seconds=700, session_used_seconds=700,
        target_active_now=True, in_game_session=verdict.in_session,
        detector_confident=verdict.confident,
        now_local=datetime(2026, 9, 21, 20, 2),
        stored_state=RuleState.WAITING_FOR_SESSION_END,
    ))
    assert decision.new_state == RuleState.ENFORCED
    assert decision.action_to_execute


@pytest.mark.parametrize("exe", ["LEAGUE OF LEGENDS.EXE", "League of Legends.exe"])
def test_exe_matching_is_case_insensitive(exe):
    det, _ = detector()
    assert det.probe(probe(exe)).in_session is True


def test_renamed_clients_are_still_recognised():
    det, _ = detector()
    verdict = det.probe(probe("league of legends beta.exe"))
    assert verdict.in_session is True
    assert "match process" in verdict.detail
