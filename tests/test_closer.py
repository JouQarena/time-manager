"""ProcessCloser: polite request, grace period, escalation, idempotence."""

import os

import pytest

from app.core.enforcement.closer import ProcessCloser, SystemProcessController
from app.testing.fakes import FakeProcessController, FakeProcessSource


@pytest.fixture()
def env():
    source = FakeProcessSource()
    source.clock = None
    pid = source.launch("discord.exe")
    return source, FakeProcessController(source), pid


def test_graceful_close_succeeds_immediately(env):
    source, controller, pid = env
    closer = ProcessCloser(controller, graceful_timeout=6.0)
    assert closer.request(1, "Discord", (pid,), now=0.0) == 1
    assert controller.requests == [pid]
    assert not controller.forces  # never forced while the app cooperates
    results = closer.poll(now=1.0)
    assert results and results[0].method == "GRACEFUL" and results[0].ok
    assert closer.pending() == ()


def test_escalates_to_force_after_grace():
    source = FakeProcessSource()
    pid = source.launch("league of legends.exe")
    controller = FakeProcessController(source, graceful_ok=False)  # ignores WM_CLOSE
    closer = ProcessCloser(controller, graceful_timeout=6.0)
    closer.request(2, "League", (pid,), now=0.0)
    assert closer.poll(now=3.0) == []  # still inside the grace period
    assert controller.forces == []
    results = closer.poll(now=6.5)
    assert controller.forces == [pid]  # escalated
    assert results[0].method == "FORCED" and results[0].ok
    assert results[0].detail == "no graceful path"


def test_grace_zero_forces_immediately(env):
    source, controller, pid = env
    closer = ProcessCloser(controller, graceful_timeout=0)
    closer.request(1, "Discord", (pid,), now=0.0)
    assert controller.requests == []
    assert controller.forces == [pid]  # no grace, straight to force
    assert closer.pending() == ()


def test_already_gone_is_reported_once(env):
    source, controller, pid = env
    closer = ProcessCloser(controller, graceful_timeout=6.0)
    source.kill(pid)
    assert closer.request(1, "Discord", (pid,), now=0.0) == 0
    results = closer.drain()
    assert results[0].method == "UNREACHABLE" and results[0].ok


def test_repeated_requests_are_not_duplicated(env):
    source, controller, pid = env
    controller.graceful_ok = False  # stays alive, so the pending entry persists
    closer = ProcessCloser(controller, graceful_timeout=6.0)
    assert closer.request(1, "Discord", (pid,), now=0.0) == 1
    assert closer.request(1, "Discord", (pid,), now=0.5) == 0  # already pending
    assert closer.pending() == (pid,)


def test_force_failure_is_reported():
    source = FakeProcessSource()
    pid = source.launch("app.exe")
    controller = FakeProcessController(source, graceful_ok=False, force_ok=False)
    closer = ProcessCloser(controller, graceful_timeout=1.0)
    closer.request(1, "App", (pid,), now=0.0)
    results = closer.poll(now=2.0)
    assert results[0].method == "FORCED" and results[0].ok is False
    assert "still running" in results[0].detail


def test_cancel_and_drain(env):
    source, controller, pid = env
    controller.graceful_ok = False
    closer = ProcessCloser(controller, graceful_timeout=60.0)
    closer.request(1, "Discord", (pid,), now=0.0)
    closer.cancel(pid)
    assert closer.pending() == ()
    assert closer.poll(now=100.0) == []


# ------------------------------------------------------- real controller guards
def test_system_controller_refuses_system_and_self_pids():
    """Defence in depth: no policy bug may ever signal pid 0/4 or ourselves.

    (On POSIX a signal to pid 0 or a negative pid hits a whole process group,
    so this guard is load-bearing safety, not politeness.)
    """
    c = SystemProcessController()
    assert c.request_close(0) is False
    assert c.request_close(4) is False
    assert c.force_close(0) is False
    assert c.force_close(os.getpid()) is False
    assert c.request_close(os.getpid()) is False


def test_system_controller_reports_own_process_alive():
    c = SystemProcessController()
    assert c.alive(os.getpid()) is True
    assert c.name_of(os.getpid())
    assert c.alive(999_999_999) is False  # nonexistent pid


def test_system_controller_missing_pid_close_is_false():
    c = SystemProcessController()
    assert c.request_close(999_999_999) is False
