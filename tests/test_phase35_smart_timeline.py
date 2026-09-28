"""Phase-35 isolated Smart Visual timeline tests.

These tests exercise the first-class pre-render model without FFmpeg. They
prove transition-aware geometry, source splitting, sequential application and
that the production resolver remains the single geometry authority.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.models import AudioInfo, ExportSettings, MediaInfo, WordTiming
from app.video_merger.smart_timeline import (
    SmartVisualTimeline,
    build_speech_units,
    insert_at_render_time,
    minimum_fragment_seconds,
    render_chain,
    validate_timeline,
)
from app.video_merger.target import resolve_export


def media(name: str, duration: float, *, smart=False, image=False, color_audio=True) -> MediaInfo:
    return MediaInfo(
        path=Path(name), duration=duration, width=320, height=180,
        effective_width=320, effective_height=180, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="16:9", source_duration=duration,
        audio=AudioInfo(present=color_audio, codec="aac", sample_rate=48000, channels=2),
        smart_visual_insertion=smart, is_image_insertion=image,
        image_timeline_insertion=image,
    )


def settings(transition=0.0) -> ExportSettings:
    return ExportSettings(
        resolution="320x180", fps_choice="30", transition_duration=transition,
        transition_type="cross_dissolve", workflow_stage="",
    )


def words_for(script: str, specs: list[tuple[str, float, float]]) -> list[WordTiming]:
    result = []
    cursor = 0
    for token, start, end in specs:
        index = script.index(token, cursor)
        result.append(WordTiming(token, start, end, 1.0, index, index + len(token)))
        cursor = index + len(token)
    return result


def test_speech_units_use_acoustic_sentence_starts_and_keep_punctuation():
    script = "Hello world! Next sentence? Final one."
    words = words_for(script, [
        ("Hello", .10, .30), ("world", .32, .55),
        ("Next", .80, 1.0), ("sentence", 1.02, 1.30),
        ("Final", 2.0, 2.2), ("one", 2.22, 2.45),
    ])
    units = build_speech_units(script, words)
    assert [unit.text for unit in units] == ["Hello world!", "Next sentence?", "Final one."]
    assert [unit.start for unit in units] == pytest.approx([.10, .80, 2.0])
    assert [unit.end for unit in units] == pytest.approx([.55, 1.30, 2.45])


def test_short_pause_is_clamped_after_previous_sentence_end():
    script = "First sentence. Second sentence."
    words = words_for(script, [
        ("First", 0.0, .5), ("sentence", .5, 1.0),
        ("Second", .98, 1.2), ("sentence", 1.2, 1.5),
    ])
    units = build_speech_units(script, words)
    assert units[1].start == pytest.approx(units[0].end)


def test_long_pause_is_preserved():
    script = "First. Much later."
    words = words_for(script, [("First", .1, .4), ("Much", 4.0, 4.2), ("later", 4.3, 4.6)])
    assert [unit.start for unit in build_speech_units(script, words)] == pytest.approx([.1, 4.0])


def test_render_chain_no_transition_is_simple_concatenation():
    chain = render_chain([media("a.mp4", 2), media("b.mp4", 3)], settings(0))
    assert [(item.final_start, item.final_end) for item in chain] == [(0, 2), (2, 5)]


def test_render_chain_xfade_matches_production_resolver():
    items = [media("a.mp4", 2), media("b.mp4", 3), media("c.mp4", 1.5)]
    cfg = settings(.7)
    chain = render_chain(items, cfg)
    resolved = resolve_export(items, cfg)
    assert chain[1].final_start == pytest.approx(resolved.effective_durations[0] - resolved.transitions[0])
    assert chain[-1].final_end == pytest.approx(sum(resolved.effective_durations) - sum(resolved.transitions))


def test_one_visual_exact_without_transition():
    items = [media("a.mp4", 4), media("b.mp4", 4)]
    visual = media("visual.mp4", 1, smart=True, color_audio=False)
    result, placement = insert_at_render_time(items, visual, 2.0, settings(0), 30)
    assert placement.status == "inserted"
    assert placement.final_expected_start == pytest.approx(2.0, abs=1 / 30)
    assert len(result) == 4  # left + visual + right + b


def test_one_visual_exact_with_xfade():
    items = [media("a.mp4", 4), media("b.mp4", 4)]
    visual = media("visual.mp4", .8, smart=True, color_audio=False)
    result, placement = insert_at_render_time(items, visual, 2.0, settings(.4), 30)
    assert placement.status == "inserted"
    assert abs(placement.placement_drift) <= 1 / 30 + 1e-9
    assert placement.xfade_overlap > 0
    assert render_chain(result, settings(.4))[result.index(visual)].final_start == pytest.approx(
        placement.final_expected_start
    )


def test_two_visuals_are_applied_sequentially():
    cfg = settings(.3)
    working = [media("a.mp4", 5), media("b.mp4", 5)]
    first = media("v1.mp4", .8, smart=True, color_audio=False)
    working, p1 = insert_at_render_time(working, first, 1.5, cfg, 30)
    second = media("v2.mp4", .8, smart=True, color_audio=False)
    before = len(working)
    working, p2 = insert_at_render_time(working, second, 4.0, cfg, 30)
    assert p1.status == p2.status == "inserted"
    assert p2.previous_insertions_present is True
    assert p2.item_count_before == before
    assert first in working and second in working
    assert abs(p2.placement_drift) <= 1 / 30 + 1e-9


def test_five_visuals_keep_using_current_chain():
    cfg = settings(.2)
    working = [media("a.mp4", 12), media("b.mp4", 12)]
    placements = []
    for index, anchor in enumerate((1.0, 3.0, 5.0, 7.0, 9.0)):
        visual = media(f"v{index}.mp4", .5, smart=True, color_audio=False)
        working, placement = insert_at_render_time(working, visual, anchor, cfg, 30)
        placements.append(placement)
    assert all(item.status == "inserted" for item in placements)
    assert all(item.previous_insertions_present for item in placements[1:])
    assert max(abs(item.placement_drift) for item in placements) <= 1 / 30 + 1e-9


def test_mixed_source_clip_durations():
    cfg = settings(.35)
    items = [media("short.mp4", .8), media("long.mp4", 5.5), media("mid.mp4", 2.1)]
    visual = media("visual.mp4", .6, smart=True, color_audio=False)
    _result, placement = insert_at_render_time(items, visual, 2.3, cfg, 30)
    assert placement.status == "inserted"
    assert abs(placement.placement_drift) <= 1 / 30 + 1e-9


def test_minimum_renderable_fragments_are_six_frames_or_point_two():
    assert minimum_fragment_seconds(30) == pytest.approx(.20)
    assert minimum_fragment_seconds(15) == pytest.approx(.4)


def test_short_source_clip_reports_constraint_instead_of_removing_guard():
    cfg = settings(.2)
    items = [media("tiny.mp4", .24), media("b.mp4", 2)]
    visual = media("visual.mp4", .5, smart=True, color_audio=False)
    _result, placement = insert_at_render_time(items, visual, .12, cfg, 30)
    assert placement.status in {"inserted", "skipped"}
    if placement.status == "inserted":
        assert placement.reason
        assert placement.placement_drift == pytest.approx(
            placement.final_expected_start - placement.requested_audio_boundary
        )


def test_split_fragments_preserve_source_offsets():
    item = media("source.mp4", 6)
    visual = media("visual.mp4", .5, smart=True, color_audio=False)
    result, placement = insert_at_render_time([item, media("tail.mp4", 2)], visual, 2, settings(0), 30)
    assert placement.reason.startswith("source_split")
    visual_index = result.index(visual)
    left, right = result[visual_index - 1], result[visual_index + 1]
    assert left.source_start == 0
    assert right.source_start == pytest.approx(left.duration * left.playback_rate)


def test_mixed_visual_types_share_one_geometry_model():
    cfg = settings(.25)
    working = [media("a.mp4", 5), media("b.mp4", 5)]
    image = media("still.png", .7, smart=True, image=True, color_audio=False)
    working, image_placement = insert_at_render_time(working, image, 1.2, cfg, 30)
    video = media("pool.mp4", .7, smart=True, color_audio=False)
    working, video_placement = insert_at_render_time(working, video, 3.2, cfg, 30)
    assert image_placement.visual_kind == "image"
    assert video_placement.visual_kind == "video"
    assert image_placement.status == video_placement.status == "inserted"


def test_final_timeline_drift_calculation_and_validation(tmp_path):
    visual_path = tmp_path / "visual.mp4"
    visual_path.touch()
    cfg = settings(.4)
    visual = replace(media(str(visual_path), .8, smart=True, color_audio=False), path=visual_path)
    result, placement = insert_at_render_time(
        [media("a.mp4", 4), media("b.mp4", 4)], visual, 2.25, cfg, 30
    )
    placement.placement_id = "sv-001"
    timeline = SmartVisualTimeline(
        enabled=True,
        placements=[placement],
        resolved_items=render_chain(result, cfg),
    )
    assert placement.placement_drift == pytest.approx(placement.final_expected_start - 2.25)
    assert validate_timeline(timeline, 30) == []
    first = timeline.finalize_identity()
    assert first == timeline.finalize_identity()


def test_source_offset_is_consumed_by_prebuilt_ffmpeg_graph():
    from app.video_merger.command_builder import FFmpegCommandBuilder

    fragment = replace(media("source.mp4", 1.5), source_duration=6.0, source_start=2.0)
    cfg = settings(0)
    resolved = resolve_export([fragment], cfg)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph([fragment], cfg, resolved)
    assert "trim=start=2:duration=1.5" in graph
    assert "atrim=start=2:duration=1.5" in graph


def test_zero_source_offset_keeps_historical_trim_expression():
    from app.video_merger.command_builder import FFmpegCommandBuilder

    item = media("source.mp4", 1.5)
    cfg = settings(0)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
        [item], cfg, resolve_export([item], cfg)
    )
    assert "trim=start=" not in graph
    assert "trim=duration=1.5" in graph


def test_disabled_workflow_does_not_invoke_phase35(monkeypatch):
    """The existing resolver has no dependency on the opt-in module."""
    items = [media("a.mp4", 2), media("b.mp4", 2)]
    baseline = resolve_export(items, settings(.3))
    # A normal resolve neither splits media nor writes Phase-35 source offsets.
    assert [item.source_start for item in items] == [0.0, 0.0]
    assert baseline.expected_duration == pytest.approx(
        sum(baseline.effective_durations) - sum(baseline.transitions)
    )
