"""League of Legends session detector — the fail-safe reference plugin.

The question it answers: *is a match running right now?* (i.e. would closing
League of Legends interrupt a game the user is playing?)

Signals, strongest first:

| Observation | Verdict | Enforcement |
| --- | --- | --- |
| `League of Legends.exe` is running (that process only exists during a match: loading screen, in game, or a reconnect attempt) | in session, confidence 1.0 | WAIT |
| The match process just exited (within `settle_seconds`) — a reconnect or the post-match client hand-over looks exactly like this | unknown, confidence 0.0 | WAIT |
| Live Client Data API answers while no match process is visible (stale process list, a match we cannot see) | in session, confidence 0.5 | WAIT |
| Client log says `ChampSelect` / `GameStart` / `InProgress` / `Reconnect`… (match is being set up; the game exe is not up yet) | in session, confidence 0.5 | WAIT |
| Game logs are still being written although no match process is visible | in session, confidence 0.5 | WAIT |
| Client (`LeagueClientUx.exe`) up, no match process for longer than `settle_seconds`, nothing else happening | **not** in session, confidence 1.0 | enforce |
| Nothing Riot-related is running at all | **not** in session, confidence 1.0 | enforce |

Only the last two rows let the engine act, and both require the match process to
have been absent for a while. Everything the detector cannot verify becomes
"unknown", and unknown always means *wait* — the state machine holds and
re-polls instead of terminating a match.

Evidence from the Live Client Data API and the logs is used for *corroboration
and for extra caution* only: it can delay enforcement, never cause it.
"""

from __future__ import annotations

import logging
import time

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict

log = logging.getLogger(__name__)

#: The match process: present from the loading screen until the game ends.
GAME_EXES = ("league of legends.exe",)
#: The launcher/client and its helpers: present in lobby, queue and champ select.
CLIENT_EXES = ("leagueclientux.exe", "leagueclient.exe", "riotclientservices.exe")

GAME_EXE_PREFIXES = ("league of legends",)  # PBE/renamed clients
CLIENT_EXE_PREFIXES = ("leagueclient", "riotclient")


def matches(exe: str, exact: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    exe = exe.lower()
    return exe in exact or any(exe.startswith(prefix) for prefix in prefixes)


class LeagueOfLegendsDetector(GameSessionDetector):
    """Fail-safe LoL detector. See the module docstring for the verdict table."""

    detector_id = "league_of_legends"
    display_name = "League of Legends"
    # Client first: that is the process a rule should target (it is the one
    # that lingers after a match, so it is the one enforcement acts on).
    known_executables = CLIENT_EXES + GAME_EXES

    def __init__(
        self,
        *,
        live_api=None,
        log_watcher=None,
        phase_reader=None,
        settle_seconds: float = 30.0,
        live_api_enabled: bool = True,
        log_scan_enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        # Signals are injected so the detector is testable without a PC that
        # runs League of Legends; None means "that evidence is unavailable".
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
            evidence = self._evidence()
            detail = "match process is running"
            if evidence:
                detail += f" ({', '.join(evidence)})"
            return SessionVerdict(in_session=True, confidence=1.0, detail=detail)

        if not client_running:
            # Nothing Riot-related at all: no match can be running.
            return SessionVerdict(in_session=False, confidence=1.0,
                                  detail="League of Legends is not running")

        settled, since = self._settled(now)
        if not settled:
            return SessionVerdict(
                in_session=True, confidence=0.0,
                detail=(f"match process exited {since:.0f}s ago — waiting out the "
                        f"reconnect window ({self.settle_seconds:.0f}s)"),
            )

        # A live client API answer means *something* is running a match, even if
        # we cannot see the process (stale list, permissions, a second client).
        if self._live_game_data() is not None:
            return SessionVerdict(
                in_session=True, confidence=0.5,
                detail="live game data is available although the match process is not visible",
            )

        session_phase = self._session_phase()
        if session_phase:
            return SessionVerdict(
                in_session=True, confidence=0.5,
                detail=f"client phase {session_phase} — match is being set up",
            )

        if self._logs_fresh():
            return SessionVerdict(
                in_session=True, confidence=0.5,
                detail="game logs are still being written",
            )

        return SessionVerdict(
            in_session=False, confidence=1.0,
            detail=f"launcher only — no match process for over {self.settle_seconds:.0f}s",
        )

    # ---------------------------------------------------------------- evidence
    def _settled(self, now: float) -> tuple[bool, float]:
        """Have we been without the match process long enough to be sure?"""
        if self._last_game_seen is None:
            return True, float("inf")
        since = max(0.0, now - self._last_game_seen)
        return since >= self.settle_seconds, since

    def _safe(self, func, *args):
        """Call a signal provider (or fetch its attribute); failure = no evidence.

        `func` may be a zero-argument callable whose *body* does the attribute
        lookup, so even a hostile provider cannot raise out of a probe.
        """
        if func is None:
            return None
        try:
            return func(*args)
        except Exception as exc:  # noqa: BLE001 - evidence must never break a probe
            log.debug("Detector signal failed (%s).", exc)
            return None

    def _live_game_data(self) -> dict | None:
        if not self.live_api_enabled:
            return None
        if self.live_api is None:
            return None
        data = self._safe(lambda: self.live_api.probe())
        return data if isinstance(data, dict) and data else None

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

    def _evidence(self) -> list[str]:
        """Corroborating signals, for the audit log and troubleshooting."""
        evidence: list[str] = []
        if self._live_game_data() is not None:
            evidence.append("live game data")
        if self._logs_fresh():
            evidence.append("game logs active")
        return evidence

    # ---------------------------------------------------------------- helpers
    def describe(self) -> dict:
        """Small self-description for `--status-json` and troubleshooting."""
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
