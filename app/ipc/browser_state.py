"""Latest state reported by connected browser extensions (thread-safe).

The extension is untrusted input. Three invariants protect accounting:

1. **Agent time only.** Every record is stamped on receipt with the agent's
   monotonic clock; the extension's `timestamp` field is informational and is
   never used for counting (docs/PROTOCOL.md).
2. **Liveness of the connection, not of each report.** A user can watch a
   single YouTube tab for an hour without the extension sending a second
   `TAB_ACTIVITY` — that must keep counting. What proves the state is still
   valid is the **heartbeat** (every 5 s): if heartbeats stop (service worker
   dead, browser suspended, extension reloaded), the whole browser's state
   expires after `stale_after` (15 s = three missed beats).
3. **Tab reports are sticky** until superseded: `TAB_ACTIVITY` replaces that
   browser's current tab state, including `window_focused:false` events the
   extension sends when the browser loses OS focus.

The store never reads a clock itself: callers pass `mono` taken from the
agent's `Clock` (SystemClock in production, FakeClock in tests) so timestamps
and freshness checks can never mix two time bases.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 15.0


@dataclass(frozen=True)
class TabState:
    browser: str
    browser_id: str
    tab_id: int
    domain: str
    active: bool
    window_focused: bool
    audible: bool
    received_mono: float

    @property
    def counts(self) -> bool:
        """Is this the tab the user is looking at? (liveness checked elsewhere)"""
        return bool(self.active and self.window_focused and self.domain)


class BrowserStateStore:
    def __init__(self, stale_after: float = STALE_AFTER_SECONDS) -> None:
        self._lock = threading.RLock()
        self._tabs: dict[tuple[str, str], TabState] = {}
        self._beats: dict[tuple[str, str], float] = {}  # last sign of life
        self.stale_after = stale_after

    # --------------------------------------------------------------- lifecycle
    def connect(self, browser: str, browser_id: str, mono: float) -> None:
        with self._lock:
            self._beats[(browser, browser_id)] = mono
            self._tabs.pop((browser, browser_id), None)  # fresh session, fresh state
            log.info("Browser connected: %s/%s", browser, browser_id[:8])

    def disconnect(self, browser: str, browser_id: str) -> None:
        with self._lock:
            self._beats.pop((browser, browser_id), None)
            self._tabs.pop((browser, browser_id), None)
            log.info("Browser disconnected: %s/%s", browser, browser_id[:8])

    def forget_all(self) -> None:
        with self._lock:
            self._beats.clear()
            self._tabs.clear()

    # ----------------------------------------------------------------- updates
    def update(
        self, browser: str, browser_id: str, *, tab_id: int, domain: str,
        active: bool, window_focused: bool, audible: bool, mono: float,
    ) -> TabState:
        state = TabState(
            browser=browser, browser_id=browser_id, tab_id=tab_id, domain=domain,
            active=active, window_focused=window_focused, audible=audible,
            received_mono=mono,
        )
        with self._lock:
            # A tab report is itself proof of life: it refreshes the same
            # heartbeat clock the watchdog uses, so a chatty extension never
            # looks stale just because beats and reports are interleaved.
            self._beats[(browser, browser_id)] = mono
            self._tabs[(browser, browser_id)] = state
        return state

    def touch(self, browser: str, browser_id: str, mono: float) -> None:
        """Heartbeat: proof the reported tab state is still valid."""
        with self._lock:
            self._beats[(browser, browser_id)] = mono

    # ------------------------------------------------------------------- reads
    def _live(self, key: tuple[str, str], now_mono: float) -> bool:
        mono = self._beats.get(key)
        return mono is not None and (now_mono - mono) <= self.stale_after

    def is_connected(self) -> bool:
        with self._lock:
            return bool(self._beats)

    def connected_count(self) -> int:
        with self._lock:
            return len(self._beats)

    def browser_ids(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._beats)

    def tabs(self) -> tuple[TabState, ...]:
        with self._lock:
            return tuple(self._tabs.values())

    def active_domain(self, now_mono: float) -> str | None:
        with self._lock:
            live = [
                (self._beats.get(key, 0.0), tab)
                for key, tab in self._tabs.items()
                if tab.counts and self._live(key, now_mono)
            ]
        if not live:
            return None
        return max(live, key=lambda pair: pair[0])[1].domain

    def web_state(self, now_mono: float) -> dict[str, float] | None:
        """`{domain: last_sign_of_life}` for the monitor's activity resolver.

        ``None``  = no extension connected at all ("no browser link yet").
        ``{}``    = connected, but no tracked web page is being looked at.
        otherwise = domains in use, timestamped by the connection heartbeat so
                    the resolver's staleness window equals "three missed
                    heartbeats" and nothing else.
        """
        with self._lock:
            if not self._beats:
                return None
            out: dict[str, float] = {}
            for key, tab in self._tabs.items():
                if not tab.counts or not self._live(key, now_mono):
                    continue
                mono = self._beats[key]
                out[tab.domain] = max(out.get(tab.domain, 0.0), mono)
            return out

    def stale_browsers(self, now_mono: float) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(
                key for key in self._beats if not self._live(key, now_mono)
            )

    def summary(self) -> dict:
        with self._lock:
            tabs = [t for t in self._tabs.values() if t.counts]
            return {
                "connected": len(self._beats),
                "tabs": len(tabs),
                "domains": sorted({t.domain for t in tabs}),
            }

    def prune(self, now_mono: float) -> int:
        """Drop tab state for browsers that went quiet (heartbeat gap)."""
        removed = 0
        with self._lock:
            for key in list(self._tabs):
                if not self._live(key, now_mono):
                    del self._tabs[key]
                    removed += 1
            for key in list(self._beats):
                if now_mono - self._beats[key] > self.stale_after * 4:
                    # Socket is gone in every practical sense; the server side
                    # closes such connections, this is the safety net.
                    del self._beats[key]
                    removed += 1
        return removed
