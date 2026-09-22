"""SystemSnapshot helpers + the real psutil source (works on Linux/Windows)."""

import pytest

from app.core.monitoring.processes import (
    NullMonitorSource,
    ProcessInfo,
    PsutilProcessSource,
    SystemSnapshot,
)


def snap(*procs, fg=None, idle=None):
    return SystemSnapshot(
        processes=tuple(ProcessInfo(pid=p, exe=e) for p, e in procs),
        foreground_pid=fg, idle_seconds=idle, captured_mono=100.0,
    )


def test_helpers():
    s = snap((10, "discord.exe"), (11, "discord.exe"), (12, "chrome.exe"), fg=11)
    assert s.exes() == frozenset({"discord.exe", "chrome.exe"})
    assert s.pids_for(["discord.exe"]) == (10, 11)
    assert s.pids_for(["DISCORD.EXE"]) == (10, 11)  # case-insensitive
    assert s.matched_pids(["discord.exe"]) == {"discord.exe": [10, 11]}
    assert s.has("chrome.exe") and not s.has("steam.exe")
    assert s.foreground_exe() == "discord.exe"


def test_foreground_unknown_pid():
    s = snap((10, "discord.exe"), fg=999)
    assert s.foreground_exe() is None  # gone mid-tick: no false attribution


def test_process_info_defaults_name():
    assert ProcessInfo(pid=1, exe="x.exe").name == "x.exe"


def test_psutil_source_sees_own_process():
    source = PsutilProcessSource()
    s = source.snapshot()
    assert s.captured_mono > 0
    # The test runner itself must be visible: use our own PID as the anchor.
    import os

    assert any(p.pid == os.getpid() for p in s.processes)
    assert s.exes()  # not empty on any real machine


def test_null_source_is_empty_but_usable():
    s = NullMonitorSource(None).snapshot()
    assert s.processes == () and s.foreground_pid is None and s.idle_seconds is None
    assert s.foreground_exe() is None
    assert "discord.exe" not in s.exes()


def test_snapshot_is_immutable():
    s = snap((10, "a.exe"))
    with pytest.raises(Exception):
        s.foreground_pid = 5  # type: ignore[misc]
