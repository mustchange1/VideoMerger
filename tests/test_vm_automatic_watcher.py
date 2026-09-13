"""Unit tests: VM Automatic filesystem watcher + debounce.

Covered per spec §6/§35 / §44: creation, modification, move, duplicate
events, debounce (a burst of events collapses into one logical update).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.vm_automatic.controller import VMAutomaticController
from app.vm_automatic.watcher import PollingWatcher, create_watcher


def _wait_until(predicate, timeout: float = 10.0, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


# ---------------------------------------------------------------------- #
# Real watcher (watchdog when available, polling fallback otherwise)
# ---------------------------------------------------------------------- #
@pytest.fixture()
def events() -> list[tuple[str, Path]]:
    return []


def test_watcher_detects_creation_and_modification(tmp_path, events):
    target = tmp_path / "inbox"
    watcher = create_watcher(target, lambda kind, path: events.append((kind, path)))
    try:
        file = target / "Topic_001.mp3"
        file.write_bytes(b"one")
        assert _wait_until(lambda: any(k == "created" and p.name == "Topic_001.mp3" for k, p in events)), events
        time.sleep(0.3)
        events.clear()
        file.write_bytes(b"one-two-three")
        assert _wait_until(lambda: any(p.name == "Topic_001.mp3" for k, p in events)), events
    finally:
        watcher.stop()


def test_watcher_detects_move_and_delete(tmp_path, events):
    target = tmp_path / "inbox"
    watcher = create_watcher(target, lambda kind, path: events.append((kind, path)))
    try:
        file = target / "Topic_002.wav"
        file.write_bytes(b"x")
        assert _wait_until(lambda: any(p.name == "Topic_002.wav" for k, p in events)), events
        events.clear()
        time.sleep(0.3)
        file.rename(target / "Topic_002_renamed.wav")
        assert _wait_until(lambda: any(p.name == "Topic_002_renamed.wav" for k, p in events)), events
        events.clear()
        time.sleep(0.3)
        (target / "Topic_002_renamed.wav").unlink()
        assert _wait_until(lambda: any(k == "deleted" for k, p in events)), events
    finally:
        watcher.stop()


def test_watcher_ignores_preexisting_files_at_start(tmp_path, events):
    """Pre-existing files are the reconciliation scan's job – the watcher
    must not fire creation events for them on startup."""
    target = tmp_path / "inbox"
    target.mkdir()
    (target / "existing.mp3").write_bytes(b"x")
    watcher = create_watcher(target, lambda kind, path: events.append((kind, path)))
    try:
        assert _wait_until(lambda: True, timeout=2.5)  # give any (wrong) event time to arrive
        time.sleep(0.5)
        assert not any(p.name == "existing.mp3" for k, p in events)
    finally:
        watcher.stop()


# ---------------------------------------------------------------------- #
# Debounce: bursts of events collapse into one logical job candidate
# ---------------------------------------------------------------------- #
def make_fast_controller(tmp_path: Path):
    from app.vm_automatic.config import VMAutomaticConfig

    config = VMAutomaticConfig.defaults(root=tmp_path)
    config.watch_folder = str(tmp_path / "watch")
    config.output_folder = str(tmp_path / "output")
    config.clip_pool_folder = str(tmp_path / "pool")
    config.debounce_seconds = 0.6
    config.stability_seconds = 0.5
    config.stability_interval_seconds = 0.1
    config.reconciliation_seconds = 3600  # disable periodic reconcile in unit test
    Path(config.watch_folder).mkdir(parents=True, exist_ok=True)
    Path(config.clip_pool_folder).mkdir(parents=True, exist_ok=True)
    controller = VMAutomaticController(
        config,
        state_path=tmp_path / "state.json",
        order_store=__import__("app.video_merger.project_order", fromlist=["ProjectOrderStore"]).ProjectOrderStore(tmp_path / "order.json"),
    )
    return controller


def _spy_observations(controller):
    observations: list[str] = []
    original = controller._observe_file

    def spy(path: Path):
        observations.append(path.name)
        original(path)

    controller._observe_file = spy  # type: ignore[method-assign]
    return observations


def test_debounce_collapses_event_burst(tmp_path):
    """A burst of created/modified events for one file must collapse into
    exactly ONE logical observation (spec §35)."""
    controller = make_fast_controller(tmp_path)
    observations = _spy_observations(controller)
    watch = controller.vm_config.resolved_watch_folder()
    file = watch / "Burst_001.mp3"
    file.write_bytes(b"v1")
    # burst: created + 4 rapid modifications (one logical file, 5 events)
    for version in range(5):
        controller._on_fs_event("created" if version == 0 else "modified", file)
        file.write_bytes(f"v{version}".encode())
        controller.tick()
    # still inside the debounce window: nothing processed yet
    time.sleep(0.2)
    controller.tick()
    assert observations == []
    # after the quiet window: exactly ONE observation for the file
    time.sleep(0.6)
    controller.tick()
    assert observations.count("Burst_001.mp3") == 1, observations
    # no further events → no further observations
    time.sleep(0.3)
    controller.tick()
    assert observations.count("Burst_001.mp3") == 1, observations


def test_debounce_reopens_on_later_event(tmp_path):
    controller = make_fast_controller(tmp_path)
    observations = _spy_observations(controller)
    watch = controller.vm_config.resolved_watch_folder()
    file = watch / "Burst_002.txt"
    file.write_bytes(b"1")
    controller._on_fs_event("created", file)
    controller.tick()
    time.sleep(0.7)
    controller.tick()
    assert observations.count("Burst_002.txt") == 1
    # a later, separate modification re-arms the debounce window
    time.sleep(0.2)
    file.write_bytes(b"12")
    controller._on_fs_event("modified", file)
    controller.tick()
    time.sleep(0.7)
    controller.tick()
    assert observations.count("Burst_002.txt") == 2


def test_events_outside_watch_folder_are_ignored(tmp_path):
    controller = make_fast_controller(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    file = outside / "Nope_001.mp3"
    file.write_bytes(b"x")
    controller._register_event("created", file)
    assert controller._debounce == {}


def test_polling_watcher_is_a_functional_fallback(tmp_path, events):
    target = tmp_path / "inbox"
    target.mkdir()
    watcher = PollingWatcher(target, lambda kind, path: events.append((kind, path)), poll_interval=0.2)
    watcher.start()
    try:
        file = target / "Poll_001.mp3"
        file.write_bytes(b"a")
        assert _wait_until(lambda: any(k == "created" and p.name == "Poll_001.mp3" for k, p in events))
        time.sleep(0.3)
        events.clear()
        file.write_bytes(b"ab")
        assert _wait_until(lambda: any(p.name == "Poll_001.mp3" for k, p in events))
    finally:
        watcher.stop()
