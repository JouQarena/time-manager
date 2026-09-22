"""Single-instance lock: one agent per profile, stale locks taken over."""

import os


from app.config.lockfile import InstanceLock, pid_alive


def test_acquire_release_cycle(tmp_path):
    lock = InstanceLock(tmp_path / "profile.lock")
    assert lock.acquire() is True
    assert lock.path.exists()
    assert lock.held_by_other() is None  # our own lock is not "another instance"
    lock.release()
    assert not lock.path.exists()
    lock.release()  # idempotent


def test_second_instance_is_refused(tmp_path, foreign_pid):
    path = tmp_path / "profile.lock"
    first = InstanceLock(path)
    assert first.acquire()
    # Simulate a live other process: pid 1 always exists in a container/OS.
    path.write_text(f"{os.getpid()}:0")  # our own pid: not "another instance"
    assert InstanceLock(path).held_by_other() is None

    # Now claim it for a live *other* process and expect a refusal.
    other = foreign_pid
    path.write_text(f"{other}:0")
    second = InstanceLock(path)
    assert second.acquire() is False
    assert second.owned is False
    holder = second.held_by_other()
    assert holder == other
    first.release()


def test_stale_lock_from_dead_process_is_taken_over(tmp_path):
    path = tmp_path / "profile.lock"
    dead_pid = 999_999_999  # nothing can have this pid
    path.write_text(f"{dead_pid}:0")
    lock = InstanceLock(path)
    assert pid_alive(dead_pid) is False
    assert lock.acquire() is True
    assert lock.path.read_text().startswith(str(os.getpid()))
    lock.release()


def test_corrupt_lock_is_treated_as_stale(tmp_path):
    path = tmp_path / "profile.lock"
    path.write_text("this is not a pid")
    lock = InstanceLock(path)
    assert lock.acquire() is True
    lock.release()


def test_release_does_not_delete_someone_elses_lock(tmp_path, foreign_pid):
    path = tmp_path / "profile.lock"
    path.write_text(f"{foreign_pid}:0")  # a live holder we never acquired
    lock = InstanceLock(path)
    assert lock.acquire() is False
    lock.release()  # must not remove a lock we do not own
    assert path.exists()


def test_pid_alive_handles_nonsense():
    assert pid_alive(0) is False
    assert pid_alive(-5) is False
    assert pid_alive(os.getpid()) is True
