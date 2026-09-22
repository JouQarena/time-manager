"""The detector selftest itself, and the wiring it proves.

`--detector-selftest` is the Phase 6 acceptance test: it drives the real
detector stack (loader → host → LeagueOfLegendsDetector → guard → state machine
→ executor) over a scripted machine. These tests assert the steps really pass,
and that the agent's own wiring (service, monitor CLI, status surface) uses the
same stack.
"""

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from app.cli.detector_selftest import run  # noqa: E402


@pytest.fixture(scope="module")
def result():
    return run(verbose=False)


def test_every_step_passes(result):
    failed = [step.name for step in result["steps"] if not step.ok]
    assert result["ok"] is True, f"failing steps: {failed}"
    assert len(result["steps"]) == 21


def test_the_fail_safe_steps_are_present(result):
    names = " | ".join(step.name for step in result["steps"])
    for expected in ("crash -> unknown + quarantine", "hang -> budget + quarantine",
                     "reconnect window -> WAIT", "champ select -> WAIT",
                     "limit hit mid-match -> WAIT", "settle window -> WAIT",
                     "settled lobby -> ENFORCED"):
        assert expected in names


def test_a_match_is_never_closed_but_the_lobby_is(result):
    assert result["mid_match_state"] == "WAITING_FOR_SESSION_END"
    assert result["final_state"] == "ENFORCED"
    assert len(result["close_requests"]) == 1  # exactly one, and only after settling
    assert result["close_requests"][0]["exe"].lower() == "leagueclientux.exe"


def test_the_audit_trail_is_written(result):
    outcomes = {row["outcome"] for row in result["enforcement"]}
    assert "EXECUTED" in outcomes
    assert any(row["action"] == "CLOSE_APP" for row in result["enforcement"])


def test_detector_status_is_reported(result):
    status = {item["id"]: item for item in result["detector_status"]}
    assert "league_of_legends" in status
    lol = status["league_of_legends"]
    assert lol["quarantined"] is False and lol["errors"] == 0
    assert lol["calls"] > 100  # it ran on every tick of the scripted match
    assert lol["last_in_session"] is False  # the lobby at the end


def test_plugin_report_is_honest(result):
    report = result["plugin_report"]
    assert "league_of_legends" in report["accepted"] and "minecraft" in report["accepted"]
    assert len(report["rejected"]) == 1
    assert "broken on purpose" in report["rejected"][0]["reason"]


# ------------------------------------------------------------------- the CLI
def test_cli_prints_a_transcript_and_exits_zero():
    proc = subprocess.run([sys.executable, "-m", "app.main", "--detector-selftest"],
                          cwd=REPO, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert "ALL PASS" in proc.stdout
    assert "[PASS] live match -> WAIT" in proc.stdout
    assert "21/21" in proc.stdout


def test_cli_json_mode_is_machine_readable():
    proc = subprocess.run([sys.executable, "-m", "app.cli.detector_selftest", "--json"],
                          cwd=REPO, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert all(step["ok"] for step in payload["steps"])
    assert payload["final_state"] == "ENFORCED"


# ------------------------------------------------------------------- the wiring
def test_service_builds_the_real_detector_host(tmp_path):
    from app.service import AgentService
    from app.testing.fakes import FakeProcessController, FakeProcessSource

    source = FakeProcessSource()
    from app.config.settings import AppSettings

    service = AgentService(tmp_path / "t.db", source=source,
                           controller=FakeProcessController(source),
                           settings=AppSettings(ipc_port=0))  # never the live port
    try:
        service.start()
        assert service.detectors is not None
        # Phase 8: LoL, VALORANT, R.E.P.O. and TFT ship built-in.
        assert service.detectors.ids() == ["league_of_legends", "repo",
                                           "teamfight_tactics", "valorant"]
        assert service.detector_report is not None
        assert set(service.detector_report.accepted) == {
            "league_of_legends", "repo", "teamfight_tactics", "valorant"}
        snapshot = service.snapshot()
        assert snapshot.detector_summary["count"] == 4
        assert snapshot.detectors[0]["id"] == "league_of_legends"
        assert len(snapshot.detectors) == 4
        # …and it is the registry the guard actually consults.
        assert service.bridge is not None
    finally:
        service.stop(backup=False)


def test_plugins_are_loaded_from_the_profile_directory(tmp_path, monkeypatch):
    """Dropping a file in <profile>/detectors makes it live in the agent."""
    from app.config import settings as settings_mod
    from app.database.db import Database
    from app.core.detection.loader import host_from_settings

    profile = tmp_path / "profile"
    (profile / "detectors").mkdir(parents=True)
    (profile / "detectors" / "custom.py").write_text('''
from app.core.detection.base import GameSessionDetector, SessionVerdict


class CustomDetector(GameSessionDetector):
    detector_id = "custom_game"
    display_name = "Custom game"
    known_executables = ("custom.exe",)

    def probe(self, snapshot):
        return SessionVerdict(in_session=False, confidence=1.0, detail="never")
''')
    db = Database(tmp_path / "t.db").connect()
    monkeypatch.setattr(settings_mod, "profile_dir", lambda: profile)
    host, report = host_from_settings(db)
    assert host.ids() == ["custom_game", "league_of_legends", "repo",
                          "teamfight_tactics", "valorant"]
    assert host.managed("custom_game").stats.source == "plugin"
    db.close()


def test_status_json_includes_detectors(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "app.main", "--status-json", "--db", str(tmp_path / "s.db")],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    detectors = payload["detectors"]
    assert detectors["count"] == 4 and detectors["enabled"] is True
    assert detectors["items"][0]["id"] == "league_of_legends"
    assert detectors["items"][0]["known_executables"]
    assert detectors["rejected"] == []


def test_monitor_cli_uses_the_same_wiring():
    """`--monitor` must not fall back to an empty registry."""
    import inspect

    from app.cli import monitor as monitor_cli

    source = inspect.getsource(monitor_cli.build_agent)
    assert "host_from_settings" in source
    assert "DetectorRegistry()" not in source


def test_simulator_still_runs_with_the_detector_layer_available():
    """The Phase 3 simulator keeps its scripted detector (documented), and the
    real detector stack imports cleanly alongside it."""
    from app.cli.simulate import ScriptedGameDetector
    from app.core.detection.registry import DetectorRegistry

    registry = DetectorRegistry()
    registry.register(ScriptedGameDetector())
    assert registry.ids() == ["scripted"]

def test_selftest_is_independent_of_the_real_wall_clock(monkeypatch):
    """Regression (found on a real Windows machine at 21:11 local): step 11
    stamps its fake log with the FakeClock's wall (20:00) but its
    GameLogWatcher judged freshness against the machine's *real* clock —
    the step only passed where the real time of day was still before 20:00.
    Rebuild the run with the real wall pushed 11 hours past the fake stamp:
    the result must not care what the real clock says.
    """
    import time as time_module

    import app.cli.detector_selftest as selftest_module

    real_now = time_module.time()

    original = selftest_module.GameLogWatcher

    def shifted_wall(*args, **kwargs):
        # Stand in for a machine whose real clock is 11 h past the fake
        # stamp; an explicit wall_clock= (the fix) is left untouched.
        kwargs.setdefault("wall_clock", lambda: real_now + 11 * 3600)
        return original(*args, **kwargs)

    monkeypatch.setattr(selftest_module, "GameLogWatcher", shifted_wall)
    result = run(verbose=False)
    failed = [step.name for step in result["steps"] if not step.ok]
    assert result["ok"] is True, f"failing steps: {failed}"
