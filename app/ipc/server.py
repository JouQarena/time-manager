"""Authenticated loopback WebSocket server (the extension's only door).

Binding: `127.0.0.1` only — never `0.0.0.0`, so nothing on the LAN can reach it.
Auth: the extension must send `HELLO` with the machine's 64-hex token within
5 seconds (constant-time compare); everything else closes the socket.

Compatibility: written against the modern `websockets.asyncio.server` API and
falls back to the legacy `websockets.serve` signature used by websockets 13.x
(the version pinned in requirements.txt), so either major version works. If
websockets is missing entirely the agent still runs — it just reports that the
browser link is unavailable instead of crashing the monitor.

Threading: `start()` runs the event loop in a daemon thread so the synchronous
monitor loop (Phase 3) can keep calling `tick_once()`. Outbound pushes from
other threads go through `broadcast()`, which uses `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field

from app import __version__ as AGENT_VERSION
from app.ipc import protocol
from app.ipc.bridge import AgentBridge
from app.ipc.protocol import (
    HEARTBEAT_INTERVAL_SECONDS,
    HELLO_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    RATE_LIMIT_COUNT,
    RATE_LIMIT_WINDOW_SECONDS,
    STALE_AFTER_SECONDS,
    BlockQuery,
    Heartbeat,
    Hello,
    ProtocolError,
    RateLimiter,
    TabActivity,
)

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
CLOSE_AUTH_FAILED = 4001
CLOSE_STALE = 4002


def websockets_available() -> tuple[bool, str]:
    """(available, detail) — never raises."""
    try:
        import websockets

        return True, getattr(websockets, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


def _serve_factory():
    """Return (serve_callable, modern_api: bool) for the installed websockets.

    websockets >= 13 ships `websockets.asyncio.server` (the maintained
    implementation); anything older only has the legacy `websockets.serve`,
    whose handler signature is `handler(ws, path)`. The import is explicit
    because `websockets.asyncio` is only bound as an attribute of the package
    once something actually imports it — probing for the attribute would
    silently pick the legacy path on the first server of a process.
    """
    import websockets

    try:
        from websockets.asyncio.server import serve as modern_serve

        return modern_serve, True
    except Exception:  # noqa: BLE001 - older websockets, or exotic builds
        return websockets.serve, False


@dataclass
class ClientSession:
    browser: str
    browser_id: str
    version: str
    connection: object
    limiter: RateLimiter = field(default_factory=RateLimiter)
    connected_mono: float = field(default_factory=time.monotonic)
    last_message_mono: float = field(default_factory=time.monotonic)
    last_blocked: dict[str, bool] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.browser}:{self.browser_id[:8]}"


class IpcServer:
    def __init__(
        self,
        bridge: AgentBridge,
        *,
        host: str = DEFAULT_HOST,
        port: int = protocol.DEFAULT_PORT,
        agent_version: str = AGENT_VERSION,
        stale_after: float = STALE_AFTER_SECONDS,
        rate_limit_count: int = RATE_LIMIT_COUNT,
        rate_limit_window: float = RATE_LIMIT_WINDOW_SECONDS,
        ping_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.bridge = bridge
        self.host = host
        self.port = port
        self.agent_version = agent_version
        self.stale_after = stale_after
        self.rate_limit_count = rate_limit_count
        self.rate_limit_window = rate_limit_window
        self.ping_interval = ping_interval
        self.clients: dict[str, ClientSession] = {}
        self.bound_port: int | None = None
        self.last_error: str = ""
        #: Set when this process must not own the port (another agent is live);
        #: status() then reports running=False with this explanation.
        self.disabled_note: str = ""
        self.started_mono: float | None = None
        self.messages_in = 0
        self.messages_out = 0
        self.rate_limited = 0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        bridge.attach_broadcast(self.broadcast)

    # --------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 5.0) -> bool:
        """Bind and serve in a background thread. False = unavailable."""
        available, detail = websockets_available()
        if not available:
            self.last_error = f"websockets unavailable: {detail}"
            log.error("IPC server not started: %s", self.last_error)
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._ready.clear()
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="ipc-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            self.last_error = self.last_error or "IPC server did not become ready in time"
            log.error("%s", self.last_error)
            return False
        return self.bound_port is not None

    def stop(self, timeout: float = 3.0) -> None:
        """Close clients, stop serving and join the thread. Never hangs.

        A graceful `_shutdown()` is scheduled first; if the loop is still
        running after the grace period (a handler wedged on a half-open
        socket), `loop.stop()` is forced so the agent can always exit.
        """
        self._stop_requested.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            with suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and loop.is_running():
                time.sleep(0.02)
            if loop.is_running():
                log.debug("Forcing the IPC event loop to stop.")
                with suppress(Exception):
                    loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None

    # ------------------------------------------------------------------- thread
    def _run(self) -> None:  # pragma: no cover - thread body
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
            if self.bound_port is not None:
                loop.run_forever()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("IPC server error: %s", self.last_error, exc_info=True)
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()
                self._ready.set()
                self._loop = None

    async def _serve(self) -> None:
        serve, modern = _serve_factory()
        try:
            self._server = await serve(
                self._handle_modern if modern else self._handle_legacy,
                self.host, self.port,
                max_size=MAX_MESSAGE_BYTES,
                ping_interval=None,  # we do our own PING/PONG bookkeeping
                ping_timeout=None,
            )
        except OSError as exc:
            self.last_error = f"{exc}"
            log.error("Cannot bind %s:%s (%s) — browser link disabled.",
                      self.host, self.port, exc)
            self.bound_port = None
            self._ready.set()
            return
        self.bound_port = self._server.sockets[0].getsockname()[1]
        self.started_mono = time.monotonic()
        log.info("IPC server listening on ws://%s:%s (websockets %s API).",
                 self.host, self.bound_port, "asyncio" if modern else "legacy")
        self._ready.set()

    async def _shutdown(self) -> None:  # pragma: no cover - thread path
        for session in list(self.clients.values()):
            try:
                await session.connection.close(1001, "agent shutting down")
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                # Bounded: a half-open client socket must not delay shutdown.
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
        log.info("IPC server stopped.")
        # Stop run_forever() as soon as the cleanup above has finished, so
        # stop() returns promptly instead of waiting for its join timeout.
        asyncio.get_running_loop().call_soon(asyncio.get_running_loop().stop)

    # ---------------------------------------------------------------- handlers
    async def _handle_modern(self, connection) -> None:  # pragma: no cover - I/O
        await self._handle(connection)

    async def _handle_legacy(self, connection, _path=None) -> None:  # pragma: no cover
        await self._handle(connection)

    async def _handle(self, connection) -> None:
        remote = self._remote(connection)
        session: ClientSession | None = None
        try:
            first = await asyncio.wait_for(connection.recv(), timeout=HELLO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self._send(connection, protocol.error("BAD_MESSAGE", "HELLO timeout."))
            await self._close(connection, 1008, "no HELLO")
            return
        except Exception:  # noqa: BLE001 - client vanished
            return

        try:
            message = protocol.parse_message(first)
            if not isinstance(message, Hello):
                raise ProtocolError("BAD_MESSAGE", "First message must be HELLO.")
        except ProtocolError as exc:
            await self._send(connection, protocol.error(exc.code, str(exc)))
            await self._close(connection, 1008, "bad hello")
            return

        if not protocol.verify_token(message.token, self.bridge.token):
            log.warning("Rejected HELLO from %s: bad token.", remote)
            await self._send(connection, protocol.error("AUTH_FAILED", "Invalid token."))
            await self._close(connection, CLOSE_AUTH_FAILED, "auth failed")
            return

        session = ClientSession(
            browser=message.browser, browser_id=message.browser_id,
            version=message.version, connection=connection,
            limiter=RateLimiter(self.rate_limit_count, self.rate_limit_window),
        )
        self.clients[session.key] = session
        self.bridge.on_connect(message.browser, message.browser_id)
        log.info("Extension connected: %s v%s (%s) from %s",
                 message.browser, message.version, session.key, remote)
        await self._send(connection, protocol.welcome(self.agent_version,
                                                      self.bridge.rule_payload()))

        watchdog = asyncio.ensure_future(self._watchdog(session))
        try:
            async for raw in connection:
                self.messages_in += 1
                session.last_message_mono = time.monotonic()
                if not session.limiter.allow():
                    self.rate_limited += 1
                    await self._send(connection, protocol.error(
                        "RATE_LIMITED", "Too many messages; slow down."))
                    continue
                await self._dispatch(session, raw)
        except Exception as exc:  # noqa: BLE001 - connection level failure
            log.debug("Connection %s ended: %r", session.key, exc)
        finally:
            watchdog.cancel()
            self.clients.pop(session.key, None)
            self.bridge.on_disconnect(session.browser, session.browser_id)
            log.info("Extension session closed: %s (messages in=%s)", session.key,
                     self.messages_in)

    async def _dispatch(self, session: ClientSession, raw) -> None:
        connection = session.connection
        try:
            message = protocol.parse_message(raw)
        except ProtocolError as exc:
            await self._send(connection, protocol.error(exc.code, str(exc)))
            return

        if isinstance(message, TabActivity):
            self.bridge.on_tab_activity(
                message.browser, message.browser_id, tab_id=message.tab_id,
                domain=message.domain, active=message.active,
                window_focused=message.window_focused, audible=message.audible,
            )
            self._db_log_tab(message)
            decision = self.bridge.block_decision(message.domain) if message.domain else None
            if decision is not None:
                key = message.domain
                if session.last_blocked.get(key) != decision.blocked:
                    session.last_blocked[key] = decision.blocked
                    await self._send(connection, decision.to_message())
            return

        if isinstance(message, Heartbeat):
            self.bridge.on_heartbeat(message.browser, message.browser_id)
            return

        if isinstance(message, BlockQuery):
            await self._send(connection, self.bridge.block_decision(message.domain).to_message())
            return

        if isinstance(message, dict) and message.get("type") == "PONG":
            return  # freshness already updated by the caller

        await self._send(connection, protocol.error("BAD_MESSAGE", "Unhandled message."))

    async def _watchdog(self, session: ClientSession) -> None:
        """Server-side liveness: PING every interval, drop silent clients.

        The extension answers PING with PONG; three missed intervals (15 s)
        means the browser is gone (crashed, suspended, extension reloaded) and
        website timers must stop counting for it.
        """
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                silence = time.monotonic() - session.last_message_mono
                if silence > self.stale_after:
                    log.info("Client %s silent for %.0fs; closing.", session.key, silence)
                    await self._close(session.connection, CLOSE_STALE, "stale")
                    return
                await self._send(session.connection, protocol.ping())
        except asyncio.CancelledError:  # normal shutdown
            raise
        except Exception:  # noqa: BLE001
            return

    # ------------------------------------------------------------------ output
    def broadcast(self, message: str, *, browser_id: str | None = None) -> int:
        """Thread-safe push. Returns how many live connections got it."""
        loop = self._loop
        if loop is None or not loop.is_running() or not self.clients:
            return 0
        targets = [
            s for s in list(self.clients.values())
            if browser_id is None or s.browser_id == browser_id
        ]
        if not targets:
            return 0
        asyncio.run_coroutine_threadsafe(self._broadcast_to(targets, message), loop)
        return len(targets)

    async def _broadcast_to(self, targets: list[ClientSession], message: str) -> None:
        for session in targets:
            await self._send(session.connection, message)

    async def _send(self, connection, message: str) -> None:
        try:
            await connection.send(message)
            self.messages_out += 1
        except Exception as exc:  # noqa: BLE001 - dead socket, not our problem
            log.debug("Send failed: %r", exc)

    async def _close(self, connection, code: int, reason: str) -> None:
        try:
            await connection.close(code, reason)
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- helpers
    def _db_log_tab(self, message: TabActivity) -> None:
        """Keep the Phase 1 `browser_events` audit table fed (best effort)."""
        try:
            self.bridge.db.log_browser_event(
                message.browser, message.domain or "(none)",
                bool(message.active and message.window_focused),
            )
        except Exception:  # noqa: BLE001
            log.debug("browser_events write failed.", exc_info=True)

    @staticmethod
    def _remote(connection) -> str:
        try:
            addr = getattr(connection, "remote_address", None)
            return f"{addr[0]}:{addr[1]}" if addr else "unknown"
        except Exception:  # noqa: BLE001
            return "unknown"

    def status(self) -> dict:
        live = bool(self._thread and self._thread.is_alive())
        return {
            "running": live and not self.disabled_note,
            "note": self.disabled_note,
            "host": self.host,
            "port": self.bound_port,
            "clients": [{"key": s.key, "browser": s.browser, "version": s.version}
                        for s in self.clients.values()],
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "rate_limited": self.rate_limited,
            "last_error": self.last_error,
        }
