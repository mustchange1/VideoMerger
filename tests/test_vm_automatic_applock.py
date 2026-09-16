"""Unit tests: VM Automatic single-instance protection."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.vm_automatic.applock import InstanceLockError, SingleInstanceLock


def test_second_instance_is_blocked(tmp_path: Path):
    lock_path = tmp_path / "instance.lock"
    first = SingleInstanceLock(lock_path)
    first.acquire()
    try:
        second = SingleInstanceLock(lock_path)
        with pytest.raises(InstanceLockError):
            second.acquire()
    finally:
        first.release()


def test_release_allows_new_instance(tmp_path: Path):
    lock_path = tmp_path / "instance.lock"
    first = SingleInstanceLock(lock_path)
    first.acquire()
    first.release()
    second = SingleInstanceLock(lock_path)
    second.acquire()
    second.release()


def test_launch_wires_the_single_instance_lock():
    """Regression guard: launch() must actually acquire the instance lock.

    The lock module once existed without being wired into the entry point,
    which silently disabled the single-instance protection.
    """
    import inspect

    from app.vm_automatic import gui

    source = inspect.getsource(gui.launch)
    assert "SingleInstanceLock" in source
    assert "lock.acquire()" in source
    assert "lock.release()" in source
    assert "InstanceLockError" in source
