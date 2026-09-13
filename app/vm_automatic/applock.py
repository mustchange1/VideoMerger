"""Single-instance lock for VM Automatic.

Prevents two VM Automatic instances from driving the same queue at the same
time (queue corruption, double renders, output collisions). Uses an OS-level
advisory lock on a file inside the VM Automatic config directory:

* Windows: ``msvcrt.locking`` (exclusive, non-blocking)
* POSIX:   ``fcntl.flock`` (exclusive, non-blocking)

The lock is released automatically by the OS when the process dies, so a
crashed instance never leaves a stale lock behind.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from ..video_merger.paths import project_root


class InstanceLockError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or (project_root() / "config" / "vm_automatic" / "instance.lock"))
        self._handle = None
        self._lock = threading.RLock()

    def acquire(self) -> None:
        with self._lock:
            if self._handle is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+", encoding="utf-8")
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise InstanceLockError(
                    "VM Automatic läuft bereits (eine andere Instanz hält die Queue). "
                    "Bitte nur eine Instanz gleichzeitig starten."
                ) from None
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            self._handle = handle

    def release(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
            finally:
                try:
                    handle.close()
                except OSError:
                    pass

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
