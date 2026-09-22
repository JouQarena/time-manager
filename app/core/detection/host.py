"""Detector host: runs game-detector plugins inside a budget and a quarantine.

A game detector answers one question — "is a match running right now?" — and the
engine's fail-safe depends on it never lying. Plugins are third-party-ish code
(they can even be dropped in from the filesystem), so this module makes sure a
plugin can misbehave *without* turning into a kill:

| Plugin behaviour | Host response | Effect on enforcement |
| --- | --- | --- |
| returns a verdict | pass it through, update stats | normal |
| raises | catch, count, return "unknown" | WAIT (never kill) |
| hangs past the budget | abandon the thread, count, return "unknown" | WAIT |
| keeps failing | quarantine the plugin, return "unknown" | WAIT, plugin listed as quarantined |
| not loaded at all | never routed to (loader rejects it) | WAIT |

An "unknown" verdict is `SessionVerdict(in_session=True, confidence=0.0)`:
`GameSessionGuard` turns any non-1.0 confidence into `(unknown, not confident)`,
and the state machine holds `WAITING_FOR_SESSION_END` forever rather than
terminating a possibly-live match.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict
from app.core.detection.registry import DetectorRegistry

log = logging.getLogger(__name__)

#: What a caller gets when the host cannot trust a plugin's answer.
UNKNOWN_VERDICT = SessionVerdict(in_session=True, confidence=0.0, detail="unknown")


@dataclass
class DetectorStats:
    """Per-detector counters for the status surface."""

    detector_id: str
    display_name: str = ""
    source: str = "builtin"  # builtin | plugin
    origin: str = ""  # module file path for plugins
    known_executables: tuple[str, ...] = ()
    calls: int = 0
    errors: int = 0
    timeouts: int = 0
    abandoned: int = 0  # probe threads still running after the budget
    consecutive_failures: int = 0
    quarantined: bool = False
    last_seconds: float = 0.0
    last_in_session: bool | None = None
    last_confidence: float = 0.0
    last_detail: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.detector_id,
            "name": self.display_name or self.detector_id,
            "source": self.source,
            "origin": self.origin,
            "known_executables": list(self.known_executables),
            "calls": self.calls,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "abandoned": self.abandoned,
            "quarantined": self.quarantined,
            "healthy": not self.quarantined and self.errors == 0 and self.timeouts == 0,
            "last_seconds": round(self.last_seconds, 4),
            "last_in_session": self.last_in_session,
            "last_confidence": round(self.last_confidence, 2),
            "last_detail": self.last_detail,
        }


class ManagedDetector(GameSessionDetector):
    """A plugin wrapped in the host's budget, error handling and quarantine.

    It *is* a `GameSessionDetector`, so it is registered in the normal registry
    and the guard needs no knowledge of any of this.
    """

    def __init__(
        self,
        detector: GameSessionDetector,
        *,
        timeout_seconds: float = 0.3,
        max_failures: int = 5,
        source: str = "builtin",
    ) -> None:
        self.inner = detector
        self.timeout_seconds = float(timeout_seconds)
        self.max_failures = int(max_failures)
        self.stats = DetectorStats(
            detector_id=detector.detector_id,
            display_name=detector.display_name,
            source=source,
            origin=str(getattr(detector, "__module_file__", "")),
            known_executables=tuple(detector.known_executables),
        )
        # Reentrant: probe() holds the lock while calling _fail(), which also
        # takes it (a plain Lock deadlocks the monitor thread on the first
        # failure — i.e. exactly when a match must be protected).
        self._lock = threading.RLock()
        self._running: threading.Thread | None = None

    # -------------------------------------------------------------- identity
    @property
    def detector_id(self) -> str:  # type: ignore[override]
        return self.inner.detector_id

    @property
    def display_name(self) -> str:  # type: ignore[override]
        return self.inner.display_name

    @property
    def known_executables(self) -> tuple[str, ...]:  # type: ignore[override]
        return tuple(self.inner.known_executables)

    # ----------------------------------------------------------------- probes
    def probe(self, snapshot: SessionProbe) -> SessionVerdict:
        """Run the plugin under the budget. Never raises, never guesses."""
        with self._lock:
            if self.stats.quarantined:
                return SessionVerdict(
                    in_session=True, confidence=0.0,
                    detail=f"detector '{self.detector_id}' quarantined after "
                           f"{self.stats.consecutive_failures} failures",
                )
            if self._running is not None and self._running.is_alive():
                # The previous probe never came back; do not pile threads on top
                # of a wedged plugin — count it and let quarantine finish it.
                # (`abandoned` stays untouched: that thread was counted already.)
                return self._fail("previous probe is still running", timeout=True)

            result: dict = {}
            done = threading.Event()

            def run() -> None:
                try:
                    result["verdict"] = self.inner.probe(snapshot)
                except BaseException as exc:  # noqa: BLE001 - plugin code, any error
                    result["error"] = exc
                finally:
                    done.set()

            started = time.monotonic()
            thread = threading.Thread(target=run, name=f"detector-{self.detector_id}", daemon=True)
            self._running = thread
            thread.start()

        finished = done.wait(self.timeout_seconds)
        elapsed = time.monotonic() - started

        with self._lock:
            self.stats.calls += 1
            self.stats.last_seconds = elapsed
            if not finished:
                # The thread cannot be killed in Python; it is daemonised so a
                # wedged plugin can never block interpreter exit, and counted so
                # the status surface shows what happened.
                self.stats.abandoned += 1
                return self._fail(
                    f"probe exceeded {self.timeout_seconds * 1000:.0f} ms budget",
                    timeout=True, elapsed=elapsed,
                )
            self._running = None
            error = result.get("error")
            if error is not None:
                return self._fail(f"detector error: {type(error).__name__}: {error}", elapsed=elapsed)
            verdict = result.get("verdict")
            if not isinstance(verdict, SessionVerdict):
                return self._fail(f"detector returned {type(verdict).__name__}, expected SessionVerdict",
                                  elapsed=elapsed)

        with self._lock:
            self.stats.consecutive_failures = 0
            self.stats.last_in_session = bool(verdict.in_session)
            self.stats.last_confidence = float(verdict.confidence)
            self.stats.last_detail = verdict.detail
        return verdict

    # --------------------------------------------------------------- failures
    def _fail(self, detail: str, *, timeout: bool = False, elapsed: float = 0.0) -> SessionVerdict:
        """Record a failure and answer "unknown" (the caller keeps waiting)."""
        with self._lock:
            if timeout:
                self.stats.timeouts += 1
            else:
                self.stats.errors += 1
            self.stats.consecutive_failures += 1
            self.stats.last_seconds = elapsed or self.stats.last_seconds
            self.stats.last_in_session = None
            self.stats.last_confidence = 0.0
            failures = self.stats.consecutive_failures
            if failures >= self.max_failures and not self.stats.quarantined:
                self.stats.quarantined = True
                log.error(
                    "Detector '%s' quarantined after %s consecutive failures (%s). "
                    "Enforcement for its games will stay fail-safe (wait, never kill).",
                    self.detector_id, failures, detail,
                )
            else:
                log.warning("Detector '%s' failed (%s); treating as unknown.", self.detector_id, detail)
            self.stats.last_detail = detail
        return SessionVerdict(in_session=True, confidence=0.0, detail=detail)

    # -------------------------------------------------------------- lifecycle
    def release(self) -> None:
        """Clear the quarantine (used by `--detector-selftest` and by resume)."""
        with self._lock:
            self.stats.quarantined = False
            self.stats.consecutive_failures = 0


class DetectorHost:
    """Owns the detectors: registration, wiring to the guard, stats."""

    def __init__(
        self,
        registry: DetectorRegistry | None = None,
        *,
        timeout_ms: int = 300,
        max_failures: int = 5,
        enabled: bool = True,
    ) -> None:
        self.registry = registry if registry is not None else DetectorRegistry()
        self.timeout_ms = int(timeout_ms)
        self.max_failures = int(max_failures)
        self.enabled = bool(enabled)
        self._managed: dict[str, ManagedDetector] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ registering
    def register(self, detector: GameSessionDetector, *, source: str = "builtin") -> ManagedDetector:
        """Wrap a detector and add it to the registry under its own id.

        Raises ValueError for a duplicate id: two detectors answering the same
        question is a configuration error, not something to paper over.
        """
        if not getattr(detector, "detector_id", ""):
            raise ValueError("detector_id is required")
        with self._lock:
            if detector.detector_id in self._managed:
                raise ValueError(f"duplicate detector_id '{detector.detector_id}'")
        managed = ManagedDetector(
            detector,
            timeout_seconds=self.timeout_ms / 1000.0,
            max_failures=self.max_failures,
            source=source,
        )
        with self._lock:
            self._managed[managed.detector_id] = managed
        self.registry.register(managed)
        log.info("Game detector registered: %s (%s, %s exe(s))",
                 managed.detector_id, source, len(managed.known_executables))
        return managed

    def register_safe(self, detector: GameSessionDetector, *, source: str = "builtin"
                      ) -> tuple[ManagedDetector | None, str]:
        """Register, reporting instead of raising (used for untrusted plugins)."""
        try:
            return self.register(detector, source=source), ""
        except Exception as exc:  # noqa: BLE001 - caller decides what to do
            return None, str(exc)

    def register_all(self, detectors, *, source: str = "builtin"
                     ) -> tuple[list[ManagedDetector], list[tuple[str, str]]]:
        accepted: list[ManagedDetector] = []
        rejected: list[tuple[str, str]] = []
        for detector in detectors:
            managed, reason = self.register_safe(detector, source=source)
            if managed is not None:
                accepted.append(managed)
            else:
                rejected.append((getattr(detector, "detector_id", "?"), reason))
        return accepted, rejected

    def managed(self, detector_id: str) -> ManagedDetector | None:
        return self._managed.get(detector_id)

    def ids(self) -> list[str]:
        return sorted(self._managed)

    def configure(self, *, timeout_ms: int | None = None, max_failures: int | None = None) -> None:
        """Apply a new budget to every registered detector (settings changed)."""
        with self._lock:
            if timeout_ms is not None:
                self.timeout_ms = int(timeout_ms)
            if max_failures is not None:
                self.max_failures = int(max_failures)
            for managed in self._managed.values():
                managed.timeout_seconds = self.timeout_ms / 1000.0
                managed.max_failures = self.max_failures

    def release(self, detector_id: str | None = None) -> int:
        """Clear quarantine for one detector (or all). Returns how many."""
        with self._lock:
            targets = (
                [self._managed[detector_id]] if detector_id in self._managed else []
            ) if detector_id else list(self._managed.values())
        for managed in targets:
            managed.release()
        return len(targets)

    # ----------------------------------------------------------------- status
    def stats(self) -> list[DetectorStats]:
        with self._lock:
            return [self._managed[key].stats for key in sorted(self._managed)]

    def status(self) -> list[dict]:
        """JSON-safe per-detector status for `--status-json` and the GUI."""
        return [stat.to_dict() for stat in self.stats()]

    def summary(self) -> dict:
        with self._lock:
            stats = [self._managed[key].stats for key in sorted(self._managed)]
        return {
            "enabled": self.enabled,
            "count": len(stats),
            "quarantined": sum(1 for s in stats if s.quarantined),
            "errors": sum(s.errors for s in stats),
            "timeouts": sum(s.timeouts for s in stats),
            "calls": sum(s.calls for s in stats),
            "timeout_ms": self.timeout_ms,
        }

    def presets(self) -> list[dict]:
        """Known games, for the rule editor's "pick a game" list."""
        with self._lock:
            managed = [self._managed[key] for key in sorted(self._managed)]
        out: list[dict] = []
        for item in managed:
            preset = self._safe_preset(item)
            if preset is not None:
                out.append(preset)
        return out

    @staticmethod
    def _safe_preset(item) -> dict | None:
        try:
            return item.inner.preset()
        except Exception:  # noqa: BLE001 - a plugin without preset() is fine
            return {
                "id": item.detector_id, "name": item.display_name,
                "executables": tuple(item.known_executables),
            }

    def unhealthy(self) -> list[str]:
        """Ids of detectors that are quarantined (used for warnings/notifications)."""
        return [stat.detector_id for stat in self.stats() if stat.quarantined]
