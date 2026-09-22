"""Teamfight Tactics session detector — the shared-client problem, solved safely.

TFT is a game mode *inside* the League client ecosystem. It has no process of
its own:

- the lobby/queue lives in `LeagueClientUx.exe` (same as League of Legends),
- a live TFT match runs in `League of Legends.exe` — the exact same match
  process an LoL match uses.

The spec's warning is therefore the core design constraint: detecting the
League client (or even the match process) proves **nothing** about a TFT game
being active. Worse, the match process is *shared*: when `League of
Legends.exe` is running, it may be an LoL match, a TFT match, or the Training
Range — and enforcing a TFT rule by closing that process could kill a
sibling LoL match. The detector is fail-safe in exactly that direction:

| Observation | Verdict | Enforcement |
| --- | --- | --- |
| Match process running + live API says `gameMode` contains `tft` | in session, confidence 1.0 | WAIT |
| Match process running, mode not verifiable (no API answer / other mode — could be an LoL match sharing the process) | in session, confidence 0.5 | WAIT (never kills the shared process) |
| Match process just exited (< `settle_seconds`) | unknown, confidence 0.0 | WAIT |
| Client log shows a match-set-up phase (`InProgress`, `ReadyCheck`, `WaitingForStats`, ...) | in session, confidence 0.5 | WAIT |
| Fresh game logs although the process is not visible | in session, confidence 0.5 | WAIT |
| Client (`LeagueClientUx.exe`) alone, settled >= `settle_seconds` | **not** in session, confidence 1.0 | enforce |
| Nothing Riot-related is running | **not** in session, confidence 1.0 | enforce |

The deliberate cost, documented: while `League of Legends.exe` is alive for
*any* mode, a TFT `WAIT_FOR_SESSION_END` rule waits. Enforcement happens when
the shared match process is gone and the client has settled. That is the only
answer that can never kill the wrong game. (Riot's Live Client Data API on
`127.0.0.1:2999` reports `gameData.gameMode`; like all extra signals here it
can only push *towards* waiting, never towards enforcement.)
"""

from __future__ import annotations

import logging
import time

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict
from app.core.detection.games.league_of_legends import (
    CLIENT_EXES,
    CLIENT_EXE_PREFIXES,
    GAME_EXES,
    GAME_EXE_PREFIXES,
    matches,
)
from app.core.detection.games.signals import ClientPhaseReader, GameLogWatcher, LiveClientApi

log = logging.getLogger(__name__)

#: Live-API `gameData.gameMode` fragments that identify a TFT match.
TFT_MODE_TOKENS = ("tft",)


class TftDetector(GameSessionDetector):
    """Fail-safe TFT detector over the shared League-client processes."""

    detector_id = "teamfight_tactics"
    display_name = "Teamfight Tactics"
    # Same process family as League of Legends — routing sees both candidates
    # and GameSessionGuard merges their verdicts fail-safe (see game_guard.py).
    known_executables = CLIENT_EXES + GAME_EXES

    def __init__(
        self,
        *,
        live_api: LiveClientApi | None = None,
        log_watcher: GameLogWatcher | None = None,
        phase_reader: ClientPhaseReader | None = None,
        settle_seconds: float = 30.0,
        live_api_enabled: bool = True,
        log_scan_enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        self.live_api = live_api
        self.log_watcher = log_watcher
        self.phase_reader = phase_reader
        self.settle_seconds = float(settle_seconds)
        self.live_api_enabled = bool(live_api_enabled)
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
            mode = self._game_mode()
            if mode is not None and any(token in mode for token in TFT_MODE_TOKENS):
                return SessionVerdict(
                    in_session=True, confidence=1.0,
                    detail=f"TFT match running (live API gameMode {mode!r})",
                )
            # The shared match process may be an LoL match: closing it from a
            # TFT rule could kill the wrong game, so this stays a WAIT.
            detail = ("shared match process is running (LoL and TFT use the same "
                      "process; mode could not be verified as TFT)")
            if self._logs_fresh():
                detail += " + game logs active"
            return SessionVerdict(in_session=True, confidence=0.5, detail=detail)

        if not client_running:
            return SessionVerdict(in_session=False, confidence=1.0,
                                  detail="Teamfight Tactics is not running")

        settled, since = self._settled(now)
        if not settled:
            return SessionVerdict(
                in_session=True, confidence=0.0,
                detail=(f"match process exited {since:.0f}s ago — waiting out the "
                        f"reconnect window ({self.settle_seconds:.0f}s)"),
            )

        session_phase = self._session_phase()
        if session_phase:
            return SessionVerdict(
                in_session=True, confidence=0.5,
                detail=f"client phase {session_phase} — a match is being set up",
            )

        if self._logs_fresh():
            return SessionVerdict(
                in_session=True, confidence=0.5,
                detail="game logs are still being written",
            )

        return SessionVerdict(
            in_session=False, confidence=1.0,
            detail=f"client only — no match process for over {self.settle_seconds:.0f}s",
        )

    # ---------------------------------------------------------------- evidence
    def _settled(self, now: float) -> tuple[bool, float]:
        if self._last_game_seen is None:
            return True, float("inf")
        since = max(0.0, now - self._last_game_seen)
        return since >= self.settle_seconds, since

    def _safe(self, func):
        if func is None:
            return None
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - evidence must never break a probe
            log.debug("TFT detector signal failed (%s).", exc)
            return None

    def _game_mode(self) -> str | None:
        """`gameData.gameMode` from the Live Client API, lowercased, or None."""
        if not self.live_api_enabled or self.live_api is None:
            return None
        data = self._safe(lambda: self.live_api.probe())
        if not isinstance(data, dict):
            return None
        mode = (data.get("gameData") or {}).get("gameMode")
        return str(mode).lower() if mode else None

    def _session_phase(self) -> str | None:
        reader = self.phase_reader
        if reader is None:
            return None
        if self._safe(lambda: reader.is_session_phase()):
            return str(self._safe(lambda: reader.phase()) or "session")
        return None

    def _logs_fresh(self) -> bool:
        if not self.log_scan_enabled or self.log_watcher is None:
            return False
        return bool(self._safe(lambda: self.log_watcher.is_fresh()))

    # ---------------------------------------------------------------- helpers
    def describe(self) -> dict:
        return {
            "id": self.detector_id,
            "name": self.display_name,
            "settle_seconds": self.settle_seconds,
            "live_api_enabled": self.live_api_enabled,
            "log_scan_enabled": self.log_scan_enabled,
            "last_verdict": None if self.last_verdict is None else {
                "in_session": self.last_verdict.in_session,
                "confidence": self.last_verdict.confidence,
                "detail": self.last_verdict.detail,
            },
        }
