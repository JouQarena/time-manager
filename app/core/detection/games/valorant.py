"""VALORANT session detector — the honest one-process problem.

The question: *is a match running right now?* For VALORANT this is genuinely
hard at user level, and pretending otherwise would be the one dishonest thing
in this project:

**The menu and the match are the same process.** `VALORANT-Win64-Shipping.exe`
(the game, Unreal Engine, under the anti-cheat's protection) runs the main
menu, the range AND live matches. Unlike League of Legends there is no separate
"match process appears, match is certain" signal. Riot's own client API can
tell match state apart, but it is an authenticated private interface (dynamic
port + token from the Riot client) — out of scope for local read-only
detection and documented as future work.

So the verdict table trades decisiveness for safety:

| Observation | Verdict | Enforcement |
| --- | --- | --- |
| `VALORANT-Win64-Shipping.exe` is running (menu, range or match — cannot tell) | in session, confidence 0.5 | WAIT (never kills a maybe-match) |
| The game process just exited (< `settle_seconds`) — match end, crash or a menu-to-menu restart look identical | unknown, confidence 0.0 | WAIT |
| Match-shaped tokens still being written to `ShooterGame.log` although the process is not visible (stale process list) | in session, confidence 0.5 | WAIT |
| Only the Riot Client is running, game process gone for >= `settle_seconds` | **not** in session, confidence 1.0 | enforce |
| Nothing VALORANT/Riot-related is running | **not** in session, confidence 1.0 | enforce |

Practical consequence (documented, honest): a `WAIT_FOR_SESSION_END` VALORANT
rule enforces once the game is fully closed — not the moment a match ends —
because match-end cannot be verified while the process lives on in the menu.
Users who want the limit at match granularity keep the game closed between
matches by design of their own rules (BLOCK/CLOSE act immediately instead).

Vanguard (`vgc.exe`, `vgm.exe`, `vgtray.exe`) runs at boot whether or not
VALORANT is played — it is never treated as game evidence.
`ShooterGame.log` (`%LOCALAPPDATA%\\VALORANT\\Saved\\Logs\\`) is scanned for
tolerant match-lifecycle tokens; Riot does not document the format, so a hit
is *corroboration only* and can delay enforcement, never cause it.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict

log = logging.getLogger(__name__)

#: The one process: menu, range and matches alike.
GAME_EXES = ("valorant-win64-shipping.exe",)
GAME_EXE_PREFIXES = ("valorant-win",)  # older builds: VALORANT-Win.exe
#: The shared Riot launcher (present while browsing/patching, not a match).
CLIENT_EXES = ("riotclientservices.exe", "riotclientux.exe")
CLIENT_EXE_PREFIXES = ("riotclient",)

#: Vanguard runs at boot; seeing it means nothing about gameplay.
NON_SIGNAL_EXES = ("vgc.exe", "vgm.exe", "vgtray.exe")

#: Tolerant match-lifecycle tokens for the ShooterGame.log tail. Deliberately
#: conservative: these corroborate a live match, they never decide one.
MATCH_TOKENS = (
    "matchid",           # a match was provisioned/joined
    "gamemap",           # a map was loaded into a session
    "provisioningflow",  # matchmaking created a game
    "gamesession",
    "mapgamemode",
    "loadingmap",
)


def matches(exe: str, exact: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    exe = exe.lower()
    return exe in exact or any(exe.startswith(prefix) for prefix in prefixes)


class ShooterGameLogWatcher:
    """Are match-shaped tokens still being written to VALORANT's log?

    Reads the tail of `ShooterGame.log` (`%LOCALAPPDATA%\\VALORANT\\Saved\\Logs\\`)
    and reports True only when the *tail* contains a match-lifecycle token AND
    the file was modified within `fresh_seconds`. Riot does not document the
    log format, so this is evidence with a version-tolerance contract: any
    read failure is `None` ("no evidence"), never an exception.
    """

    def __init__(
        self,
        *,
        dirs: list[str | Path] | None = None,
        file_names: tuple[str, ...] = ("ShooterGame.log",),
        fresh_seconds: float = 120.0,
        tail_bytes: int = 128 * 1024,
        cache_seconds: float = 5.0,
        clock=time.monotonic,
        wall_clock=time.time,
        tokens: tuple[str, ...] = MATCH_TOKENS,
    ) -> None:
        if dirs is None:
            dirs = default_log_dirs()
        self.dirs = [Path(d) for d in dirs]
        self.file_names = file_names
        self.fresh_seconds = float(fresh_seconds)
        self.tail_bytes = int(tail_bytes)
        self.cache_seconds = float(cache_seconds)
        self._clock = clock
        self._wall = wall_clock
        self._tokens = tuple(t.lower() for t in tokens)
        self._cached: bool | None = None
        self._cached_at: float | None = None

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

    def is_fresh(self) -> bool | None:
        """True = fresh file with match tokens; False = no match signal;
        None = nothing readable at all."""
        now = self._clock()
        if self._cached_at is not None and (now - self._cached_at) < self.cache_seconds:
            return self._cached
        path = self.log_path()
        if path is None:
            self._cached, self._cached_at = None, now
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._cached, self._cached_at = None, now
            return None
        fresh_file = (self._wall() - mtime) <= self.fresh_seconds
        if not fresh_file:
            self._cached, self._cached_at = False, now
            return False
        tail = self.tail().lower()
        self._cached = any(token in tail for token in self._tokens)
        self._cached_at = now
        return self._cached


def default_log_dirs() -> list[Path]:
    """Where VALORANT writes `ShooterGame.log` (most likely first)."""
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if local:
        dirs.append(Path(local) / "VALORANT" / "Saved" / "Logs")
    dirs.append(Path.home() / ".local" / "share" / "VALORANT" / "Saved" / "Logs")
    return dirs


import os  # noqa: E402 - kept next to default_log_dirs for clarity


class ValorantDetector(GameSessionDetector):
    """Fail-safe VALORANT detector. See the module docstring for the truth table."""

    detector_id = "valorant"
    display_name = "VALORANT"
    # The game exe first: it is the target that must be closed for enforcement,
    # and the one a rule should list.
    known_executables = GAME_EXES + CLIENT_EXES

    def __init__(
        self,
        *,
        log_watcher: ShooterGameLogWatcher | None = None,
        settle_seconds: float = 30.0,
        log_scan_enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        self.log_watcher = log_watcher
        self.settle_seconds = float(settle_seconds)
        self.log_scan_enabled = bool(log_scan_enabled)
        self._clock = clock
        self._last_game_seen: float | None = None
        self.last_verdict: SessionVerdict | None = None

    # ------------------------------------------------------------------- probe
    def probe(self, snapshot: SessionProbe) -> SessionVerdict:
        verdict = self._verdict(snapshot)
        self.last_verdict = verdict
        return verdict

    def _verdict(self, snapshot: SessionProbe) -> SessionVerdict:
        exes = {exe.lower() for exe in (snapshot.running_exes or frozenset())}
        game_running = any(matches(e, GAME_EXES, GAME_EXE_PREFIXES) for e in exes)
        client_running = any(matches(e, CLIENT_EXES, CLIENT_EXE_PREFIXES) for e in exes)
        now = self._clock()

        if game_running:
            self._last_game_seen = now
            detail = ("game process is running (menu, range and matches share one "
                      "process — match state cannot be verified)")
            if self._logs_fresh():
                detail += " + match log tokens"
            return SessionVerdict(in_session=True, confidence=0.5, detail=detail)

        if not client_running:
            return SessionVerdict(in_session=False, confidence=1.0,
                                  detail="VALORANT is not running")

        settled, since = self._settled(now)
        if not settled:
            return SessionVerdict(
                in_session=True, confidence=0.0,
                detail=(f"game process exited {since:.0f}s ago — waiting out the "
                        f"reconnect window ({self.settle_seconds:.0f}s)"),
            )

        if self._logs_fresh():
            return SessionVerdict(
                in_session=True, confidence=0.5,
                detail="match tokens in ShooterGame.log although the game process is not visible",
            )

        return SessionVerdict(
            in_session=False, confidence=1.0,
            detail=f"launcher only — game process gone for over {self.settle_seconds:.0f}s",
        )

    # ---------------------------------------------------------------- evidence
    def _settled(self, now: float) -> tuple[bool, float]:
        if self._last_game_seen is None:
            return True, float("inf")
        since = max(0.0, now - self._last_game_seen)
        return since >= self.settle_seconds, since

    def _logs_fresh(self) -> bool:
        if not self.log_scan_enabled or self.log_watcher is None:
            return False
        try:
            return bool(self.log_watcher.is_fresh())
        except Exception as exc:  # noqa: BLE001 - evidence must never break a probe
            log.debug("VALORANT log signal failed (%s).", exc)
            return False

    # ---------------------------------------------------------------- helpers
    def describe(self) -> dict:
        return {
            "id": self.detector_id,
            "name": self.display_name,
            "settle_seconds": self.settle_seconds,
            "log_scan_enabled": self.log_scan_enabled,
            "last_verdict": None if self.last_verdict is None else {
                "in_session": self.last_verdict.in_session,
                "confidence": self.last_verdict.confidence,
                "detail": self.last_verdict.detail,
            },
        }
