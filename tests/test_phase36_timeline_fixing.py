"""Phase 36: audio-locked semantic Smart Visual timeline regressions."""
from __future__ import annotations

from pathlib import Path
from random import Random

import pytest

from app.video_merger.models import AudioInfo, ExportSettings, MediaInfo
from app.video_merger.smart_timeline import (
    PHASE36_HARD_MIN_SECONDS,
    PHASE36_TARGET_MAX_SECONDS,
    SemanticSection,
    SpeechUnit,
    build_semantic_sections,
    lock_timeline_duration,
    render_chain,
)
from app.video_merger.smart_visuals import (
    MediaIndexEntry,
    SlotDraft,
    SmartVisualProfile,
    TimedSlot,
    assign_smart_visual_selections,
)


def unit(index: int, text: str, start: float, end: float) -> SpeechUnit:
    return SpeechUnit(
        index=index,
        text=text,
        char_start=index * 20,
        char_end=index * 20 + len(text),
        start=start,
        end=end,
        first_word_index=index,
        last_word_index=index,
    )


def media(name: str, duration: float, *, smart: bool = False) -> MediaInfo:
    return MediaInfo(
        path=Path(name),
        duration=duration,
        source_duration=duration,
        width=320,
        height=180,
        effective_width=320,
        effective_height=180,
        fps=30.0,
        fps_fraction="30/1",
        video_codec="h264",
        pixel_format="yuv420p",
        sar="1:1",
        dar="16:9",
        audio=AudioInfo(present=False),
        smart_visual_insertion=smart,
        smart_visual_audio_anchored=smart,
    )


def settings(transition: float = 0.3) -> ExportSettings:
    return ExportSettings(
        resolution="320x180",
        fps_choice="30",
        transition_type="cross_dissolve",
        transition_duration=transition,
        duration_before_merge=1.0,
        workflow_stage="",
    )


def test_timeline_boundary_is_locked_to_voiceover_and_has_no_frames_past_end():
    cfg = settings(0.3)
    # This models the Phase-35 bug directly: a fitted 20-second source chain
    # receives ten additional seconds of Smart Visual material.
    overflowing = [
        media("source-a.mp4", 10.15),
        media("smart-a.mp4", 5.0, smart=True),
        media("source-b.mp4", 10.15),
        media("smart-b.mp4", 5.0, smart=True),
    ]
    locked = lock_timeline_duration(overflowing, 20.0, cfg, 30.0)
    chain = render_chain(locked, cfg)
    assert chain[-1].final_end == pytest.approx(20.0, abs=1 / 600)
    assert all(item.final_start < 20.0 and item.final_end <= 20.0 + 1 / 600 for item in chain)
    assert len(locked) < len(overflowing) or locked[-1].duration < overflowing[-1].duration


def test_duration_guard_has_no_generated_fragment_below_four_or_above_fifteen():
    units = [
        unit(0, "Forest roots share water.", 0.0, 4.8),
        unit(1, "Forest trees protect the soil.", 5.0, 9.8),
        unit(2, "Ocean currents carry heat.", 10.0, 14.8),
        unit(3, "Ocean currents shape weather.", 15.0, 20.0),
        unit(4, "Cities adapt to change.", 20.2, 25.0),
    ]
    sections = build_semantic_sections(units)
    assert sections
    assert all(section.duration >= PHASE36_HARD_MIN_SECONDS for section in sections)
    assert all(section.duration <= PHASE36_TARGET_MAX_SECONDS for section in sections)


def test_long_22_second_section_splits_into_three_strict_sentence_units():
    units = [
        unit(0, "Climate systems store energy.", 0.0, 5.4),
        unit(1, "Climate systems move energy.", 5.5, 10.9),
        unit(2, "Climate systems release energy.", 11.0, 16.4),
        unit(3, "Climate systems affect everyone.", 16.5, 22.0),
    ]
    sections = build_semantic_sections(units)
    assert len(sections) == 3
    assert [section.speech_unit_indices for section in sections] == [(0,), (1, 2), (3,)]
    assert [section.duration for section in sections] == pytest.approx([5.4, 10.9, 5.5], abs=0.2)
    assert all(4.0 <= section.duration <= 12.0 for section in sections)


def test_short_section_merges_instead_of_creating_a_flash():
    units = [
        unit(0, "A brief warning.", 0.0, 2.0),
        unit(1, "The warning explains the complete safety procedure.", 2.1, 8.0),
        unit(2, "A final note.", 8.2, 10.5),
    ]
    sections = build_semantic_sections(units)
    assert sections == [
        SemanticSection(
            index=0,
            text=" ".join(item.text for item in units),
            start=0.0,
            end=10.5,
            speech_unit_indices=(0, 1, 2),
        )
    ]


def test_single_long_sentence_gets_inferred_strict_thought_boundaries():
    sections = build_semantic_sections([
        unit(0, "One intentionally uninterrupted spoken sentence.", 0.0, 18.0)
    ])
    assert len(sections) == 2
    assert [section.duration for section in sections] == pytest.approx([9.0, 9.0])
    assert all(section.unsplittable is False for section in sections)
    assert all(section.boundary_reason == "inferred_thought_boundary" for section in sections)


def test_gui_removes_block_7_and_exposes_unified_media_controls():
    source = (Path(__file__).parents[1] / "app" / "video_merger" / "gui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    assert 'QGroupBox("7 · Image Timeline & Visual Effects")' not in source
    assert 'QGroupBox("7 · Smart Visuals — Unified Timeline")' in source
    assert 'QPushButton("Reload / Reshuffle Smart Order")' in source
    assert 'w["image_motion"]' in source
    assert 'w["image_fit_mode"]' in source
    assert 'w["broll_behavior"]' in source


def test_smart_order_reshuffle_changes_sequence_but_keeps_relevant_candidates():
    timed = [
        TimedSlot(
            start=0.0,
            end=7.0,
            draft=SlotDraft(
                sentences=["Forest trees and forest roots."],
                keywords=["forest", "trees"],
                sentence_indices=[0],
            ),
        ),
        TimedSlot(
            start=7.0,
            end=14.0,
            draft=SlotDraft(
                sentences=["Forest paths cross forest trees."],
                keywords=["forest", "trees"],
                sentence_indices=[1],
            ),
        ),
    ]
    entries = [
        MediaIndexEntry(
            path=f"/pool/forest_{name}.jpg",
            kind="image",
            category="forest",
            keywords=("forest", "trees"),
            title=f"Forest {name}",
            signature=name,
            metadata_text="forest trees roots paths",
        )
        for name in ("a", "b", "c")
    ]
    base = assign_smart_visual_selections(
        timed,
        entries,
        profile=SmartVisualProfile(enabled=True, folders=("/pool",), mode="smart_match", randomize_nonce=0),
        rng=Random(1),
    )
    shuffled = assign_smart_visual_selections(
        timed,
        entries,
        profile=SmartVisualProfile(enabled=True, folders=("/pool",), mode="smart_match", randomize_nonce=1),
        rng=Random(2),
    )
    base_paths = [slot.selected_path for slot in base]
    shuffled_paths = [slot.selected_path for slot in shuffled]
    assert shuffled_paths != base_paths
    assert all(slot.source_mode == "SMART" for slot in shuffled)
    assert all(slot.score >= 0.5 for slot in shuffled)


def test_dynamic_smart_refresh_uses_widget_free_qthread_worker() -> None:
    root = Path(__file__).resolve().parents[1]
    main_source = (root / "app/video_merger/gui/main_window.py").read_text(encoding="utf-8")
    worker_source = (root / "app/video_merger/gui/workers.py").read_text(encoding="utf-8")
    assert "timer.timeout.connect(lambda p=prefix: self._smart_visual_recalculate_background(p))" in main_source
    assert "worker.moveToThread(thread)" in main_source
    assert "class MasterTimelineWorker(QObject):" in worker_source
    assert "build_smart_visual_plan" not in worker_source
    assert "self.ready.emit(self.request.build())" in worker_source
