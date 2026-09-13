"""File-stability protection for VM Automatic.

A newly created file may still be copying. VM Automatic must never begin
processing a job simply because a filesystem event fired. A file is *ready*
only when, for a configurable stability period:

* it exists and is a regular file,
* its size is non-zero,
* its size and modification time have stopped changing,
* it can be opened and read,
* it is not actively locked where the platform can tell.

This is deliberately metadata-first (size + mtime via ``os.stat``); it never
hashes large files, so the idle cost stays tiny and the reconciliation scan
scales to many files.

The check is implemented as a small per-file state machine so callers can
feed it repeatedly (watcher events / periodic reconciliation) without blocking
a thread in a tight sleep loop.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Sentinel reason codes (stable strings, useful for tests and logs).
REASON_MISSING = "missing"
REASON_NOT_FILE = "not_a_file"
REASON_EMPTY = "empty"
REASON_READ_ERROR = "read_error"
REASON_LOCKED = "locked"
REASON_GROWING = "growing"
REASON_WARMING = "warming"
REASON_STABLE = "stable"


@dataclass(slots=True)
class _Observed:
    size: int
    mtime_ns: int
    first_seen: float
    last_change: float
    consecutive_quiet: int = 0


@dataclass(slots=True)
class StabilityResult:
    stable: bool
    reason: str
    size: int
    attempts: int


def _snapshot(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return stat.st_size, stat.st_mtime_ns


def _is_readable(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _is_locked(path: Path) -> bool:
    """Best-effort lock detection where the platform can determine it.

    * Windows: a file being copied is typically opened exclusively; opening
      it read/write then fails.
    * POSIX: a non-blocking exclusive ``flock`` is attempted; it fails while
      another process/thread holds an exclusive ``flock`` (covers the common
      case; processes that use plain opens without locks are – as on all
      platforms – additionally guarded by the size/mtime stability check).
    """
    try:
        if os.name == "nt":
            with open(path, "r+b"):
                return False
        import fcntl

        with open(path, "r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True


class FileStability:
    """Tracks whether files have settled (metadata only, non-blocking)."""

    def __init__(
        self,
        stability_seconds: float = 8.0,
        sample_interval: float = 1.0,
        now: Callable[[], float] | None = None,
    ):
        self.stability_seconds = max(0.0, float(stability_seconds))
        self.sample_interval = max(0.0, float(sample_interval))
        self._now = now or time.monotonic
        self._observed: dict[str, _Observed] = {}

    # ------------------------------------------------------------------ #
    def _key(self, path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    def forget(self, path: Path) -> None:
        self._observed.pop(self._key(path), None)

    def forget_all(self) -> None:
        self._observed.clear()

    # ------------------------------------------------------------------ #
    def check(self, path: Path) -> StabilityResult:
        """Record one observation; report whether the file is stable.

        A file becomes *stable* only after it has been seen with an unchanged
        (size, mtime) for at least ``stability_seconds`` of monotonic time.
        """
        key = self._key(path)
        now = self._now()
        snapshot = _snapshot(path)
        if snapshot is None:
            self._observed.pop(key, None)
            return StabilityResult(False, REASON_MISSING, 0, 0)
        size, mtime_ns = snapshot
        if size <= 0:
            self._observed.pop(key, None)
            return StabilityResult(False, REASON_EMPTY, 0, 0)

        previous = self._observed.get(key)
        if previous is None or (previous.size, previous.mtime_ns) != (size, mtime_ns):
            observed = _Observed(size=size, mtime_ns=mtime_ns, first_seen=now, last_change=now)
            self._observed[key] = observed
            attempts = (previous.consecutive_quiet if previous else 0)
            return StabilityResult(False, REASON_GROWING, size, attempts + 1)

        if now - previous.last_change < self.sample_interval:
            # Too soon to sample again; keep the previous verdict pending.
            return StabilityResult(False, REASON_WARMING, size, previous.consecutive_quiet)

        if not _is_readable(path):
            previous.last_change = now
            previous.consecutive_quiet = 0
            return StabilityResult(False, REASON_READ_ERROR, size, previous.consecutive_quiet)
        if _is_locked(path):
            previous.last_change = now
            previous.consecutive_quiet = 0
            return StabilityResult(False, REASON_LOCKED, size, previous.consecutive_quiet)

        previous.consecutive_quiet += 1
        previous.last_change = now
        settled = now - previous.first_seen >= self.stability_seconds
        if settled:
            return StabilityResult(True, REASON_STABLE, size, previous.consecutive_quiet)
        return StabilityResult(False, REASON_WARMING, size, previous.consecutive_quiet)

    def stable_now(self, path: Path, sample_interval: float | None = None) -> bool:
        """Convenience: force the sample clock and report stability."""
        if sample_interval is not None:
            self.sample_interval = sample_interval
        return self.check(path).stable

    # ------------------------------------------------------------------ #
    def many(self, paths: list[Path]) -> dict[Path, StabilityResult]:
        return {path: self.check(path) for path in paths}
