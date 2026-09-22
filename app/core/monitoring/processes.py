"""Process/window snapshots — the OS-independent shape of "what is running".

`SystemSnapshot` is the ONLY thing the rule engine, activity resolver and
enforcement policy ever see about the machine. That keeps all of Phase 3's
logic testable on any OS: tests hand in hand-built snapshots, production uses
`PsutilProcessSource` (+ win32 for the foreground window, see
`app/windows/win_monitor.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from app.core.timeutils import monotonic

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    exe: str  # lowercased image name, e.g. "discord.exe"
    name: str = ""  # original-case display name

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", self.exe)


@dataclass(frozen=True)
class SystemSnapshot:
    """Everything one monitoring tick needs to know about the machine."""

    processes: tuple[ProcessInfo, ...] = ()
    foreground_pid: int | None = None
    idle_seconds: float | None = None  # seconds since last user input, None = unknown
    captured_mono: float = field(default_factory=monotonic)

    # ---------------------------------------------------------------- helpers
    def exes(self) -> frozenset[str]:
        return frozenset(p.exe for p in self.processes)

    def pids_for(self, exes: Iterable[str]) -> tuple[int, ...]:
        wanted = {e.lower() for e in exes}
        return tuple(p.pid for p in self.processes if p.exe in wanted)

    def matched_pids(self, exes: Iterable[str]) -> dict[str, list[int]]:
        """exe -> pids, for the exes that are actually running."""
        wanted = {e.lower() for e in exes}
        out: dict[str, list[int]] = {}
        for p in self.processes:
            if p.exe in wanted:
                out.setdefault(p.exe, []).append(p.pid)
        return out

    def has(self, exe: str) -> bool:
        return exe.lower() in self.exes()

    def foreground_exe(self) -> str | None:
        if self.foreground_pid is None:
            return None
        for p in self.processes:
            if p.pid == self.foreground_pid:
                return p.exe
        return None


class ProcessSource(Protocol):
    """Anything that can produce a snapshot (psutil, tests, replay files)."""

    def snapshot(self) -> SystemSnapshot: ...


class PsutilProcessSource:
    """Real process enumeration via psutil.

    Uses image names (`Process.name()`), not full paths: matching rules are
    basename-based, and `exe()` raises AccessDenied for many system processes
    on Windows while `name()` does not.
    """

    def __init__(self) -> None:
        import psutil  # local import: keeps core importable without psutil

        self._psutil = psutil

    def processes(self) -> tuple[ProcessInfo, ...]:
        out: list[ProcessInfo] = []
        for proc in self._psutil.process_iter(["pid", "name"]):
            try:
                name = proc.info.get("name") or ""
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue  # process died mid-iteration
            if not name:
                continue
            out.append(ProcessInfo(pid=proc.info["pid"], exe=name.lower(), name=name))
        return tuple(out)

    def snapshot(self) -> SystemSnapshot:
        return SystemSnapshot(processes=self.processes(), captured_mono=monotonic())


class NullMonitorSource:
    """Snapshot source with no OS integration: processes only, no foreground.

    Used on non-Windows hosts so the monitor loop, IPC server and UI can be
    developed and tested everywhere. Foreground-dependent policies simply
    never see a foreground match.
    """

    def __init__(self, psutil_source: PsutilProcessSource | None = None) -> None:
        self._ps = psutil_source

    def snapshot(self) -> SystemSnapshot:
        procs = self._ps.processes() if self._ps is not None else ()
        return SystemSnapshot(processes=procs, foreground_pid=None,
                              idle_seconds=None, captured_mono=monotonic())
