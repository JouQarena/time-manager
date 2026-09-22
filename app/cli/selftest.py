"""`python -m app.main --ipc-selftest` — end-to-end proof of the browser link.

Starts the real IPC server on an ephemeral port against a temporary database,
then talks to it with a real WebSocket client over the real loopback socket:

    1. wrong token            -> ERROR{AUTH_FAILED} + close
    2. correct HELLO          -> WELCOME with the website rule snapshot
    3. TAB_ACTIVITY youtube   -> agent counts website time (monitor ticks)
    4. limit reached          -> BLOCK_DECISION pushed to the browser
    5. BLOCK_QUERY            -> answered from the same state
    6. heartbeat + PING/PONG  -> session stays alive
    7. disconnect             -> tab state dropped (no ghost counting)

Every step prints what actually crossed the wire. Runs on any OS (no Windows
needed: the fake machine from `app.testing.fakes` stands in for the desktop),
which is exactly why it doubles as the Phase 4 acceptance test.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.cli.simulate import build_demo_db
from app.core.clock import FakeClock
from app.core.enforcement.adapters import EnforcementExecutor, RecordingNotifier
from app.core.enforcement.closer import ProcessCloser
from app.core.enforcement.policy import KillPolicy
from app.core.monitoring.activity import ActivityPolicy, ActivityResolver
from app.core.monitoring.monitor import MonitorLoop
from app.core.rules.engine import RuleEngine
from app.core.tracking.tracker import Tracker
from app.database.db import Database
from app.ipc.bridge import AgentBridge
from app.ipc.server import IpcServer
from app.testing.fakes import FakeProcessController, FakeProcessSource
from app.testing.ws_client import (
    WsTestClient,
    block_query,
    client_connect,
    heartbeat,
    hello,
    tab_activity,
)

TOKEN = "a" * 64  # not a real secret: this is a throwaway selftest database
BROWSER_ID = "11111111-2222-3333-4444-555555555555"
BROWSER_ID_2 = "99999999-8888-7777-6666-555555555555"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"  [{'PASS' if self.ok else 'FAIL'}] {self.name:<34} {self.detail}"


def _build(tmpdir: Path, token: str):
    db, ids = build_demo_db(tmpdir / "ipc-selftest.db")
    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 9, 0))
    source = FakeProcessSource()
    source.clock = clock
    controller = FakeProcessController(source)
    tracker = Tracker(db, clock)
    engine = RuleEngine(db, tracker, clock)
    closer = ProcessCloser(controller, graceful_timeout=5.0)
    executor = EnforcementExecutor(db, closer, clock, policy=KillPolicy(),
                                  notifier=RecordingNotifier())
    bridge = AgentBridge(db, clock, token, tracker=tracker)
    resolver = ActivityResolver(policy=ActivityPolicy(website_grace_seconds=0))
    loop = MonitorLoop(db, engine, tracker, resolver, executor, source, clock,
                       closer=closer, interval=5.0)
    loop.set_web_state_provider(bridge.web_state)
    loop.set_tick_hook(lambda report: bridge.on_tick(report.outcomes))
    executor._browser_sink = bridge.browser_sink  # website enforcement -> browser
    return db, ids, clock, source, tracker, bridge, loop, executor


async def _simulate(client, clock, loop, browser_id: str, seconds: int,
                     tick: int = 5) -> None:
    """Advance simulated time the way a real browser would: a 5 s heartbeat
    plus a monitor tick per interval.

    Heartbeats are what keep a watched tab "live" (a user can sit on one tab
    for an hour without the extension sending anything else), so the selftest
    must send them exactly like the extension does.
    """
    for i in range(seconds // tick):
        clock.advance(tick)
        loop.tick_once()
        if i % 2 == 0:  # a beat every 10 simulated seconds keeps the link live
            await client.send(heartbeat("chrome", browser_id))
        await asyncio.sleep(0.01)  # let the server thread process it


async def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll until the server thread has processed an async message.

    TAB_ACTIVITY has no acknowledgement (by design: the extension must not
    block on the agent), so tests observe state instead of waiting for a reply.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _collect_until(client: WsTestClient, predicate, timeout: float = 3.0) -> list[dict]:
    """Read messages until `predicate(all_messages)` holds or time runs out."""
    import time as _time

    collected: list[dict] = []
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            collected.append(await client.recv(timeout=0.3))
        except (asyncio.TimeoutError, TimeoutError):
            if predicate(collected):
                break
            continue
        if predicate(collected):
            break
    return collected


async def _run(tmpdir: Path, token: str, verbose: bool = True,
               ping_interval: float = 5.0) -> dict:
    db, ids, clock, source, tracker, bridge, loop, executor = _build(tmpdir, token)
    server = IpcServer(
        bridge, port=0, agent_version="selftest",
        rate_limit_count=500,  # this client is far burstier than a real extension
        ping_interval=ping_interval,
    )
    steps: list[Step] = []
    transcript: list[str] = []

    if not server.start():
        return {"steps": [Step("server start", False, server.last_error)], "ok": False}
    uri = f"ws://127.0.0.1:{server.bound_port}/"
    transcript.append(f"listening on {uri}")

    # ---------------------------------------------------------- 1. bad token
    async with client_connect(uri) as conn:
        client = WsTestClient(conn)
        await client.send(hello("chrome", BROWSER_ID, "deadbeef" * 8))
        reply = await client.recv()
        steps.append(Step(
            "reject wrong token", reply.get("type") == "ERROR"
            and reply.get("code") == "AUTH_FAILED", json.dumps(reply),
        ))
        transcript.append(f"HELLO(bad token) -> {json.dumps(reply)}")

    # ------------------------------------------------- 2. hello + welcome
    connection = await client_connect(uri).__aenter__()
    client = WsTestClient(connection)
    await client.send(hello("chrome", BROWSER_ID, token))
    welcome = await client.recv_type("WELCOME")
    rules = welcome.get("website_rules", [])
    yt = next((r for r in rules if r["domain"] == "youtube.com"), None)
    steps.append(Step(
        "HELLO -> WELCOME", bool(yt) and yt.get("blocked") is False,
        f"{len(rules)} website rule(s), youtube remaining={yt and yt.get('remaining_seconds')}s",
    ))
    transcript.append("HELLO(good) -> WELCOME " + json.dumps(welcome)[:160])

    # ------------------------------------- 3. tab activity -> agent counts
    await client.send(tab_activity("chrome", BROWSER_ID, "youtube.com"))
    processed = await _wait_for(lambda: bool(bridge.web_state()))
    state = bridge.web_state()
    steps.append(Step(
        "TAB_ACTIVITY updates state", processed and "youtube.com" in (state or {}),
        f"web_state={state}",
    ))
    # The resolver cross-checks the OS: the extension's word is not enough.
    source.launch("chrome.exe")
    source.focus("chrome.exe")
    await _simulate(client, clock, loop, BROWSER_ID, 60)
    used = tracker.today_total(ids["yt"])
    steps.append(Step("website time accrues", used >= 55,
                      f"youtube used={used}s after 60s of ticks"))
    transcript.append(f"12 ticks x 5s -> youtube used={used}s")

    # --------------------------------------------- 4. limit -> BLOCK_DECISION
    await _simulate(client, clock, loop, BROWSER_ID, 180)  # 3-minute demo limit
    pushed = await _collect_until(
        client, lambda msgs: any(
            m.get("type") == "BLOCK_DECISION" and m.get("blocked") for m in msgs
        ), timeout=3.0,
    )
    decisions = [m for m in pushed if m.get("type") == "BLOCK_DECISION" and m.get("blocked")]
    steps.append(Step(
        "limit reached pushes BLOCK_DECISION", bool(decisions),
        json.dumps(decisions[-1])[:150] if decisions
        else f"got {[m.get('type') for m in pushed]}",
    ))
    rows = [r for r in db.list_enforcement(limit=50) if r["action"] == "BLOCK_WEBSITE"]
    steps.append(Step(
        "block logged as EXECUTED", bool(rows) and rows[-1]["outcome"] == "EXECUTED",
        f"enforcement_log: {rows[-1]['action'] if rows else 'none'} / "
        f"{rows[-1]['outcome'] if rows else '-'}",
    ))

    # --------------------------------------------------- 5. BLOCK_QUERY
    await client.send(block_query("chrome", BROWSER_ID, "music.youtube.com"))
    decision = await client.recv_where(
        lambda m: m.get("type") == "BLOCK_DECISION" and m.get("domain") == "music.youtube.com",
        timeout=3.0, what="decision for music.youtube.com")
    steps.append(Step(
        "BLOCK_QUERY answered", decision.get("blocked") is True
        and decision.get("reason") == "DAILY_LIMIT_REACHED",
        f"blocked={decision.get('blocked')} reason={decision.get('reason')}",
    ))
    await client.send(block_query("chrome", BROWSER_ID, "example.com"))
    free = await client.recv_where(
        lambda m: m.get("type") == "BLOCK_DECISION" and m.get("domain") == "example.com",
        timeout=3.0, what="decision for example.com")
    steps.append(Step(
        "unrelated domain is free", free.get("blocked") is False
        and free.get("reason") == "UNBLOCKED",
        f"blocked={free.get('blocked')} reason={free.get('reason')}",
    ))

    # ------------------------------------------------- 6. heartbeat + PING
    await client.send({"type": "HEARTBEAT", "browser": "chrome",
                       "browser_id": BROWSER_ID, "timestamp": 1})
    server._loop.call_soon_threadsafe(lambda: None)  # let the loop breathe
    pong_seen = False
    try:
        # The agent sends PING every 5 s; rule pushes may be queued ahead of it.
        await client.recv_where(lambda m: m.get("type") == "PING",
                                timeout=ping_interval + 4.0, what="PING")
        await client.send({"type": "PONG", "browser_id": BROWSER_ID})
        pong_seen = True
    except (asyncio.TimeoutError, TimeoutError):
        pass
    steps.append(Step("server PING -> client PONG", pong_seen,
                      "session still alive after heartbeat" if pong_seen
                      else "no PING within 7s"))
    steps.append(Step("session registered", server.status()["clients"] != [],
                      json.dumps(server.status()["clients"])))

    # ------------------------------------------- 7. stale data + disconnect
    await client.close()
    await asyncio.sleep(0.2)
    steps.append(Step("disconnect clears browser", not bridge.browsers.is_connected(),
                      f"connected={bridge.browsers.connected_count()}"))
    steps.append(Step("no ghost web activity", bridge.web_state() is None,
                      "web_state=None -> website rules stop counting"))

    # --------------------------------------------- 8. rate limit is enforced
    strict = IpcServer(bridge, port=0, agent_version="selftest",
                       rate_limit_count=5, rate_limit_window=10.0)
    started = strict.start()
    if started:
        limited_uri = f"ws://127.0.0.1:{strict.bound_port}/"
        async with client_connect(limited_uri) as conn:
            strict_client = WsTestClient(conn)
            await strict_client.send(hello("edge", BROWSER_ID_2, token))
            await strict_client.recv_type("WELCOME")
            for _ in range(12):  # 12 beats against a 5-message window
                await strict_client.send({"type": "HEARTBEAT", "browser": "edge",
                                          "browser_id": BROWSER_ID_2, "timestamp": 1})
            got = await _collect_until(
                strict_client,
                lambda msgs: any(m.get("code") == "RATE_LIMITED" for m in msgs),
                timeout=3.0,
            )
        strict.stop()
        steps.append(Step(
            "rate limit rejects a flood",
            any(m.get("code") == "RATE_LIMITED" for m in got),
            f"rate_limited={strict.rate_limited}, "
            f"codes={sorted({m.get('code') for m in got if m.get('code')})}",
        ))
    else:
        steps.append(Step("rate limit rejects a flood", False, strict.last_error))

    server.stop()
    # Snapshot the audit trail before the temporary database goes away.
    enforcement_rows = [dict(row) for row in db.list_enforcement(limit=200)]
    totals = {
        name: tracker.today_total(rid) for name, rid in ids.items()
    }
    db.close()

    ok = all(step.ok for step in steps)
    if verbose:
        print("IPC selftest — real WebSocket over 127.0.0.1, fake desktop.\n")
        for step in steps:
            print(step.render())
        print("\nWire transcript:")
        for line in transcript:
            print(f"  {line}")
        print(f"\nServer stats: in={server.messages_in} out={server.messages_out} "
              f"rate_limited={server.rate_limited} "
              f"last_error={server.last_error or 'none'}")
        print("Tracked totals: " + ", ".join(
            f"{name}={seconds}s" for name, seconds in totals.items()))
        print(f"\n{'ALL STEPS PASSED' if ok else 'SELFTEST FAILED'}")
    return {
        "steps": steps, "ok": ok, "server": server, "bridge": bridge,
        "enforcement": enforcement_rows, "totals": totals,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="IPC end-to-end selftest")
    parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="tm-ipc-") as tmp:
        result = asyncio.run(_run(Path(tmp), TOKEN))
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
