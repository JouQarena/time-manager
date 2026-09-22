"""Shared test fixtures.

The Qt tests need a single QApplication for the whole session (Qt refuses to
build a second one) and no display: everything runs offscreen.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """The one QApplication for this test session (skips if Qt is absent)."""
    pytest.importorskip("PySide6", reason="PySide6 is optional (GUI extra)")
    from app.ui.qt.app import create_app

    return create_app([])

@pytest.fixture
def foreign_pid() -> int:
    """A PID that is alive and is not ours, on any OS.

    The tests used to hardcode pid 1 ("always exists on Linux"). On Windows
    pid 1 does not exist (System is pid 4), so the lock was — correctly —
    treated as stale and the tests failed. Ask the OS instead: prefer
    ultra-stable system processes, then any live process.
    """
    import psutil

    me = os.getpid()
    for candidate in (4, 1):  # Windows "System", Linux/macOS init/launchd
        if candidate != me and psutil.pid_exists(candidate):
            return candidate
    for proc in psutil.process_iter():
        if proc.pid != me:
            return proc.pid
    pytest.skip("no other live process to stand in for a foreign instance")
