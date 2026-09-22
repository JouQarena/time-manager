"""Phase 7: Task Scheduler watchdog — command building + install/remove flows
with a fake runner (no real schtasks is ever invoked in tests)."""

import pytest

import app.windows.watchdog as wd
from app.windows.watchdog import TASK_NAME, WatchdogResult, WatchdogScheduler


class FakeRunner:
    """Returns scripted (rc, out, err) tuples keyed by the schtasks verb."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]):
        self.calls.append(list(cmd))
        verb = "/Query" in cmd and "query" or "/Create" in cmd and "create" or "delete"
        return self.results.get(verb, (0, "", ""))


def test_install_builds_the_expected_task(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")
    runner = FakeRunner({"query": (1, "", "not found")})  # not installed yet
    w = WatchdogScheduler(runner)

    result = w.install()
    assert result.ok and result.action == "INSTALLED"

    create = runner.calls[-1]
    assert create[:2] == ["schtasks", "/Create"]
    assert "/F" in create and "/SC" in create
    assert create[create.index("/MO") + 1] == "1"  # every minute
    assert create[create.index("/TN") + 1] == TASK_NAME
    tr = create[create.index("/TR") + 1]
    assert tr.endswith("--monitor")  # the watchdog IS the normal startup path


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")
    runner = FakeRunner({"query": (0, "", "")})  # already installed
    w = WatchdogScheduler(runner)
    result = w.install()
    assert result.ok and result.action == "ALREADY_PRESENT"
    assert not any("/Create" in c for c in runner.calls)  # no re-create


def test_uninstall_removes_the_task(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")
    runner = FakeRunner({"query": (0, "", "")})
    w = WatchdogScheduler(runner)
    result = w.uninstall()
    assert result.ok and result.action == "REMOVED"
    delete = runner.calls[-1]
    assert delete[:2] == ["schtasks", "/Delete"]
    assert delete[delete.index("/TN") + 1] == TASK_NAME


def test_uninstall_when_absent(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")
    runner = FakeRunner({"query": (1, "", "")})
    w = WatchdogScheduler(runner)
    result = w.uninstall()
    assert result.ok and result.action == "NOT_PRESENT"
    assert not any("/Delete" in c for c in runner.calls)


def test_failed_create_reports_failure(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")
    runner = FakeRunner({"query": (1, "", ""), "create": (1, "", "Access is denied.")})
    result = WatchdogScheduler(runner).install()
    assert not result.ok and result.action == "FAILED"
    assert "Access is denied" in result.detail


def test_runner_exception_reports_failure(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")

    def boom(_cmd):
        raise OSError("schtasks not found")

    result = WatchdogScheduler(boom).install()
    assert not result.ok and result.action == "FAILED"


def test_off_windows_is_unavailable(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "posix")
    w = WatchdogScheduler(FakeRunner())
    assert w.available is False
    assert w.install() == WatchdogResult(
        False, "UNAVAILABLE", "Task Scheduler watchdog needs Windows."
    )
    assert w.uninstall().action == "UNAVAILABLE"
    assert w.is_installed() is None
    status = w.status()
    assert status["available"] is False


def test_ensure_matches_the_flag(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")
    runner = FakeRunner({"query": (1, "", "")})
    w = WatchdogScheduler(runner)
    assert w.ensure(True).action == "INSTALLED"
    runner.results["query"] = (0, "", "")
    assert w.ensure(False).action == "REMOVED"


def test_status_shape(monkeypatch):
    monkeypatch.setattr(wd.os, "name", "nt")
    runner = FakeRunner({"query": (0, "", "")})
    status = WatchdogScheduler(runner).status()
    assert status["available"] is True
    assert status["installed"] is True
    assert status["task_name"] == TASK_NAME
    assert status["command"].endswith("--monitor")
