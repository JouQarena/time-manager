"""Start with Windows: HKCU\\...\\Run entry (no admin rights needed).

`winreg` is imported lazily and can be injected, so the whole module is
unit-testable on Linux with a fake registry (see tests/test_startup.py).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from app.config.settings import APP_NAME

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _load_winreg(winreg: Any | None = None) -> Any | None:
    if winreg is not None:
        return winreg
    if os.name != "nt":
        return None
    import winreg as _w  # noqa: PLC0415 - Windows-only import

    return _w


def launch_command(*, gui: bool = False) -> str:
    """Command line that (re)starts the agent.

    gui=False (default): the head-less monitor loop. The watchdog task uses
    this — on a windowed (PyInstaller) build it produces no console flash
    every minute, and a windowed build logs to the profile's `logs/` folder.
    gui=True: the desktop app (tray + dashboard, which runs the monitor
    thread) — autostart uses this so logging in restores the tray.

    Frozen builds use the running executable itself; source builds use
    `python -m app.main`.
    """
    arg = "--gui" if gui else "--monitor"
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {arg}'
    return f'"{sys.executable}" -m app.main {arg}'


def is_enabled(winreg: Any | None = None, name: str = APP_NAME) -> bool:
    w = _load_winreg(winreg)
    if w is None:
        return False
    try:
        with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_READ) as key:
            value, _ = w.QueryValueEx(key, name)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        log.warning("Could not read the Run key.", exc_info=True)
        return False


def enable(
    command: str | None = None, winreg: Any | None = None, name: str = APP_NAME,
    *, gui: bool = True,
) -> bool:
    """Add/refresh the autostart entry. Returns True on success.

    Autostart launches the desktop app (gui=True): the tray runs the monitor
    thread, so logging in brings the agent back visibly.
    """
    w = _load_winreg(winreg)
    if w is None:
        log.warning("Autostart is Windows-only; ignoring enable().")
        return False
    cmd = command or launch_command(gui=gui)
    try:
        with w.CreateKeyEx(
            w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE
        ) as key:
            w.SetValueEx(key, name, 0, w.REG_SZ, cmd)
        log.info("Autostart enabled: %s", cmd)
        return True
    except OSError:
        log.exception("Could not write the Run key.")
        return False


def disable(winreg: Any | None = None, name: str = APP_NAME) -> bool:
    w = _load_winreg(winreg)
    if w is None:
        return False
    try:
        with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_SET_VALUE) as key:
            w.DeleteValue(key, name)
        log.info("Autostart disabled.")
        return True
    except FileNotFoundError:
        return True  # already absent
    except OSError:
        log.exception("Could not delete the Run value.")
        return False


def status(winreg: Any | None = None, name: str = APP_NAME) -> dict[str, object]:
    w = _load_winreg(winreg)
    check = is_enabled(w, name)
    command = None
    if check and w is not None:
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, RUN_KEY, 0, w.KEY_READ) as key:
                command, _ = w.QueryValueEx(key, name)
        except OSError:
            command = None
    return {
        "platform": os.name,
        "supported": w is not None,
        "enabled": check,
        "command": command,
        "expected": launch_command(),
    }


def sync_with_setting(launch_at_startup: bool, winreg: Any | None = None,
                      *, gui: bool = True) -> bool:
    """Make the registry match the user's setting; never raises.

    Autostart launches the desktop app (gui=True): the tray runs the monitor
    thread, so the user sees the agent come back after login.
    """
    return enable(winreg=winreg, gui=gui) if launch_at_startup else disable(winreg=winreg)
