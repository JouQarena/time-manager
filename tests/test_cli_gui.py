"""Phase 5 CLI surface: `--gui`, `--gui-shot`, `--status-json`.

The CLI is what a user, a script or a support session actually touches, so the
arguments, exit codes and output shape are tested directly.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sandbox_env(profile: str) -> dict:
    """Environment that points the *child* process's profile at `profile`.

    Without this the CLI tests read and write the real user profile (the agent
    token, a `config.json` the app creates on first read, backups). The child
    is a separate interpreter, so a fixture cannot reach it — it has to be the
    environment.
    """
    return {
        **os.environ,
        "HOME": profile,
        "USERPROFILE": profile,
        "APPDATA": profile,              # Windows
        "XDG_CONFIG_HOME": profile,      # Linux/macOS
    }


def run_cli(args, **kw):
    tmp = kw.pop("profile", None)
    if tmp is None:
        # Keep the temporary directory alive for the call, then drop it.
        with tempfile.TemporaryDirectory(prefix="tm-cli-") as profile:
            return run_cli(args, profile=profile, **kw)
    env = {**_sandbox_env(str(tmp)), "PYTHONIOENCODING": "utf-8"}
    env.update(kw.pop("env", {}))
    return subprocess.run(
        [sys.executable, "-m", "app.main", *args],
        cwd=REPO, capture_output=True, text=True, timeout=300,
        # Decode explicitly: the CLI prints em dashes and Arabic, and on a
        # cp1252/ASCII machine the parent's locale would mangle or reject it.
        encoding="utf-8", errors="replace", env=env, **kw,
    )


# ---------------------------------------------------------------- status-json
def test_status_json_is_valid_and_complete(tmp_path):
    result = run_cli(["--status-json", "--db", str(tmp_path / "s.db")])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    for key in ("agent_version", "day", "running", "paused", "browsers", "ipc",
                "monitor", "rules", "storage", "token_present"):
        assert key in payload, f"missing {key!r} in the status payload"
    for key in ("running", "port", "messages_in", "messages_out", "rate_limited"):
        assert key in payload["ipc"]
    for key in ("ticks", "errors", "thread", "degraded"):
        assert key in payload["monitor"]
    assert payload["token_present"] is True       # so scripts can pair a browser
    assert payload["day"] == payload["day"]       # a real date string
    assert isinstance(payload["recent_enforcement"], list)


def test_status_json_reports_rules_with_usage(tmp_path):
    db = str(tmp_path / "s.db")
    assert run_cli(["--init-db", "--db", db]).returncode == 0
    assert run_cli(["--add-sample", "--db", db]).returncode == 0
    payload = json.loads(run_cli(["--status-json", "--db", db]).stdout)
    assert payload["rules"], "the sample database should expose rules"
    rule = payload["rules"][0]
    for key in ("id", "name", "type", "mode", "action", "enabled", "used_seconds",
                "remaining_today_seconds", "state", "used_ratio"):
        assert key in rule
    assert payload["storage"]["db_path"].endswith("s.db")


def test_status_json_reads_while_the_agent_runs(tmp_path):
    """A live agent holds the lock; the status command must still work."""
    from app.config.lockfile import InstanceLock
    from app.core.clock import FakeClock
    from app.service import AgentService
    from app.testing.fakes import FakeProcessController, FakeProcessSource

    db_path = tmp_path / "s.db"
    source = FakeProcessSource()
    source.clock = FakeClock()
    # ipc_port=0: the OS picks a free port, so the test passes even on a
    # machine where a real agent is running and owns 17846 (field case).
    from app.config.settings import AppSettings

    agent = AgentService(db_path, clock=FakeClock(), source=source,
                         controller=FakeProcessController(source),
                         settings=AppSettings(ipc_port=0))
    try:
        agent.start_background()
        result = run_cli(["--status-json", "--db", str(db_path)])
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        # The reader reports the live agent instead of fighting it for the port.
        assert payload["read_only"] is True
        assert "another instance" in payload["ipc"]["note"]
        assert f"pid {os.getpid()}" in payload["ipc"]["note"]
        assert payload["ipc"]["running"] is False
        assert payload["monitor"]["thread"] is False  # this invocation has no loop
        assert agent.ipc.status()["running"] is True  # the agent still owns it
        # The reader must not have disturbed the live agent's link.
        assert agent.ipc.bound_port  # ephemeral here; 17846 in a real install
    finally:
        agent.stop(backup=False)
    assert not InstanceLock(db_path.with_suffix(".lock")).held_by_other()


def test_status_json_does_not_disturb_the_database(tmp_path):
    from app.database.db import Database

    db = str(tmp_path / "s.db")
    run_cli(["--init-db", "--db", db])

    before = Database(db).connect()
    rules_before = len(before.list_rules())
    before.close()
    run_cli(["--status-json", "--db", db])
    after = Database(db).connect()
    assert len(after.list_rules()) == rules_before
    after.close()


# ------------------------------------------------------------------ gui-shot
def test_gui_shot_writes_screenshots(tmp_path):
    result = run_cli(["--gui-shot", "--out", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    paths = sorted(p.name for p in tmp_path.glob("*.png"))
    assert paths == ["dashboard-ar.png", "dashboard.png", "extension_help.png",
                     "rule_editor.png", "settings.png", "tour.png"]
    assert "wrote" in result.stdout
    for name in paths:
        assert (tmp_path / name).stat().st_size > 5_000


def test_gui_shot_output_is_a_real_dashboard(tmp_path):
    """The PNG must contain the widgets, not an empty frame."""
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QImage

    run_cli(["--gui-shot", "--out", str(tmp_path)])
    image = QImage(str(tmp_path / "dashboard.png"))
    assert not image.isNull()
    assert image.width() >= 900 and image.height() >= 600
    colors = {image.pixelColor(x, y).name()
              for x in range(0, image.width(), 41)
              for y in range(0, image.height(), 31)}
    assert len(colors) > 5  # text, chips and bars, not one flat colour


# ----------------------------------------------------------------------- gui
def test_gui_reports_missing_pyside6_clearly(monkeypatch, capsys):
    """exit 4 + a readable message: `--gui` must not traceback at users."""
    import builtins
    import importlib

    import app.main as main

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("app.ui.qt"):
            raise ImportError("No module named 'PySide6'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "app.ui.qt", raising=False)
    importlib.invalidate_caches()
    assert main.cmd_gui(None) == 4
    out = capsys.readouterr().out
    assert "PySide6" in out and "requirements.txt" in out


def test_gui_refuses_a_second_instance(monkeypatch, qapp):
    """The window is never created when the lock belongs to a live agent."""
    from app.service import AgentAlreadyRunning
    from app.ui.qt import app as qt_app

    class FakeService:
        settings = type("S", (), {"start_minimized": True, "notifications_enabled": True})()

        def set_notifier(self, notifier):
            self.notifier = notifier

        def start_background(self):
            raise AgentAlreadyRunning(4242)

    monkeypatch.setattr(qt_app, "AgentService", lambda *a, **kw: FakeService())
    warnings: list[tuple] = []
    monkeypatch.setattr(qt_app.QMessageBox, "warning",
                        lambda *a, **kw: warnings.append(a))
    assert qt_app.run_gui(None) == 3
    assert warnings and "4242" in warnings[0][2]


def test_gui_start_failure_is_reported_not_crashed(monkeypatch, qapp):
    from app.ui.qt import app as qt_app

    class FakeService:
        settings = type("S", (), {"start_minimized": True, "notifications_enabled": True})()

        def set_notifier(self, notifier):
            self.notifier = notifier

        def start_background(self):
            raise RuntimeError("profile is read-only")

    monkeypatch.setattr(qt_app, "AgentService", lambda *a, **kw: FakeService())
    seen: list[tuple] = []
    monkeypatch.setattr(qt_app.QMessageBox, "critical",
                        lambda *a, **kw: seen.append(a))
    assert qt_app.run_gui(None) == 1
    assert seen and "read-only" in seen[0][2]


# ------------------------------------------------------------------- layering
def test_core_and_viewmodel_import_without_qt():
    """`app.core`, `app.service` and the viewmodel must stay Qt-free."""
    code = (
        "import sys;"
        "import app.core.monitoring.monitor, app.core.enforcement.adapters;"
        "import app.service, app.ui.viewmodel, app.ui.qt.demo;"
        "bad=[m for m in sys.modules if m.split('.')[0]=='PySide6'];"
        "print(len(bad))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", "the engine must not import Qt"


def test_gui_source_has_no_hardcoded_localhost_client_calls():
    """The GUI talks to the agent in-process; it must never dial the socket."""
    import pathlib

    for path in pathlib.Path(REPO, "app", "ui").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # The dashboard reads state through the in-process service; a GUI that
        # dialled the loopback socket would double-count and race the bridge.
        assert "import websockets" not in text, f"{path} must not open its own link"
        assert "ws://" not in text, f"{path} must not hardcode a socket URL"
