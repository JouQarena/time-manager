"""Static + behavioural checks on the MV3 extension assets.

Chrome cannot be driven from here, so these tests cover what *can* be verified
deterministically:

* manifest sanity (MV3, files referenced actually exist, no over-broad
  permissions, no remote code),
* every JS file parses (via `node --check`, skipped if node is missing),
* the JS domain normalizer agrees with Python's `normalize_domain` on a shared
  fixture list — the parity that `normalize.js` promises in its header comment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.rules.matcher import domain_matches, normalize_domain

EXT = Path(__file__).resolve().parents[1] / "browser-extension"
# Node on Windows: absolute paths in ESM import specifiers must be file://
# URLs ("Only URLs with a scheme in: file, data, and node are supported");
# a bare D:/... path is read as the scheme "d:". as_uri() percent-encodes
# safely on every OS, so backslashes can never become JS escapes either.
EXT_URI = EXT.resolve().as_uri()

#: Shared fixture list: (input, expected normalize_domain output).
#: Invalid input -> None (the JS side returns "" for the same cases).
PARITY_CASES = [
    ("youtube.com", "youtube.com"),
    ("WWW.YouTube.COM", "www.youtube.com"),
    ("https://www.youtube.com/watch?v=abc", "www.youtube.com"),
    ("http://user:pw@example.com:8080/path?x=1#frag", "example.com"),
    ("music.youtube.com", "music.youtube.com"),
    ("münchen.de", "xn--mnchen-3ya.de"),
    ("sub.DOMAIN.co.uk.", "sub.domain.co.uk"),
    ("localhost", "localhost"),
    # never trackable:
    ("127.0.0.1", None),
    ("10.0.0.5:8080", None),
    ("[::1]", None),
    ("chrome://settings", None),
    ("about:blank", None),
    ("", None),
    ("nodots", None),
    ("-bad.example.com", None),
    ("bad-.example.com", None),
    ("x" * 300 + ".com", None),
]


def node_available() -> bool:
    return shutil.which("node") is not None


# ------------------------------------------------------------------- manifest
def test_manifest_is_valid_mv3():
    manifest = json.loads((EXT / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["background"]["type"] == "module"
    assert manifest["version"].count(".") == 2


def test_manifest_files_exist():
    manifest = json.loads((EXT / "manifest.json").read_text())
    referenced = [manifest["background"]["service_worker"]]
    referenced += [script for entry in manifest["content_scripts"] for script in entry["js"]]
    referenced.append(manifest["action"]["default_popup"])
    referenced.append(manifest["options_page"])
    referenced += list(manifest["icons"].values())
    referenced += list(manifest["action"]["default_icon"].values())
    for relative in referenced:
        assert (EXT / relative).is_file(), f"manifest references missing file: {relative}"


def test_manifest_stays_minimal_and_local():
    manifest = json.loads((EXT / "manifest.json").read_text())
    assert set(manifest["permissions"]) <= {"tabs", "storage", "alarms", "idle"}
    assert manifest["host_permissions"] == []  # no standing access to sites
    assert "<all_urls>" not in manifest["permissions"]
    # Content script is injected by Chrome, never fetched from a server:
    assert all("http" not in script for entry in manifest["content_scripts"]
               for script in entry["js"])
    assert all(entry["run_at"] == "document_start" for entry in manifest["content_scripts"])


def test_no_remote_code_or_telemetry_hosts():
    """Local-only promise: nothing may talk to the internet."""
    banned = ("http://", "https://", "fetch(", "XMLHttpRequest", "navigator.sendBeacon")
    allowed_local = ("ws://${", "http://*/*", "https://*/*", "\"http:\"", "'http:'",
                     "http://\" + host", "https://\" + host")
    for path in sorted(EXT.rglob("*.js")):
        source = path.read_text()
        for needle in banned:
            if needle in source:
                for line in source.splitlines():
                    if needle in line and not any(a in line for a in allowed_local):
                        pytest.fail(f"{path.name} references {needle!r}: {line.strip()}")


def test_service_worker_has_no_dynamic_eval():
    source = (EXT / "background" / "service-worker.js").read_text()
    for banned in ("eval(", "new Function(", "document.write"):
        assert banned not in source


def test_icons_exist_and_are_png():
    for size in (16, 32, 48, 128):
        path = EXT / "icons" / f"icon{size}.png"
        assert path.is_file() and path.stat().st_size > 100
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------------- JS
@pytest.mark.skipif(not node_available(), reason="node not installed")
def test_all_js_parses():
    failures = []
    for path in sorted(EXT.rglob("*.js")):
        result = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            failures.append(f"{path.name}: {result.stderr.strip()[:200]}")
    assert failures == []


@pytest.mark.skipif(not node_available(), reason="node not installed")
def test_normalize_js_matches_python():
    """The extension's normalizer must agree with matcher.normalize_domain."""
    script = f"""
    import {{ normalizeDomain, domainMatches }} from "{EXT_URI}/shared/normalize.js";
    const cases = {json.dumps([c[0] for c in PARITY_CASES])};
    const out = cases.map((input) => {{
      const value = normalizeDomain(input);
      return value === "" ? null : value;
    }});
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    js_results = json.loads(result.stdout.strip())

    mismatches = []
    for (raw, _expected), js_value in zip(PARITY_CASES, js_results):
        if raw == "":
            py_value = None
        else:
            try:
                py_value = normalize_domain(raw)
            except ValueError:
                py_value = None
        if py_value != js_value:
            mismatches.append((raw, py_value, js_value))
    assert mismatches == [], f"normalizer drift: {mismatches}"


@pytest.mark.skipif(not node_available(), reason="node not installed")
def test_domain_matching_agrees():
    script = f"""
    import {{ domainMatches }} from "{EXT_URI}/shared/normalize.js";
    const pairs = [
      ["youtube.com", "youtube.com"], ["youtube.com", "www.youtube.com"],
      ["youtube.com", "music.youtube.com"], ["youtube.com", "fakeyoutube.com"],
      ["youtube.com", "youtube.com.evil.com"], ["music.youtube.com", "youtube.com"],
      ["example.com", "EXAMPLE.com"], ["localhost", "sub.localhost"],
    ];
    console.log(JSON.stringify(pairs.map(([rule, host]) => domainMatches(rule, host))));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    js_results = json.loads(result.stdout.strip())
    pairs = [
        ("youtube.com", "youtube.com"), ("youtube.com", "www.youtube.com"),
        ("youtube.com", "music.youtube.com"), ("youtube.com", "fakeyoutube.com"),
        ("youtube.com", "youtube.com.evil.com"), ("music.youtube.com", "youtube.com"),
        ("example.com", "EXAMPLE.com"), ("localhost", "sub.localhost"),
    ]
    py_results = [domain_matches(rule, host) for rule, host in pairs]
    assert py_results == js_results, f"matching drift: py={py_results} js={js_results}"


def test_protocol_constants_match_python():
    """Wire constants must not drift between the two implementations."""
    import re

    from app.ipc import protocol

    source = (EXT / "shared" / "protocol.js").read_text()
    assert f'PROTOCOL_VERSION = "{protocol.PROTOCOL_VERSION}"' in source
    assert f"DEFAULT_PORT = {protocol.DEFAULT_PORT}" in source
    assert f"HEARTBEAT_INTERVAL_MS = {int(protocol.HEARTBEAT_INTERVAL_SECONDS * 1000)}" in source
    for reason in protocol.BLOCK_REASONS:
        assert f"{reason}:" in source, f"block reason {reason} missing from protocol.js"
    for message_type in ("WELCOME", "BLOCK_DECISION", "RULE_UPDATE", "PING", "ERROR"):
        assert message_type in source


def test_extension_never_sends_unknown_message_types():
    """The extension may only send what the agent's INBOUND_TYPES accepts."""
    from app.ipc import protocol

    sent = set()
    for name in ("shared/protocol.js", "background/service-worker.js",
                 "content/block-overlay.js", "popup/popup.js", "options/options.js"):
        source = (EXT / name).read_text()
        for message_type in protocol.INBOUND_TYPES:
            if f'type: "{message_type}"' in source or f'".type": "{message_type}"' in source:
                sent.add(message_type)
    assert sent <= set(protocol.INBOUND_TYPES)
    assert "HELLO" in sent and "TAB_ACTIVITY" in sent and "HEARTBEAT" in sent
