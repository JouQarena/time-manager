"""Thin, defensive win32 wrappers.

Every function here returns a safe default (None/False/0) instead of raising
when win32 is unavailable or a call fails — the monitor must keep running even
if one OS call misbehaves. Nothing in this module is imported at app start on
non-Windows hosts (callers import it lazily).

Handles are 64-bit on 64-bit Windows, so `user32`/`kernel32` prototypes are
declared explicitly (ctypes' default int restype would truncate HWNDs).

NOTE: these wrappers can only be verified on a real Windows box; docs/PHASE3.md
lists exactly what to confirm by hand.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

_WM_CLOSE = 0x0010
_SMTO_ABORTIFHUNG = 0x0002
_GA_ROOT = 2
_PROCESS_TERMINATE = 0x0001

_user32: Any = None
_kernel32: Any = None


def is_windows() -> bool:
    return IS_WINDOWS


def _u32() -> Any:
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wt.HWND
        u.GetForegroundWindow.argtypes = []
        u.GetWindowThreadProcessId.restype = wt.DWORD
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.IsWindowVisible.restype = wt.BOOL
        u.IsWindowVisible.argtypes = [wt.HWND]
        u.GetAncestor.restype = wt.HWND
        u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        u.SendMessageTimeoutW.restype = wt.LPARAM
        u.SendMessageTimeoutW.argtypes = [
            wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        u.EnumWindows.restype = wt.BOOL
        u.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
        u.GetLastInputInfo.restype = wt.BOOL
        u.GetLastInputInfo.argtypes = [ctypes.c_void_p]
        _user32 = u
    return _user32


def _k32() -> Any:
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetTickCount64.restype = ctypes.c_ulonglong
        k.GetTickCount64.argtypes = []
        k.OpenProcess.restype = wt.HANDLE
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.TerminateProcess.restype = wt.BOOL
        k.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        k.CloseHandle.restype = wt.BOOL
        k.CloseHandle.argtypes = [wt.HANDLE]
        _kernel32 = k
    return _kernel32


# --------------------------------------------------------------------- windows
def foreground_pid() -> int | None:
    """PID owning the foreground window, or None if unavailable.

    None is a legitimate answer (UAC/secure desktop, lock screen, no window):
    callers treat it as "no app is being used right now" (nothing counted),
    never as "the previous app is still in use".
    """
    if not IS_WINDOWS:
        return None
    try:
        hwnd = _u32().GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD(0)
        _u32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:  # noqa: BLE001
        log.debug("GetForegroundWindow failed.", exc_info=True)
        return None


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input, or None if unavailable."""
    if not IS_WINDOWS:
        return None
    try:
        info = wt.LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not _u32().GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = int(_k32().GetTickCount64())
        # dwTime wraps every 49.7 days; mask to 32 bits before comparing.
        return max(0, tick - int(info.dwTime)) / 1000.0
    except Exception:  # noqa: BLE001
        log.debug("GetLastInputInfo failed.", exc_info=True)
        return None


# ------------------------------------------------------------------ processes
def post_wm_close(pid: int) -> int:
    """Politely ask every top-level window of `pid` to close (the X button).

    Returns how many windows a WM_CLOSE was delivered to. 0 means the process
    has no visible message-handling window (tray apps, services, a frozen UI)
    and the caller should escalate to a forced terminate.
    """
    if not IS_WINDOWS:
        return 0
    try:
        u = _u32()
    except Exception:  # noqa: BLE001
        return 0

    target = int(pid)
    sent = 0
    wndproc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        nonlocal sent
        try:
            wpid = wt.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if int(wpid.value) != target:
                return True
            # Visible top-level windows only: hidden helper windows are noise,
            # and WM_CLOSE to a child can hang behind a modal dialog.
            if not u.IsWindowVisible(hwnd):
                return True
            if u.GetAncestor(hwnd, _GA_ROOT) != hwnd:
                return True
            result = ctypes.c_ulonglong(0)
            u.SendMessageTimeoutW(
                hwnd, _WM_CLOSE, 0, 0, _SMTO_ABORTIFHUNG, 1000, ctypes.byref(result)
            )
            sent += 1
        except Exception:  # noqa: BLE001 - one bad window must not stop the rest
            log.debug("WM_CLOSE failed for a window of pid %s.", pid, exc_info=True)
        return True

    try:
        u.EnumWindows(wndproc(_callback), 0)
    except Exception:  # noqa: BLE001
        log.debug("EnumWindows failed for pid %s.", pid, exc_info=True)
    return sent


def force_terminate(pid: int) -> bool:
    """TerminateProcess via OpenProcess (what Task Manager's 'End task' does)."""
    if not IS_WINDOWS:
        return False
    try:
        k = _k32()
        handle = k.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(k.TerminateProcess(handle, 1))
        finally:
            k.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        log.debug("TerminateProcess failed for pid %s.", pid, exc_info=True)
        return False


# -------------------------------------------------------------------- helpers
def app_launch_command() -> str:
    """Command line used for the Run registry entry (see startup.py)."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return f'"{sys.executable}" --monitor'
    return f'"{sys.executable}" -m app.main --monitor'


def current_exe_path() -> Path:
    return Path(sys.executable)
