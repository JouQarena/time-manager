"""Windows layer: import safety, degradation, win32 call shapes, power gaps."""

import sys

import pytest

from app.core.monitoring.processes import NullMonitorSource, PsutilProcessSource
from app.windows import api, power
from app.windows.win_monitor import build_monitor_source


def test_api_degrades_gracefully_off_windows():
    if api.is_windows():
        pytest.skip("Windows host")
    assert api.foreground_pid() is None
    assert api.idle_seconds() is None
    assert api.post_wm_close(1234) == 0
    assert api.force_terminate(1234) is False


def test_launch_command_is_quoted_and_monitored():
    cmd = api.app_launch_command()
    assert cmd.startswith('"') and cmd.endswith('--monitor')


def test_build_monitor_source_works_everywhere():
    source = build_monitor_source()
    snap = source.snapshot()
    assert snap.captured_mono > 0
    if not api.is_windows():
        assert snap.foreground_pid is None  # documented degradation
        assert isinstance(source, NullMonitorSource)


def test_monitor_source_survives_missing_psutil(monkeypatch):
    """No psutil (or a broken one) must not crash the monitor."""
    def broken():
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr("app.windows.win_monitor.PsutilProcessSource", broken)
    source = build_monitor_source()
    snap = source.snapshot()
    assert snap.processes == ()


def test_gap_detector_ignores_normal_ticks():
    detector = power.GapDetector(interval=1.0)
    assert detector.check(100.0) is None  # first sample establishes the baseline
    assert detector.check(101.0) is None
    assert detector.check(102.5) is None  # slow tick, still normal


def test_gap_detector_flags_sleep():
    detector = power.GapDetector(interval=1.0, factor=3.0, floor=5.0)
    detector.check(0.0)
    assert detector.check(2.0) is None
    gap = detector.check(2.0 + 3600)
    assert gap == pytest.approx(3600.0)
    # after reporting, the baseline resets
    assert detector.check(2.0 + 3601) is None
    assert detector.threshold() == 5.0


def test_gap_detector_floor_respects_longer_intervals():
    assert power.GapDetector(interval=10.0).threshold() == 30.0


def test_power_constants_match_win32():
    assert power.WM_POWERBROADCAST == 0x0218
    assert power.PBT_APMSUSPEND == 0x0004
    assert power.PBT_APMRESUMEAUTOMATIC == 0x0012


def test_install_listener_is_safe_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows host")
    thread = power.install_win32_power_listener(lambda: None, lambda: None)
    assert thread is None  # unavailable, but no exception


def test_attach_to_tracker_tolerates_missing_hook():
    assert power.attach_to_tracker(object()) is False


def test_psutil_source_and_null_source_share_the_interface():
    for source in (PsutilProcessSource(), NullMonitorSource(None)):
        snap = source.snapshot()
        assert snap.exes() == frozenset(p.exe for p in snap.processes)

def test_power_registration_uses_the_three_arg_prototype():
    """Field bug (first real Windows run): the suspend/resume registration
    passed 2 arguments in swapped order with no ctypes prototype, dying with
    "OverflowError: int too long to convert" (64-bit HWND vs untyped int).
    The helper must send flags, the full 64-bit handle, and an out-pointer,
    in that order, with proper argtypes."""
    import ctypes
    import ctypes.wintypes as wt

    class FakeFunc:
        def __init__(self) -> None:
            self.argtypes = None
            self.restype = None
            self.seen = None

        def __call__(self, *args):
            self.seen = args
            return 0  # ERROR_SUCCESS

    class FakePowrprof:
        PowerRegisterSuspendResumeNotification = FakeFunc()

    fake = FakePowrprof()
    hwnd = 0x00000000BAADC0DE  # a plausible 64-bit window handle
    rc = power._register_suspend_resume_notification(fake, hwnd)
    assert rc == 0
    func = fake.PowerRegisterSuspendResumeNotification
    flags, receiver, out_ref = func.seen
    assert flags.value == power.DEVICE_NOTIFY_WINDOW_HANDLE == 0
    assert receiver.value == hwnd  # the full 64-bit handle must survive
    assert isinstance(out_ref._obj, wt.HANDLE)  # byref()'d registration out-param
    assert func.restype == wt.DWORD
    assert func.argtypes[0] == wt.DWORD and func.argtypes[1] == wt.HANDLE
