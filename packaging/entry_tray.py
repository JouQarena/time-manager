"""PyInstaller windowed entry point — `TimeManagerTray.exe`.

Built with console=False: no terminal window flashes when Windows autostart
or the watchdog task launches it. It starts the desktop app (tray + dashboard,
which runs the monitor thread); `--monitor` also works and simply runs the
loop silently with logging to the profile's `logs/agent.log`.

A windowed process that dies before Qt comes up would exit *silently* —
stderr is lost, no dialog, nothing in Task Manager a second later (this
happened on the first real Windows machine: no window, no process, no log
file). So everything is wrapped in a crash net: any unhandled exception is
written to `%APPDATA%\\TimeManager\\logs\\tray-crash.log` and shown in a
native message box that needs no Qt at all.
"""

import sys
from pathlib import Path


def _attach_streams() -> None:
    """Windowed exes have no console: sys.stdout/stderr are None.

    Anything that writes to them directly — a logging StreamHandler, a
    library, an error printer — raises where the console exe would not.
    That is the one mechanism that can kill the tray exe before any dialog
    (a working `TimeManager.exe --gui` on the same machine, windowed exe
    silently gone, is exactly this signature). Mirror both streams into
    the profile's log folder; on a console build this is a no-op.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    import os

    try:
        base = os.environ.get("APPDATA") or str(Path.home() / ".config")
        log_dir = Path(base) / "TimeManager" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(  # noqa: SIM115 - lives for the process
            log_dir / "tray-stdio.log", "a", buffering=1,
            encoding="utf-8", errors="replace",
        )
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_attach_streams()


def _message_box(text: str) -> None:
    """Native error box. A separate function purely so tests can patch it —
    without the seam, running the test suite on Windows popped a *real*
    dialog mid-build (field-observed: "boom / line two" appearing during
    build.bat's pytest gate)."""
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None, text[:3500], "Time Manager — start-up error", 0x00000010
    )


def _show_crash(text: str) -> None:
    """Persist and display a start-up failure without depending on Qt."""
    try:
        import os

        base = os.environ.get("APPDATA") or str(Path.home() / ".config")
        log_dir = Path(base) / "TimeManager" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "tray-crash.log").write_text(text, encoding="utf-8")
    except OSError:
        pass  # even the crash log needs a writable profile; the box still shows
    try:
        _message_box(text)
    except Exception:  # noqa: BLE001 - non-Windows or no user32: file only
        pass


if __name__ == "__main__":
    code = 1
    try:
        from app.main import main

        # Default to the desktop app; extra flags still work, so
        # `TimeManagerTray.exe --gui-shot` stays usable as a diagnostic.
        code = main(["--gui"] + sys.argv[1:])
        # --gui-shot (and friends) already returned; never "pause" for them.
        ran_helper = bool(sys.argv[1:])
    except Exception:  # noqa: BLE001 - the net is the whole point
        import traceback

        _show_crash(traceback.format_exc())
        code = 1
        ran_helper = False
    if getattr(sys, "frozen", False) and not ran_helper and code not in (0, 3):
        # 0 = fine, 3 = "already running" (its own dialog was shown).
        # Anything else used to vanish wordlessly from a windowed exe.
        _show_crash(
            f"Time Manager exited with code {code}.\n\n"
            "If a window just explained the problem, ignore this notice.\n"
            "Details: %APPDATA%\\TimeManager\\logs\\ (agent.log, tray-crash.log)"
        )
    sys.exit(code)
