"""IpcServer over a real loopback socket (no mocks of the transport).

These tests bind an ephemeral port on 127.0.0.1, so they cover what unit tests
cannot: the actual websockets wiring, auth handshake, framing limits, watchdog
behaviour and broadcast threading.
"""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from app.core.clock import FakeClock
from app.core.rules.models import Rule
from app.core.tracking.tracker import Tracker
from app.core.types import Action, RuleType
from app.database.db import Database
from app.ipc import protocol
from app.ipc.bridge import AgentBridge
from app.ipc.server import IpcServer, websockets_available
from app.testing.ws_client import (
    WsTestClient,
    block_query,
    client_connect,
    hello,
    tab_activity,
)

pytestmark = pytest.mark.skipif(
    not websockets_available()[0], reason="websockets not installed"
)

TOKEN = "b" * 64
BROWSER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class ServerHarness:
    def __init__(self, tmp_path: Path, **server_kwargs):
        self.db = Database(tmp_path / "ipc.db").connect()
        self.clock = FakeClock()
        self.clock.set_wall(datetime(2026, 9, 21, 9, 0))
        self.tracker = Tracker(self.db, self.clock)
        self.bridge = AgentBridge(self.db, self.clock, TOKEN, tracker=self.tracker)
        self.server = IpcServer(self.bridge, port=0, **server_kwargs)

    def add_web_rule(self, domain="youtube.com", **kw):
        base = dict(name="YouTube", type=RuleType.WEBSITE, target=domain, domain=domain,
                    daily_limit_seconds=600, warning_seconds=(60,), action=Action.BLOCK)
        base.update(kw)
        return self.db.add_rule(Rule(**base))

    @property
    def uri(self) -> str:
        return f"ws://127.0.0.1:{self.server.bound_port}/"

    def close(self):
        self.server.stop()
        self.db.close()


@pytest.fixture()
def harness(tmp_path):
    h = ServerHarness(tmp_path, ping_interval=1.0, stale_after=2.0,
                      rate_limit_count=500)
    assert h.server.start(), h.server.last_error
    yield h
    h.close()


async def connect(uri: str, token: str = TOKEN, browser: str = "chrome",
                  browser_id: str = BROWSER_ID) -> WsTestClient:
    connection = await client_connect(uri).__aenter__()
    client = WsTestClient(connection)
    await client.send(hello(browser, browser_id, token))
    await client.recv_type("WELCOME")
    return client


# ------------------------------------------------------------------ handshake
def test_binds_loopback_and_reports_status(harness):
    status = harness.server.status()
    assert status["running"] and status["port"] > 0
    assert harness.server.host == "127.0.0.1"  # never 0.0.0.0
    assert status["clients"] == []


def test_welcome_contains_agent_version_and_rules(harness):
    harness.add_web_rule()

    async def scenario():
        async with await connect(harness.uri) as client:
            return client.received[0]

    welcome = asyncio.run(scenario())
    assert welcome["type"] == "WELCOME"
    assert welcome["agent_version"]
    assert welcome["website_rules"][0]["domain"] == "youtube.com"
    assert isinstance(welcome["server_time"], int)


def test_bad_token_is_rejected_and_closed(harness):
    async def scenario():
        connection = await client_connect(harness.uri).__aenter__()
        client = WsTestClient(connection)
        await client.send(hello("chrome", BROWSER_ID, "f" * 64))
        reply = await client.recv()
        closed = False
        try:
            await asyncio.wait_for(connection.recv(), timeout=2.0)
        except Exception:  # noqa: BLE001 - closed or timed out
            closed = True
        return reply, closed

    reply, closed = asyncio.run(scenario())
    assert reply["type"] == "ERROR" and reply["code"] == "AUTH_FAILED"
    assert closed, "server must close after an auth failure"
    assert harness.bridge.browsers.is_connected() is False


def test_non_hello_first_message_is_rejected(harness):
    async def scenario():
        connection = await client_connect(harness.uri).__aenter__()
        client = WsTestClient(connection)
        await client.send({"type": "HEARTBEAT", "browser": "chrome",
                           "browser_id": BROWSER_ID, "timestamp": 1})
        return await client.recv()

    reply = asyncio.run(scenario())
    assert reply["type"] == "ERROR" and reply["code"] == "BAD_MESSAGE"


def test_hello_timeout(harness):
    """An extension that connects and says nothing must be dropped."""
    async def scenario():
        connection = await client_connect(harness.uri).__aenter__()
        client = WsTestClient(connection)
        try:
            reply = await client.recv(timeout=protocol.HELLO_TIMEOUT_SECONDS + 2.0)
            return reply
        except Exception as exc:  # noqa: BLE001
            return {"type": "CLOSED", "error": repr(exc)}

    reply = asyncio.run(scenario())
    assert reply["type"] in ("ERROR", "CLOSED")


# ------------------------------------------------------------ message handling
def test_tab_activity_updates_bridge_state(harness):
    async def scenario():
        async with await connect(harness.uri) as client:
            await client.send(tab_activity("chrome", BROWSER_ID, "youtube.com"))
            for _ in range(50):
                if harness.bridge.web_state():
                    return harness.bridge.web_state()
                await asyncio.sleep(0.02)
        return None

    state = asyncio.run(scenario())
    assert state == {"youtube.com": harness.clock.mono()}


def test_block_query_answered(harness):
    rid = harness.add_web_rule(daily_limit_seconds=60)
    harness.db.add_daily(rid, "2026-09-21", 60)

    async def scenario():
        async with await connect(harness.uri) as client:
            await client.send(block_query("chrome", BROWSER_ID, "youtube.com"))
            return await client.recv_where(
                lambda m: m.get("type") == "BLOCK_DECISION", timeout=3.0)

    decision = asyncio.run(scenario())
    assert decision["blocked"] is True and decision["reason"] == "DAILY_LIMIT_REACHED"


def test_invalid_json_and_unknown_type_get_errors(harness):
    async def scenario():
        async with await connect(harness.uri) as client:
            await client.raw_send("{not json")
            first = await client.recv_type("ERROR")
            await client.send({"type": "EXECUTE_ME", "cmd": "whoami"})
            second = await client.recv_type("ERROR")
            return first, second

    first, second = asyncio.run(scenario())
    assert first["code"] == "BAD_MESSAGE" and second["code"] == "BAD_MESSAGE"
    # The session survives bad input: the agent never crashes on junk.
    assert harness.server.status()["messages_in"] >= 2


def test_oversized_message_is_rejected(harness):
    async def scenario():
        async with await connect(harness.uri) as client:
            await client.raw_send(json.dumps({
                "type": "TAB_ACTIVITY", "browser": "chrome", "browser_id": BROWSER_ID,
                "tab_id": 1, "domain": "youtube.com", "active": True,
                "window_focused": True, "timestamp": 1, "padding": "x" * 9000,
            }))
            try:
                return await client.recv(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                return {"type": "CLOSED", "error": repr(exc)}

    reply = asyncio.run(scenario())
    assert reply["type"] in ("ERROR", "CLOSED")  # 8 KiB cap enforced


def test_bad_domain_is_rejected_but_session_survives(harness):
    async def scenario():
        async with await connect(harness.uri) as client:
            await client.send(tab_activity("chrome", BROWSER_ID, "http://10.0.0.5/admin"))
            error = await client.recv_type("ERROR")
            await client.send(tab_activity("chrome", BROWSER_ID, "youtube.com"))
            for _ in range(50):
                if harness.bridge.web_state():
                    break
                await asyncio.sleep(0.02)
            return error, harness.bridge.web_state()

    error, state = asyncio.run(scenario())
    assert error["code"] == "BAD_MESSAGE"  # IP literals are refused
    assert state == {"youtube.com": harness.clock.mono()}


def test_ip_tab_activity_with_empty_domain_is_allowed(harness):
    """Non-web pages report an empty domain (docs/PROTOCOL.md)."""
    async def scenario():
        async with await connect(harness.uri) as client:
            before = harness.server.messages_in
            await client.send(tab_activity("chrome", BROWSER_ID, "", active=False,
                                           focused=True))
            for _ in range(60):
                if harness.server.messages_in > before:
                    break
                await asyncio.sleep(0.02)
            # No ERROR came back, and the store correctly holds no trackable tab
            # while the connection is still open.
            return harness.server.messages_in - before, harness.bridge.web_state()

    accepted, state = asyncio.run(scenario())
    assert accepted == 1 and state == {}


# ------------------------------------------------------------------ rate limit
def test_rate_limit(harness):
    harness.server.rate_limit_count = 5

    async def scenario():
        async with await connect(harness.uri) as client:
            for _ in range(12):
                await client.send({"type": "HEARTBEAT", "browser": "chrome",
                                   "browser_id": BROWSER_ID, "timestamp": 1})
            return await client.recv_where(
                lambda m: m.get("code") == "RATE_LIMITED", timeout=3.0)

    reply = asyncio.run(scenario())
    assert reply["type"] == "ERROR" and harness.server.rate_limited >= 1


# -------------------------------------------------------------------- liveness
def test_watchdog_pings_and_drops_silent_clients(harness):
    async def scenario():
        connection = await client_connect(harness.uri).__aenter__()
        client = WsTestClient(connection)
        await client.send(hello("chrome", BROWSER_ID, TOKEN))
        await client.recv_type("WELCOME")
        pings = 0
        closed = False
        try:
            while True:  # never answer PING -> must be dropped after 2 s
                message = await client.recv(timeout=3.0)
                if message.get("type") == "PING":
                    pings += 1
        except Exception:  # noqa: BLE001
            closed = True
        # The server-side cleanup (on_disconnect) runs right after the close.
        for _ in range(100):
            if not harness.bridge.browsers.is_connected():
                break
            await asyncio.sleep(0.02)
        return pings, closed, harness.bridge.browsers.is_connected()

    pings, closed, still_connected = asyncio.run(scenario())
    assert pings >= 1, "the server must PING to prove the client is alive"
    assert closed, "a silent client must be disconnected"
    assert still_connected is False, "a dropped client must stop being counted"


def test_disconnect_clears_tab_state(harness):
    async def scenario():
        client = await connect(harness.uri)
        await client.send(tab_activity("chrome", BROWSER_ID, "youtube.com"))
        for _ in range(50):
            if harness.bridge.web_state():
                break
            await asyncio.sleep(0.02)
        await client.close()
        for _ in range(50):
            if not harness.bridge.browsers.is_connected():
                break
            await asyncio.sleep(0.02)
        return harness.bridge.web_state()

    assert asyncio.run(scenario()) is None  # no ghost counting


# ---------------------------------------------------------------- broad/casting
def test_broadcast_reaches_all_clients(harness):
    async def scenario():
        first = await connect(harness.uri, browser_id="browser-1")
        second = await connect(harness.uri, browser_id="browser-2", browser="edge")
        await asyncio.sleep(0.1)
        sent = harness.server.broadcast(protocol.ping())
        got = []
        for client in (first, second):
            try:
                got.append(await client.recv_where(
                    lambda m: m.get("type") == "PING", timeout=3.0))
            except TimeoutError:
                got.append(None)
        await first.close()
        await second.close()
        return sent, got

    sent, got = asyncio.run(scenario())
    assert sent == 2 and all(g is not None for g in got)


def test_broadcast_without_clients_is_zero(harness):
    assert harness.server.broadcast(protocol.ping()) == 0


def test_broadcast_to_one_browser_only(harness):
    async def scenario():
        first = await connect(harness.uri, browser_id="browser-1")
        second = await connect(harness.uri, browser_id="browser-2", browser="edge")
        await asyncio.sleep(0.1)
        sent = harness.server.broadcast(protocol.ping(), browser_id="browser-2")
        await asyncio.sleep(0.2)
        first_got = [m for m in first.received if m.get("type") == "PING"]
        second_got = [m for m in second.received if m.get("type") == "PING"]
        await first.close()
        await second.close()
        return sent, first_got, second_got

    sent, first_got, second_got = asyncio.run(scenario())
    assert sent == 1 and first_got == []


def test_second_instance_same_port_fails_cleanly(tmp_path):
    """Two agents must not fight over the port: the loser reports, not crashes."""
    first = ServerHarness(tmp_path)
    assert first.server.start()
    second_bridge = AgentBridge(first.db, first.clock, TOKEN)
    second = IpcServer(second_bridge, port=first.server.bound_port)
    assert second.start(timeout=3.0) is False
    assert second.last_error
    second.stop()
    first.close()


def test_stop_is_clean_and_idempotent(harness):
    harness.server.stop()
    harness.server.stop()
    assert harness.server.status()["running"] is False
