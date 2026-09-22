"""What exactly may be terminated, and when.

Two modes, one crisp distinction (documented in docs/PHASE3.md):

NORMAL  — polite. Processes that were already running when the limit was
          reached are *grandfathered* to the end of their run (we never yank
          an app out from under unsaved work at enforcement time); anything
          launched afterwards is closed on sight. CLOSE rules close
          immediately, that is what the user asked for.
STRICT  — no grandfathering. Every matching process is closed, every tick,
          for as long as the limit is exceeded.

Hard safety rules that nothing can override:
  * protected system processes are never touched,
  * the agent never terminates itself or its parent,
  * a rule's exes empty / unmatched => do nothing.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core.rules.models import Rule
from app.core.types import Mode

log = logging.getLogger(__name__)

#: Windows-critical image names the agent must never terminate (lowercase).
#: Small on purpose: only shells/brokers whose death breaks the session.
PROTECTED_EXES: frozenset[str] = frozenset({
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "sihost.exe",
    "taskhostw.exe", "ctfmon.exe", "fontdrvhost.exe", "audiodg.exe",
    "wudfhost.exe", "searchindexer.exe", "securityhealthservice.exe",
    "spoolsv.exe", "conhost.exe", "runtimebroker.exe",
})

_EXE_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".scr")


def _self_names() -> frozenset[str]:
    """Image names that would be the agent itself (frozen or scripted)."""
    names = set()
    for candidate in (sys.executable, sys.argv[0] if sys.argv else ""):
        if not candidate:
            continue
        names.add(Path(candidate).name.lower())
    # PyInstaller one-file builds unpack to a _MEI* dir; the real image name is
    # the frozen exe, already covered by sys.executable above.
    return names


def is_protected(
    exe: str,
    pid: int,
    *,
    extra: frozenset[str] = frozenset(),
    self_pid: int | None = None,
) -> str | None:
    """Return a human reason when this process must NOT be killed, else None."""
    exe_l = exe.lower()
    if exe_l in PROTECTED_EXES or exe_l in extra:
        return f"protected process ({exe_l})"
    if self_pid is None:
        self_pid = os.getpid()
    if pid == self_pid:
        return "agent process"
    if pid <= 4:  # Idle/System on Windows
        return f"system pid {pid}"
    if exe_l in _self_names():
        return "agent image"
    return None


@dataclass(frozen=True)
class KillDecision:
    pids_to_close: tuple[int, ...] = ()
    grandfathered: tuple[int, ...] = ()  # left running on purpose (NORMAL)
    skipped: tuple[tuple[str, int, str], ...] = ()  # (exe, pid, reason)
    reason: str = ""

    @property
    def has_work(self) -> bool:
        return bool(self.pids_to_close)

    def detail(self) -> str:
        bits = []
        if self.pids_to_close:
            bits.append("pids=" + ",".join(str(p) for p in self.pids_to_close))
        if self.grandfathered:
            bits.append("grandfathered=" + ",".join(str(p) for p in self.grandfathered))
        if self.skipped:
            bits.append("skipped=" + ",".join(f"{e}:{p}({r})" for e, p, r in self.skipped))
        return "; ".join(bits)


@dataclass
class GrandpaStore:
    """Remembers which PIDs were already running when enforcement began.

    Persisted in `settings` (keyed per rule/day) so a restart mid-enforcement
    does not suddenly declare every running instance a "new launch".
    """

    db: object  # Database (kept duck-typed to avoid an import cycle)
    day: str

    @staticmethod
    def _key(rule_id: int) -> str:
        return f"enforce.grandfathered.{rule_id}"

    def load(self, rule_id: int) -> frozenset[int] | None:
        raw = self.db.get_setting(self._key(rule_id), None)  # type: ignore[attr-defined]
        if not isinstance(raw, dict) or raw.get("day") != self.day:
            return None
        return frozenset(int(p) for p in raw.get("pids", []))

    def save(self, rule_id: int, pids: frozenset[int]) -> None:
        self.db.set_setting(  # type: ignore[attr-defined]
            self._key(rule_id), {"day": self.day, "pids": sorted(pids)}
        )

    def clear(self, rule_id: int) -> None:
        self.db.set_setting(self._key(rule_id), {"day": self.day, "pids": []})  # type: ignore[attr-defined]


class KillPolicy:
    """Pure decision: given what is running, which PIDs may be closed now?"""

    def __init__(
        self,
        *,
        extra_protected: frozenset[str] = frozenset(),
        self_pid: int | None = None,
    ) -> None:
        self._extra = frozenset(e.lower() for e in extra_protected)
        self._self_pid = self_pid

    def decide(
        self,
        *,
        rule: Rule,
        action: str | None,
        matched: dict[str, list[int]],
        grandfathered: frozenset[int] = frozenset(),
        first_enforcement: bool = False,
    ) -> KillDecision:
        if action not in ("CLOSE_APP", "PREVENT_LAUNCH"):
            return KillDecision(reason=f"action {action} needs no process work")

        running = sorted(pid for pids in matched.values() for pid in pids)
        live: set[int] = set()
        skipped: list[tuple[str, int, str]] = []
        for exe, pids in matched.items():
            for pid in pids:
                why = is_protected(exe, pid, extra=self._extra, self_pid=self._self_pid)
                if why:
                    skipped.append((exe, pid, why))
                else:
                    live.add(pid)

        # Only NORMAL + BLOCK has the notion of an exemption. CLOSE means
        # close, and STRICT means no leniency — for those, any `grandfathered`
        # set a caller passes is ignored, so a stale exemption can never
        # protect a process it should not.
        exempting = rule.mode == Mode.NORMAL and action == "PREVENT_LAUNCH"

        if exempting and first_enforcement:
            # The instances already running when the limit hit finish their
            # run; we only prevent *new* launches from now on.
            return KillDecision(
                pids_to_close=(),
                grandfathered=tuple(sorted(live)),
                skipped=tuple(skipped),
                reason="NORMAL/BLOCK: existing instances grandfathered",
            )

        if exempting:
            exempt = set(grandfathered) & live
            doomed = sorted(live - exempt)
        else:
            exempt, doomed = set(), sorted(live)

        return KillDecision(
            pids_to_close=tuple(doomed),
            grandfathered=tuple(sorted(exempt)),
            skipped=tuple(skipped),
            reason=(
                f"{rule.mode.value}/{rule.action.value}: closing {len(doomed)} "
                f"of {len(running)} matched process(es)"
            ),
        )
