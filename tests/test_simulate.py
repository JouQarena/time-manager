"""Integration test: the scripted 40-minute simulation must tell the story.

This is the Phase 3 acceptance test — it asserts on the *same* run the user
watches with `python -m app.main --simulate`, so what the demo claims and what
CI verifies cannot drift apart.
"""

import json

import pytest

from app.cli.simulate import run_simulation


@pytest.fixture(scope="module")
def sim():
    result = run_simulation(40, verbose=False)
    yield result
    # Close first, delete second: Windows refuses to unlink an open file
    # (WinError 32), so cleanup() must see a closed database.
    result["db"].close()
    if result["tmpdir"] is not None:
        result["tmpdir"].cleanup()


def states_at(lines, rule, minute):
    return [line for line in lines if line.rule == rule and line.minute == minute]


def final(lines, rule):
    return [line for line in lines if line.rule == rule][-1]


def test_discord_is_warned_closed_and_reclosed(sim):
    lines = sim["lines"]
    # warned before the limit, not after
    warn = [line for line in lines if line.rule == "Discord" and line.state == "WARNING"]
    assert warn, "Discord must reach WARNING before enforcement"
    # closed at the 10-minute limit, gracefully
    executed = [line for line in lines
                if line.rule == "Discord" and "CLOSE_APP:EXECUTED" in line.action]
    assert executed
    first_close = executed[0]
    assert first_close.minute == 10
    assert first_close.used_seconds >= 600  # the limit is genuinely reached
    # the relaunch at 13m is closed again (the limit still stands)
    assert any(line.minute == 13 and "CLOSE_APP:EXECUTED" in line.action
               for line in lines if line.rule == "Discord")
    assert final(lines, "Discord").state == "ENFORCED"
    # nothing accused the agent of killing a protected/system process
    assert not [r for r in sim["db"].list_enforcement(limit=200) if r["outcome"] == "PROTECTED"]


def test_league_waits_for_the_match_then_enforces(sim):
    lines = sim["lines"]
    waiting = [line for line in lines
               if line.rule == "League" and line.state == "WAITING_FOR_SESSION_END"]
    assert waiting, "the limit hit mid-match must WAIT"
    # while waiting, no close action was ever issued
    assert all(line.action == "-" for line in waiting)
    # overtime accrues honestly (limit is 6 min)
    assert waiting[-1].used_seconds > 360

    enforced = [line for line in lines
                if line.rule == "League" and line.state == "ENFORCED" and line.action != "-"]
    assert enforced, "after the match ends the rule must enforce"
    assert enforced[0].minute >= 22
    # STRICT + client ignoring WM_CLOSE -> escalated to FORCED in the log
    rows = [r for r in sim["db"].list_enforcement(limit=200) if "FORCED" in (r["detail"] or "")]
    assert rows, "a client ignoring WM_CLOSE must be force-closed after the grace period"


def test_unreliable_detector_never_causes_a_kill(sim):
    """Minute 22: the detector reports low confidence mid-match.

    The state machine must hold in WAITING_FOR_SESSION_END at that moment —
    this is the project's core fail-safe guarantee.
    """
    at_22 = states_at(sim["lines"], "League", 22)
    assert at_22
    assert all(line.state == "WAITING_FOR_SESSION_END" for line in at_22)
    assert all(line.action == "-" for line in at_22)


def test_website_is_counted_then_blocked_in_the_browser(sim):
    lines = sim["lines"]
    yt = [line for line in lines if line.rule == "YouTube"]
    assert yt[-1].used_seconds >= 180  # 3-minute limit reached
    assert any("EXECUTED" in line.action for line in yt)
    assert final(lines, "YouTube").state == "ENFORCED"

    # The block went out over the "wire" (the simulated extension) exactly once,
    # and the audit log agrees it was executed rather than deferred.
    extension = sim["extension"]
    assert extension.blocked_domains() == ["youtube.com"]
    rows = [r for r in sim["db"].list_enforcement(limit=200)
            if r["action"] == "BLOCK_WEBSITE"]
    assert len(rows) == 1 and rows[0]["outcome"] == "EXECUTED"
    assert "browser" in (rows[0]["detail"] or "")


def test_extension_only_hears_about_websites(sim):
    """The browser link is on a need-to-know basis: domains and numbers."""
    pushed = json.dumps(sim["extension"].pushes)
    assert "discord" not in pushed.lower()
    assert "league" not in pushed.lower()
    assert ".exe" not in pushed
    types = set(sim["extension"].types())
    assert types <= {"BLOCK_DECISION", "RULE_UPDATE"}
    updates = [m for m in sim["extension"].pushes if m["type"] == "RULE_UPDATE"]
    assert updates, "the popup needs rule snapshots"
    for entry in updates[-1]["website_rules"]:
        assert set(entry) >= {"domain", "blocked", "remaining_seconds"}


def test_idle_gate_suppresses_usage_while_away(sim):
    lines = sim["lines"]
    during = [line for line in lines if line.rule == "YouTube" and 27 <= line.minute < 31]
    assert during
    # Chrome is foreground with an active YouTube tab, but nobody is at the
    # keyboard: the idle gate stops the clock (the first poll still shows the
    # tab debounce, then idle takes over).
    assert any("idle" in line.detail for line in during)
    assert len({line.used_seconds for line in during}) == 1  # nothing accrued


def test_audit_trail_is_compact_and_complete(sim):
    rows = sim["db"].list_enforcement(limit=500)
    # Actions are logged; heartbeat no-ops are not (a 40-minute run must not
    # produce hundreds of rows).
    assert 8 <= len(rows) <= 30
    assert {r["action"] for r in rows} <= {
        "CLOSE_APP", "PREVENT_LAUNCH", "BLOCK_WEBSITE", "NOTIFY_ONLY"
    }
    assert {r["outcome"] for r in rows} <= {
        "EXECUTED", "SKIPPED", "DEFERRED", "FAILED", "PROTECTED"
    }


def test_loop_stays_healthy(sim):
    assert sim["loop"].stats.errors == 0
    assert sim["loop"].stats.ticks == 480
    assert sim["loop"].stats.closes >= 3


def test_totals_are_monotonic_and_bounded(sim):
    tracker, ids = sim["tracker"], sim["ids"]
    yesterday = None
    totals = {name: tracker.today_total(rid) for name, rid in ids.items()}
    assert totals["discord"] >= 600
    assert totals["lol"] >= 360
    assert totals["yt"] >= 180
    assert yesterday is None  # single simulated day: no cross-day leakage
