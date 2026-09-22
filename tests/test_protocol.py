import json

import pytest

from app.ipc import protocol as p


def test_hello_ok():
    m = p.parse_message(json.dumps({
        "type": "HELLO", "browser": "chrome", "browser_id": "abc",
        "version": "0.1.0", "token": "x" * 64, "timestamp": 1726900000000}))
    assert isinstance(m, p.Hello) and m.browser == "chrome"


def test_tab_activity_ok_and_domain_normalized():
    m = p.parse_message(json.dumps({
        "type": "TAB_ACTIVITY", "browser": "edge", "browser_id": "abc", "tab_id": 9,
        "domain": "WWW.YouTube.com", "active": True, "window_focused": True,
        "timestamp": 1726900000000}))
    assert isinstance(m, p.TabActivity) and m.domain == "www.youtube.com"
    assert m.active and m.window_focused


def test_rejects_garbage_without_crashing():
    bad = [
        "not json",
        "[]",
        json.dumps({"type": "EXECUTE_ANYTHING"}),  # no such type, ever
        json.dumps({"type": "HELLO"}),  # missing fields
        json.dumps({"type": "TAB_ACTIVITY", "browser": "chrome", "browser_id": "a",
                    "tab_id": -1, "domain": "x.com", "active": True,
                    "window_focused": True, "timestamp": 1}),  # bad tab_id
        json.dumps({"type": "TAB_ACTIVITY", "browser": "netscape", "browser_id": "a",
                    "tab_id": 1, "domain": "x.com", "active": True,
                    "window_focused": True, "timestamp": 1}),  # bad browser
        json.dumps({"type": "TAB_ACTIVITY", "browser": "chrome", "browser_id": "a",
                    "tab_id": 1, "domain": "x.com", "active": "yes",  # not bool
                    "window_focused": True, "timestamp": 1}),
        "x" * (p.MAX_MESSAGE_BYTES + 1),
    ]
    for raw in bad:
        with pytest.raises(p.ProtocolError):
            p.parse_message(raw)


def test_verify_token_constant_time():
    assert p.verify_token("a" * 64, "a" * 64)
    assert not p.verify_token("a" * 64, "b" * 64)
    assert not p.verify_token("", "b" * 64)


def test_builders_and_rate_limiter():
    w = json.loads(p.welcome("1.0.0", []))
    assert w["type"] == "WELCOME"
    b = json.loads(p.block_decision("youtube.com", True, "DAILY_LIMIT_REACHED",
                                    "2026-09-22T00:00:00+03:00", "msg"))
    assert b["blocked"] is True and b["reason"] == "DAILY_LIMIT_REACHED"
    with pytest.raises(ValueError):
        p.block_decision("x.com", True, "NOPE", "t", "m")

    rl = p.RateLimiter(max_count=2, window=60)
    assert rl.allow(now=0.0) and rl.allow(now=1.0)
    assert not rl.allow(now=2.0)
    assert rl.allow(now=61.0)  # window slid
