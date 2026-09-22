"""Phase 8: VALORANT detector — the one-process (menu == match) problem.

Matrix (mirrors docs/PHASE8.md): launcher/client, loading, active match,
post-match, closed, crash — each against the fail-safe rules.
"""

import pytest

from app.core.clock import FakeClock
from app.core.detection.base import SessionProbe
from app.core.detection.games.valorant import (
    ShooterGameLogWatcher,
    ValorantDetector,
)

GAME = "valorant-win64-shipping.exe"
LEGACY = "valorant-win.exe"
CLIENT = "riotclientservices.exe"
VANGUARD = "vgc.exe"          # runs at boot: never evidence


def probe(*exes):
    return SessionProbe(running_exes=frozenset(exes), hints={})


def detector(tmp_path=None, clock=None, **kw):
    watcher = None
    if tmp_path is not None:
        watcher = ShooterGameLogWatcher(
            dirs=[tmp_path], fresh_seconds=60, cache_seconds=0,
            clock=clock.mono, wall_clock=clock.wall,
        )
    return ValorantDetector(log_watcher=watcher, settle_seconds=30, clock=clock.mono, **kw)


# ------------------------------------------------------------------ process table
def test_nothing_running_is_decisively_not_a_session():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe())
    assert v.in_session is False and v.confidence == 1.0


def test_vanguard_alone_is_not_evidence_of_anything():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(VANGUARD))
    assert v.in_session is False and v.confidence == 1.0


def test_launcher_only_is_decisive():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(CLIENT))
    assert v.in_session is False and v.confidence == 1.0
    assert "launcher only" in v.detail


def test_game_process_means_wait_even_without_match_evidence():
    """THE VALORANT case: menu and match share a process, so presence alone
    can never be decisive -> low confidence -> the engine WAITs."""
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(GAME, CLIENT))
    assert v.in_session is True and v.confidence == 0.5
    assert "cannot be verified" in v.detail


def test_legacy_process_name_is_matched():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(LEGACY))
    assert v.in_session is True and v.confidence == 0.5


# ---------------------------------------------------------------------- settle
def test_game_exit_opens_the_reconnect_window():
    clock = FakeClock()
    d = detector(clock=clock)
    d.probe(probe(GAME, CLIENT))
    clock.advance(29)
    v = d.probe(probe(CLIENT))
    assert v.in_session is True and v.confidence == 0.0
    assert "reconnect window" in v.detail


def test_settled_launcher_is_enforceable():
    clock = FakeClock()
    d = detector(clock=clock)
    d.probe(probe(GAME, CLIENT))
    clock.advance(31)
    v = d.probe(probe(CLIENT))
    assert v.in_session is False and v.confidence == 1.0


def test_never_seen_game_settles_immediately():
    clock = FakeClock()
    d = detector(clock=clock)
    clock.advance(1000)
    v = d.probe(probe(CLIENT))
    assert v.in_session is False and v.confidence == 1.0


# ------------------------------------------------------------------ log evidence
def test_match_tokens_in_a_fresh_log_corroborate(tmp_path):
    clock = FakeClock()
    log_file = tmp_path / "ShooterGame.log"
    log_file.write_text("2026-09-21 MatchID: 1234-abcd GameMap: /Game/Maps/Ascent\n")
    stamp = clock.wall()
    import os
    os.utime(log_file, (stamp, stamp))

    d = detector(tmp_path, clock)
    v = d.probe(probe(CLIENT))  # process not visible, log says a match lives
    assert v.in_session is True and v.confidence == 0.5
    assert "ShooterGame.log" in v.detail

    # Evidence can only ever *add caution*: with the process visible the
    # verdict stays a 0.5 WAIT, never a stronger claim.
    v2 = d.probe(probe(GAME, CLIENT))
    assert v2.in_session is True and v2.confidence == 0.5


def test_quiet_or_stale_log_is_not_evidence(tmp_path):
    clock = FakeClock()
    log_file = tmp_path / "ShooterGame.log"
    log_file.write_text("menu stuff, no match tokens here\n")
    stamp = clock.wall()
    import os
    os.utime(log_file, (stamp, stamp))
    d = detector(tmp_path, clock)
    assert d.probe(probe(CLIENT)).confidence == 1.0  # no tokens -> no evidence

    clock.advance(3600)
    log_file.write_text("MatchID: old-but-freshly-written\n")
    stamp = clock.wall()
    os.utime(log_file, (stamp, stamp))
    d2 = detector(tmp_path, clock)
    d2.probe(probe(GAME, CLIENT))
    clock.advance(3600)  # log now stale relative to the fresh window
    assert d2.probe(probe(CLIENT)).confidence == 1.0


def test_crashed_game_gets_the_same_protection(tmp_path):
    """A crash (process gone, log tokens fresh) must WAIT, not enforce."""
    clock = FakeClock()
    log_file = tmp_path / "ShooterGame.log"
    log_file.write_text("MatchID: 42\n")
    d = detector(tmp_path, clock)
    d.probe(probe(GAME, CLIENT))
    clock.advance(31)  # past settle...
    log_file.write_text("MatchID: 42 still writing\n")
    stamp = clock.wall()
    import os
    os.utime(log_file, (stamp, stamp))
    v = d.probe(probe(CLIENT))
    assert v.in_session is True and v.confidence == 0.5


def test_log_signal_failure_never_breaks_the_probe(tmp_path):
    class Boom:
        def is_fresh(self):
            raise RuntimeError("disk on fire")

    clock = FakeClock()
    d = ValorantDetector(log_watcher=Boom(), settle_seconds=30, clock=clock.mono)
    v = d.probe(probe(CLIENT))
    assert v.in_session is False and v.confidence == 1.0


def test_preset_and_describe():
    d = ValorantDetector()
    preset = d.preset()
    assert preset["id"] == "valorant" and GAME in preset["executables"]
    assert GAME in d.known_executables and CLIENT in d.known_executables
    assert "last_verdict" in d.describe()
