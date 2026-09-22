"""Detector registry: route a rule's exe set to the right detector."""

from __future__ import annotations

from app.core.detection.base import GameSessionDetector


class DetectorRegistry:
    def __init__(self) -> None:
        self._detectors: dict[str, GameSessionDetector] = {}

    def register(self, detector: GameSessionDetector) -> None:
        self._detectors[detector.detector_id] = detector

    def get(self, detector_id: str) -> GameSessionDetector | None:
        return self._detectors.get(detector_id)

    def for_executables(self, exes: tuple[str, ...] | list[str]) -> GameSessionDetector | None:
        """Best detector whose known exes intersect the rule's exes."""
        candidates = self.candidates_for(exes)
        return candidates[0] if candidates else None

    def candidates_for(self, exes: tuple[str, ...] | list[str]) -> list[GameSessionDetector]:
        """Every detector whose known exes intersect the rule's exes.

        Best overlap first. Games that share processes (League of Legends and
        Teamfight Tactics) return several candidates; `GameSessionGuard`
        probes them all and merges their verdicts fail-safe, so a shared
        process can never let the wrong detector decide a kill.
        """
        lowered = {e.lower() for e in exes}
        scored: list[tuple[int, int, GameSessionDetector]] = []
        for order, det in enumerate(self._detectors.values()):
            overlap = len(lowered & {e.lower() for e in det.known_executables})
            if overlap > 0:
                scored.append((overlap, order, det))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [det for _overlap, _order, det in scored]

    def ids(self) -> list[str]:
        return sorted(self._detectors)


# Global registry; game modules register on import (Phase 6).
registry = DetectorRegistry()
