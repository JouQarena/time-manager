"""Shared test fixtures.

The Qt tests need a single QApplication for the whole session (Qt refuses to
build a second one) and no display: everything runs offscreen.

The other job of this file is to keep the tests **out of the real user
profile**. `app.config.settings.app_dir()` is `%APPDATA%\TimeManager` on
Windows and `$XDG_CONFIG_HOME|~/.config/TimeManager` elsewhere — so a test
that merely calls `load_settings()` (which creates `config.json` when missing)
writes to the machine that happens to be running pytest. That is not
hypothetical: the first Windows run of the i18n tests overwrote a real
`config.json` because they redirected only `XDG_CONFIG_HOME`, which Windows
ignores. `isolated_profile` redirects *every* path the app can pick, and
`real_profile_untouched` fails the run if anything still leaks through.
"""

import hashlib
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: Environment variables `app.config.settings.app_dir()` (and everything built
#: on it: config, token, database, backups, logs) can resolve a profile from.
PROFILE_ENV_VARS = ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME")

#: The profile directory of the machine actually running the tests, captured at
#: import time — *before* any fixture redirects the environment. Everything the
#: suite writes must land in a temp directory, never here.
_REAL_PROFILE_DIR = None
try:  # import-time: a broken profile path must not stop collection
    from app.config.settings import app_dir as _app_dir

    _REAL_PROFILE_DIR = Path(_app_dir())
except Exception:  # noqa: BLE001
    pass


@pytest.fixture(autouse=True, scope="session")
def _hermetic_profile(tmp_path_factory):
    """Run the whole session against a throwaway profile directory.

    A test that calls `load_settings()` (the app creates `config.json` on first
    read), keeps a backup, or writes a pairing token must not touch the real
    `%APPDATA%\TimeManager` / `~/.config/TimeManager`. Redirecting once for the
    session covers tests written later that never opt in — the first Windows run
    of this suite rewrote a real `config.json`, and that must be impossible, not
    just unlikely.
    """
    profile = tmp_path_factory.mktemp("session-profile")
    saved = {name: os.environ.get(name) for name in PROFILE_ENV_VARS}
    for name in PROFILE_ENV_VARS:
        os.environ[name] = str(profile / name.lower())
    try:
        yield profile
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


#: Files in the real profile that no test may ever modify. `agent_token` and
#: `backups/` are deliberately *not* listed as protected: the CLI subprocess
#: tests legitimately exercise `--show-token` and the backup path, and both
#: are recreated by the app when missing.
PROTECTED_PROFILE_FILES = ("config.json",)


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protected_snapshot() -> dict[str, str | None]:
    """Hashes of the protected files in the *real* profile (if present).

    Uses the path captured at import time, so the session-wide environment
    redirect cannot hide a leak from this check.
    """
    if _REAL_PROFILE_DIR is None:
        return {}
    return {name: _digest(_REAL_PROFILE_DIR / name)
            for name in PROTECTED_PROFILE_FILES}


#: Tests seen writing to the real profile, by node id. Filled in by the
#: per-test watcher below so the session guard can name the offender instead of
#: just failing at the end of the run.
_PROFILE_WRITERS: list[str] = []


@pytest.fixture(autouse=True)
def _watch_profile_writes(request):
    """Remember which test changed the real profile (if any)."""
    before = _protected_snapshot()
    yield
    if _protected_snapshot() != before:
        _PROFILE_WRITERS.append(request.node.nodeid)


@pytest.fixture(autouse=True, scope="session")
def real_profile_untouched():
    """Fail the session if any test alters the real profile's config.

    Runs on every platform: on Windows the leak would be `%APPDATA%`, on
    Linux/macOS `~/.config`. A test that escapes its sandbox must be a red
    test, not a silently rewritten user setting.
    """
    before = _protected_snapshot()
    yield
    after = _protected_snapshot()
    changed = {name: (before.get(name), after.get(name))
               for name in set(before) | set(after)
               if before.get(name) != after.get(name)}
    assert not changed, (
        "a test wrote to the REAL Time Manager profile "
        f"({changed}). Offending test(s): {_PROFILE_WRITERS or 'unknown'}. "
        "Use the `isolated_profile` fixture (tests/conftest.py) for anything "
        "that reads or writes settings."
    )


@pytest.fixture()
def isolated_profile(tmp_path, monkeypatch):
    """Point the app's profile directory at `tmp_path` on any OS.

    Sets every variable `app_dir()` consults — `%APPDATA%` on Windows,
    `XDG_CONFIG_HOME` (then `~/.config`) elsewhere, and the two home
    variables that back the fallbacks. The redirection is then *verified*, so a
    future change to `app_dir()` cannot turn this fixture into a no-op that
    quietly writes to the developer's own profile.
    """
    home = tmp_path / "home"
    roaming = tmp_path / "Roaming"
    config = tmp_path / "config"
    for path in (home, roaming, config):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(roaming))            # Windows
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))     # Linux/macOS

    from app.config.settings import app_dir

    resolved = Path(app_dir()).resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"profile redirection failed: app_dir() -> {resolved}, "
        f"expected something under {tmp_path}"
    )
    return resolved


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
