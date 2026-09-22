"""The IPC selftest is a test: if the demo breaks, CI notices.

`app/cli/selftest.py` is the acceptance demo the user runs by hand
(`python -m app.main --ipc-selftest`); this suite runs the exact same code and
asserts every step passed, so the printed transcript cannot drift from reality.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.cli.selftest import TOKEN, _run


@pytest.fixture(scope="module")
def result():
    with tempfile.TemporaryDirectory(prefix="tm-ipc-test-") as tmp:
        yield asyncio.run(_run(Path(tmp), TOKEN, verbose=False, ping_interval=1.0))


def test_every_selftest_step_passes(result):
    failed = [f"{step.name}: {step.detail}" for step in result["steps"] if not step.ok]
    assert failed == [], f"selftest failures: {failed}"


def test_selftest_covers_the_whole_flow(result):
    names = " | ".join(step.name for step in result["steps"])
    for expected in ("reject wrong token", "HELLO -> WELCOME", "TAB_ACTIVITY",
                     "accrues", "BLOCK_DECISION", "EXECUTED", "BLOCK_QUERY",
                     "PING", "disconnect", "rate limit"):
        assert expected in names, f"selftest no longer covers: {expected}"


def test_website_enforcement_is_executed_not_deferred(result):
    """With the browser link up, the audit log must say EXECUTED."""
    rows = [r for r in result["enforcement"] if r["action"] == "BLOCK_WEBSITE"]
    assert rows and rows[-1]["outcome"] == "EXECUTED"


def test_no_messages_were_lost(result):
    server = result["server"]
    assert server.messages_in > 0 and server.messages_out > 0
    assert server.last_error == ""
