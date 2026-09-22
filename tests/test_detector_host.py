"""DetectorHost: the plugin failure matrix that keeps a match alive.

Every failure mode here must end in the same place — an "unknown" verdict — so
the state machine keeps WAITING_FOR_SESSION_END instead of killing a game.
"""

import threading
import time

import pytest

from app.core.detection.base import GameSessionDetector, SessionProbe, SessionVerdict
from app.core.detection.host import DetectorHost, ManagedDetector
from app.core.enforcement.game_guard import GameSessionGuard
from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.rules.models import Rule
from app.core.types import Action, RuleType


class Plugin(GameSessionDetector):
    detector_id = "plugin"
    display_name = "Test plugin"
    known_executables = ("league of legends.exe",)

    def __init__(self, verdict=None, *, raises=False, sleeps=0.0, detector_id="plugin",
                 exes=("league of legends.exe",)):
        if detector_id != "plugin":
            self.detector_id = detector_id  # instance attribute shadows the class one
        self.known_executables = tuple(exes)
        self._verdict = verdict or SessionVerdict(in_session=False, confidence=1.0, detail="plugin")
        self.raises = raises
        self.sleeps = sleeps
        self.calls = 0

    def probe(self, snapshot):
        self.calls += 1
        if self.sleeps:
            time.sleep(self.sleeps)
        if self.raises:
            raise RuntimeError("plugin exploded")
        return self._verdict


def empty_probe():
    return SessionProbe(running_exes=frozenset({"league of legends.exe"}), hints={})


def game_rule():
    rule = Rule(name="LoL", type=RuleType.GAME, target="league of legends.exe",
                executable="league of legends.exe", daily_limit_seconds=600,
                action=Action.WAIT_FOR_SESSION_END)
    rule.id = 1
    return rule


def snapshot():
    return SystemSnapshot(
        processes=(ProcessInfo(pid=10, exe="league of legends.exe"),),
        foreground_pid=10, captured_mono=1.0,
    )


# ------------------------------------------------------------------ happy path
def test_healthy_detector_passes_verdicts_through():
    host = DetectorHost()
    plugin = Plugin(SessionVerdict(in_session=True, confidence=1.0, detail="in game"))
    host.register(plugin)
    assert host.managed("plugin").probe(empty_probe()).in_session is True
    assert plugin.calls == 1
    stats = host.status()[0]
    assert stats["calls"] == 1 and stats["errors"] == 0 and stats["timeouts"] == 0
    assert stats["healthy"] is True and stats["last_in_session"] is True
    assert stats["last_detail"] == "in game"


def test_managed_detector_reports_plugin_identity():
    host = DetectorHost()
    managed = host.register(Plugin())
    assert managed.detector_id == "plugin"
    assert managed.display_name == "Test plugin"
    assert managed.known_executables == ("league of legends.exe",)
    assert host.registry.for_executables(["league of legends.exe"]) is managed
    assert host.ids() == ["plugin"]


def test_registration_is_reported_in_status():
    host = DetectorHost(timeout_ms=250, max_failures=3)
    host.register(Plugin(), source="plugin")
    entry = host.status()[0]
    assert entry["source"] == "plugin"
    summary = host.summary()
    assert summary == {"enabled": True, "count": 1, "quarantined": 0, "errors": 0,
                       "timeouts": 0, "calls": 0, "timeout_ms": 250}


# --------------------------------------------------------------- failure modes
def test_exception_becomes_unknown_and_is_counted():
    host = DetectorHost()
    host.register(Plugin(raises=True))
    verdict = host.managed("plugin").probe(empty_probe())
    assert verdict.in_session is True and verdict.confidence == 0.0
    assert "plugin exploded" in verdict.detail
    entry = host.status()[0]
    assert entry["errors"] == 1 and entry["healthy"] is False
    assert entry["last_in_session"] is None


def test_timeout_becomes_unknown_and_abandons_the_thread():
    host = DetectorHost(timeout_ms=50)
    host.register(Plugin(sleeps=0.5, detector_id="slow"))
    started = time.monotonic()
    verdict = host.managed("slow").probe(empty_probe())
    elapsed = time.monotonic() - started
    assert elapsed < 0.4, "the host must not wait for a wedged plugin"
    assert verdict.confidence == 0.0 and "budget" in verdict.detail
    entry = host.status()[0]
    assert entry["timeouts"] == 1 and entry["abandoned"] == 1


def test_a_wedged_plugin_does_not_get_a_thread_per_call():
    host = DetectorHost(timeout_ms=30)
    managed = host.register(Plugin(sleeps=5.0, detector_id="wedged"))
    verdicts = [managed.probe(empty_probe()) for _ in range(4)]
    assert all(v.confidence == 0.0 for v in verdicts)
    assert managed.stats.abandoned == 1, "only the first probe gets a thread"
    assert managed.stats.timeouts == 4, "every probe is still counted as a failure"
    leaked = [t for t in threading.enumerate() if t.name == "detector-wedged"]
    assert len(leaked) == 1, "the abandoned thread is the only one, and it is daemon"


def test_quarantine_after_repeated_failures():
    host = DetectorHost(timeout_ms=200, max_failures=2)
    managed = host.register(Plugin(raises=True))
    first = managed.probe(empty_probe())
    assert first.confidence == 0.0
    managed.probe(empty_probe())
    third = managed.probe(empty_probe())
    assert managed.stats.quarantined is True
    assert "quarantined" in third.detail
    assert managed.stats.calls == 2, "a quarantined plugin is not called again"
    assert host.unhealthy() == ["plugin"]


def test_quarantine_can_be_released():
    host = DetectorHost(max_failures=1)
    managed = host.register(Plugin(raises=True))
    managed.probe(empty_probe())
    assert managed.stats.quarantined is True
    assert host.release("plugin") == 1
    assert managed.stats.quarantined is False
    assert host.release("nope") == 0


def test_a_success_resets_the_failure_streak():
    host = DetectorHost(max_failures=3)
    flaky = Plugin()
    managed = host.register(flaky)
    flaky.raises = True
    managed.probe(empty_probe())
    managed.probe(empty_probe())
    flaky.raises = False
    managed.probe(empty_probe())
    assert managed.stats.consecutive_failures == 0
    flaky.raises = True
    managed.probe(empty_probe())
    assert managed.stats.quarantined is False  # the streak restarted


def test_wrong_return_type_is_a_failure():
    class Sloppy(GameSessionDetector):
        detector_id = "sloppy"
        known_executables = ("league of legends.exe",)

        def probe(self, snapshot):
            return True  # not a SessionVerdict

    host = DetectorHost()
    host.register(Sloppy())
    verdict = host.managed("sloppy").probe(empty_probe())
    assert verdict.confidence == 0.0 and "SessionVerdict" in verdict.detail
    assert host.status()[0]["errors"] == 1


def test_configure_updates_the_budget_of_registered_detectors():
    host = DetectorHost(timeout_ms=3000)
    managed = host.register(Plugin(sleeps=0.2))
    host.configure(timeout_ms=50, max_failures=2)
    assert managed.timeout_seconds == 0.05 and managed.max_failures == 2
    assert managed.probe(empty_probe()).confidence == 0.0  # now too slow to trust
    assert host.summary()["timeout_ms"] == 50


# ----------------------------------------------------------------- guard level
def test_guard_turns_every_failure_into_wait():
    """The contract that matters: a broken plugin can never cause a kill."""
    for plugin, reason in ((Plugin(raises=True), "error"), (Plugin(sleeps=0.3), "timeout")):
        host = DetectorHost(timeout_ms=40)
        host.register(plugin)
        verdict = GameSessionGuard(host.registry).for_rule(game_rule(), snapshot())
        # Never confident -> the state machine holds WAITING_FOR_SESSION_END.
        assert verdict.confident is False, reason
        assert verdict.unknown is True

    # A decisive "not in a session" answer is the only one that allows action.
    host = DetectorHost()
    host.register(Plugin(SessionVerdict(in_session=False, confidence=1.0, detail="launcher only")))
    verdict = GameSessionGuard(host.registry).for_rule(game_rule(), snapshot())
    assert (verdict.in_session, verdict.confident) == (False, True)


def test_host_keeps_working_when_one_plugin_dies():
    host = DetectorHost(max_failures=1)
    host.register(Plugin(raises=True, detector_id="broken"))
    healthy = host.register(Plugin(SessionVerdict(in_session=True, confidence=1.0),
                                   detector_id="fine"))

    bad = host.managed("broken").probe(empty_probe())
    good = healthy.probe(empty_probe())
    assert bad.confidence == 0.0 and good.confidence == 1.0
    assert host.summary()["quarantined"] == 1


def test_stats_reads_are_thread_safe():
    # A generous budget: this test proves concurrent stats reads are safe,
    # not budget timing — a loaded machine (or CI runner) must not turn a
    # chance 200 ms trip into a quarantine and a flaky count.
    host = DetectorHost(timeout_ms=10_000)
    managed = host.register(Plugin())
    errors: list[str] = []

    def hammer():
        try:
            for _ in range(50):
                managed.probe(empty_probe())
                host.status()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert managed.stats.calls == 200


def test_registry_routing_prefers_the_most_specific_detector():
    host = DetectorHost()
    host.register(Plugin())  # knows league of legends.exe

    class Wide(GameSessionDetector):
        detector_id = "wide"
        known_executables = ("league of legends.exe", "leagueclientux.exe")

        def probe(self, snapshot):
            return SessionVerdict(in_session=False, confidence=1.0)

    host.register(Wide())
    chosen = host.registry.for_executables(["league of legends.exe", "leagueclientux.exe"])
    assert chosen.detector_id == "wide"  # two matches beats one
    assert host.registry.for_executables(["league of legends.exe"]).detector_id in {
        "wide", "plugin"}  # tie: either is acceptable


def test_duplicate_registration_is_refused():
    host = DetectorHost()
    host.register(Plugin())
    with pytest.raises(ValueError, match="duplicate"):
        host.register(Plugin())
    managed, reason = host.register_safe(Plugin())
    assert managed is None and "duplicate" in reason


def test_register_all_reports_rejections_without_losing_good_ones():
    host = DetectorHost()
    accepted, rejected = host.register_all([
        Plugin(detector_id="one"), Plugin(detector_id="one"), Plugin(detector_id="two")])
    assert [d.detector_id for d in accepted] == ["one", "two"]
    assert rejected and rejected[0][0] == "one"
    assert host.ids() == ["one", "two"]


def test_managed_detector_is_a_detector():
    managed = ManagedDetector(Plugin())
    assert isinstance(managed, GameSessionDetector)
    assert managed.is_in_active_session(empty_probe()) is False
    with pytest.raises(NotImplementedError):
        # The wrapper always delegates; the ABC's own method is untouched.
        GameSessionDetector.probe(managed, empty_probe())
