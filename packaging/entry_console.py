"""PyInstaller console entry point — `TimeManager.exe`.

Every CLI command stays available in the packaged build (--monitor, --status,
--security, --init-db, --simulate, --gui-shot, ...). With no arguments the
packaged exe shows help instead of running the demo simulator (which is the
right default for a source checkout, but a confusing thing to double-click).
"""

import sys

from app.main import main

if __name__ == "__main__":
    args = sys.argv[1:] or ["--help"]
    code = main(args)
    # A double-clicked exe closes its console the instant it exits, which
    # erases the help text before anyone can read it (field-reported). When
    # the user gave NO arguments at all — i.e. they double-clicked — wait
    # for a keypress. Explicit invocations (--monitor from the watchdog,
    # scripts) never pause.
    if not sys.argv[1:] and getattr(sys, "frozen", False):
        try:
            input("\nPress Enter to close ...")
        except (EOFError, OSError):  # no interactive console available
            pass
    sys.exit(code)
