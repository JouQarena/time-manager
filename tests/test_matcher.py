import pytest

from app.core.rules.matcher import (
    domain_matches,
    executable_matches,
    normalize_domain,
    normalize_executable,
)


def test_normalize_domain_variants():
    assert normalize_domain("YouTube.COM") == "youtube.com"
    assert normalize_domain("https://www.youtube.com/watch?v=x") == "www.youtube.com"
    assert normalize_domain("m.youtube.com:443/path") == "m.youtube.com"
    assert normalize_domain("  youtube.com. ") == "youtube.com"


def test_normalize_domain_rejects():
    for bad in ["", "http://", "192.168.1.1", "[::1]", "no-tld", "a" * 300, "bad_*.com"]:
        with pytest.raises(ValueError):
            normalize_domain(bad)


def test_domain_matching_scoping():
    assert domain_matches("youtube.com", "youtube.com")
    assert domain_matches("youtube.com", "www.youtube.com")
    assert domain_matches("youtube.com", "m.youtube.com")
    assert domain_matches("youtube.com", "music.youtube.com")
    # Must NOT match lookalikes:
    assert not domain_matches("youtube.com", "fakeyoutube.com")
    assert not domain_matches("youtube.com", "youtube.com.evil.com")
    assert not domain_matches("youtube.com", "youtube.co")
    # Explicit subdomain rule is narrower:
    assert domain_matches("music.youtube.com", "music.youtube.com")
    assert not domain_matches("music.youtube.com", "www.youtube.com")


def test_normalize_executable():
    assert normalize_executable("Discord.EXE") == "discord.exe"
    assert normalize_executable("discord") == "discord.exe"
    assert normalize_executable("C:\\Games\\LeagueClient.exe") == "leagueclient.exe"
    with pytest.raises(ValueError):
        normalize_executable("")
    with pytest.raises(ValueError):
        normalize_executable("not-an-exe.txt")


def test_executable_matches():
    assert executable_matches(("discord.exe",), "C:\\App\\DISCORD.exe")
    assert not executable_matches(("discord.exe",), "discord-ptb.exe")
