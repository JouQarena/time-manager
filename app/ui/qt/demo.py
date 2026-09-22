"""Demo data for screenshots and GUI smoke tests.

Built with `app.cli.simulate`'s fixtures so the numbers on screen come from a
real run of the engine rather than hand-written constants.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from app import __version__
from app.cli.simulate import build_demo_db
from app.core.clock import FakeClock
from app.core.rules.models import Rule
from app.core.types import Action, Mode, RuleState, RuleType
from app.service import AgentService
from app.testing.fakes import FakeProcessSource


def demo_db(path: str | Path) -> Path:
    """A database with a day of plausible usage already recorded."""
    path = Path(path)
    db, ids = build_demo_db(path)
    day = "2026-09-21"
    db.add_daily(ids["discord"], day, 8 * 60)    # 10-min demo limit: well over
    db.add_daily(ids["lol"], day, 100 * 60)      # over its 6-min demo limit
    db.add_daily(ids["yt"], day, 12 * 60)        # 3-min demo limit: also over
    db.add_daily(ids["yt"], day, 12 * 60)              # 45m limit, light use

    # Extra rules so the dashboard shows a realistic mix.
    db.add_rule(Rule(
        name="Steam", type=RuleType.APPLICATION, target="steam.exe",
        executable="steam.exe", daily_limit_seconds=90 * 60,
        session_limit_seconds=45 * 60, warning_seconds=(900, 300),
        action=Action.CLOSE, mode=Mode.NORMAL,
    ))
    db.add_rule(Rule(
        name="Instagram", type=RuleType.WEBSITE, target="instagram.com",
        domain="instagram.com", daily_limit_seconds=20 * 60,
        warning_seconds=(300,), action=Action.BLOCK, mode=Mode.STRICT,
    ))
    db.add_rule(Rule(
        name="TikTok (study hours only)", type=RuleType.WEBSITE, target="tiktok.com",
        domain="tiktok.com", daily_limit_seconds=15 * 60,
        warning_seconds=(300,), action=Action.BLOCK, mode=Mode.NORMAL,
        schedule={"days": "WEEKDAYS", "windows": [["16:00", "19:00"]]},
    ))

    day_states = {
        ids["lol"]: RuleState.ENFORCED,
        ids["discord"]: RuleState.WARNING,
    }
    for rule_id, state in day_states.items():
        db.put_state(rule_id, day, state, frozenset({600}))

    # Times inside the demo day, so the timeline reads like a real afternoon.
    db.log_enforcement(ids["lol"], day, "CLOSE_APP", "STRICT", "EXECUTED",
                       "pids=4821; match finished, then closed",
                       created_at=f"{day}T14:52:11+00:00")
    db.log_enforcement(ids["discord"], day, "CLOSE_APP", "NORMAL", "SKIPPED",
                       "grandfathered=1180; already-running instance left alone",
                       created_at=f"{day}T15:07:03+00:00")
    db.log_enforcement(ids["yt"], day, "BLOCK_WEBSITE", "NORMAL", "EXECUTED",
                       "rule pushed to browser", created_at=f"{day}T15:31:48+00:00")
    db.close()
    return path


def demo_service(db_path: str | Path | None = None) -> AgentService:
    """A started AgentService on demo data with a fake clock and no OS calls.

    Time is fake, the process table is fake and the browser link is real but
    unauthenticated (no client connects), so this is safe to run anywhere.
    """
    tmp = None
    if db_path is None:
        tmp = tempfile.TemporaryDirectory(prefix="tm-demo-")
        db_path = Path(tmp.name) / "demo.db"
    demo_db(db_path)

    clock = FakeClock()
    clock.set_wall(datetime(2026, 9, 21, 15, 42))
    source = FakeProcessSource()
    source.clock = clock
    source.launch("discord.exe")
    source.focus("discord.exe")

    # Explicit settings: load_settings() would read the *user's real*
    # config.json (a test run on the developer's machine then inherited her
    # strict_watchdog=True and launch_at_startup=True) and the demo would
    # bind the real agent port. Isolated defaults + an OS-chosen port.
    from app.config.settings import AppSettings

    service = AgentService(db_path, clock=clock, source=source,
                           settings=AppSettings(ipc_port=0),
                           concurrency_guard=False, agent_version=f"{__version__}-demo")
    service._tmpdir = tmp  # keep the temp dir alive for the caller
    service.degraded = ""
    service.start_background()
    # One real tick so every rule state in the snapshot is computed by the
    # engine rather than hand-written by this demo module.
    if service.monitor is not None:
        service.monitor.tick_once()
    return service
