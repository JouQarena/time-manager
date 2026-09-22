"""Autostart registry handling, driven by a fake winreg (runs on any OS)."""

import sys

import pytest

from app.windows import startup


class FakeKey:
    def __init__(self, store, path):
        self.store = store
        self.path = path
        self.deleted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinReg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}
        self.writes: list[tuple[str, str, str]] = []

    def CreateKeyEx(self, root, path, reserved, access):
        return FakeKey(self, path)

    def OpenKey(self, root, path, reserved, access):
        if not any(k[0] == path for k in self.values) and access == self.KEY_READ:
            raise FileNotFoundError(path)
        return FakeKey(self, path)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[(key.path, name)] = value
        self.writes.append((key.path, name, value))

    def QueryValueEx(self, key, name):
        try:
            return self.values[(key.path, name)], self.REG_SZ
        except KeyError:
            raise FileNotFoundError(name) from None

    def DeleteValue(self, key, name):
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[(key.path, name)]


def test_enable_writes_run_entry():
    reg = FakeWinReg()
    assert startup.enable(winreg=reg) is True
    assert startup.is_enabled(reg) is True
    path, name, value = reg.writes[0]
    assert path.endswith("CurrentVersion\\Run")
    assert name == startup.APP_NAME
    # Autostart (default) launches the desktop app, not the bare monitor.
    assert "--gui" in value


def test_enable_with_explicit_command():
    reg = FakeWinReg()
    startup.enable('"C:\\tm\\TimeManager.exe" --monitor', winreg=reg)
    # An explicit command always wins verbatim (this is what the watchdog
    # and the packaged autostart hand in).
    assert reg.values[(startup.RUN_KEY, startup.APP_NAME)] == '"C:\\tm\\TimeManager.exe" --monitor'


def test_disable_is_idempotent():
    reg = FakeWinReg()
    startup.enable(winreg=reg)
    assert startup.disable(reg) is True
    assert startup.is_enabled(reg) is False
    assert startup.disable(reg) is True  # already gone is still success


def test_status_reports_everything():
    reg = FakeWinReg()
    st = startup.status(winreg=reg)
    assert st["supported"] is True and st["enabled"] is False
    assert isinstance(st["expected"], str) and "--monitor" in st["expected"]
    startup.enable(winreg=reg)
    st = startup.status(winreg=reg)
    assert st["enabled"] is True and st["command"]


def test_sync_with_setting():
    reg = FakeWinReg()
    assert startup.sync_with_setting(True, winreg=reg) is True
    assert startup.is_enabled(reg)
    assert startup.sync_with_setting(False, winreg=reg) is True
    assert not startup.is_enabled(reg)


def test_launch_command_for_frozen_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert "--monitor" in startup.launch_command()
    assert startup.launch_command().startswith('"')


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows no-op path")
def test_non_windows_is_a_safe_noop():
    assert startup.enable() is False
    assert startup.disable() is False
    assert startup.is_enabled() is False
    st = startup.status()
    assert st["supported"] is False and st["enabled"] is False
