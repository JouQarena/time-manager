import pytest

from app.core.rules.models import Rule


def test_app_rule_ok():
    r = Rule(
        name="Discord", type="APPLICATION", target="discord.exe",
        executable="discord.exe", daily_limit_seconds=3600, action="CLOSE",
    )
    assert r.executable == "discord.exe"
    assert r.domain is None
    assert r.executables == ("discord.exe",)


def test_game_rule_extra_exes():
    r = Rule(
        name="LoL", type="GAME", target="LeagueClient.exe",
        executable="LeagueClient.exe",
        extra_executables=["League of Legends.exe"],
        daily_limit_seconds=7200, action="WAIT_FOR_SESSION_END", mode="STRICT",
    )
    assert r.executables == ("leagueclient.exe", "league of legends.exe")


def test_website_rule_ok():
    r = Rule(
        name="YT", type="WEBSITE", target="youtube.com", domain="www.YouTube.com",
        daily_limit_seconds=2700, action="BLOCK",
    )
    assert r.domain == "www.youtube.com"  # stored as typed-normalized


def test_rules_reject_bad_combos():
    with pytest.raises(ValueError):  # app without exe
        Rule(name="x", type="APPLICATION", target="x", daily_limit_seconds=60, action="CLOSE")
    with pytest.raises(ValueError):  # website without domain
        Rule(name="x", type="WEBSITE", target="x", daily_limit_seconds=60, action="BLOCK")
    with pytest.raises(ValueError):  # website + CLOSE
        Rule(name="x", type="WEBSITE", target="x", domain="x.com",
             daily_limit_seconds=60, action="CLOSE")
    with pytest.raises(ValueError):  # no limits at all
        Rule(name="x", type="WEBSITE", target="x", domain="x.com", action="BLOCK")
    with pytest.raises(ValueError):  # empty name
        Rule(name="  ", type="WEBSITE", target="x", domain="x.com",
             daily_limit_seconds=60, action="BLOCK")


def test_rule_row_roundtrip():
    r = Rule(
        name="YT", type="WEBSITE", target="youtube.com", domain="youtube.com",
        daily_limit_seconds=2700, session_limit_seconds=900, action="BLOCK",
    )
    row = r.to_row()
    row["id"] = 7
    r2 = Rule.from_row(row)
    assert r2.id == 7 and r2.domain == "youtube.com"
    assert r2.daily_limit_seconds == 2700 and r2.session_limit_seconds == 900
