"""Shared enums for the Time Manager core.

Kept in one tiny module so rules / enforcement / tracking / IPC all agree
on the exact same vocabulary. No logic here.
"""

from __future__ import annotations

from enum import Enum


class RuleType(str, Enum):
    APPLICATION = "APPLICATION"
    GAME = "GAME"
    WEBSITE = "WEBSITE"


class Action(str, Enum):
    BLOCK = "BLOCK"  # websites: extension block page; apps: prevent launch
    CLOSE = "CLOSE"  # terminate the desktop application
    WAIT_FOR_SESSION_END = "WAIT_FOR_SESSION_END"  # games: finish match, then enforce
    WARN_ONLY = "WARN_ONLY"  # notify, never force anything


class Mode(str, Enum):
    NORMAL = "NORMAL"
    STRICT = "STRICT"  # relaunch-guard, startup repair, harder to pause


class RuleState(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    LIMIT_REACHED = "LIMIT_REACHED"  # transient, resolved within one tick
    WAITING_FOR_SESSION_END = "WAITING_FOR_SESSION_END"
    ENFORCED = "ENFORCED"  # terminal for the day
    DISABLED = "DISABLED"  # rule off or outside schedule


class SessionType(str, Enum):
    FOREGROUND = "FOREGROUND"
    WEBSITE = "WEBSITE"
    GAME = "GAME"


class LimitReason(str, Enum):
    NONE = "NONE"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"
    SESSION_LIMIT_REACHED = "SESSION_LIMIT_REACHED"
    SCHEDULE_BLOCKED = "SCHEDULE_BLOCKED"
    MANUAL = "MANUAL"
