"""App settings: JSON file in the user profile + well-known paths.

Windows: %APPDATA%\\TimeManager\\{config.json, agent_token, timemanager.db}
Other OS (dev/tests): $XDG_CONFIG_HOME or ~/.config/TimeManager/...

The secret IPC token lives in `agent_token` (created once, 32 random bytes
hex). It is NEVER logged.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "TimeManager"
IPC_DEFAULT_PORT = 17846


def app_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        return base / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


def profile_dir() -> Path:
    """Directory holding the database, token, config and backups."""
    return app_dir()


def default_db_path() -> Path:
    return app_dir() / "timemanager.db"


def token_path() -> Path:
    return app_dir() / "agent_token"


def config_path() -> Path:
    return app_dir() / "config.json"


@dataclass
class AppSettings:
    monitoring_interval: float = 1.0
    website_grace_seconds: int = 3
    website_stale_seconds: int = 15
    strict_relaunch_guard_seconds: int = 300
    default_warning_seconds: list[int] = field(default_factory=lambda: [600, 300, 60])
    theme: str = "system"  # system|light|dark
    language: str = "en"
    launch_at_startup: bool = False
    start_minimized: bool = False
    notifications_enabled: bool = True
    ipc_port: int = IPC_DEFAULT_PORT
    db_path: str = ""  # empty = default_db_path()
    # Phase 7: opt-in Task Scheduler watchdog that revives a killed agent
    # (Windows-only; transparent named task, see app/windows/watchdog.py).
    strict_watchdog: bool = False

    def validate(self) -> None:
        if not 0.25 <= self.monitoring_interval <= 60:
            raise ValueError("monitoring_interval must be 0.25..60 seconds.")
        # ipc_port 0 = "OS, pick a free port" — for the demo and the tests,
        # so they never fight a real agent for 17846 (found when the first
        # Windows machine ran pytest while its own agent was running).
        if self.theme not in ("system", "light", "dark"):
            raise ValueError("theme must be system|light|dark.")
        if self.ipc_port != 0 and not 1024 <= self.ipc_port <= 65535:
            raise ValueError("ipc_port must be 1024..65535 (or 0: OS chooses).")

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path) if self.db_path else default_db_path()


def load_settings() -> AppSettings:
    path = config_path()
    if not path.exists():
        settings = AppSettings()
        save_settings(settings)
        return settings
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        settings = AppSettings(**{k: v for k, v in data.items() if k in AppSettings.__dataclass_fields__})
    except Exception as exc:
        log.warning("Config %s unreadable (%s); using defaults.", path, exc)
        return AppSettings()
    try:
        settings.validate()
    except ValueError as exc:
        log.warning("Config invalid (%s); using defaults.", exc)
        return AppSettings()
    return settings


def save_settings(settings: AppSettings) -> None:
    settings.validate()
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def get_or_create_token() -> str:
    """Return the machine's IPC token, creating it once if missing."""
    path = token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
        log.warning("Token file too short; regenerating.")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows ACLs differ; file is already in the user's profile.
    return token


def regenerate_token() -> str:
    path = token_path()
    if path.exists():
        path.unlink()
    return get_or_create_token()
