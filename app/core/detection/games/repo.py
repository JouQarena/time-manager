"""R.E.P.O. session detector — honest coarseness, by the spec's own rule.

R.E.P.O. (semiwork, Unity, Steam) has **no public local indicator** that
separates "in the shop/menu" from "inside an active level": one process
(`REPO.exe`) runs everything, there is no game API, and `Player.log`
(`%USERPROFILE%\\AppData\\LocalLow\\semiwork\\Repo\\Player.log`) is a Unity
log with no documented, version-stable session contract (it is known to grow
enormously during play, which makes *freshness* weak evidence at best).

The spec is explicit about this case: when reliable session detection is not
technically possible with supported local signals, document the limitation and
fall back to the safest available behaviour — never pretend to be accurate.

| Observation | Verdict | Enforcement |
| --- | --- | --- |
| `REPO.exe` is running (menu, shop or level — cannot tell apart) | in session, confidence 0.5 | WAIT |
| The game process just exited (< `settle_seconds`) — a level end followed by a crash-to-desktop or a clean exit look identical | unknown, confidence 0.0 | WAIT |
| Only Steam is running, game gone for >= `settle_seconds` | **not** in session, confidence 1.0 | enforce |
| Nothing REPO/Steam-related is running | **not** in session, confidence 1.0 | enforce |

Practical consequence (documented): a `WAIT_FOR_SESSION_END` R.E.P.O. rule
enforces once the game is closed, not when the current level ends. That is the
*only* honest answer with public local signals; `BLOCK`/`CLOSE` rules act
immediately for users who prefer that trade-off.

Steam itself (`steam.exe`, `steamwebhelper.exe`) is never evidence of gameplay:
it runs whenever the user is in the library or a chat.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict

log = logging.getLogger(__name__)

GAME_EXES = ("repo.exe",)
LAUNCHER_EXES = ("steam.exe", "steamwebhelper.exe")


def default_log_dirs() -> list[Path]:
    """Unity's per-game log folder for R.E.P.O. (company `semiwork`)."""
    dirs: list[Path] = []
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    dirs.append(Path(profile) / "AppData" / "LocalLow" / "semiwork" / "Repo")
    dirs.append(Path.home() / ".config" / "unity3d" / "semiwork" / "Repo")
    return dirs


class PlayerLogWatcher:
    """Freshness of R.E.P.O.'s Unity `Player.log` — weak evidence, honestly.

    The game writes this log in the menu as well as in levels, so freshness
    alone can NOT distinguish a session. It is used only as corroborating
    detail on a verdict the process table already produced; a read failure is
    always `None`.
    """

    FILE_NAMES = ("Player.log", "output_log.txt")

    def __init__(
        self,
        *,
        dirs: list[str | Path] | None = None,
        fresh_seconds: float = 120.0,
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

    def log_path(self) -> Path | None:
        for directory in self.dirs:
            for name in self.FILE_NAMES:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        return None

    def is_fresh(self) -> bool | None:
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
        self._cached = (self._wall() - mtime) <= self.fresh_seconds
        self._cached_at = now
        return self._cached


class RepoDetector(GameSessionDetector):
    """Fail-safe R.E.P.O. detector: the process table is the only truth."""

    detector_id = "repo"
    display_name = "R.E.P.O."
    known_executables = GAME_EXES + LAUNCHER_EXES

    def __init__(
        self,
        *,
        log_watcher: PlayerLogWatcher | None = None,
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
        game_running = any(e in GAME_EXES for e in exes)
        launcher_running = any(e in LAUNCHER_EXES for e in exes)
        now = self._clock()

        if game_running:
            self._last_game_seen = now
            detail = ("game process is running (menu, shop and levels share one "
                      "process — no public local signal separates them)")
            if self._logs_fresh():
                detail += " + Player.log active"
            return SessionVerdict(in_session=True, confidence=0.5, detail=detail)

        settled, since = self._settled(now)
        if launcher_running and not settled:
            return SessionVerdict(
                in_session=True, confidence=0.0,
                detail=(f"game process exited {since:.0f}s ago — waiting out the "
                        f"settle window ({self.settle_seconds:.0f}s)"),
            )
        if settled:
            # Steam may or may not be up; with the game process gone and
            # settled there is no session to protect.
            reason = ("R.E.P.O. is not running" if not launcher_running
                      else f"game closed — Steam only, for over {self.settle_seconds:.0f}s")
            return SessionVerdict(in_session=False, confidence=1.0, detail=reason)
        # Not settled and no launcher seen: still protect the just-exited game.
        return SessionVerdict(
            in_session=True, confidence=0.0,
            detail=(f"game process exited {since:.0f}s ago — waiting out the "
                    f"settle window ({self.settle_seconds:.0f}s)"),
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
            log.debug("REPO log signal failed (%s).", exc)
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
