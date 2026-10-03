"""Signal providers: the Live Client API, log freshness and client-phase parsing.

The Live Client API tests talk to a *real* local HTTP server (Riot's own API is
HTTPS on 127.0.0.1:2999; the client takes an injectable scheme/port so the tests
exercise real sockets without a certificate).
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core.detection.games.signals import (
    SESSION_PHASES,
    ClientPhaseReader,
    GameLogWatcher,
    LiveClientApi,
    default_log_dirs,
)

GAME_DATA = {
    "gameData": {"gameTime": 742.5, "mapName": "Map11"},
    "activePlayer": {"summonerName": "Someone"},
}


@pytest.fixture()
def live_api_server():
    """A local HTTP stand-in for the Live Client Data API."""
    state = {"status": 200, "body": GAME_DATA, "hits": 0, "delay": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            state["hits"] += 1
            if state["delay"]:
                time.sleep(state["delay"])
            if state["body"] is None:
                self.send_error(503, "no game")
                return
            payload = json.dumps(state["body"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # keep the test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, state
    finally:
        server.shutdown()
        server.server_close()


def api_for(server, **kw):
    port = server.server_address[1]
    return LiveClientApi(scheme="http", port=port, host="127.0.0.1", **kw)


# ------------------------------------------------------------- live client api
def test_live_api_returns_parsed_game_data(live_api_server):
    server, state = live_api_server
    api = api_for(server)
    data = api.all_game_data()
    assert data == GAME_DATA
    assert state["hits"] == 1
    assert api.game_time_seconds() == 742.5


def test_live_api_caches_within_the_window(live_api_server):
    server, state = live_api_server
    fake_now = {"t": 100.0}
    api = api_for(server, cache_seconds=2.0, clock=lambda: fake_now["t"])
    api.probe()
    api.probe()
    api.probe()
    assert state["hits"] == 1, "the monitoring loop must not hammer the game API"
    assert api.hits == 2

    fake_now["t"] += 3.0
    api.probe()
    assert state["hits"] == 2


def test_live_api_returns_none_when_no_game_is_running(live_api_server):
    server, state = live_api_server
    state["body"] = None  # Riot answers 503 outside a match
    api = api_for(server)
    assert api.all_game_data() is None
    assert api.game_time_seconds() is None


def test_live_api_returns_none_on_a_closed_port():
    api = LiveClientApi(scheme="http", host="127.0.0.1", port=1, timeout=0.2)
    assert api.all_game_data() is None  # refused, not raised


def test_live_api_survives_a_hanging_server_and_bad_payload(live_api_server):
    server, state = live_api_server
    state["delay"] = 0.3
    api = api_for(server, timeout=0.05)
    started = time.monotonic()
    assert api.all_game_data() is None
    assert time.monotonic() - started < 0.25, "the timeout must be honoured"

    state["delay"] = 0.0
    state["body"] = "not a dict"
    api2 = api_for(server)
    assert api2.all_game_data() is None


def test_live_api_reports_missing_game_time(live_api_server):
    server, state = live_api_server
    state["body"] = {"gameData": {}}
    api = api_for(server)
    assert api.game_time_seconds() is None
    assert api.all_game_data() == {"gameData": {}}


def test_live_api_url_is_loopback_only():
    api = LiveClientApi()
    assert api.url == "https://127.0.0.1:2999/liveclientdata/allgamedata"
    assert "0.0.0.0" not in api.url


# ------------------------------------------------------------------ log watcher
def test_watcher_reports_fresh_and_stale_logs(tmp_path):
    logs = tmp_path / "Logs"
    (logs / "GameLogs").mkdir(parents=True)
    log_file = logs / "GameLogs" / "2026-09-21T20-00-00.log"
    log_file.write_text("game started\n", encoding='utf-8')
    watcher = GameLogWatcher(dirs=[logs], fresh_seconds=90, cache_seconds=0)
    assert watcher.is_fresh() is True
    assert watcher.newest_log()[0] == log_file

    old = time.time() - 3600
    os.utime(log_file, (old, old))
    assert watcher.is_fresh() is False


def test_watcher_returns_none_without_logs(tmp_path):
    watcher = GameLogWatcher(dirs=[tmp_path / "nope"], cache_seconds=0)
    assert watcher.is_fresh() is None
    assert watcher.newest_log() is None
    assert watcher.log_dirs() == []


def test_watcher_picks_the_newest_log_across_directories(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    (b / "GameLogs").mkdir(parents=True)
    older = a / "client.log"
    newer = b / "GameLogs" / "game.log"
    older.write_text("x", encoding='utf-8')
    newer.write_text("y", encoding='utf-8')
    now = time.time()
    os.utime(older, (now - 500, now - 500))
    os.utime(newer, (now - 1, now - 1))
    watcher = GameLogWatcher(dirs=[a, b], fresh_seconds=90, cache_seconds=0)
    assert watcher.newest_log()[0] == newer
    assert watcher.is_fresh() is True


def test_watcher_caches_the_directory_scan(tmp_path):
    logs = tmp_path / "Logs"
    (logs / "GameLogs").mkdir(parents=True)
    log_file = logs / "GameLogs" / "x.log"
    log_file.write_text("x", encoding='utf-8')
    fake_now = {"t": 0.0}
    watcher = GameLogWatcher(dirs=[logs], cache_seconds=5.0, clock=lambda: fake_now["t"],
                             wall_clock=lambda: time.time())
    assert watcher.is_fresh() is True
    log_file.unlink()
    assert watcher.is_fresh() is True  # cached
    fake_now["t"] = 10.0
    assert watcher.is_fresh() is None  # rescanned


def test_a_fresh_client_log_is_not_match_evidence(tmp_path):
    """The client log is written in the lobby too: it must never mean "in game"."""
    logs = tmp_path / "Logs"
    (logs / "GameLogs").mkdir(parents=True)
    (logs / "LeagueClientUx.log").write_text('"phase":"Lobby"\n', encoding='utf-8')  # fresh, always
    watcher = GameLogWatcher(dirs=[logs], fresh_seconds=90, cache_seconds=0)
    assert watcher.newest_log() is None
    assert watcher.is_fresh() is None, "client log freshness would stall WAIT rules"
    # …and once the *match* log appears, the signal turns on.
    (logs / "GameLogs" / "match.log").write_text("loading\n", encoding='utf-8')
    assert watcher.is_fresh() is True


def test_watcher_accepts_nested_game_log_folders(tmp_path):
    logs = tmp_path / "Logs"
    nested = logs / "GameLogs" / "2026-09-21"
    nested.mkdir(parents=True)
    (nested / "r3dlog.txt.log").write_text("deep\n", encoding='utf-8')
    watcher = GameLogWatcher(dirs=[logs], fresh_seconds=90, cache_seconds=0)
    assert watcher.newest_log() is not None
    assert watcher.is_fresh() is True


def test_watcher_survives_an_unreadable_directory(tmp_path):
    class Unreadable(type(tmp_path)):
        def is_dir(self):
            return True

        def glob(self, pattern):
            raise OSError("access denied")

    watcher = GameLogWatcher(dirs=[Unreadable(tmp_path)], cache_seconds=0)
    assert watcher.is_fresh() is None


def test_default_log_dirs_include_windows_locations(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/Someone/AppData/Local")
    dirs = [str(d) for d in default_log_dirs()]
    assert any("Riot Games" in d and "League of Legends" in d for d in dirs)
    assert any(d.startswith("C:\\Riot Games") or d.startswith("C:/Riot Games") for d in dirs)


# ------------------------------------------------------------- phase reader
CLIENT_LOG_PHASES = [
    '2026-09-21 20:00:01 | info | GameFlowSession{"phase":"Lobby"}',
    '2026-09-21 20:05:12 | info | Gameflow phase: ChampSelect',
    '2026-09-21 20:06:40 | info | "phase": "InProgress"',
]


@pytest.mark.parametrize("line,expected", [
    ('"phase":"ChampSelect"', "champselect"),
    ("Gameflow phase: InProgress", "inprogress"),
    ("phase: Reconnect", "reconnect"),
    ('GameFlowPhase="WaitingForStats"', "waitingforstats"),
])
def test_phase_reader_accepts_several_log_shapes(tmp_path, line, expected):
    (tmp_path / "LeagueClientUx.log").write_text(line + "\n", encoding='utf-8')
    reader = ClientPhaseReader(dirs=[tmp_path], cache_seconds=0)
    assert reader.phase() == expected


def test_phase_reader_reads_the_last_phase(tmp_path):
    (tmp_path / "LeagueClientUx.log").write_text("\n".join(CLIENT_LOG_PHASES) + "\n")
    reader = ClientPhaseReader(dirs=[tmp_path], cache_seconds=0)
    assert reader.phase() == "inprogress"
    assert reader.is_session_phase() is True


def test_phase_reader_distinguishes_lobby_from_session(tmp_path):
    (tmp_path / "LeagueClientUx.log").write_text('   "phase" : "Lobby"\n', encoding='utf-8')
    reader = ClientPhaseReader(dirs=[tmp_path], cache_seconds=0)
    assert reader.phase() == "lobby"
    assert reader.is_session_phase() is False


def test_phase_reader_without_a_log(tmp_path):
    reader = ClientPhaseReader(dirs=[tmp_path], cache_seconds=0)
    assert reader.log_path() is None
    assert reader.phase() is None
    assert reader.is_session_phase() is None


def test_phase_reader_only_reads_the_tail(tmp_path):
    path = tmp_path / "LeagueClientUx.log"
    with path.open("w") as handle:
        handle.write('"phase":"ChampSelect"\n')
        handle.write("x" * (200 * 1024))  # pushes the interesting line out of the tail
        handle.write('\n"phase":"Lobby"\n')
    reader = ClientPhaseReader(dirs=[tmp_path], tail_bytes=4096, cache_seconds=0)
    assert reader.phase() == "lobby"


def test_phase_reader_caches_and_survives_errors(tmp_path):
    path = tmp_path / "LeagueClientUx.log"
    path.write_text('"phase":"InProgress"\n', encoding='utf-8')
    fake_now = {"t": 0.0}
    reader = ClientPhaseReader(dirs=[tmp_path], cache_seconds=5.0, clock=lambda: fake_now["t"])
    assert reader.phase() == "inprogress"
    path.write_text('"phase":"Lobby"\n', encoding='utf-8')
    assert reader.phase() == "inprogress"  # cached
    fake_now["t"] = 9.0
    assert reader.phase() == "lobby"

    path.unlink()
    fake_now["t"] = 20.0
    assert reader.phase() is None


def test_session_phases_cover_the_risky_transitions():
    for phase in ("champselect", "gamestart", "inprogress", "reconnect"):
        assert phase in SESSION_PHASES


def test_readers_never_touch_the_network(tmp_path, monkeypatch):
    """Only the live API may do I/O beyond the filesystem, and only on loopback."""
    def explode(*args, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("the log readers must not open sockets")

    monkeypatch.setattr("socket.socket.connect", explode)
    (tmp_path / "LeagueClientUx.log").write_text('"phase":"Lobby"\n', encoding='utf-8')
    ClientPhaseReader(dirs=[tmp_path], cache_seconds=0).phase()
    GameLogWatcher(dirs=[tmp_path], cache_seconds=0).is_fresh()
