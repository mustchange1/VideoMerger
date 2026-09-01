"""Focused regression coverage for the 1.4.0 workflow enhancement."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

import app.video_merger.engine as engine_module
from app.video_merger.discovery import discover_videos
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.models import ExportSettings, ValidationReport
from app.video_merger.project_order import ProjectOrderStore
from app.video_merger.render_cache import stage1_fingerprint
from app.video_merger.settings_store import SettingsStore
from app.video_merger.timeline import (
    duration_after_merge_value,
    duration_before_merge_value,
)
from app.video_merger.video_pool import folder_aware_order, select_required_media
from tests.conftest import fake_media


def _folder_media(root: Path, folder: str, count: int, duration: float = 2.0):
    return [
        fake_media(str(root / folder / f"clip_{index}.mp4"), duration=duration)
        for index in range(count)
    ]


def test_multiple_source_folders_are_discovered_and_ordered_persistently(tmp_path):
    first = tmp_path / "Folder A"
    second = tmp_path / "Folder B"
    first.mkdir()
    second.mkdir()
    for folder, names in ((first, ["clip10.mp4", "clip2.mp4", "clip1.mp4"]), (second, ["b2.mp4", "b1.mp4"])):
        for name in names:
            (folder / name).touch()

    state = tmp_path / "project_order.json"
    store = ProjectOrderStore(state)
    found = discover_videos([first, second], order_store=store)
    assert [path.parent.name for path in found] == ["Folder A", "Folder A", "Folder A", "Folder B", "Folder B"]
    assert [path.name for path in found[:3]] == ["clip1.mp4", "clip2.mp4", "clip10.mp4"]

    restarted = discover_videos([first, second], order_store=ProjectOrderStore(state))
    assert restarted == found
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2


def test_explicit_child_folders_are_the_multi_source_workflow(tmp_path):
    root = tmp_path / "library"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "A" / "a.mp4").touch()
    (root / "B" / "b.mp4").touch()
    found = discover_videos([root / "A", root / "B"])
    assert {path.parent.name for path in found} == {"A", "B"}


def test_folder_aware_selection_randomly_alternates_and_falls_back(tmp_path):
    media = _folder_media(tmp_path, "A", 4) + _folder_media(tmp_path, "B", 2)
    selected = folder_aware_order(media, rng=random.Random(7))
    folders = [item.source_folder or str(item.path.parent) for item in selected]
    folders = [Path(folder).name for folder in folders]
    assert folders[:4].count("A") + folders[:4].count("B") == 4
    for index in range(len(folders) - 1):
        if folders[index + 1] == folders[index]:
            # A same-folder continuation is legal only once every alternative
            # source folder has no remaining usable clip.
            assert not any(folder != folders[index] for folder in folders[index + 1:])
    # Every clip remains exactly once and within-folder order is retained.
    assert [item.path.name for item in selected if item.path.parent.name == "A"] == [f"clip_{i}.mp4" for i in range(4)]
    assert [item.path.name for item in selected if item.path.parent.name == "B"] == [f"clip_{i}.mp4" for i in range(2)]


def test_selection_preserves_explicit_manual_order(tmp_path):
    media = _folder_media(tmp_path, "A", 2) + _folder_media(tmp_path, "B", 2)
    manual = [media[0], media[1], media[2], media[3]]
    selected, _ = select_required_media(
        manual, target_duration=20.0, transition_duration=0.1, fps=30.0, folder_aware=False,
    )
    assert [item.path for item in selected] == [item.path for item in manual]


def test_independent_merge_duration_defaults_and_legacy_migration(tmp_path):
    defaults = ExportSettings()
    assert defaults.duration_before_merge == 0.70
    assert defaults.duration_after_merge == 1.0
    assert defaults.duration_after_merge_enabled is False
    assert duration_before_merge_value(defaults) == 0.70
    assert duration_after_merge_value(defaults) == 1.0

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"video_speed": 0.55, "quote_duration": 2.0}), encoding="utf-8")
    migrated = SettingsStore(path).load()
    assert migrated.duration_before_merge == 0.55
    assert migrated.quote_duration == 2.0  # explicit existing override survives

    selected = ExportSettings(
        source_folders=[str(tmp_path / "A"), str(tmp_path / "B")],
        duration_before_merge=0.70,
        duration_after_merge=0.80,
        duration_after_merge_enabled=True,
    )
    SettingsStore(path).save(selected)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["source_folders"] == selected.source_folders
    assert persisted["duration_after_merge_enabled"] is True
    assert "video_speed" not in persisted
    assert SettingsStore(path).load() == selected


def test_after_merge_is_a_separate_whole_master_operation(tmp_path, monkeypatch):
    source = tmp_path / "clean-master.mp4"
    source.write_bytes(b"before")
    media = _folder_media(tmp_path, "A", 1)
    settings = ExportSettings(
        workflow_stage="main", duration_after_merge=0.8, duration_after_merge_enabled=True,
    )
    resolved = engine_module.resolve_export(media, settings)
    engine = VideoMergerEngine.__new__(VideoMergerEngine)
    engine.ffmpeg_path = tmp_path / "ffmpeg"
    engine.ffprobe_path = tmp_path / "ffprobe"
    calls = []

    def fake_execute(command, *_args):
        calls.append(command)
        Path(command[-1]).write_bytes(b"after")

    def fake_validate(path, *_args):
        return ValidationReport(True, [], Path(path), duration=resolved.expected_duration)

    monkeypatch.setattr(engine, "_execute", fake_execute)
    monkeypatch.setattr(engine_module, "validate_output", fake_validate)
    report = engine.post_process_duration(source, resolved, settings, media)

    assert report.ok
    assert source.read_bytes() == b"after"
    assert len(calls) == 1
    command = calls[0]
    graph = command[command.index("-filter_complex") + 1]
    assert "[0:v]setpts=PTS/0.800000" in graph
    assert "[0:a]aresample=48000:async=1,atempo=0.8" in graph
    assert command[command.index("-i") + 1] == str(source.resolve())


def test_stage1_cache_identity_includes_before_after_and_source_folder(tmp_path):
    media = _folder_media(tmp_path, "A", 1)
    resolved = type("Resolved", (), {
        "width": 160, "height": 90, "fps": 30.0, "fps_expr": "30",
        "effective_durations": [2.0], "transitions": [], "expected_duration": 2.0,
        "encoder": "libx264", "encoder_label": "CPU", "crf": 18,
        "preset": "fast", "quality_label": "High",
    })()
    first = stage1_fingerprint(media, ExportSettings(), resolved)[0]
    changed = stage1_fingerprint(
        media,
        ExportSettings(duration_after_merge=0.8, duration_after_merge_enabled=True),
        resolved,
    )[0]
    changed_before = stage1_fingerprint(
        media, ExportSettings(duration_before_merge=1.0), resolved,
    )[0]
    changed_folder = stage1_fingerprint(
        [replace(media[0], source_folder=str(tmp_path / "another-folder"))],
        ExportSettings(), resolved,
    )[0]
    assert changed != first
    assert changed_before != first
    assert changed_folder != first
