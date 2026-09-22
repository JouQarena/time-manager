"""Single-instance guard.

Two agents would double-count every monitored second, so exactly one may own
the profile at a time. The lock is a tiny file holding the owner PID:

* created atomically (`O_CREAT|O_EXCL`) so two racing starts cannot both win,
* stale locks (owner crashed) are detected by checking whether that PID is
  still alive and taken over,
* the lock is advisory only — it protects the *profile*, not the data file
  (SQLite/WAL handles concurrent readers on its own).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check that never raises."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import psutil

            return psutil.pid_exists(pid)
        os.kill(pid, 0)  # signal 0 = existence probe
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by somebody else
    except Exception:  # noqa: BLE001 - psutil missing, exotic OS
        return True  # be conservative: do not steal an unknown lock


@dataclass
class InstanceLock:
    path: Path
    owned: bool = False

    def read_owner(self) -> tuple[int, float] | None:
        """(pid, mtime) of the current lock holder, or None."""
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            pid = int(raw.split(":", 1)[0])
        except (OSError, ValueError):
            return None
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return pid, mtime

    def held_by_other(self) -> int | None:
        """PID of a *live* other holder, else None."""
        owner = self.read_owner()
        if owner is None:
            return None
        pid, _ = owner
        if pid == os.getpid():
            return None
        return pid if pid_alive(pid) else None

    def acquire(self) -> bool:
        """Take the lock. True = we own the profile."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"{os.getpid()}:{int(time.time())}"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            other = self.held_by_other()
            if other is not None:
                log.error("Another Time Manager instance is running (pid %s).", other)
                return False
            log.warning("Removing stale lock left by a dead process.")
            try:
                self.path.unlink()
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                log.exception("Could not take over the lock file.")
                return False
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        self.owned = True
        return True

    def release(self) -> None:
        if not self.owned:
            return
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            log.debug("Could not remove the lock file.", exc_info=True)
        self.owned = False
