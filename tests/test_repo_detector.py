"""Phase 8: R.E.P.O. detector — honest coarseness (spec: document the limit).

Matrix: launcher (Steam), active level, post-level, closed, crash. The known
limitation under test: menu/shop vs level cannot be separated with public
local signals, so the game process itself is the safe session definition.
"""

from app.core.clock import FakeClock
from app.core.detection.base import SessionProbe
from app.core.detection.games.repo import PlayerLogWatcher, RepoDetector

GAME = "repo.exe"
STEAM = "steam.exe"


def probe(*exes):
    return SessionProbe(running_exes=frozenset(exes), hints={})


def detector(tmp_path=None, clock=None):
    watcher = None
    if tmp_path is not None:
        watcher = PlayerLogWatcher(dirs=[tmp_path], fresh_seconds=60,
                                   cache_seconds=0, clock=clock.mono,
                                   wall_clock=clock.wall)
    return RepoDetector(log_watcher=watcher, settle_seconds=30, clock=clock.mono)


def test_nothing_running_is_decisive():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe())
    assert v.in_session is False and v.confidence == 1.0
    assert "not running" in v.detail


def test_steam_alone_is_not_a_session():
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(STEAM))
    assert v.in_session is False and v.confidence == 1.0


def test_game_running_waits_menu_or_level_alike():
    """The documented limitation: shop/lobby vs active level is invisible, so
    the safe verdict is an uncertain in-session -> WAIT."""
    clock = FakeClock()
    v = detector(clock=clock).probe(probe(GAME, STEAM))
    assert v.in_session is True and v.confidence == 0.5
    assert "cannot be separated" in v.detail or "share one" in v.detail


def test_exit_inside_the_settle_window_waits():
    clock = FakeClock()
    d = detector(clock=clock)
    d.probe(probe(GAME, STEAM))
    clock.advance(10)
    v = d.probe(probe(STEAM))
    assert v.in_session is True and v.confidence == 0.0
    assert "settle window" in v.detail


def test_exit_without_steam_still_waits_out_the_window():
    """A crash that also took Steam down must not be mistaken for safe."""
    clock = FakeClock()
    d = detector(clock=clock)
    d.probe(probe(GAME))
    clock.advance(5)
    v = d.probe(probe())
    assert v.in_session is True and v.confidence == 0.0


def test_settled_game_is_enforceable_with_or_without_steam():
    clock = FakeClock()
    d = detector(clock=clock)
    d.probe(probe(GAME, STEAM))
    clock.advance(31)
    v = d.probe(probe(STEAM))
    assert v.in_session is False and v.confidence == 1.0
    assert "Steam only" in v.detail

    d2 = detector(clock=clock)
    d2.probe(probe(GAME))
    clock.advance(31)
    v2 = d2.probe(probe())
    assert v2.in_session is False and v2.confidence == 1.0


def test_player_log_freshness_is_detail_only(tmp_path):
    import os

    clock = FakeClock()
    log_file = tmp_path / "Player.log"
    log_file.write_text("unity spam\n")
    stamp = clock.wall()
    os.utime(log_file, (stamp, stamp))
    d = detector(tmp_path, clock)

    v = d.probe(probe(GAME, STEAM))
    assert "Player.log" in v.detail
    assert v.confidence == 0.5  # freshness never strengthens the verdict

    # Fresh logs without the process do NOT create a session (weak evidence
    # by design: Unity writes in the menu too). Past the settle window.
    clock.advance(31)
    v2 = d.probe(probe(STEAM))
    assert v2.in_session is False and v2.confidence == 1.0


def test_default_log_dirs_point_at_semiwork():
    dirs = [str(d) for d in RepoDetector.__mro__ and PlayerLogWatcher().dirs]
    assert any("semiwork" in d and "Repo" in d for d in dirs)


def test_preset():
    preset = RepoDetector().preset()
    assert preset["id"] == "repo" and GAME in preset["executables"]
