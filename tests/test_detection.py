from app.core.detection.base import SessionProbe, SessionVerdict
from app.core.detection.registry import DetectorRegistry
from app.core.detection.base import GameSessionDetector


class FakeLoL(GameSessionDetector):
    detector_id = "league_of_legends"
    display_name = "LoL (test fake)"
    known_executables = ("leagueclient.exe", "league of legends.exe")

    def probe(self, snapshot):
        exes = snapshot.running_exes
        if "league of legends.exe" in exes:
            return SessionVerdict(True, 1.0, "game client present")
        if "leagueclient.exe" in exes:
            return SessionVerdict(False, 1.0, "launcher only")
        return SessionVerdict(False, 0.5, "no LoL exes")


def test_registry_routing_and_failsafe():
    reg = DetectorRegistry()
    reg.register(FakeLoL())
    det = reg.for_executables(("LeagueClient.exe",))
    assert det is not None and det.detector_id == "league_of_legends"
    assert reg.for_executables(("notepad.exe",)) is None

    assert det.is_in_active_session(
        SessionProbe(running_exes=frozenset({"leagueclient.exe", "league of legends.exe"})))
    assert not det.is_in_active_session(
        SessionProbe(running_exes=frozenset({"leagueclient.exe"})))
    # Unknown -> fail-safe False (engine will WAIT, never kill).
    assert not det.is_in_active_session(SessionProbe(running_exes=frozenset()))
