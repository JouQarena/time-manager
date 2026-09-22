"""Opt-in watchdog: a named Task Scheduler entry that revives a killed agent.

Bypass vector this closes (spec §15: "kill the monitoring process"): while
STRICT rules exist, killing the agent must not silently end enforcement.

How it stays honest and simple:
- The task is a plain, user-scope scheduled task named "Time Manager Watchdog"
  — visible in Task Scheduler, deletable there, never hidden.
- It runs the agent's normal `--monitor` entry point once a minute. If the
  agent is alive, the new instance hits the single-instance lock and exits
  with code 3 within moments; if the agent is gone, the new instance takes
  over the profile. No second code path, no duplicated logic — the watchdog
  *is* the normal startup path, invoked once a minute.
- No administrator rights are required: the task is created under the current
  user via the standard `schtasks.exe` CLI.
- Opt-in via the `strict_watchdog` setting; installing/removing it is written
  to the `agent_audit` trail, so the user can always see what is registered.

Off-Windows this module reports `unavailable` instead of pretending to work.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from app.windows.startup import launch_command

log = logging.getLogger(__name__)

TASK_NAME = "Time Manager Watchdog"
RUNNER_TIMEOUT = 15.0

#: runner(cmd) -> (returncode, stdout, stderr); injectable for tests.
Runner = Callable[[list[str]], tuple[int, str, str]]


def _subprocess_runner(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, timeout=RUNNER_TIMEOUT,
    )
    return proc.returncode, proc.stdout, proc.stderr


@dataclass(frozen=True)
class WatchdogResult:
    ok: bool
    action: str  # INSTALLED|ALREADY_PRESENT|REMOVED|NOT_PRESENT|UNAVAILABLE|FAILED
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "action": self.action, "detail": self.detail}


class WatchdogScheduler:
    def __init__(self, runner: Runner | None = None) -> None:
        self._runner: Runner = runner or _subprocess_runner

    # ------------------------------------------------------------------ state
    @property
    def available(self) -> bool:
        return os.name == "nt"

    def task_command(self) -> str:
        """The command line the scheduled task executes."""
        return f"{launch_command()} --monitor"

    def is_installed(self) -> bool | None:
        """True/False when known; None when it could not be determined."""
        if not self.available:
            return None
        try:
            rc, _out, _err = self._runner(
                ["schtasks", "/Query", "/TN", TASK_NAME]
            )
        except Exception as exc:  # noqa: BLE001 - schtasks missing/timeout
            log.warning("Watchdog status query failed: %r", exc)
            return None
        return rc == 0

    # ---------------------------------------------------------------- actions
    def install(self) -> WatchdogResult:
        if not self.available:
            return WatchdogResult(False, "UNAVAILABLE", "Task Scheduler watchdog needs Windows.")
        installed = self.is_installed()
        if installed is True:
            return WatchdogResult(True, "ALREADY_PRESENT", TASK_NAME)
        cmd = [
            "schtasks", "/Create", "/F",
            "/SC", "MINUTE", "/MO", "1",
            "/TN", TASK_NAME,
            "/TR", self.task_command(),
        ]
        try:
            rc, out, err = self._runner(cmd)
        except Exception as exc:  # noqa: BLE001
            return WatchdogResult(False, "FAILED", f"schtasks error: {exc!r}")
        if rc == 0:
            log.info("Watchdog task installed (%s).", self.task_command())
            return WatchdogResult(True, "INSTALLED", self.task_command())
        return WatchdogResult(False, "FAILED", (err or out or f"rc={rc}").strip()[:300])

    def uninstall(self) -> WatchdogResult:
        if not self.available:
            return WatchdogResult(False, "UNAVAILABLE", "Task Scheduler watchdog needs Windows.")
        installed = self.is_installed()
        if installed is False:
            return WatchdogResult(True, "NOT_PRESENT", TASK_NAME)
        try:
            rc, out, err = self._runner(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        except Exception as exc:  # noqa: BLE001
            return WatchdogResult(False, "FAILED", f"schtasks error: {exc!r}")
        if rc == 0:
            log.info("Watchdog task removed.")
            return WatchdogResult(True, "REMOVED", TASK_NAME)
        return WatchdogResult(False, "FAILED", (err or out or f"rc={rc}").strip()[:300])

    def ensure(self, enabled: bool) -> WatchdogResult:
        """Make reality match the setting; idempotent."""
        return self.install() if enabled else self.uninstall()

    def status(self) -> dict:
        return {
            "available": self.available,
            "installed": self.is_installed(),
            "task_name": TASK_NAME,
            "command": self.task_command(),
        }
