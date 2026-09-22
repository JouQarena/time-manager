"""Detector plugin loading: built-ins plus optional drop-in plugins.

Built-in detectors live in `app/core/detection/games/`. Users (and the future
"bring your own game" feature) can drop a `*.py` file into a plugin directory —
`<profile>/detectors` by default, or whatever `detector_plugins_dir` says.

A plugin file exposes a detector in any of three ways (checked in order):

```python
def create() -> GameSessionDetector: ...       # preferred
DETECTORS = [MyDetector()]                     # a list/tuple of instances
class MyDetector(GameSessionDetector): ...     # or just define the class
```

The third form instantiates every concrete `GameSessionDetector` subclass that
is defined *in that file* (imported ones are ignored) with no arguments.

Everything about loading is fail-safe and reported: a plugin that cannot be
imported, that does not implement the interface, that reuses another detector's
id, that has no executables, or that raises during a smoke probe is **rejected
with a reason**. Rejection is always better than a detector that might say "no
match" while a match is running, so the bar for accepting a plugin is high.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict
from app.core.detection.host import DetectorHost

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginReport:
    """What happened while loading detectors."""

    accepted: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()  # (origin, reason)
    directories: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "accepted": list(self.accepted),
            "rejected": [{"origin": origin, "reason": reason} for origin, reason in self.rejected],
            "directories": list(self.directories),
        }


def builtin_detectors() -> list[GameSessionDetector]:
    """Every detector that ships with the agent."""
    from app.core.detection.games.league_of_legends import LeagueOfLegendsDetector
    from app.core.detection.games.repo import RepoDetector
    from app.core.detection.games.teamfight_tactics import TftDetector
    from app.core.detection.games.valorant import ValorantDetector

    return [
        LeagueOfLegendsDetector(),
        ValorantDetector(),
        RepoDetector(),
        TftDetector(),
    ]


# --------------------------------------------------------------------- loading
def _smoke_probe(detector: GameSessionDetector) -> str | None:
    """Probe once with an empty snapshot; the reason it is unusable, or None.

    Catches the common plugin mistakes early (attribute errors in `probe`, a
    detector that needs signals it cannot build) instead of at the first real
    game — where the cost of a wrong answer is a killed match.
    """
    empty = SessionProbe(running_exes=frozenset(), foreground_exe=None, hints={})
    try:
        verdict = detector.probe(empty)
    except Exception as exc:  # noqa: BLE001 - plugin code
        return f"probe() raised on an empty snapshot: {type(exc).__name__}: {exc}"
    if not isinstance(verdict, SessionVerdict):
        return f"probe() returned {type(verdict).__name__}, expected SessionVerdict"
    if not 0.0 <= float(verdict.confidence) <= 1.0:
        return f"confidence {verdict.confidence!r} is outside 0..1"
    return None


def _validate(candidate: object, origin: str, seen_ids: set[str]) -> tuple[
        GameSessionDetector | None, str]:
    if not isinstance(candidate, GameSessionDetector):
        return None, "does not implement GameSessionDetector"
    detector_id = getattr(candidate, "detector_id", "") or ""
    if not detector_id or detector_id == "base":
        return None, "detector_id is empty"
    if detector_id in seen_ids:
        return None, f"detector_id '{detector_id}' is already registered"
    exes = tuple(getattr(candidate, "known_executables", ()) or ())
    if not exes:
        return None, "known_executables is empty (nothing would ever route to it)"
    if any(not isinstance(e, str) or not e.strip() for e in exes):
        return None, "known_executables must be non-empty strings"
    if not all(e == e.lower() for e in exes):
        return None, "known_executables must be lowercase image names"
    reason = _smoke_probe(candidate)
    if reason is not None:
        return None, reason
    return candidate, ""


def load_plugin_file(path: str | Path, *, seen_ids: set[str] | None = None) -> tuple[
        list[GameSessionDetector], str]:
    """Import one plugin file. Returns (detectors, rejection_reason)."""
    path = Path(path)
    seen_ids = seen_ids if seen_ids is not None else set()
    module_name = f"tm_detector_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return [], "not an importable Python file"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - plugin code
        sys.modules.pop(module_name, None)
        return [], f"import failed: {type(exc).__name__}: {exc}"

    candidates: list[object] = []
    factory = getattr(module, "create", None)
    if callable(factory):
        try:
            candidates.append(factory())
        except Exception as exc:  # noqa: BLE001 - plugin code
            return [], f"create() raised: {type(exc).__name__}: {exc}"
    declared = getattr(module, "DETECTORS", None)
    if isinstance(declared, (list, tuple)):
        candidates.extend(declared)
    elif declared is not None and not callable(factory):
        return [], "DETECTORS must be a list or tuple of detector instances"
    if not candidates:
        # Third form: concrete detector classes defined in this very file.
        for name, obj in list(vars(module).items()):
            if not isinstance(obj, type) or obj is GameSessionDetector:
                continue
            if not issubclass(obj, GameSessionDetector):
                continue
            if getattr(obj, "__abstractmethods__", None):
                continue  # still abstract: nothing to instantiate
            if obj.__module__ != module_name:
                continue  # imported for reuse, not declared here
            try:
                candidates.append(obj())
            except Exception as exc:  # noqa: BLE001 - plugin code
                return [], f"{name}() could not be constructed: {type(exc).__name__}: {exc}"
    if not candidates:
        return [], ("no create() function, no DETECTORS list and no "
                    "GameSessionDetector subclass in this file")

    accepted: list[GameSessionDetector] = []
    reasons: list[str] = []
    for candidate in candidates:
        detector, reason = _validate(candidate, str(path), seen_ids)
        if detector is None:
            reasons.append(reason)
            continue
        detector.__module_file__ = str(path)  # type: ignore[attr-defined]
        seen_ids.add(detector.detector_id)
        accepted.append(detector)
    if not accepted:
        return [], "; ".join(reasons) or "no usable detector in this file"
    if reasons:
        # Partially usable file: keep the good ones, report the rest.
        log.warning("Plugin %s: accepted %s, rejected %s", path, len(accepted), reasons)
    return accepted, ""


def load_plugin_dir(directory: str | Path, *, seen_ids: set[str] | None = None) -> tuple[
        list[GameSessionDetector], list[tuple[str, str]]]:
    """Load every `*.py` file in a directory (sorted, deterministic)."""
    directory = Path(directory)
    accepted: list[GameSessionDetector] = []
    rejected: list[tuple[str, str]] = []
    if not directory.is_dir():
        return accepted, rejected
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue  # `__init__.py`, scratch files: not plugins
        detectors, reason = load_plugin_file(path, seen_ids=seen_ids)
        if detectors:
            accepted.extend(detectors)
            for detector in detectors:
                log.info("Plugin detector loaded: %s from %s", detector.detector_id, path)
        else:
            rejected.append((str(path), reason))
            log.error("Plugin rejected (%s): %s", path, reason)
    return accepted, rejected


def host_from_settings(db, *, profile_dir_getter=None) -> tuple[DetectorHost, PluginReport]:
    """Build the host from the database settings (what a running agent uses).

    Kept here rather than in the service so `--monitor`, the simulator and the
    tests all wire detectors exactly the same way.
    """
    from pathlib import Path

    def setting(key, default):
        try:
            value = db.get_setting(key, default)
        except Exception:  # noqa: BLE001 - never let a settings read stop detection
            return default
        return default if value in (None, "") else value

    plugin_dir = str(setting("detector_plugins_dir", "") or "")
    if not plugin_dir:
        if profile_dir_getter is not None:
            base = profile_dir_getter()
        else:
            from app.config.settings import profile_dir as default_profile_dir

            base = default_profile_dir()
        plugin_dir = str(Path(base) / "detectors")

    # The Live Client API reads Riot's local endpoint; the log signals just read
    # files. All three are optional and degrade to None on their own.
    from app.core.detection.games.signals import (
        ClientPhaseReader,
        GameLogWatcher,
        LiveClientApi,
    )

    live_api = LiveClientApi(
        port=int(setting("game_live_api_port", 2999) or 2999),
        timeout=min(0.5, int(setting("game_probe_timeout_ms", 300) or 300) / 1000.0 * 0.5),
    ) if setting("game_live_api_enabled", True) else None

    log_dirs = setting("game_log_dirs", None)
    logs_scan = bool(setting("game_log_scan_enabled", True))
    watcher = GameLogWatcher(
        dirs=list(log_dirs) if log_dirs else None,
        fresh_seconds=float(setting("game_log_fresh_seconds", 90) or 90),
    ) if logs_scan else None
    phase_reader = ClientPhaseReader(
        dirs=list(log_dirs) if log_dirs else None,
    ) if logs_scan else None

    builtins = _builtin_with(live_api=live_api, log_watcher=watcher, phase_reader=phase_reader,
                             settle_seconds=float(setting("game_settle_seconds", 30) or 0))
    return build_host(
        plugin_dir=plugin_dir,
        timeout_ms=int(setting("game_probe_timeout_ms", 300) or 300),
        max_failures=int(setting("game_plugin_max_failures", 5) or 5),
        enabled=bool(setting("game_detector_enabled", True)),
        builtins=builtins,
    )


def _builtin_with(**kw) -> list[GameSessionDetector]:
    """Built-ins wired with the real signal providers (or None to disable one).

    Phase 8: all four games ship wired. League of Legends and Teamfight
    Tactics share the Riot signal providers (one Live Client API client, one
    log watcher — they are cached and read-only, so sharing costs nothing);
    VALORANT and R.E.P.O. get their own log watchers pointed at their games'
    log folders.
    """
    from app.core.detection.games.league_of_legends import LeagueOfLegendsDetector
    from app.core.detection.games.repo import PlayerLogWatcher, RepoDetector
    from app.core.detection.games.teamfight_tactics import TftDetector
    from app.core.detection.games.valorant import ShooterGameLogWatcher, ValorantDetector

    settle = float(kw.get("settle_seconds", 30) or 0)
    riot_log_scan = kw.get("log_watcher") is not None or kw.get("phase_reader") is not None
    lol = LeagueOfLegendsDetector(
        live_api=kw.get("live_api"),
        live_api_enabled=kw.get("live_api") is not None,
        log_watcher=kw.get("log_watcher"),
        log_scan_enabled=riot_log_scan,
        phase_reader=kw.get("phase_reader"),
        settle_seconds=settle,
    )
    tft = TftDetector(
        live_api=kw.get("live_api"),
        live_api_enabled=kw.get("live_api") is not None,
        log_watcher=kw.get("log_watcher"),
        log_scan_enabled=riot_log_scan,
        phase_reader=kw.get("phase_reader"),
        settle_seconds=settle,
    )
    valorant = ValorantDetector(
        log_watcher=ShooterGameLogWatcher() if kw.get("log_watcher") is not None else None,
        settle_seconds=settle,
    )
    repo = RepoDetector(
        log_watcher=PlayerLogWatcher() if kw.get("log_watcher") is not None else None,
        settle_seconds=settle,
    )
    return [lol, valorant, repo, tft]


def build_host(
    *,
    plugin_dir: str | Path | None = None,
    timeout_ms: int = 300,
    max_failures: int = 5,
    enabled: bool = True,
    builtins: list[GameSessionDetector] | None = None,
) -> tuple[DetectorHost, PluginReport]:
    """Build the host: built-ins first, then plugins. Never raises."""
    host = DetectorHost(timeout_ms=timeout_ms, max_failures=max_failures, enabled=enabled)
    accepted: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen_ids: set[str] = set()

    for detector in (builtins if builtins is not None else builtin_detectors()):
        try:
            host.register(detector, source="builtin")
            accepted.append(detector.detector_id)
            seen_ids.add(detector.detector_id)
        except Exception as exc:  # noqa: BLE001 - a broken built-in must not stop the app
            rejected.append((detector.__class__.__name__, f"registration failed: {exc!r}"))
            log.error("Built-in detector %s could not be registered: %r",
                      detector.__class__.__name__, exc)

    directories: list[str] = []
    if plugin_dir:
        directories.append(str(plugin_dir))
        detectors, failures = load_plugin_dir(plugin_dir, seen_ids=seen_ids)
        registered, registration_failures = host.register_all(detectors, source="plugin")
        accepted.extend(d.detector_id for d in registered)
        rejected.extend(failures)
        rejected.extend(registration_failures)

    return host, PluginReport(
        accepted=tuple(accepted), rejected=tuple(rejected), directories=tuple(directories)
    )
