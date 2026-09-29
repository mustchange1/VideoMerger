from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.asset_cooldown import AssetCooldownHistory
from app.video_merger.master_timeline import (
    MASTER_MODE_RANDOM,
    MASTER_MODE_SMART,
    MASTER_SLOT_MAX_SECONDS,
    MASTER_SLOT_MIN_SECONDS,
    MasterTimelineBuilder,
)
from app.video_merger.media_pool import MediaPool, MediaPoolAsset
from app.video_merger.smart_timeline import SemanticSection, SpeechUnit, build_semantic_sections


def _pool(root: Path, *, images: int = 5, videos: int = 8) -> MediaPool:
    assets = []
    for index in range(images):
        assets.append(MediaPoolAsset(
            path=str(root / f"forest_image_{index}.jpg"), kind="image",
            category="forest", keywords=("forest", "trees"), metadata_text="forest trees",
        ))
    for index in range(videos):
        assets.append(MediaPoolAsset(
            path=str(root / f"city_video_{index}.mp4"), kind="video",
            category="city", keywords=("city", "street"), metadata_text="city street",
        ))
    return MediaPool(assets)


def _section(duration: float, text: str = "A forest topic continues through the complete thought.") -> SemanticSection:
    return SemanticSection(
        index=0, text=text, start=0.0, end=duration, speech_unit_indices=(0,),
        boundary_reason="semantic_section",
    )


def test_master_timeline_is_exactly_voiceover_locked_and_strictly_paced(tmp_path: Path) -> None:
    timeline = MasterTimelineBuilder(
        media_pool=_pool(tmp_path), sections=[_section(28.0)], voiceover_duration=28.0,
        mode=MASTER_MODE_RANDOM, image_ratio_min=0, image_ratio_max=100,
        seed_parts=("exact-lock",),
    ).build()
    assert timeline.slots[0].start == 0.0
    assert timeline.slots[-1].end == pytest.approx(28.0)
    assert sum(slot.duration for slot in timeline.slots) == pytest.approx(28.0)
    assert len(timeline.slots) in {3, 4}
    assert all(MASTER_SLOT_MIN_SECONDS <= slot.duration <= MASTER_SLOT_MAX_SECONDS for slot in timeline.slots)
    assert all(slot.end <= 28.0 for slot in timeline.slots)


def test_long_single_thought_splits_into_three_or_four_same_topic_visuals() -> None:
    units = [SpeechUnit(
        index=0, text="One continuous topic", char_start=0, char_end=20,
        start=0.0, end=24.0, first_word_index=0, last_word_index=20,
    )]
    sections = build_semantic_sections(units)
    assert len(sections) in {3, 4}
    assert all(section.text == "One continuous topic" for section in sections)
    assert all(4.0 <= section.duration <= 12.0 for section in sections)
    assert sections[-1].end == pytest.approx(24.0)


def test_ratio_is_enforced_across_available_slots(tmp_path: Path) -> None:
    timeline = MasterTimelineBuilder(
        media_pool=_pool(tmp_path, images=10, videos=10), sections=[_section(96.0)],
        voiceover_duration=96.0, mode=MASTER_MODE_SMART,
        image_ratio_min=30, image_ratio_max=40, seed_parts=("ratio",),
    ).build()
    assert 30.0 <= timeline.image_percent <= 40.0
    assert timeline.image_count + timeline.video_count == len(timeline.slots)


def test_cooldown_lockout_across_five_consecutive_renders(tmp_path: Path) -> None:
    history = AssetCooldownHistory(tmp_path / "data" / "asset_cooldown_history.json")
    pool = _pool(tmp_path, images=0, videos=10)
    selected_by_render: list[set[str]] = []
    for render_index in range(5):
        timeline = MasterTimelineBuilder(
            media_pool=pool, sections=[_section(8.0, "city street topic")],
            voiceover_duration=8.0, mode=MASTER_MODE_RANDOM,
            image_ratio_min=0, image_ratio_max=0, cooldown_videos=3,
            cooldown_history=history, seed_parts=("render", render_index),
        ).build()
        selected = set(timeline.selected_asset_ids)
        assert all(selected.isdisjoint(previous) for previous in selected_by_render[-3:])
        history.record_video(selected)
        selected_by_render.append(selected)
    assert len(history.videos()) == 5


def test_preview_does_not_consume_cooldown(tmp_path: Path) -> None:
    history = AssetCooldownHistory(tmp_path / "history.json")
    builder = MasterTimelineBuilder(
        media_pool=_pool(tmp_path), sections=[_section(8.0)], voiceover_duration=8.0,
        cooldown_history=history,
    )
    builder.build()
    assert history.videos() == []


def test_gui_exposes_cooldown_and_ratio_sliders() -> None:
    source = (Path(__file__).resolve().parents[1] / "app/video_merger/gui/main_window.py").read_text(encoding="utf-8")
    assert '"Skip used asset for next N videos"' in source
    assert 'w["image_ratio_min"] = QSlider(Qt.Horizontal)' in source
    assert 'w["image_ratio_max"] = QSlider(Qt.Horizontal)' in source
    assert '"Pure Random Mode"' in source
    assert '"Smart Visuals Mode (semantic relevance)"' in source


def test_cooldown_and_ratio_settings_stay_separate_per_output_profile() -> None:
    from app.video_merger.models import ExportSettings
    from app.video_merger.youtube_outputs import ShortJob, short_settings

    settings = ExportSettings(
        asset_cooldown_videos=2, image_ratio_min=10, image_ratio_max=20,
        shorts_asset_cooldown_videos=5, shorts_image_ratio_min=45,
        shorts_image_ratio_max=55,
    )
    short = short_settings(settings, ShortJob(
        index=0, voiceover_path=Path("voice.wav"), script_path=None,
        output_name="Short_001", cache_key="test",
    ))
    assert (settings.asset_cooldown_videos, settings.image_ratio_min, settings.image_ratio_max) == (2, 10, 20)
    assert (short.asset_cooldown_videos, short.image_ratio_min, short.image_ratio_max) == (5, 45, 55)
