"""Shipped test/dev doubles.

These live in `app/` (not `tests/`) on purpose: the CLI simulator
(`app/cli/simulate.py`) and the automated tests exercise the *same* fakes, so
what you see in `python -m app.main --simulate` is produced by the exact code
path the unit tests assert on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.monitoring.processes import ProcessInfo, SystemSnapshot
from app.core.timeutils import monotonic


class FakeProcessSource:
    """Mutable fake machine: tests/dev code add/remove processes at will."""

    def __init__(self, processes: list[ProcessInfo] | None = None) -> None:
        self.processes: list[ProcessInfo] = list(processes or [])
        self.foreground_pid: int | None = None
        self.idle_seconds: float | None = 0.0
        self.clock = None  # set by the caller when a fake clock drives time
        self._next_pid = 1000
        self.enable_foreground_match = True

    # ------------------------------------------------------------ simulation
    def launch(self, exe: str) -> int:
        pid = self._next_pid
        self._next_pid += 1
        self.processes.append(ProcessInfo(pid=pid, exe=exe.lower(), name=exe))
        return pid

    def kill(self, pid: int) -> None:
        self.processes = [p for p in self.processes if p.pid != pid]
        if self.foreground_pid == pid:
            self.foreground_pid = None

    def focus(self, exe: str | None) -> None:
        if exe is None:
            self.foreground_pid = None
            return
        for p in self.processes:
            if p.exe == exe.lower():
                self.foreground_pid = p.pid
                return
        self.foreground_pid = None

    def focus_pid(self, pid: int) -> None:
        self.foreground_pid = pid

    def running(self, exe: str) -> bool:
        return any(p.exe == exe.lower() for p in self.processes)

    def exe_of(self, pid: int) -> str | None:
        for p in self.processes:
            if p.pid == pid:
                return p.exe
        return None

    def pid_of(self, exe: str) -> int | None:
        for p in self.processes:
            if p.exe == exe.lower():
                return p.pid
        return None

    # ------------------------------------------------------------- ProcessSource
    def snapshot(self) -> SystemSnapshot:
        captured = self.clock.mono() if self.clock is not None else monotonic()
        fg = self.foreground_pid
        if not self.enable_foreground_match:
            fg = None
        return SystemSnapshot(
            processes=tuple(self.processes),
            foreground_pid=fg,
            idle_seconds=self.idle_seconds,
            captured_mono=captured,
        )


@dataclass
class FakeProcessController:
    """Controller twin of FakeProcessSource: closing really removes processes."""

    source: FakeProcessSource
    graceful_ok: bool = True
    force_ok: bool = True
    requests: list[int] = field(default_factory=list)
    forces: list[int] = field(default_factory=list)

    def alive(self, pid: int) -> bool:
        return any(p.pid == pid for p in self.source.processes)

    def name_of(self, pid: int) -> str:
        for p in self.source.processes:
            if p.pid == pid:
                return p.exe
        return ""

    def request_close(self, pid: int) -> bool:
        self.requests.append(pid)
        if self.graceful_ok and self.alive(pid):
            self.source.kill(pid)  # simulated app honours the close request
        return self.graceful_ok

    def force_close(self, pid: int) -> bool:
        self.forces.append(pid)
        if self.force_ok:
            self.source.kill(pid)
        return self.force_ok

class FakeSchtasksRunner:
    """In-memory `schtasks.exe`: state for /Query, /Create, /Delete.

    Service tests used to construct a WatchdogScheduler with the *real*
    runner, so a pytest run on a real Windows machine actually registered a
    "Time Manager Watchdog" scheduled task mid-test (field-observed). With
    this, tests exercise the same code paths with zero OS side effects.
    """

    def __init__(self, *, installed: bool = False, fail_on: str | None = None) -> None:
        self.installed = installed
        self.fail_on = fail_on  # e.g. "Create" to force a FAILED outcome
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(cmd))
        action = cmd[1] if len(cmd) > 1 else ""
        if self.fail_on == action:
            return 1, "", "injected failure"
        if action == "/Query":
            return (0, "task", "") if self.installed else (1, "", "not found")
        if action == "/Create":
            self.installed = True
            return 0, "", ""
        if action == "/Delete":
            self.installed = False
            return 0, "", ""
        return 1, "", f"unsupported: {action}"

