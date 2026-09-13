"""Unit tests: VM Automatic file-stability protection.

Covered per spec §8 / §44: growing file, stable file, locked file,
delayed file, partially copied file. The clock is injected so the tests
are deterministic and fast.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from app.vm_automatic.stability import (
    FileStability, REASON_EMPTY, REASON_GROWING, REASON_LOCKED,
    REASON_MISSING, REASON_STABLE, REASON_WARMING,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_tracker(stability: float = 2.0, interval: float = 0.5) -> tuple[FileStability, FakeClock]:
    clock = FakeClock()
    return FileStability(stability_seconds=stability, sample_interval=interval, now=clock), clock


def advance_until(clock: FakeClock, tracker: FileStability, path: Path,
                  expect: str, horizon: float = 60.0) -> bool:
    """Advance the fake clock in sample steps until the expected reason appears."""
    end = clock.now + horizon
    while clock.now < end:
        result = tracker.check(path)
        if result.reason == expect:
            return True
        clock.advance(0.5)
    return tracker.check(path).reason == expect


# ---------------------------------------------------------------------- #
def test_stable_file_becomes_stable_after_window(tmp_path):
    file = tmp_path / "a.txt"
    file.write_bytes(b"hello world")
    tracker, clock = make_tracker(stability=2.0)
    assert tracker.check(file).reason == REASON_GROWING  # first observation
    assert advance_until(clock, tracker, file, REASON_STABLE)
    # still stable afterwards
    clock.advance(10)
    assert tracker.check(file).reason == REASON_STABLE


def test_growing_file_resets_stability_window(tmp_path):
    file = tmp_path / "copy.bin"
    file.write_bytes(b"a" * 100)
    tracker, clock = make_tracker(stability=2.0)
    tracker.check(file)
    clock.advance(2.0)
    assert tracker.check(file).reason == REASON_STABLE
    # the copy continues -> size changes -> must go back to not-stable
    file.write_bytes(b"a" * 100 + b"b" * 50)
    result = tracker.check(file)
    assert result.reason in {REASON_GROWING, REASON_WARMING}
    # and it becomes stable again only after a full quiet window
    assert advance_until(clock, tracker, file, REASON_STABLE)


def test_partially_copied_file_stays_unstable_while_changing(tmp_path):
    file = tmp_path / "big.mp4"
    file.write_bytes(b"")
    tracker, clock = make_tracker(stability=2.0)
    for chunk in range(6):
        file.write_bytes(b"x" * (chunk + 1) * 64)
        clock.advance(0.3)
        result = tracker.check(file)
        assert not result.stable, f"chunk {chunk}: {result.reason}"
    clock.advance(2.5)
    assert tracker.check(file).reason == REASON_STABLE


def test_zero_size_file_never_stable(tmp_path):
    file = tmp_path / "empty.txt"
    file.write_bytes(b"")
    tracker, clock = make_tracker(stability=0.5)
    assert tracker.check(file).reason == REASON_EMPTY
    clock.advance(10)
    assert tracker.check(file).reason == REASON_EMPTY


def test_missing_file_reported(tmp_path):
    tracker, clock = make_tracker()
    result = tracker.check(tmp_path / "ghost.mp3")
    assert result.reason == REASON_MISSING
    assert not result.stable


def test_delayed_file_becomes_stable_only_after_full_window(tmp_path):
    file = tmp_path / "late.wav"
    file.write_bytes(b"data" * 10)
    tracker, clock = make_tracker(stability=3.0, interval=1.0)
    tracker.check(file)
    # before the window elapses: warming, not stable
    clock.advance(1.0)
    assert not tracker.check(file).stable
    clock.advance(1.0)
    assert not tracker.check(file).stable
    clock.advance(1.0)
    assert tracker.check(file).reason == REASON_STABLE


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock only")
def test_locked_file_detected_and_released(tmp_path):
    file = tmp_path / "locked.bin"
    file.write_bytes(b"lock me" * 10)
    tracker, clock = make_tracker(stability=0.5, interval=0.1)
    released = threading.Event()

    def hold_lock():
        import fcntl

        with open(file, "r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            released.wait(10)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    locker = threading.Thread(target=hold_lock, daemon=True)
    locker.start()
    time.sleep(0.2)  # let the lock settle
    tracker.check(file)
    clock.advance(0.1)
    result = tracker.check(file)
    assert result.reason == REASON_LOCKED
    released.set()
    locker.join(timeout=5)
    clock.advance(0.1)
    assert advance_until(clock, tracker, file, REASON_STABLE)


def test_file_reappearing_after_missing_starts_fresh(tmp_path):
    file = tmp_path / "flaky.mp3"
    file.write_bytes(b"v1" * 100)
    tracker, clock = make_tracker(stability=1.0, interval=0.2)
    tracker.check(file)
    file.unlink()
    assert tracker.check(file).reason == REASON_MISSING
    # re-created with different content -> fresh window
    file.write_bytes(b"v2" * 200)
    tracker.check(file)
    assert advance_until(clock, tracker, file, REASON_STABLE)
    assert tracker.check(file).size == 400
