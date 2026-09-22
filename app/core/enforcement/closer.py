"""Graceful-then-force process closing.

Windows fact of life: `TerminateProcess` (and therefore `psutil.kill()`) is
*not* graceful — the target gets no chance to save. The polite path is to post
WM_CLOSE to its top-level windows (what clicking the X does), wait a grace
period, and only then force-terminate.

This module owns the escalation timeline; the actual OS calls sit behind
`ProcessController` so tests drive a fake and nothing real is ever killed
during a test run.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


class ProcessController(Protocol):
    def alive(self, pid: int) -> bool: ...
    def name_of(self, pid: int) -> str: ...
    def request_close(self, pid: int) -> bool:
        """Politely ask the process to exit. False = could not even ask."""
        ...

    def force_close(self, pid: int) -> bool:
        """Terminate. True = signal delivered (or process already gone)."""
        ...


@dataclass
class _Pending:
    rule_id: int
    rule_name: str
    pid: int
    deadline: float  # monotonic
    asked: bool = False


@dataclass(frozen=True)
class CloseResult:
    rule_id: int
    rule_name: str
    pid: int
    method: str  # GRACEFUL | FORCED | UNREACHABLE
    ok: bool
    detail: str = ""

    def as_detail(self) -> str:
        return f"pid={self.pid} {self.method}{(' ' + self.detail) if self.detail else ''}"


@dataclass
class ProcessCloser:
    """Runs the polite request -> wait -> force escalation."""

    controller: ProcessController
    graceful_timeout: float = 6.0
    force_wait: float = 2.0
    _pending: dict[int, _Pending] = field(default_factory=dict, init=False)
    _results: list[CloseResult] = field(default_factory=list, init=False)

    # ------------------------------------------------------------------ API
    def request(self, rule_id: int, rule_name: str, pids: tuple[int, ...], now: float) -> int:
        """Start closing `pids`; returns how many were newly scheduled."""
        started = 0
        for pid in pids:
            if pid in self._pending:
                continue  # already being closed
            if not self.controller.alive(pid):
                self._results.append(
                    CloseResult(rule_id, rule_name, pid, "UNREACHABLE", True, "already gone")
                )
                continue
            if self.graceful_timeout <= 0:
                ok = self.controller.force_close(pid)
                self._results.append(
                    CloseResult(rule_id, rule_name, pid, "FORCED", ok, "no-grace mode")
                )
                continue
            asked = self.controller.request_close(pid)
            self._pending[pid] = _Pending(
                rule_id=rule_id,
                rule_name=rule_name,
                pid=pid,
                deadline=now + self.graceful_timeout,
                asked=asked,
            )
            started += 1
            if not asked:
                log.info("Graceful close unavailable for pid %s; will force after timeout.", pid)
        return started

    def poll(self, now: float) -> list[CloseResult]:
        """Advance the escalation timeline; returns results ready this tick."""
        out: list[CloseResult] = []
        for pid, p in list(self._pending.items()):
            if not self.controller.alive(pid):
                out.append(CloseResult(p.rule_id, p.rule_name, pid, "GRACEFUL", True,
                                       "exited after request"))
                self._pending.pop(pid, None)
                continue
            if now >= p.deadline:
                delivered = self.controller.force_close(pid)
                method = "FORCED"
                if self.controller.alive(pid):
                    ok, detail = False, "still running after force"
                elif delivered:
                    ok, detail = True, "grace expired" if p.asked else "no graceful path"
                else:
                    ok, detail = True, "exited during grace period"
                out.append(CloseResult(p.rule_id, p.rule_name, pid, method, ok, detail))
                self._pending.pop(pid, None)
                if not ok:
                    log.warning("Failed to terminate pid %s (%s).", pid, p.rule_name)
        self._results.extend(out)
        return out

    def cancel(self, pid: int) -> None:
        self._pending.pop(pid, None)

    def pending(self) -> tuple[int, ...]:
        return tuple(sorted(self._pending))

    def drain(self) -> list[CloseResult]:
        """All results since the last drain (for logging/DB writes)."""
        out, self._results = self._results, []
        return out


class SystemProcessController:
    """Real OS controller: win32 WM_CLOSE on Windows, SIGTERM elsewhere.

    All win32 usage is imported lazily so this module stays importable and
    testable on any OS.

    Second line of defence: even if a caller bypasses `KillPolicy`, this
    controller refuses system PIDs and itself. (On POSIX, signalling pid 0 or a
    negative pid targets a whole process group — never acceptable here.)
    """

    def __init__(self) -> None:
        self._psutil = None

    @staticmethod
    def _refuse(pid: int) -> bool:
        if pid <= 4:
            log.error("Refusing to signal system pid %s.", pid)
            return True
        if pid == os.getpid():
            log.error("Refusing to signal our own pid.")
            return True
        return False

    def _ps(self):  # pragma: no cover - trivial
        if self._psutil is None:
            import psutil

            self._psutil = psutil
        return self._psutil

    def alive(self, pid: int) -> bool:
        try:
            ps = self._ps()
            if not ps.pid_exists(pid):
                return False
            return ps.Process(pid).status() not in (ps.STATUS_ZOMBIE, ps.STATUS_DEAD)
        except Exception:  # noqa: BLE001 - access denied etc.
            # AccessDenied means the process exists but is protected/other-user.
            return True

    def name_of(self, pid: int) -> str:
        try:
            return self._ps().Process(pid).name().lower()
        except Exception:  # noqa: BLE001
            return ""

    def request_close(self, pid: int) -> bool:
        if self._refuse(pid):
            return False
        if os.name == "nt":
            try:
                from app.windows import api  # lazy: Windows-only module

                return api.post_wm_close(pid) > 0
            except Exception:  # noqa: BLE001
                log.debug("WM_CLOSE path unavailable for pid %s.", pid, exc_info=True)
                return False
        try:
            os.kill(pid, signal.SIGTERM)  # dev/CI hosts only
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def force_close(self, pid: int) -> bool:
        if self._refuse(pid):
            return False
        try:
            self._ps().Process(pid).kill()
            # Give the OS a beat so immediate liveness checks are accurate.
            deadline = time.monotonic() + self.force_wait
            while time.monotonic() < deadline:
                if not self.alive(pid):
                    return True
                time.sleep(0.05)
            return not self.alive(pid)
        except Exception:  # noqa: BLE001
            return False
