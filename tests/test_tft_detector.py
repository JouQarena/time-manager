"""Phase 8: TFT detector — the shared-match-process problem.

The core safety property under test: TFT shares `League of Legends.exe` with
every other League mode. A TFT rule may therefore only enforce when the match
process is GONE — proving "this is an LoL match, not TFT" must still WAIT,
because enforcing would kill the sibling game.
"""

from app.core.clock import FakeClock
from app.core.detection.base import SessionProbe
from app.core.detection.games.signals import ClientPhaseReader, GameLogWatcher, LiveClientApi
from app.core.detection.games.teamfight_tactics import TftDetector

GAME = "league of legends.exe"
CLIENT = "leagueclientux.exe"


def probe(*exes):
    return SessionProbe(running_exes=frozenset(exes), hints={})


def api_serving(mode="TFT"):
    class FakeApi:
        def probe(self):
            return {"gameData": {"gameMode": mode, "gameTime": 100.0}}

    return FakeApi()


def detector(clock=None, *, api=None, reader=None, watcher=None):
    return TftDetector(live_api=api, phase_reader=reader, log_watcher=watcher,
                       settle_seconds=30, clock=clock.mono)


# ------------------------------------------------------------- the shared process
def test_nothing_running_is_decisive():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe())
    assert v.in_session is False and v.confidence == 1.0


def test_client_only_is_decisive_after_settle():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(CLIENT))
    assert v.in_session is False and v.confidence == 1.0
    assert "client only" in v.detail


def test_match_process_waits_even_when_it_is_another_mode():
    """THE TFT case: process up, live API says CLASSIC (an LoL match).
    Enforcing would kill the shared process -> WAIT, low confidence."""
    clock = FakeClock()
    d = detector(clock=clock, api=api_serving("CLASSIC"))
    v = d.probe(probe(GAME, CLIENT))
    assert v.in_session is True and v.confidence == 0.5
    assert "mode could not be verified" in v.detail


def test_match_process_with_no_api_waits_too():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(GAME, CLIENT))
    assert v.in_session is True and v.confidence == 0.5


def test_verified_tft_match_is_confident_wait():
    clock = FakeClock()
    d = detector(clock=clock, api=api_serving("TFT"))
    v = d.probe(probe(GAME, CLIENT))
    assert v.in_session is True and v.confidence == 1.0
    assert "TFT match running" in v.detail


def test_tft_token_matches_case_and_prefixes():
    clock = FakeClock()
    d = detector(clock=clock, api=api_serving("TFTDoubleUp"))
    v = d.probe(probe(GAME, CLIENT))
    assert v.in_session is True and v.confidence == 1.0


# ------------------------------------------------------------------ settle etc.
def test_match_exit_opens_the_reconnect_window():
    clock = FakeClock()
    d = detector(clock=clock)
    d.probe(probe(GAME, CLIENT))
    clock.advance(29)
    v = d.probe(probe(CLIENT))
    assert v.in_session is True and v.confidence == 0.0


def test_settled_client_enforces():
    clock = FakeClock()
    d = detector(clock=clock)
    d.probe(probe(GAME, CLIENT))
    clock.advance(31)
    v = d.probe(probe(CLIENT))
    assert v.in_session is False and v.confidence == 1.0


def test_loading_phase_waits(tmp_path):
    """TFT has no champ select; queue accept goes straight to a loading
    session. Any match-set-up phase in the client log -> WAIT."""
    import os

    clock = FakeClock()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "LeagueClientUx.log").write_text(
        '2026-09-21 20:00:00 | info | gameflow phase: InProgress\n', encoding='utf-8')
    reader = ClientPhaseReader(dirs=[logs], cache_seconds=0)
    d = detector(clock=clock, reader=reader)
    v = d.probe(probe(CLIENT))
    assert v.in_session is True and v.confidence == 0.5
    assert "inprogress" in v.detail


def test_fresh_game_logs_wait(tmp_path):
    import os

    clock = FakeClock()
    logs = tmp_path / "logs"
    (logs / "GameLogs").mkdir(parents=True)
    log = logs / "GameLogs" / "tft-session.log"
    log.write_text("match log\n", encoding='utf-8')
    stamp = clock.wall()
    os.utime(log, (stamp, stamp))
    watcher = GameLogWatcher(dirs=[logs], fresh_seconds=60, cache_seconds=0,
                             clock=clock.mono, wall_clock=clock.wall)
    d = detector(clock=clock, watcher=watcher)
    v = d.probe(probe(CLIENT))
    assert v.in_session is True and v.confidence == 0.5


def test_shared_exes_route_both_lol_and_tft():
    """Routing: a TFT rule's exe set intersects both detectors; the guard's
    fail-safe merge (tested in test_game_guard) probes them all."""
    from app.core.detection.registry import DetectorRegistry
    from app.core.detection.games.league_of_legends import LeagueOfLegendsDetector

    registry = DetectorRegistry()
    registry.register(LeagueOfLegendsDetector())
    registry.register(TftDetector())
    ids = [d.detector_id for d in registry.candidates_for((GAME, CLIENT))]
    assert ids == ["league_of_legends", "teamfight_tactics"]


def test_preset():
    preset = TftDetector().preset()
    assert preset["id"] == "teamfight_tactics"
    assert GAME in preset["executables"] and CLIENT in preset["executables"]
