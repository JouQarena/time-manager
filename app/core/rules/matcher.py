"""Target matching: exe normalization + domain normalization/matching.

Domain rule (mirrored in the browser extension, see docs/PROTOCOL.md):
- rule `youtube.com` matches `youtube.com`, `www.youtube.com`, `m.youtube.com`
  (exact or any subdomain), but NOT `fakeyoutube.com` or `youtube.com.evil.com`.
- rule `music.youtube.com` matches only it and its subdomains.
"""

from __future__ import annotations

import re

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalize_executable(value: str) -> str:
    """Lowercase exe file name: `C:\\..\\Discord.EXE` -> `discord.exe`."""
    name = value.strip().replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if not name:
        raise ValueError("Executable must not be empty.")
    if len(name) > 255 or "/" in name or "\x00" in name:
        raise ValueError(f"Invalid executable name: {value!r}")
    if not name.endswith(".exe"):
        # Allow bare names ("discord") but store canonical with .exe.
        if "." in name:
            raise ValueError(f"Executable should be a .exe file name: {value!r}")
        name += ".exe"
    return name


def normalize_domain(value: str) -> str:
    """Lowercase, strip port/path, IDNA-encode, validate labels."""
    host = value.strip().lower()
    # Tolerate users pasting URLs: strip scheme and path.
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split("@", 1)[-1]  # strip userinfo
    if host.startswith("[") and "]" in host:  # IPv6 literal -> reject
        raise ValueError(f"IP literals cannot be website rules: {value!r}")
    if ":" in host:  # strip port (single colon; IPv6 already rejected)
        host = host.rsplit(":", 1)[0]
    host = host.strip().strip(".")
    if not host or len(host) > 253:
        raise ValueError(f"Invalid domain: {value!r}")
    try:
        host = host.encode("idna").decode("ascii")
    except Exception as exc:
        raise ValueError(f"Invalid domain: {value!r}") from exc
    labels = host.split(".")
    if len(labels) < 2 and host != "localhost":
        raise ValueError(f"Domain needs at least two labels: {value!r}")
    if any(not _LABEL.match(label) for label in labels):
        raise ValueError(f"Invalid domain: {value!r}")
    # Reject pure IPv4.
    if len(labels) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in labels):
        raise ValueError(f"IP literals cannot be website rules: {value!r}")
    return host


def domain_matches(rule_domain: str, observed_host: str) -> bool:
    """True if `observed_host` falls under `rule_domain` (exact or subdomain)."""
    try:
        rule = normalize_domain(rule_domain)
        host = normalize_domain(observed_host)
    except ValueError:
        return False
    return host == rule or host.endswith("." + rule)


def executable_matches(rule_exes: tuple[str, ...] | list[str], process_exe: str) -> bool:
    """True if the process exe name equals any of the rule's exes."""
    try:
        proc = normalize_executable(process_exe)
    except ValueError:
        return False
    return any(proc == normalize_executable(e) for e in rule_exes)
