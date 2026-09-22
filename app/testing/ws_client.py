"""WebSocket client helper for tests, the CLI selftest and dev scripts.

Handles both websockets APIs (>=14 `websockets.asyncio.client` and the legacy
`websockets.connect`) so the same helper works with the version pinned in
requirements.txt and with current releases.
"""

from __future__ import annotations

import json
from typing import Any


def client_connect(uri: str, **kwargs: Any):
    """Return an awaitable/async-context-manager client connection."""
    import websockets

    mod = getattr(websockets, "asyncio", None)
    if mod is not None and hasattr(mod, "client"):
        return mod.client.connect(uri, **kwargs)
    return websockets.connect(uri, **kwargs)  # pragma: no cover - legacy path


class WsTestClient:
    """Thin async wrapper: typed HELLO, JSON send/recv, timeouts, drain."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.received: list[dict] = []

    async def send(self, payload: dict) -> None:
        await self.connection.send(json.dumps(payload))

    async def raw_send(self, text: str | bytes) -> None:
        await self.connection.send(text)

    async def recv(self, timeout: float = 3.0) -> dict:
        import asyncio

        raw = await asyncio.wait_for(self.connection.recv(), timeout=timeout)
        message = json.loads(raw)
        self.received.append(message)
        return message

    async def recv_where(self, predicate, timeout: float = 3.0, what: str = "message") -> dict:
        """Read (and keep skipping) until `predicate(message)` holds.

        The agent pushes RULE_UPDATE/BLOCK_DECISION/PING at its own pace, so a
        test that wants "the reply to what I just sent" must skip whatever else
        is queued ahead of it.
        """
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"No {what} within {timeout}s (saw {[m.get('type') for m in self.received]})"
                )
            message = await self.recv(timeout=remaining)
            if predicate(message):
                return message

    async def recv_type(self, wanted: str, timeout: float = 3.0) -> dict:
        """Read until a message of `wanted` type arrives (or timeout)."""
        return await self.recv_where(lambda m: m.get("type") == wanted, timeout, wanted)

    async def drain(self, timeout: float = 0.4) -> list[dict]:
        """Collect whatever is waiting (used before asserting on pushes)."""
        import asyncio

        out: list[dict] = []
        while True:
            try:
                out.append(await self.recv(timeout=timeout))
            except (asyncio.TimeoutError, TimeoutError):
                return out

    async def __aenter__(self) -> "WsTestClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self.connection.close()
        except Exception:  # noqa: BLE001
            pass


def hello(browser: str, browser_id: str, token: str, version: str = "1.0.0") -> dict:
    import time

    return {
        "type": "HELLO", "browser": browser, "browser_id": browser_id,
        "version": version, "token": token, "timestamp": int(time.time() * 1000),
    }


def tab_activity(browser: str, browser_id: str, domain: str, *, tab_id: int = 1,
                 active: bool = True, focused: bool = True, audible: bool = False) -> dict:
    import time

    return {
        "type": "TAB_ACTIVITY", "browser": browser, "browser_id": browser_id,
        "tab_id": tab_id, "domain": domain, "active": active,
        "window_focused": focused, "audible": audible,
        "timestamp": int(time.time() * 1000),
    }


def block_query(browser: str, browser_id: str, domain: str) -> dict:
    import time

    return {
        "type": "BLOCK_QUERY", "browser": browser, "browser_id": browser_id,
        "domain": domain, "timestamp": int(time.time() * 1000),
    }


def heartbeat(browser: str, browser_id: str) -> dict:
    import time

    return {"type": "HEARTBEAT", "browser": browser, "browser_id": browser_id,
            "timestamp": int(time.time() * 1000)}
