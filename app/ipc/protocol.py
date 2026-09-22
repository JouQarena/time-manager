"""Local WebSocket IPC protocol v1 (validation + message builders).

The agent's WS server lives in app/ipc/server.py; this module is the shared
contract:
strict validators for every inbound field and builders for outbound messages.
Nothing here does I/O, so it is fully unit-testable.

Security rules enforced here:
- max message size 8 KiB; unknown types rejected (never crash, never dispatch).
- extension timestamps are parsed but MUST NOT be used for accounting.
- token comparison is constant-time (in server code; helper here).
"""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass

from app.core.rules.matcher import normalize_domain

PROTOCOL_VERSION = "1.0.0"
MAX_MESSAGE_BYTES = 8 * 1024
DEFAULT_PORT = 17846
HELLO_TIMEOUT_SECONDS = 5.0
HEARTBEAT_INTERVAL_SECONDS = 5.0
STALE_AFTER_SECONDS = 15.0
RATE_LIMIT_COUNT = 30
RATE_LIMIT_WINDOW_SECONDS = 10.0

BROWSERS = ("chrome", "edge", "firefox")

INBOUND_TYPES = ("HELLO", "TAB_ACTIVITY", "HEARTBEAT", "BLOCK_QUERY", "PONG")
BLOCK_REASONS = (
    "DAILY_LIMIT_REACHED",
    "SESSION_LIMIT_REACHED",
    "SCHEDULE_BLOCKED",
    "MANUAL",
    "UNBLOCKED",
)
ERROR_CODES = ("AUTH_FAILED", "BAD_MESSAGE", "RATE_LIMITED", "SERVER_ERROR")


class ProtocolError(ValueError):
    """Raised when an inbound message fails validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_token(provided: str, expected: str) -> bool:
    """Constant-time token comparison (both hex strings)."""
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


# ------------------------------------------------------------- inbound models
@dataclass(frozen=True)
class Hello:
    browser: str
    browser_id: str
    version: str
    token: str
    timestamp: int


@dataclass(frozen=True)
class TabActivity:
    browser: str
    browser_id: str
    tab_id: int
    domain: str  # normalized ("" allowed = non-web page, never matches rules)
    active: bool
    window_focused: bool
    audible: bool
    timestamp: int


@dataclass(frozen=True)
class Heartbeat:
    browser: str
    browser_id: str
    timestamp: int


@dataclass(frozen=True)
class BlockQuery:
    browser: str
    browser_id: str
    domain: str
    timestamp: int


def _req_str(obj: dict, field: str, *, max_len: int = 256) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ProtocolError("BAD_MESSAGE", f"Field {field!r} must be a non-empty string <={max_len}.")
    return value


def _req_browser(obj: dict) -> str:
    browser = _req_str(obj, "browser", max_len=16).lower()
    if browser not in BROWSERS:
        raise ProtocolError("BAD_MESSAGE", f"Unknown browser {browser!r}.")
    return browser


def _req_timestamp(obj: dict) -> int:
    ts = obj.get("timestamp")
    if isinstance(ts, bool) or not isinstance(ts, int) or ts < 0 or ts > 9_999_999_999_999:
        raise ProtocolError("BAD_MESSAGE", "Field 'timestamp' must be ms-epoch int.")
    return ts


def _req_bool(obj: dict, field: str) -> bool:
    value = obj.get(field)
    if not isinstance(value, bool):
        raise ProtocolError("BAD_MESSAGE", f"Field {field!r} must be boolean.")
    return value


def parse_message(raw: str | bytes) -> Hello | TabActivity | Heartbeat | BlockQuery | dict:
    """Parse + validate one inbound extension message.

    Returns a typed dataclass, or `{"type": "PONG", ...}` dict for PONG.
    Raises ProtocolError on any problem (caller replies ERROR, keeps serving).
    """
    if isinstance(raw, bytes):
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ProtocolError("BAD_MESSAGE", "Message too large.")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("BAD_MESSAGE", "Message is not UTF-8.") from exc
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("BAD_MESSAGE", "Message too large.")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("BAD_MESSAGE", f"Invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("BAD_MESSAGE", "Message must be a JSON object.")
    msg_type = obj.get("type")
    if msg_type not in INBOUND_TYPES:
        raise ProtocolError("BAD_MESSAGE", f"Unknown message type {msg_type!r}.")

    if msg_type == "HELLO":
        return Hello(
            browser=_req_browser(obj),
            browser_id=_req_str(obj, "browser_id", max_len=64),
            version=_req_str(obj, "version", max_len=32),
            token=_req_str(obj, "token", max_len=256),
            timestamp=_req_timestamp(obj),
        )
    if msg_type == "TAB_ACTIVITY":
        tab_id = obj.get("tab_id")
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id < 0:
            raise ProtocolError("BAD_MESSAGE", "Field 'tab_id' must be int >= 0.")
        raw_domain = obj.get("domain")
        if not isinstance(raw_domain, str) or len(raw_domain) > 253:
            raise ProtocolError("BAD_MESSAGE", "Field 'domain' must be a string <=253.")
        domain = ""
        if raw_domain.strip():
            try:
                domain = normalize_domain(raw_domain)
            except ValueError as exc:
                raise ProtocolError("BAD_MESSAGE", f"Bad domain: {exc}") from exc
        return TabActivity(
            browser=_req_browser(obj),
            browser_id=_req_str(obj, "browser_id", max_len=64),
            tab_id=tab_id,
            domain=domain,
            active=_req_bool(obj, "active"),
            window_focused=_req_bool(obj, "window_focused"),
            audible=obj.get("audible", False) if isinstance(obj.get("audible", False), bool) else False,
            timestamp=_req_timestamp(obj),
        )
    if msg_type == "HEARTBEAT":
        return Heartbeat(
            browser=_req_browser(obj),
            browser_id=_req_str(obj, "browser_id", max_len=64),
            timestamp=_req_timestamp(obj),
        )
    if msg_type == "BLOCK_QUERY":
        raw_domain = _req_str(obj, "domain", max_len=253)
        try:
            domain = normalize_domain(raw_domain)
        except ValueError as exc:
            raise ProtocolError("BAD_MESSAGE", f"Bad domain: {exc}") from exc
        return BlockQuery(
            browser=_req_browser(obj),
            browser_id=_req_str(obj, "browser_id", max_len=64),
            domain=domain,
            timestamp=_req_timestamp(obj),
        )
    # PONG
    return {"type": "PONG", "browser_id": _req_str(obj, "browser_id", max_len=64)}


# ------------------------------------------------------------ outbound builders
def now_ms() -> int:
    return int(time.time() * 1000)


def welcome(agent_version: str, website_rules: list[dict]) -> str:
    return json.dumps(
        {
            "type": "WELCOME",
            "agent_version": agent_version,
            "server_time": now_ms(),
            "website_rules": website_rules,
        }
    )


def block_decision(
    domain: str, blocked: bool, reason: str, reset_at: str, message: str
) -> str:
    if reason not in BLOCK_REASONS:
        raise ValueError(f"Unknown block reason {reason!r}.")
    return json.dumps(
        {
            "type": "BLOCK_DECISION",
            "domain": domain,
            "blocked": blocked,
            "reason": reason,
            "reset_at": reset_at,
            "message": message,
        }
    )


def rule_update(website_rules: list[dict]) -> str:
    return json.dumps({"type": "RULE_UPDATE", "website_rules": website_rules})


def ping() -> str:
    return json.dumps({"type": "PING", "server_time": now_ms()})


def error(code: str, message: str) -> str:
    if code not in ERROR_CODES:
        code = "SERVER_ERROR"
    return json.dumps({"type": "ERROR", "code": code, "message": message})


# ---------------------------------------------------------------- rate limiter
class RateLimiter:
    """Sliding-window limiter: N messages per window per connection."""

    def __init__(
        self,
        max_count: int = RATE_LIMIT_COUNT,
        window: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self.max_count = max_count
        self.window = window
        self._hits: list[float] = []

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        self._hits = [h for h in self._hits if h > cutoff]
        if len(self._hits) >= self.max_count:
            return False
        self._hits.append(now)
        return True
