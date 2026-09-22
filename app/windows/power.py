"""Suspend/resume awareness.

Two layers, because neither alone is trustworthy:

1. `GapDetector` — deterministic, tested: the monitor loop sees a monotonic
   gap far bigger than its interval and concludes the machine was frozen
   (sleep, hibernate, or a fully blocked process). Works everywhere, no API.
2. `install_win32_power_listener` — precise: a message-only window receives
   WM_POWERBROADCAST / PBT_APMSUSPEND|PBT_APMRESUMEAUTOMATIC, so we react the
   instant the machine wakes rather than on the next tick. Windows-only and
   unverifiable here; docs/PHASE3.md lists the manual check.

Both paths call the same tracker hook (`notify_suspend`), which closes open
sessions so a nap never counts as usage.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from app.core.timeutils import monotonic

log = logging.getLogger(__name__)

WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007

DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000

#: ctypes callbacks must be kept alive for as long as Windows may call them;
#: a garbage-collected wndproc is a native crash waiting for the next message.
_WIN_KEEPALIVE: list = []


def _register_suspend_resume_notification(powrprof, hwnd) -> int:
    """DWORD PowerRegisterSuspendResumeNotification(Flags, Receiver, OUT Reg).

    The 3-argument, correctly-ordered prototype. The first real Windows run
    passed 2 arguments in swapped order with no argtypes, and ctypes died
    with "OverflowError: int too long to convert" (a 64-bit window handle
    does not fit an untyped 32-bit C int).
    """
    import ctypes
    import ctypes.wintypes as wt

    powrprof.PowerRegisterSuspendResumeNotification.argtypes = (
        wt.DWORD, wt.HANDLE, ctypes.POINTER(wt.HANDLE),
    )
    powrprof.PowerRegisterSuspendResumeNotification.restype = wt.DWORD
    registration = wt.HANDLE()
    return powrprof.PowerRegisterSuspendResumeNotification(
        wt.DWORD(DEVICE_NOTIFY_WINDOW_HANDLE), wt.HANDLE(hwnd),
        ctypes.byref(registration),
    )


@dataclass
class GapDetector:
    """Turns 'the clock jumped while nobody was looking' into a suspend event."""

    interval: float = 1.0
    factor: float = 3.0
    floor: float = 5.0
    _last: float | None = None

    def threshold(self) -> float:
        return max(self.interval * self.factor, self.floor)

    def check(self, now: float | None = None) -> float | None:
        """Return the anomalous gap in seconds (and reset), else None."""
        now = monotonic() if now is None else now
        previous, self._last = self._last, now
        if previous is None:
            return None
        gap = now - previous
        if gap > self.threshold():
            log.info("Gap of %.1fs implies suspend/resume (threshold %.1fs).", gap, self.threshold())
            return gap
        return None

    def reset(self) -> None:
        self._last = None


def install_win32_power_listener(
    on_suspend: Callable[[], None], on_resume: Callable[[], None]
) -> threading.Thread | None:
    """Start a hidden message-only window pumping power broadcasts.

    Returns the daemon thread, or None when unavailable/unsupported. Any
    failure is logged and swallowed: the GapDetector fallback keeps working.
    """
    try:
        import ctypes
        import ctypes.wintypes as wt

        if not hasattr(ctypes, "WINFUNCTYPE"):  # non-Windows
            return None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        powrprof = ctypes.WinDLL("powrprof", use_last_error=True)

        # A message-only window needs a registered class + a window proc.
        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
        )

        def _wndproc(hwnd, msg, wparam, lparam):  # pragma: no cover - Windows only
            if msg == WM_POWERBROADCAST:
                if wparam == PBT_APMSUSPEND:
                    _safe(on_suspend, "suspend")
                elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    _safe(on_resume, "resume")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = wndproc_type(_wndproc)
        _WIN_KEEPALIVE.append(proc)  # GC of a live callback = native crash

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT), ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
            ]

        wc_struct = WNDCLASS()
        wc_struct.lpfnWndProc = proc
        wc_struct.lpszClassName = "TimeManagerPowerSink"
        wc_struct.hInstance = kernel32.GetModuleHandleW(None)
        if not user32.RegisterClassW(ctypes.byref(wc_struct)):
            log.debug("RegisterClassW failed (already registered?).")
        # HWND restype: without it ctypes truncates the 64-bit window handle
        # to a signed 32-bit int, which overflowed on real Windows.
        user32.CreateWindowExW.restype = wt.HWND
        hwnd = user32.CreateWindowExW(
            0, wc_struct.lpszClassName, "TimeManagerPowerSink", 0,
            0, 0, 0, 0, None, None, wc_struct.hInstance, None
        )
        if not hwnd:
            log.warning("Could not create the power-sink window; using gap detection only.")
            return None
        # Modern API: survives across suspend, unlike RegisterPowerSettingNotification
        # hacks and works without an administrative service. Returns a DWORD
        # status (ERROR_SUCCESS = 0), not a handle.
        rc = _register_suspend_resume_notification(powrprof, hwnd)
        if rc != 0:
            log.warning(
                "PowerRegisterSuspendResumeNotification failed (rc=%s); using gap detection.",
                rc,
            )

        msg = wt.MSG()

        def _pump() -> None:  # pragma: no cover - Windows only
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            log.info("Power message loop exited.")

        thread = threading.Thread(target=_pump, name="power-events", daemon=True)
        thread.start()
        log.info("Win32 suspend/resume listener active.")
        return thread
    except Exception:  # noqa: BLE001
        log.warning("Win32 power listener unavailable; relying on gap detection.", exc_info=True)
        return None


def _safe(callback: Callable[[], None], what: str) -> None:  # pragma: no cover
    try:
        callback()
    except Exception:  # noqa: BLE001
        log.warning("Power %s callback failed.", what, exc_info=True)


def attach_to_tracker(tracker: object) -> bool:
    """Best-effort: wire suspend/resume into the tracker's sleep guard."""
    on_suspend = getattr(tracker, "notify_suspend", None)
    if on_suspend is None:
        return False
    return install_win32_power_listener(on_suspend, lambda: None) is not None
