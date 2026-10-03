"""Phase 9: frozen-aware resource paths + packaged-build CLI helpers."""

import os
import sys
from pathlib import Path

import pytest

from app import resources
from app.resources import app_icon_path, bundle_dir, executable_dir, extension_dir, is_frozen


def test_not_frozen_when_running_from_source():
    assert is_frozen() is False


def test_source_paths_point_at_the_project_root():
    root = Path(__file__).resolve().parents[1]
    assert bundle_dir() == root
    assert executable_dir() == root
    assert extension_dir() == root / "browser-extension"
    assert (extension_dir() / "manifest.json").is_file()  # really shipped


def test_frozen_paths_use_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "TimeManagerTray.exe"),
                        raising=False)

    assert is_frozen() is True
    assert bundle_dir() == tmp_path / "_internal"
    assert extension_dir() == tmp_path / "_internal" / "browser-extension"
    assert executable_dir() == tmp_path  # next to the exe, not inside _internal


def test_frozen_without_meipass_falls_back_to_the_exe_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", None, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "TimeManager.exe"),
                        raising=False)
    assert bundle_dir() == tmp_path


def test_icon_asset_is_shipped_and_non_empty():
    icon = app_icon_path()
    assert icon.name == "app.ico"
    assert icon.is_file() and icon.stat().st_size > 1000


def test_cli_show_extension_path_reports_and_validates(capsys):
    from app.main import cmd_show_extension_path

    assert cmd_show_extension_path() == 0
    out = capsys.readouterr().out
    assert "browser-extension" in out
    assert "Load unpacked" in out


def test_cli_show_extension_path_fails_on_a_broken_bundle(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("app.resources.extension_dir", lambda: tmp_path / "missing")
    from app.main import cmd_show_extension_path

    assert cmd_show_extension_path() == 1
    assert "manifest.json not found" in capsys.readouterr().out


# ------------------------------------------------------- startup/win packaging
def test_launch_command_variants(monkeypatch):
    from app.windows import startup

    # Source build: module invocation.
    monitor = startup.launch_command()
    gui = startup.launch_command(gui=True)
    assert monitor.endswith('-m app.main --monitor')
    assert gui.endswith('-m app.main --gui')

    # Frozen build: the running executable itself.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert startup.launch_command() == f'"{sys.executable}" --monitor'
    assert startup.launch_command(gui=True) == f'"{sys.executable}" --gui'

def test_entry_console_runs_and_exits_clean():
    """The packaged CLI entry point: explicit arguments are passed through
    (here --version) and exit 0. The double-click pause only exists in a
    frozen build, so this source-level subprocess must return immediately.
    """
    import subprocess
    import sys

    entry = Path(__file__).resolve().parents[1] / "packaging" / "entry_console.py"
    # Script mode puts packaging/ on sys.path, not the repo root (the same
    # mechanics that bit the PyInstaller console script), so the source-level
    # subprocess needs PYTHONPATH; the frozen build bundles `app` instead.
    env = {**os.environ, "PYTHONPATH": str(entry.parents[1])}
    proc = subprocess.run(
        [sys.executable, str(entry), "--version"],
        capture_output=True, text=True, timeout=120, cwd=entry.parents[1],
        encoding="utf-8", errors="replace",
        env={**env, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "TimeManager" in proc.stdout or "1." in proc.stdout

def test_entry_tray_crash_net_writes_the_log(tmp_path, monkeypatch):
    """The windowed exe must never die silently: a start-up crash has to land
    in %APPDATA%/TimeManager/logs/tray-crash.log (a real Windows machine hit
    exactly that silent-exit state — no window, no process, no logs)."""
    import os

    monkeypatch.setenv("APPDATA", str(tmp_path))
    entry = Path(__file__).resolve().parents[1] / "packaging" / "entry_tray.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("entry_tray", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Silence the native box via the seam — without this, running the suite
    # on Windows popped a real "boom / line two" dialog during the build.
    shown: list[str] = []
    monkeypatch.setattr(module, "_message_box", lambda text: shown.append(text))
    module._show_crash("boom\nline two")
    assert shown == ["boom\nline two"]
    written = tmp_path / "TimeManager" / "logs" / "tray-crash.log"
    assert written.is_file()
    assert "boom" in written.read_text(encoding="utf-8")

def test_entry_tray_survives_missing_streams(tmp_path, monkeypatch):
    """In a windowed exe sys.stdout/stderr are None; the tray entry must
    replace them with real file streams before anything can write (a
    working console exe beside a silently-dead windowed one is this exact
    signature on real Windows)."""
    import io

    entry = Path(__file__).resolve().parents[1] / "packaging" / "entry_tray.py"
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import importlib.util

    spec = importlib.util.spec_from_file_location("entry_tray2", entry)
    module = importlib.util.module_from_spec(spec)
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = None  # the windowed-exe condition
    spec.loader.exec_module(module)
    tray_stream = sys.stderr  # what the entry attached (grab before restore)
    sys.stdout, sys.stderr = saved_out, saved_err
    log = tmp_path / "TimeManager" / "logs" / "tray-stdio.log"
    assert log.is_file()
    tray_stream.write("hello\n")  # a line-buffered file stream, not None
    assert "hello" in log.read_text(encoding="utf-8")


def test_spec_builds_each_exe_from_its_own_entry_script():
    """Regression: the tray exe must not be built from the CLI entry point.

    The spec used to feed a single Analysis to both EXEs, so
    `TimeManagerTray.exe` shipped the *console* program: double-clicking it
    ran the CLI, which with no arguments prints `--help` and exits 0. In a
    windowed build there is no stdout/stderr, so the help went nowhere and
    the exe vanished in silence — no window, no dialog, no log — which is
    exactly the "double-clicking TimeManagerTray.exe does nothing" report.
    Each entry point now gets its own Analysis, pyz and scripts.
    """
    spec_source = (Path(__file__).resolve().parents[1] / "time-manager.spec").read_text(
        encoding="utf-8"
    )
    assert 'analyze("entry_console.py")' in spec_source
    assert 'analyze("entry_tray.py")' in spec_source  # was absent: dead entry file
    assert "a_console.scripts" in spec_source
    assert "a_tray.scripts" in spec_source
