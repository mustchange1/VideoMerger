"""Filesystem watching for VM Automatic.

PRIMARY: event-driven notifications via ``watchdog`` (on Windows this uses
ReadDirectoryChangesW – immediate events, no polling).
FALLBACK: a lightweight 1 s directory polling watcher, used only when
watchdog cannot be imported (keeps VM Automatic functional everywhere).

The watcher is deliberately thin: it only reports (kind, path) tuples for
direct files of the watched folder. Debouncing, pairing, stability and
queueing all happen in the controller.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

EventHandler = Callable[[str, Path], None]  # (kind, path)


@dataclass(frozen=True)
class _SnapshotEntry:
    size: int
    mtime_ns: int


def create_watcher(
    folder: Path | str,
    handler: EventHandler,
    poll_interval: float = 1.0,
    log: Callable[[str], None] | None = None,
) -> "WatchdogWatcher | PollingWatcher":
    """Create the best available watcher for ``folder`` (single level)."""
    log = log or (lambda _message: None)
    folder = Path(folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        from watchdog.events import FileSystemEventHandler  # type: ignore
        from watchdog.observers import Observer  # type: ignore
    except Exception:  # pragma: no cover - depends on environment
        log("VM Automatic: watchdog nicht verfügbar – Polling-Fallback aktiv.")
        return PollingWatcher(folder, handler, poll_interval=poll_interval)

    class _Handler(FileSystemEventHandler):
        def _emit(self, kind: str, src: str) -> None:
            try:
                path = Path(src)
            except TypeError:
                return
            # For deletions the file is already gone – do not require it to
            # still exist (created/modified/moved must refer to a real file).
            if kind != "deleted" and not path.is_file():
                return
            handler(kind, path)

        def on_created(self, event):
            if not getattr(event, "is_directory", False):
                self._emit("created", event.src_path)

        def on_modified(self, event):
            if not getattr(event, "is_directory", False):
                self._emit("modified", event.src_path)

        def on_moved(self, event):
            if not getattr(event, "is_directory", False):
                self._emit("moved", event.dest_path)

        def on_deleted(self, event):
            if not getattr(event, "is_directory", False):
                self._emit("deleted", event.src_path)

    observer = Observer()
    observer.schedule(_Handler(), str(folder), recursive=False)
    observer.daemon = True
    observer.start()
    log("VM Automatic: Dateiwächter aktiv (Event-gesteuert / watchdog).")
    return WatchdogWatcher(observer)


class WatchdogWatcher:
    """Wrapper around a running watchdog Observer (started by create_watcher)."""

    backend = "watchdog"

    def __init__(self, observer):
        self._observer = observer

    def start(self) -> None:  # already running; kept for a uniform API
        return None

    def stop(self) -> None:
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:  # pragma: no cover - observer quirks
            pass


class PollingWatcher:
    """Metadata polling watcher (fallback; also used in tests)."""

    backend = "polling"

    def __init__(self, folder: Path, handler: EventHandler, poll_interval: float = 1.0):
        self._folder = Path(folder).expanduser().resolve()
        self._handler = handler
        self._poll_interval = max(0.2, float(poll_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[str, _SnapshotEntry] = {}

    # ------------------------------------------------------------------ #
    def _scan(self) -> dict[str, _SnapshotEntry]:
        current: dict[str, _SnapshotEntry] = {}
        try:
            with os.scandir(self._folder) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        stat = os.stat(entry.path, follow_symlinks=False)
                        current[entry.name] = _SnapshotEntry(stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        continue
        except OSError:
            pass
        return current

    def _tick(self) -> None:
        current = self._scan()
        previous = self._last
        for name, entry in current.items():
            path = self._folder / name
            if name not in previous:
                self._handler("created", path)
            elif previous[name] != entry:
                self._handler("modified", path)
        for name in previous:
            if name not in current:
                self._handler("deleted", self._folder / name)
        self._last = current

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Prime the snapshot silently (do not emit events for pre-existing
        # files – the startup reconciliation handles those).
        self._last = self._scan()
        self._thread = threading.Thread(target=self._run, name="vm-auto-poll", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # keep the watcher alive no matter what
                pass
            self._stop.wait(self._poll_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
