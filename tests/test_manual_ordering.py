from __future__ import annotations

import json

import pytest

from app.video_merger.project_order import ProjectOrderStore


def _files(folder, names):
    paths = []
    for name in names:
        path = folder / name
        path.touch()
        paths.append(path)
    return paths


def test_manual_order_survives_restart_rescan_add_remove_and_reset(tmp_path):
    folder = tmp_path / "Clips ä"
    folder.mkdir()
    first_in = _files(folder, ["video_B.mp4", "video_A.mp4", "video_C.mp4"])
    state = tmp_path / "project_order.json"
    store = ProjectOrderStore(state)

    assert [p.name for p in store.order(folder, first_in)] == [
        "video_A.mp4", "video_B.mp4", "video_C.mp4"
    ]
    manual = [first_in[2], first_in[0], first_in[1]]
    store.set_active_order(folder, manual)

    restarted = ProjectOrderStore(state)
    assert [p.name for p in restarted.order(folder, list(reversed(first_in)))] == [
        "video_C.mp4", "video_B.mp4", "video_A.mp4"
    ]

    # Missing B is removed from both current sequences. A genuinely new D is
    # appended after surviving manual items, regardless of detector order.
    first_in[0].unlink()
    new_file = folder / "video_D.mp4"
    new_file.touch()
    detector_order = [new_file, first_in[1], first_in[2]]
    active = restarted.order(folder, detector_order)
    assert [p.name for p in active] == ["video_C.mp4", "video_A.mp4", "video_D.mp4"]

    restored = restarted.reset_to_first_in(folder, active)
    assert [p.name for p in restored] == ["video_A.mp4", "video_C.mp4", "video_D.mp4"]
    assert [p.name for p in ProjectOrderStore(state).order(folder, detector_order)] == [
        "video_A.mp4", "video_C.mp4", "video_D.mp4"
    ]


def test_active_order_is_exact_not_alphabetical_and_rejects_duplicates(tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    detected = _files(folder, ["z.mp4", "a.mp4", "m.mp4"])
    store = ProjectOrderStore(tmp_path / "state.json")
    store.order(folder, detected)
    exact = [detected[1], detected[2], detected[0]]
    store.set_active_order(folder, exact)
    assert store.order(folder, detected) == exact
    with pytest.raises(ValueError, match="doppelte"):
        store.set_active_order(folder, [detected[0], detected[0]])


def test_version_one_state_migrates_without_losing_first_in(tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    paths = _files(folder, ["B.mp4", "A.mp4"])
    state = tmp_path / "old.json"
    key = ProjectOrderStore._folder_key(folder)
    state.write_text(json.dumps({key: ["B.mp4", "A.mp4"]}), encoding="utf-8")

    store = ProjectOrderStore(state)
    assert store.order(folder, list(reversed(paths))) == paths
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["folders"][key]["first_in"] == ["B.mp4", "A.mp4"]
    assert payload["folders"][key]["active"] == ["B.mp4", "A.mp4"]
