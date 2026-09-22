"""Game session detector plugin interface.

Detectors answer ONE question: "is the user inside a live game session right
now?" They must fail safe: any uncertainty -> `confidence < 1.0` and the
enforcement engine will WAIT instead of killing the game.

Concrete detectors (Phase 6: LeagueOfLegendsDetector) live in
`app/core/detection/games/` and register via `registry.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionProbe:
    """Snapshot handed to a detector (built by the monitor in Phase 3)."""

    # Lowercased exe names currently running, e.g. {"leagueclient.exe", ...}.
    running_exes: frozenset[str]
    # Foreground process exe (lowercased) or None if unknown.
    foreground_exe: str | None = None
    # Free-form extra signals (window titles if title-logging enabled, child
    # process map, etc.). Detectors must treat every entry as optional.
    hints: dict | None = None


@dataclass(frozen=True)
class SessionVerdict:
    in_session: bool
    # 0.0..1.0. Enforcement kills only when in_session is decisive AND
    # confidence is 1.0; anything less -> keep waiting.
    confidence: float
    detail: str = ""  # e.g. "game client exe present", "launcher only"


class GameSessionDetector(ABC):
    """Interface every game detector implements."""

    #: Stable id used in logs/config, e.g. "league_of_legends".
    detector_id: str = "base"
    #: Human label.
    display_name: str = "Base detector"
    #: Exes this game is known to use (lowercase); used for candidate routing.
    known_executables: tuple[str, ...] = ()

    @abstractmethod
    def probe(self, snapshot: SessionProbe) -> SessionVerdict:
        """Return the current session verdict for this game."""
        raise NotImplementedError

    def preset(self) -> dict:
        """What the rule editor needs to offer this game as a preset."""
        return {
            "id": self.detector_id,
            "name": self.display_name,
            "executables": tuple(self.known_executables),
        }

    # Convenience alias matching the spec's sketch.
    def is_in_active_session(self, snapshot: SessionProbe) -> bool:
        """True only on a fully-confident in-session verdict (fail-safe)."""
        verdict = self.probe(snapshot)
        return bool(verdict.in_session and verdict.confidence >= 1.0)
