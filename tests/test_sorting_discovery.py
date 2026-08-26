from pathlib import Path

import pytest

from app.video_merger import discovery
from app.video_merger.discovery import discover_videos
from app.video_merger.project_order import GeneratedOutputStore, ProjectOrderStore


def test_detection_order_is_first_in_not_alphabetical(tmp_path, monkeypatch):
    paths = []
    for name in ("video_B.mp4", "video_A.mp4", "video_C.mp4"):
        path = tmp_path / name
        path.touch()
        paths.append(path)
    # Simulate the detector reporting B, A, C. The discovery layer must not
    # replace this with an alphabetical order.
    monkeypatch.setattr(discovery, "_detect_current_folder", lambda _root, _excluded: paths)
    found = discover_videos(tmp_path)
    assert [path.name for path in found] == ["video_B.mp4", "video_A.mp4", "video_C.mp4"]


def test_persistent_order_survives_restart_and_new_detection_order(tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    detected = []
    for name in ("video_B.mp4", "video_A.mp4", "video_C.mp4"):
        path = folder / name
        path.touch()
        detected.append(path)
    state_file = tmp_path / "persistent_order.json"
    first_store = ProjectOrderStore(state_file)
    # 1.2.3: the default order for a brand-new folder is the natural
    # numeric/alphabetical order (A, B, C), not the detector sequence.
    assert [p.name for p in first_store.order(folder, detected)] == ["video_A.mp4", "video_B.mp4", "video_C.mp4"]
    # A fresh store instance simulates an application restart. Even if the OS
    # detector now returns another order, the persisted default order remains
    # stable and natural.
    restarted_store = ProjectOrderStore(state_file)
    assert [p.name for p in restarted_store.order(folder, list(reversed(detected)))] == ["video_A.mp4", "video_B.mp4", "video_C.mp4"]
    new_file = folder / "video_D.mp4"
    new_file.touch()
    assert [p.name for p in restarted_store.order(folder, [new_file, *reversed(detected)])] == [
        "video_A.mp4", "video_B.mp4", "video_C.mp4", "video_D.mp4"
    ]


def test_unicode_detection_order(tmp_path, monkeypatch):
    paths = []
    for name in ("Clip ü.mp4", "Clip ä.mp4", "Clip ö.mp4"):
        path = tmp_path / name
        path.touch()
        paths.append(path)
    monkeypatch.setattr(discovery, "_detect_current_folder", lambda _root, _excluded: paths)
    assert [p.name for p in discover_videos(tmp_path)] == ["Clip ü.mp4", "Clip ä.mp4", "Clip ö.mp4"]


def test_discovery_ignores_non_video_and_subfolders(tmp_path):
    (tmp_path / "video_B.MOV").touch()
    (tmp_path / "video_A.mp4").touch()
    (tmp_path / "notes.txt").touch()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "video_C.mp4").touch()
    found = discover_videos(tmp_path)
    assert {p.name for p in found} == {"video_B.MOV", "video_A.mp4"}


def test_generated_hidden_temporary_and_registered_outputs_are_excluded(tmp_path):
    source = tmp_path / "video_A.mp4"
    registered = tmp_path / "custom_previous_export.mp4"
    for name in (
        source.name,
        registered.name,
        "merged_16x9_old.mp4",
        "preview_transition_old.mp4",
        ".hidden.mp4",
        "copy.partial.mp4",
        "copy.crdownload.mp4",
    ):
        (tmp_path / name).touch()
    registry = GeneratedOutputStore(tmp_path / "generated_outputs.json")
    registry.add(registered)
    found = discover_videos(tmp_path, excluded_paths=registry.paths())
    assert [path.name for path in found] == [source.name]


def test_discovery_empty_folder_has_clear_error(tmp_path):
    with pytest.raises(Exception, match="Keine geeigneten"):
        discover_videos(tmp_path)
