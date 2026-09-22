"""KillPolicy: who may be terminated, who is exempt, who is untouchable."""

import os

import pytest

from app.core.enforcement.policy import (
    PROTECTED_EXES,
    GrandpaStore,
    KillPolicy,
    is_protected,
)
from app.core.rules.models import Rule
from app.core.types import Action, Mode, RuleType
from app.database.db import Database


def rule(action=Action.CLOSE, mode=Mode.NORMAL, rule_type=RuleType.APPLICATION,
         exe="discord.exe"):
    r = Rule(name="X", type=rule_type, target=exe, executable=exe,
             daily_limit_seconds=600, action=action, mode=mode)
    r.id = 1
    return r


MATCHED = {"discord.exe": [10, 11]}


def test_close_kills_all_matches():
    d = KillPolicy().decide(rule=rule(), action="CLOSE_APP", matched=MATCHED)
    assert d.pids_to_close == (10, 11)
    assert d.reason.startswith("NORMAL/CLOSE")


def test_strict_block_is_not_grandfathered():
    d = KillPolicy().decide(
        rule=rule(action=Action.BLOCK, mode=Mode.STRICT), action="PREVENT_LAUNCH",
        matched=MATCHED, grandfathered=frozenset({10, 11}), first_enforcement=True,
    )
    # first_enforcement only exempts in NORMAL mode; STRICT closes everything
    assert d.pids_to_close == (10, 11)


def test_normal_block_grandfathers_existing_instance():
    d = KillPolicy().decide(
        rule=rule(action=Action.BLOCK, mode=Mode.NORMAL), action="PREVENT_LAUNCH",
        matched=MATCHED, first_enforcement=True,
    )
    assert d.pids_to_close == ()
    assert d.grandfathered == (10, 11)
    assert "grandfathered" in d.reason


def test_normal_block_closes_new_launches():
    # grandfathered set from the first enforcement tick: only pid 10 existed
    d = KillPolicy().decide(
        rule=rule(action=Action.BLOCK, mode=Mode.NORMAL), action="PREVENT_LAUNCH",
        matched={"discord.exe": [10, 11]}, grandfathered=frozenset({10}),
    )
    assert d.pids_to_close == (11,)  # the relaunch dies, the old one lives
    assert d.grandfathered == (10,)


@pytest.mark.parametrize("exe", sorted(PROTECTED_EXES))
def test_protected_processes_are_never_closed(exe):
    d = KillPolicy().decide(
        rule=rule(action=Action.CLOSE), action="CLOSE_APP",
        matched={exe: [999]},
    )
    assert d.pids_to_close == ()
    assert d.skipped and "protected" in d.skipped[0][2]


def test_agent_never_kills_itself():
    pid = os.getpid()
    d = KillPolicy(self_pid=pid).decide(
        rule=rule(), action="CLOSE_APP", matched={"python.exe": [pid]},
    )
    assert d.pids_to_close == ()
    assert "agent process" in d.skipped[0][2]


def test_low_pids_are_protected():
    d = KillPolicy(self_pid=12345).decide(
        rule=rule(), action="CLOSE_APP", matched={"discord.exe": [4]},
    )
    assert d.pids_to_close == ()


def test_extra_protected_from_settings():
    d = KillPolicy(extra_protected=frozenset({"discord.exe"})).decide(
        rule=rule(), action="CLOSE_APP", matched={"discord.exe": [42]},
    )
    assert d.pids_to_close == () and "protected" in d.skipped[0][2]


def test_is_protected_reasons():
    assert is_protected("explorer.exe", 100) is not None
    assert is_protected("discord.exe", 100, self_pid=999) is None
    assert is_protected("discord.exe", 0, self_pid=999) is not None


def test_website_action_needs_no_process_work():
    web = Rule(name="YT", type=RuleType.WEBSITE, target="youtube.com",
               domain="youtube.com", daily_limit_seconds=600, action=Action.BLOCK)
    web.id = 3
    d = KillPolicy().decide(rule=web, action="BLOCK_WEBSITE", matched={})
    assert d.pids_to_close == () and "no process work" in d.reason


# ---------------------------------------------------------------- persistence
def test_grandfather_store_roundtrip_and_day_scoping(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    store = GrandpaStore(db, "2026-09-21")
    assert store.load(1) is None
    store.save(1, frozenset({10, 11}))
    assert store.load(1) == frozenset({10, 11})
    # A new day must not inherit yesterday's exemption.
    assert GrandpaStore(db, "2026-09-22").load(1) is None
    store.clear(1)
    assert store.load(1) == frozenset()
    db.close()


def test_grandfather_store_survives_restart(tmp_path):
    db = Database(tmp_path / "t.db").connect()
    GrandpaStore(db, "2026-09-21").save(7, frozenset({42}))
    db.close()
    db2 = Database(tmp_path / "t.db").connect()
    assert GrandpaStore(db2, "2026-09-21").load(7) == frozenset({42})
    db2.close()
