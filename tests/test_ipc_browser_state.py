"""BrowserStateStore: what the agent believes about browsers, and when it expires."""

import pytest

from app.ipc.browser_state import BrowserStateStore

CHROME = ("chrome", "uuid-chrome-0001")
EDGE = ("edge", "uuid-edge-0002")


@pytest.fixture()
def store():
    return BrowserStateStore(stale_after=15.0)


def test_no_extension_means_no_web_state(store):
    # None is not {} : "no browser link yet" must not be confused with
    # "connected but nothing active" (docs/PROTOCOL.md).
    assert store.web_state(0.0) is None
    assert store.is_connected() is False


def test_connected_but_idle_is_empty_dict(store):
    store.connect(*CHROME, 0.0)
    assert store.web_state(0.0) == {}
    assert store.web_state(5.0) == {}  # heartbeats keep it alive, but no tab
    assert store.active_domain(5.0) is None


def test_tab_activity_then_heartbeat_keeps_it_live(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=True, mono=0.0)
    assert store.web_state(1.0) == {"youtube.com": 0.0}

    # The extension does NOT resend TAB_ACTIVITY while a tab is watched;
    # heartbeats carry the liveness instead.
    for beat in (5.0, 10.0, 15.0, 20.0, 60.0, 3600.0):
        store.touch(*CHROME, beat)
        assert store.web_state(beat) == {"youtube.com": beat}


def test_missed_heartbeats_expire_the_state(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    store.touch(*CHROME, 5.0)
    assert store.web_state(5.0) == {"youtube.com": 5.0}
    assert store.web_state(20.0) == {"youtube.com": 5.0}  # exactly at the edge
    assert store.web_state(20.5) == {}  # past 15 s of silence -> stale
    assert store.stale_browsers(20.5) == (CHROME,)


def test_unfocused_or_inactive_tabs_do_not_count(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=False,
                 window_focused=True, audible=False, mono=0.0)
    assert store.web_state(0.0) == {}
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=False, audible=False, mono=1.0)
    assert store.web_state(1.0) == {}
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=2.0)
    # Values are the last *sign of life* (heartbeat or report), used only for
    # the staleness window; time accounting uses the agent's own clock.
    assert store.web_state(2.0) == {"youtube.com": 2.0}


def test_non_web_page_reports_empty_domain(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=7, domain="", active=True,
                 window_focused=True, audible=False, mono=0.0)
    assert store.web_state(0.0) == {}
    assert store.active_domain(0.0) is None


def test_multiple_browsers_and_domains(store):
    store.connect(*CHROME, 0.0)
    store.connect(*EDGE, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    store.update(*EDGE, tab_id=2, domain="twitter.com", active=True,
                 window_focused=True, audible=False, mono=0.5)
    state = store.web_state(1.0)
    assert state == {"youtube.com": 0.0, "twitter.com": 0.5}
    assert store.connected_count() == 2
    assert store.connected_count() == 2
    assert store.active_domain(1.0) == "twitter.com"  # most recent report


def test_disconnect_clears_everything_for_that_browser(store):
    store.connect(*CHROME, 0.0)
    store.connect(*EDGE, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    store.update(*EDGE, tab_id=2, domain="twitter.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    store.disconnect(*CHROME)
    assert store.web_state(0.0) == {"twitter.com": 0.0}
    store.disconnect(*EDGE)
    assert store.web_state(0.0) is None  # nobody left -> no link


def test_reconnect_does_not_resurrect_old_tabs(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    store.disconnect(*CHROME)
    store.connect(*CHROME, 100.0)  # same browser id, new session
    assert store.web_state(100.0) == {}  # state must be re-reported


def test_prune_drops_stale_tabs_and_dead_browsers(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    assert store.prune(5.0) == 0  # still fresh
    assert store.prune(20.0) == 1  # tab state dropped
    assert store.web_state(20.0) == {}
    assert store.prune(20.0 + 15 * 4 + 1) == 1  # the dead browser goes too
    assert store.is_connected() is False


def test_forget_all(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    store.forget_all()
    assert store.is_connected() is False and store.web_state(0.0) is None


def test_summary_for_status_reporting(store):
    store.connect(*CHROME, 0.0)
    store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                 window_focused=True, audible=False, mono=0.0)
    summary = store.summary()
    assert summary["connected"] == 1
    assert summary["tabs"] == 1  # the tab actually being looked at
    assert summary["domains"] == ["youtube.com"]

    # One browser keeps ONE current tab: switching to an unfocused/background
    # tab replaces it (the switch itself is reported by the extension).
    store.update(*CHROME, tab_id=2, domain="reddit.com", active=False,
                 window_focused=True, audible=False, mono=0.1)
    summary = store.summary()
    assert summary["tabs"] == 0  # the new current tab is not being looked at
    assert summary["domains"] == []


def test_thread_safety_smoke(store):
    import threading

    store.connect(*CHROME, 0.0)
    errors: list[str] = []

    def writer():
        try:
            for i in range(500):
                store.update(*CHROME, tab_id=1, domain="youtube.com", active=True,
                             window_focused=True, audible=False, mono=float(i))
                store.touch(*CHROME, float(i))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def reader():
        try:
            for i in range(500):
                store.web_state(float(i))
                store.summary()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
