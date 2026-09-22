"""Phase 7 — security & anti-bypass hardening.

Everything in this package is *defensive and transparent*: it detects and
records bypass attempts, it never hides anything from the user, and nothing
here can ever terminate a process or mutate enforcement decisions on its own.

Modules:
- `lifecycle` — agent START/STOP audit; detects an unclean (killed/crashed)
  previous run and reports the enforcement gap.
- `floor`     — per-day usage floors stored OUTSIDE the database, so erasing
  or rolling back `daily_usage` cannot grant free time to STRICT rules.
- `bypass`    — runtime bypass signals (extension silent while a STRICT
  website rule is armed and a browser is running).

The Windows-only watchdog (Task Scheduler) lives in `app/windows/watchdog.py`
because it is a Win32 adapter, but it is wired from the same place.
"""

from app.core.security.bypass import BypassAlert, BypassWatch
from app.core.security.floor import TamperAlert, UsageFloor, floor_path_for
from app.core.security.lifecycle import LifecycleAudit, UncleanStop

__all__ = [
    "BypassAlert",
    "BypassWatch",
    "LifecycleAudit",
    "TamperAlert",
    "UncleanStop",
    "UsageFloor",
    "floor_path_for",
]
