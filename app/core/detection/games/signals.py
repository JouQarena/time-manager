"""Local signals a game detector can use besides the process table.

Everything here is *optional evidence*. Each provider degrades to `None`
("signal unavailable") instead of raising, because a detector that dies is a
detector that stops protecting a match. None of this leaves the machine:

* `LiveClientApi` — Riot's own Live Client Data API on `https://127.0.0.1:2999`
  (self-signed certificate, live matches only, read-only).
* `GameLogWatcher` — freshness of the newest `Logs/GameLogs/*.log` file. Only
  the *match* log counts: the client log is written continuously, in the lobby
  as well, so it is not evidence of a game in progress.
* `ClientPhaseReader` — the last `GameFlowPhase`-ish token in the League client
  log tail, so a match that is loading (game exe not up yet) still reads as
  "in session, low confidence" -> WAIT.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
from pathlib import Path
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

#: Riot's fixed local port for the Live Client Data API.
DEFAULT_LIVE_API_PORT = 2999

#: Phases that mean "a match is under way or about to be" (LCU `GameFlowPhase`
#: values, lowercased). Enforcing during any of these can break a match.
SESSION_PHASES = frozenset({
    "champselect", "gameStart".lower(), "inprogress", "reconnect", "gamesession",
    "waitingforstats", "preendofgame", "endofgame", "terminatedinerror",
})


def default_log_dirs() -> list[Path]:
    """Places the League client/game logs live, most likely first."""
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if local:
        dirs.append(Path(local) / "Riot Games" / "League of Legends" / "Logs")
    programdata = os.environ.get("PROGRAMDATA")
    if programdata:
        dirs.append(Path(programdata) / "Riot Games" / "League of Legends" / "Logs")
    dirs.append(Path("C:/Riot Games/League of Legends/Logs"))
    dirs.append(Path.home() / ".local" / "share" / "League of Legends" / "Logs")
    return dirs


_TLS_CONTEXT: ssl.SSLContext | None = None


def _insecure_tls_context() -> ssl.SSLContext:
    """A verification-disabled TLS context, built exactly once per process.

    Thread-safe: SSLContext is designed to be shared across threads; wrapping
    sockets is the expensive part and that happens per request either way.
    """
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        _TLS_CONTEXT = context
    return _TLS_CONTEXT


class LiveClientApi:
    """Read-only client for Riot's local Live Client Data API.

    The API only answers while a match is live, uses a self-signed certificate
    and is bound to loopback. Answers are cached for a couple of seconds so the
    monitoring loop never blocks on it more than once per interval, and every
    failure is `None` — never an exception.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_LIVE_API_PORT,
        host: str = "127.0.0.1",
        scheme: str = "https",
        timeout: float = 0.25,
        cache_seconds: float = 2.0,
        clock=time.monotonic,
        opener=None,
    ) -> None:
        self.port = int(port)
        self.host = host
        self.scheme = scheme
        self.timeout = float(timeout)
        self.cache_seconds = float(cache_seconds)
        self._clock = clock
        self._opener = opener or self._default_opener
        self._cached: dict | None = None
        self._cached_at: float | None = None
        self.calls = 0
        self.hits = 0

    # ------------------------------------------------------------------ wiring
    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}/liveclientdata/allgamedata"

    def _default_opener(self, url: str, timeout: float):
        # Riot's certificate is self-signed and loopback-only; verification is
        # impossible and pointless here (any local process could bind the port,
        # which is why this signal is evidence, never authority). One shared
        # context: ssl.create_default_context() per probe loads the OS trust
        # store we never use, which alone blew the 200 ms probe budget on
        # Windows and quarantined the detector (found by the first real
        # Windows run of the suite).
        context = _insecure_tls_context()
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", "replace") or "{}")

    # ------------------------------------------------------------------- probe
    def all_game_data(self) -> dict | None:
        """The raw payload, or None when no live match is reachable."""
        now = self._clock()
        if self._cached_at is not None and (now - self._cached_at) < self.cache_seconds:
            self.hits += 1
            return self._cached
        self.calls += 1
        data: dict | None = None
        try:
            raw = self._opener(self.url, self.timeout)
            if isinstance(raw, dict):
                data = raw
        except Exception as exc:  # noqa: BLE001 - any failure means "no evidence"
            log.debug("Live Client API unreachable (%s).", exc)
        self._cached = data
        self._cached_at = now
        return data

    # Alias used by detectors (they only ask "is there a game?").
    def probe(self) -> dict | None:
        return self.all_game_data()

    def game_time_seconds(self) -> float | None:
        """In-game clock if a match is live (`gameData.gameTime`), else None."""
        data = self.all_game_data()
        if not isinstance(data, dict):
            return None
        value = (data.get("gameData") or {}).get("gameTime")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class GameLogWatcher:
    """Is the *match* still writing its log? (lobby = quiet, match = noisy)

    Only files under a `GameLogs` directory count. The League client log sits
    one level up and is written all the time — treating it as evidence would
    make a WAIT rule wait forever in the lobby.
    """

    def __init__(
        self,
        *,
        dirs: list[str | Path] | None = None,
        fresh_seconds: float = 90.0,
        cache_seconds: float = 5.0,
        clock=time.monotonic,
        wall_clock=time.time,
    ) -> None:
        self.dirs = [Path(d) for d in (dirs if dirs is not None else default_log_dirs())]
        self.fresh_seconds = float(fresh_seconds)
        self.cache_seconds = float(cache_seconds)
        self._clock = clock
        self._wall = wall_clock
        self._cached: bool | None = None
        self._cached_at: float | None = None

    def log_dirs(self) -> list[Path]:
        """Candidate directories that actually exist (game logs live below them)."""
        return [d for d in self.dirs if d.is_dir()]

    #: Match logs only (see the class docstring).
    PATTERNS = ("GameLogs/*.log", "GameLogs/*/*.log")

    def newest_log(self) -> tuple[Path, float] | None:
        """Newest match log below the candidate dirs: (path, wall mtime)."""
        newest: tuple[Path, float] | None = None
        for root in self.log_dirs():
            for pattern in self.PATTERNS:
                try:
                    candidates = list(root.glob(pattern))
                except OSError:  # pragma: no cover - unreadable dir
                    continue
                for path in candidates:
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if newest is None or mtime > newest[1]:
                        newest = (path, mtime)
        return newest

    def is_fresh(self) -> bool | None:
        """True/False when a log exists, None when there is nothing to read."""
        now = self._clock()
        if self._cached_at is not None and (now - self._cached_at) < self.cache_seconds:
            return self._cached
        newest = self.newest_log()
        if newest is None:
            self._cached = None
        else:
            self._cached = (self._wall() - newest[1]) <= self.fresh_seconds
        self._cached_at = now
        return self._cached


class ClientPhaseReader:
    """Last `GameFlowPhase` token in the League client log tail.

    Deliberately tolerant: Riot's log format is not a contract and changes
    between releases, so several shapes are accepted and an unreadable log is
    simply "no phase information" (the detector then falls back to the process
    table, which is the ground truth).
    """

    PHASE_PATTERNS = (
        re.compile(r'"phase"\s*:\s*"([A-Za-z ]{3,24})"', re.I),
        re.compile(r'gameflow[\s_-]*phase["\s:=]+([A-Za-z ]{3,24})', re.I),
        re.compile(r'phase[:\s]+([A-Za-z ]{3,24})', re.I),
    )

    def __init__(
        self,
        *,
        dirs: list[str | Path] | None = None,
        file_names: tuple[str, ...] = ("LeagueClientUx.log", "LeagueClient.log"),
        tail_bytes: int = 96 * 1024,
        cache_seconds: float = 5.0,
        clock=time.monotonic,
    ) -> None:
        self.dirs = [Path(d) for d in (dirs if dirs is not None else default_log_dirs())]
        self.file_names = file_names
        self.tail_bytes = int(tail_bytes)
        self.cache_seconds = float(cache_seconds)
        self._clock = clock
        self._cached: str | None = None
        self._cached_at: float | None = None

    # ------------------------------------------------------------------ reads
    def log_path(self) -> Path | None:
        for name in self.file_names:
            for directory in self.dirs:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        return None

    def tail(self) -> str:
        path = self.log_path()
        if path is None:
            return ""
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > self.tail_bytes:
                    handle.seek(size - self.tail_bytes)
                return handle.read().decode("utf-8", "replace")
        except OSError as exc:
            log.debug("Could not read %s (%s).", path, exc)
            return ""

    def phase(self) -> str | None:
        """Lowercased phase token ('champselect', 'inprogress', ...) or None."""
        now = self._clock()
        if self._cached_at is not None and (now - self._cached_at) < self.cache_seconds:
            return self._cached
        text = self.tail()
        # The *last* mention in the tail wins, no matter which pattern matched
        # it: the client appends as the phase progresses.
        best: tuple[int, str] | None = None
        for pattern in self.PHASE_PATTERNS:
            for match in pattern.finditer(text):
                token = str(match.group(1)).strip().strip('.,;').lower().replace(" ", "")
                if token and (best is None or match.end() > best[0]):
                    best = (match.end(), token)
        found = best[1] if best is not None else None
        self._cached = found
        self._cached_at = now
        return found

    def is_session_phase(self) -> bool | None:
        """True/False for a known phase, None when there is no phase to read."""
        phase = self.phase()
        if phase is None:
            return None
        if phase in SESSION_PHASES:
            return True
        return False
