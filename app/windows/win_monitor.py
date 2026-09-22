"""The production snapshot source: psutil processes + win32 foreground + idle."""

from __future__ import annotations

import logging

from app.core.monitoring.processes import (
    NullMonitorSource,
    ProcessSource,
    PsutilProcessSource,
    SystemSnapshot,
)
from app.core.timeutils import monotonic

log = logging.getLogger(__name__)


class WindowsMonitorSource(ProcessSource):
    def __init__(self, psutil_source: PsutilProcessSource | None = None) -> None:
        self._ps = psutil_source or PsutilProcessSource()
        self._warned = False

    def snapshot(self) -> SystemSnapshot:
        from app.windows import api  # lazy: keeps non-Windows imports clean

        procs = self._ps.processes()
        fg = api.foreground_pid()
        idle = api.idle_seconds()
        if fg is None and not self._warned:
            self._warned = True
            log.warning(
                "Foreground window could not be read; rules will fall back to "
                "'running but not foreground' until this works."
            )
        return SystemSnapshot(
            processes=procs, foreground_pid=fg, idle_seconds=idle, captured_mono=monotonic()
        )


def build_monitor_source() -> ProcessSource:
    """Windows source when possible, else a process-only source.

    On non-Windows hosts (dev, CI, this sandbox) the agent still runs the full
    loop; only foreground/idle signals are missing, which the UI surfaces as
    "monitoring degraded - Windows required for foreground detection".
    """
    from app.windows.api import is_windows

    if is_windows():
        try:
            return WindowsMonitorSource()
        except Exception:  # noqa: BLE001
            log.exception("Windows monitor source failed to initialise; degrading.")
    else:
        log.warning("Non-Windows host: foreground/idle detection unavailable (dev mode).")
    try:
        return NullMonitorSource(PsutilProcessSource())
    except Exception:  # noqa: BLE001 - psutil missing entirely
        log.exception("psutil unavailable; monitoring process list is empty.")
        return NullMonitorSource(None)
