"""Time Manager desktop agent — entry point (Phase 1: CLI foundation).

GUI/tray/monitor loop arrive in Phases 3-4. Today this verifies the
foundation end to end: config -> database -> rules -> recovery.

Commands:
    python -m app.main --init-db     create/migrate DB, recover sessions, backup
    python -m app.main --status       show rules + today's usage + states
    python -m app.main --add-sample   insert 3 sample rules (dev convenience)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app import __version__
from app.config.settings import IPC_DEFAULT_PORT
from app.config.settings import (
    app_dir,
    default_db_path,
    get_or_create_token,
    load_settings,
)
from app.core.clock import SystemClock
from app.core.rules.models import Rule
from app.core.security.lifecycle import LifecycleAudit
from app.core.timeutils import format_duration, local_day_str
from app.database.db import Database

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("timemanager")


def cmd_init_db(db_path: str | None) -> int:
    settings = load_settings()
    path = db_path or str(settings.resolved_db_path)
    db = Database(path).connect()
    try:
        recovered = db.recover_open_sessions()
        backup = db.backup(app_dir() / "backups")
        get_or_create_token()  # ensure IPC token exists
        print(f"Time Manager v{__version__}")
        print(f"  app dir : {app_dir()}")
        print(f"  database: {path}")
        print(f"  recovered sessions: {recovered}")
        print(f"  backup: {backup}")
    finally:
        db.close()
    return 0


def cmd_status(db_path: str | None) -> int:
    settings = load_settings()
    path = db_path or str(settings.resolved_db_path)
    db = Database(path).connect()
    try:
        today = local_day_str()
        rules = db.list_rules()
        print(f"Time Manager v{__version__} — {today} — {len(rules)} rule(s)")
        if not rules:
            print("  (no rules yet; use --add-sample to try the demo rules)")
            return 0
        for rule in rules:
            assert rule.id is not None
            used = db.get_daily(rule.id, today)
            limit = rule.daily_limit_seconds
            if limit:
                bar = _bar(used, limit)
                print(
                    f"  [{rule.type.value:11}] {rule.name}: {bar} "
                    f"{format_duration(used)} / {format_duration(limit)} "
                    f"({format_duration(limit - used)} left)"
                )
            else:
                print(f"  [{rule.type.value:11}] {rule.name}: {format_duration(used)} today (no daily cap)")
            state = db.get_state(rule.id)
            if state:
                print(f"      state: {state[1].value} (day {state[0]})")
        tail = db.list_audit(limit=5)
        if tail:
            print("  recent security events:")
            for row in reversed(tail):
                print(f"      {row['recorded_at'][:19]} {row['kind']}: {row['detail'] or ''}")
        return 0
    finally:
        db.close()


def cmd_security(db_path: str | None) -> int:
    """Phase 7: what the anti-bypass layer knows (read-only; safe any time)."""
    from app.config.settings import app_dir
    from app.core.security.floor import UsageFloor, floor_path_for
    from app.core.types import Mode
    from app.windows.watchdog import WatchdogScheduler

    settings = load_settings()
    path = Path(db_path) if db_path else settings.resolved_db_path
    db = Database(path).connect()
    try:
        rules = db.list_rules()
        strict = [r for r in rules if r.mode == Mode.STRICT and r.enabled]
        print(f"Time Manager v{__version__} — anti-bypass status")
        print(f"  strict rules armed : {len(strict)}"
              + (f" ({', '.join(r.name for r in strict)})" if strict else ""))

        floor = UsageFloor(floor_path_for(path), SystemClock())
        floor.load()
        snap = floor.snapshot()
        if snap["day"] and snap["rules"]:
            tops = ", ".join(f"rule {k}: {v}s" for k, v in list(snap["rules"].items())[:5])
            print(f"  usage floor        : {snap['day']} ({tops})")
        else:
            print("  usage floor        : none recorded yet")

        wd = WatchdogScheduler()
        installed = wd.is_installed()
        print(f"  watchdog task      : "
              f"{'installed' if installed else 'not installed' if installed is not None else 'unavailable (non-Windows)'}"
              f"{' (enabled in settings)' if settings.strict_watchdog else ''}")

        regressed = floor.check_clock_regression()
        if regressed:
            print(f"  !! clock is {regressed:.0f}s BEHIND the last agent run — check for tampering")

        unclean = LifecycleAudit(db, SystemClock()).detect_unclean_stop()
        if unclean:
            print(f"  !! previous run (started {unclean.last_start_at}) never shut down cleanly")

        rows = db.list_audit(limit=15)
        if rows:
            print("  audit trail (newest first):")
            for row in rows:
                print(f"    {row['recorded_at'][:19]} [{row['severity']:8}] {row['kind']}: {row['detail'] or ''}")
        else:
            print("  audit trail        : empty")
        return 0
    finally:
        db.close()


def _bar(used: int, limit: int, width: int = 14) -> str:
    filled = min(width, int(round(width * used / max(1, limit))))
    return "█" * filled + "░" * (width - filled)


def cmd_add_sample(db_path: str | None) -> int:
    settings = load_settings()
    path = db_path or str(settings.resolved_db_path)
    db = Database(path).connect()
    try:
        samples = [
            Rule(
                name="League of Legends",
                type="GAME",  # type: ignore[arg-type]
                # Executable matching is exact, so all four are listed: the
                # client UI (LeagueClientUx.exe is the real one), the launcher
                # wrapper, the Riot bootstrap and the match process.
                target="LeagueClientUx.exe",
                executable="LeagueClientUx.exe",
                extra_executables=("LeagueClient.exe", "RiotClientServices.exe",
                                   "League of Legends.exe"),
                daily_limit_seconds=2 * 3600,
                session_limit_seconds=45 * 60,
                warning_seconds=(600, 300, 60),
                action="WAIT_FOR_SESSION_END",  # type: ignore[arg-type]
                mode="STRICT",  # type: ignore[arg-type]
            ),
            Rule(
                name="YouTube",
                type="WEBSITE",  # type: ignore[arg-type]
                target="youtube.com",
                domain="youtube.com",
                daily_limit_seconds=45 * 60,
                action="BLOCK",  # type: ignore[arg-type]
            ),
            Rule(
                name="Discord",
                type="APPLICATION",  # type: ignore[arg-type]
                target="discord.exe",
                executable="discord.exe",
                daily_limit_seconds=3600,
                action="CLOSE",  # type: ignore[arg-type]
            ),
        ]
        for rule in samples:
            rule_id = db.add_rule(rule)
            print(f"  added [{rule_id}] {rule.name}")
        return 0
    finally:
        db.close()


def cmd_simulate(minutes: int, tick: int) -> int:
    """Deterministic Phase 3 demo: full pipeline on a fake machine."""
    from app.cli.simulate import run_simulation

    run_simulation(minutes, tick_seconds=tick)
    return 0


def cmd_show_token() -> int:
    """Print the pairing token + connection details for the extension options."""
    from app.config.settings import get_or_create_token, token_path

    token = get_or_create_token()
    print("Browser extension pairing")
    print(f"  token : {token}")
    print(f"  stored: {token_path()}")
    print(f"  url   : ws://127.0.0.1:{IPC_DEFAULT_PORT}/")
    print("\nPaste the token into the extension's Options page "
          "(chrome://extensions -> Time Manager Companion -> Options).")
    print("The token is a machine secret: keep it out of screenshots.")
    return 0


def cmd_regen_token() -> int:
    from app.config.settings import regenerate_token

    print(f"New token: {regenerate_token()}")
    print("Restart the agent (--monitor) and re-paste it in the extension options.")
    return 0


def cmd_show_extension_path() -> int:
    """Where the unpacked browser extension lives in this install."""
    from app.resources import extension_dir, is_frozen

    path = extension_dir()
    print(f"Browser extension folder: {path}")
    if not (path / "manifest.json").is_file():
        print("  !! manifest.json not found — this build is missing its extension data.")
        return 1
    print("Install it in Chrome/Edge:")
    print("  1. Open chrome://extensions (or edge://extensions)")
    print("  2. Enable Developer mode")
    print("  3. 'Load unpacked' -> select the folder above")
    print("  4. Open the extension's Options and paste the pairing token (--show-token)")
    if is_frozen():
        print("(Packaged build: keep the folder in place; the agent reads it from there.)")
    return 0


def cmd_ipc_selftest() -> int:
    """End-to-end proof of the browser link over a real loopback socket."""
    import logging

    logging.basicConfig(level=logging.INFO, format="  · %(name)s: %(message)s")
    # The websockets library narrates every frame; keep our own story readable.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    from app.cli.selftest import main as selftest_main

    return selftest_main([])


def cmd_gui(db_path: str | None) -> int:
    """Launch the desktop app (tray + dashboard)."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    _setup_frozen_file_logging()
    try:
        from app.ui.qt.app import run_gui
    except ImportError as exc:
        print(f"GUI unavailable: {exc}\nInstall PySide6 (pip install -r requirements.txt).")
        return 4
    return run_gui(db_path)


def cmd_gui_shot(db_path: str | None, out_dir: str | None) -> int:
    """Render the GUI offscreen to PNGs (docs/CI; needs no display)."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.ui.qt.app import create_app, render_screenshots
    from app.ui.qt.demo import demo_service

    target_dir = out_dir or str(Path(__file__).resolve().parents[1] / "docs" / "screenshots")
    service = demo_service(db_path)
    service.start()
    create_app()
    written = render_screenshots(target_dir, service=service)
    service.stop()
    for path in written:
        print(f"  wrote {path}")
    return 0


def cmd_status_json(db_path: str | None, *, include_history: bool = True) -> int:
    """Machine-readable status for scripts, tests and support requests.

    Runs the agent *without* starting the monitor loop: it reads the database,
    the settings and (when another instance is live) reports that instead of
    disturbing it.
    """
    import json

    from app.config.lockfile import InstanceLock
    from app.config.settings import get_or_create_token
    from app.service import AgentService

    path = Path(db_path) if db_path else load_settings().resolved_db_path  # property
    holder = InstanceLock(path.with_suffix(".lock")).held_by_other()
    note = f"another instance is running (pid {holder}); read-only view" if holder else ""

    # Read-only: never take the profile lock and never claim the browser port
    # that the live agent owns — this command must be safe to run at any time.
    service = AgentService(db_path, concurrency_guard=False, bind_ipc=not holder,
                           read_only_note=note)
    payload: dict
    try:
        service.start()
        snapshot = service.snapshot()
        payload = snapshot.to_dict()
        if include_history:
            payload["recent_enforcement"] = service.enforcement_history(limit=20)
    finally:
        service.stop(backup=False)
    payload["token_present"] = bool(get_or_create_token())
    print(json.dumps(payload, indent=2, sort_keys=False))
    return 0


def cmd_detector_selftest() -> int:
    """End-to-end proof of the game-detector fail-safe chain (Phase 6)."""
    from app.cli.detector_selftest import run

    result = run(verbose=True)
    return 0 if result["ok"] else 1


def _setup_frozen_file_logging() -> None:
    """Packaged builds log to the profile: a windowed exe has no console, and
    the monitor must leave a trail (profile/logs/agent.log, 3 x 1 MB)."""
    if not getattr(sys, "frozen", False):
        return
    from logging.handlers import RotatingFileHandler

    from app.config.settings import profile_dir

    logs_dir = profile_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / "agent.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)


def cmd_monitor(db_path: str | None) -> int:
    """Run the real monitor loop (Windows; Ctrl-C to stop)."""
    import logging

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    _setup_frozen_file_logging()
    from app.cli.monitor import run_monitor

    return run_monitor(db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time Manager desktop agent")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", default=None, help="SQLite path (default: profile dir)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init-db", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--add-sample", action="store_true")
    group.add_argument("--simulate", nargs="?", type=int, const=40, metavar="MINUTES",
                       help="Phase 3 end-to-end demo on a fake machine (default 40)")
    group.add_argument("--monitor", action="store_true",
                       help="run the real monitoring loop (Windows; Ctrl-C to stop)")
    group.add_argument("--show-extension-path", action="store_true",
                       help="print where the unpacked browser extension lives")
    group.add_argument("--show-token", action="store_true",
                       help="print the browser-extension pairing token")
    group.add_argument("--regen-token", action="store_true",
                       help="invalidate the old token and print a new one")
    group.add_argument("--ipc-selftest", action="store_true",
                       help="end-to-end browser-link test over a real WebSocket")
    group.add_argument("--detector-selftest", action="store_true",
                       help="end-to-end game-detector (LoL) fail-safe test")
    group.add_argument("--gui", action="store_true",
                       help="launch the desktop app (tray + dashboard)")
    group.add_argument("--gui-shot", action="store_true",
                       help="render the GUI offscreen to PNGs (docs/CI, no display needed)")
    group.add_argument("--status-json", action="store_true",
                       help="print machine-readable agent status as JSON")
    group.add_argument("--security", action="store_true",
                       help="show anti-bypass status and the security audit trail")
    parser.add_argument("--tick", type=int, default=5, help="simulator tick seconds")
    parser.add_argument("--out", default=None, help="output directory for --gui-shot")
    args = parser.parse_args(argv)

    try:
        if args.init_db:
            return cmd_init_db(args.db)
        if args.status:
            return cmd_status(args.db)
        if args.security:
            return cmd_security(args.db)
        if args.add_sample:
            return cmd_add_sample(args.db)
        if args.monitor:
            return cmd_monitor(args.db)
        if args.show_token:
            return cmd_show_token()
        if args.show_extension_path:
            return cmd_show_extension_path()
        if args.regen_token:
            return cmd_regen_token()
        if args.ipc_selftest:
            return cmd_ipc_selftest()
        if args.detector_selftest:
            return cmd_detector_selftest()
        if args.gui:
            return cmd_gui(args.db)
        if args.gui_shot:
            return cmd_gui_shot(args.db, args.out)
        if args.status_json:
            return cmd_status_json(args.db)
        return cmd_simulate(args.simulate or 40, args.tick)
    except Exception:
        log.exception("Command failed")  # full traceback: a bare message
        return 1                          # is not debuggable from a screenshot


if __name__ == "__main__":
    sys.exit(main())
