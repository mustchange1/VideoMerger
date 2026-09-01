"""Focused coverage for the project-level Video Order modes."""

from __future__ import annotations

import json
import random
from pathlib import Path

from app.video_merger.models import AudioInfo, ExportSettings, MediaInfo, ResolvedExport, ValidationReport
from app.video_merger.render_cache import stage1_fingerprint
from app.video_merger.settings_store import SettingsStore
from app.video_merger.video_pool import (
    VIDEO_ORDER_ALPHABETICAL,
    VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING,
    VIDEO_ORDER_MANUAL,
    VIDEO_ORDER_MODES,
    VIDEO_ORDER_NATURAL,
    VIDEO_ORDER_RANDOM,
    normalize_video_order_mode,
    order_media_for_video_order,
    select_required_media,
)


def _media(path: str, duration: float = 2.0) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=1920, height=1080,
        effective_width=1920, effective_height=1080, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="", audio=AudioInfo(),
    )


def test_selector_modes_and_legacy_normalization():
    assert VIDEO_ORDER_MODES == ("natural", "alphabetical", "random", "manual")
    assert normalize_video_order_mode("Random") == VIDEO_ORDER_RANDOM
    assert normalize_video_order_mode("folder_alternating") == VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING
    assert normalize_video_order_mode("not-a-mode") == VIDEO_ORDER_NATURAL


def test_natural_and_alphabetical_are_numeric_and_filename_aware():
    media = [_media("/pool/clip10.mp4"), _media("/pool/clip2.mp4"), _media("/pool/clip1.mp4")]
    assert [item.path.name for item in order_media_for_video_order(media, VIDEO_ORDER_NATURAL)] == [
        "clip1.mp4", "clip2.mp4", "clip10.mp4",
    ]
    alpha = [_media("/pool/Z.mp4"), _media("/pool/a10.mp4"), _media("/pool/a2.mp4")]
    assert [item.path.name for item in order_media_for_video_order(alpha, VIDEO_ORDER_ALPHABETICAL)] == [
        "a10.mp4", "a2.mp4", "Z.mp4",
    ]


def test_random_is_seeded_permutation_and_does_not_become_manual():
    media = [_media(f"/pool/clip_{i}.mp4") for i in range(8)]
    first = order_media_for_video_order(media, VIDEO_ORDER_RANDOM, seed=1234)
    again = order_media_for_video_order(media, VIDEO_ORDER_RANDOM, seed=1234)
    assert [item.path for item in first] == [item.path for item in again]
    assert {item.path for item in first} == {item.path for item in media}
    assert [item.path for item in first] != [item.path for item in media]
    assert [item.path for item in order_media_for_video_order(media, VIDEO_ORDER_MANUAL)] == [
        item.path for item in media
    ]


def test_random_applies_folder_alternation_after_permutation():
    media = [
        _media(f"/pool/A/a{i}.mp4") for i in range(4)
    ] + [
        _media(f"/pool/B/b{i}.mp4") for i in range(3)
    ]
    ordered = order_media_for_video_order(media, VIDEO_ORDER_RANDOM, rng=random.Random(9))
    folders = [item.path.parent.name for item in ordered]
    assert sorted(item.path for item in ordered) == sorted(item.path for item in media)
    for index in range(len(folders) - 1):
        if folders[index] == folders[index + 1]:
            assert not any(folder != folders[index] for folder in folders[index + 1:])


def test_required_only_selection_uses_the_same_seeded_effective_prefix():
    media = [_media(f"/pool/{name}.mp4", duration) for name, duration in (
        ("a", 1.0), ("b", 7.0), ("c", 1.0), ("d", 1.0),
    )]
    expected = order_media_for_video_order(media, VIDEO_ORDER_RANDOM, seed=4)
    selected, _warnings = select_required_media(
        media, target_duration=6.0, transition_duration=0.0, fps=30.0,
        folder_aware=False, video_order_mode=VIDEO_ORDER_RANDOM, video_order_seed=4,
    )
    assert [item.path for item in selected] == [item.path for item in expected]
    assert [item.path for item in selected] != [item.path for item in media]


def test_main_project_uses_effective_order_before_plan_and_export(tmp_path, monkeypatch):
    import app.video_merger.main_project as main_project_module
    from app.video_merger.main_project import MainProjectEngine

    class StubEngine:
        ffprobe_path = tmp_path / "ffprobe"

        def __init__(self):
            self.seen = []

        def make_plan(self, media, settings, _log):
            self.seen.append(("plan", [item.path.name for item in media], settings.video_order_mode))
            return ResolvedExport(
                160, 90, 30.0, "30", [item.duration for item in media],
                [0.0] * max(0, len(media) - 1), sum(item.duration for item in media),
                encoder="libx264",
            )

        def export(self, media, settings, _resolved, output_path, **kwargs):
            self.seen.append(("export", [item.path.name for item in media], settings.video_order_mode, kwargs.get("video_order_applied")))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"stub")
            return ValidationReport(True, [], output_path, duration=3.0, width=160, height=90, fps=30.0, has_video=True)

    monkeypatch.setattr(
        main_project_module,
        "validate_output",
        lambda path, *_args: ValidationReport(True, [], Path(path), duration=3.0, width=160, height=90, fps=30.0, has_video=True),
    )
    engine = StubEngine()
    media = [_media("/pool/clip3.mp4"), _media("/pool/clip1.mp4"), _media("/pool/clip2.mp4")]
    MainProjectEngine(engine).create_main(
        media,
        ExportSettings(video_order_mode=VIDEO_ORDER_NATURAL, resolution="160x90", encoding="CPU"),
        tmp_path / "output",
    )
    assert engine.seen[0][1] == ["clip1.mp4", "clip2.mp4", "clip3.mp4"]
    assert engine.seen[1][1] == engine.seen[0][1]
    assert engine.seen[1][2:] == (VIDEO_ORDER_NATURAL, True)


def test_video_order_setting_persists_and_modes_are_cache_distinct(tmp_path):
    settings_path = tmp_path / "settings.json"
    selected = ExportSettings(video_order_mode=VIDEO_ORDER_RANDOM)
    SettingsStore(settings_path).save(selected)
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload["video_order_mode"] == VIDEO_ORDER_RANDOM
    assert SettingsStore(settings_path).load().video_order_mode == VIDEO_ORDER_RANDOM

    media = [_media("/pool/a.mp4"), _media("/pool/b.mp4")]
    natural = stage1_fingerprint(media, ExportSettings(video_order_mode=VIDEO_ORDER_NATURAL), _resolved())[0]
    random_mode = stage1_fingerprint(media, selected, _resolved())[0]
    manual = stage1_fingerprint(media, ExportSettings(video_order_mode=VIDEO_ORDER_MANUAL), _resolved())[0]
    assert natural != random_mode
    assert random_mode != manual


def _resolved():
    return type("Resolved", (), {
        "width": 1920, "height": 1080, "fps": 30.0, "fps_expr": "30",
        "effective_durations": [2.0, 2.0], "transitions": [0.0],
        "expected_duration": 4.0, "encoder": "libx264", "encoder_label": "CPU",
        "crf": 18, "preset": "slow", "quality_label": "High",
    })()
