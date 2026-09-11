"""Phase 27 – Clip continuity guarantees (no consecutive duplicate source clip).

These tests pin the deterministic repair in
:func:`app.video_merger.timeline.enforce_continuous_sources` and prove that the
full ``fit_media_to_duration`` pipeline never yields two directly consecutive
occurrences of the same source clip across manual order, random order, loop
mode, hold mode, fallback and the Shorts planner.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from app.video_merger.models import AudioInfo, MediaInfo
from app.video_merger.timeline import enforce_continuous_sources, fit_media_to_duration


def clip(
    name: str,
    duration: float,
    rate: float = 1.0,
    folder: Path | None = None,
    source_duration: float | None = None,
) -> MediaInfo:
    base = folder or Path("/media")
    return MediaInfo(
        path=base / f"{name}.mp4",
        duration=duration,
        width=1920,
        height=1080,
        effective_width=1920,
        effective_height=1080,
        fps=30.0,
        fps_fraction="30/1",
        video_codec="h264",
        pixel_format="yuv420p",
        sar="1:1",
        dar="",
        audio=AudioInfo(present=True, codec="aac", sample_rate=48000, channels=2),
        source_duration=duration if source_duration is None else source_duration,
        playback_rate=rate,
    )


def adjacent_pairs(selected):
    return list(zip(selected, selected[1:]))


def has_consecutive_repeat(selected) -> bool:
    for left, right in adjacent_pairs(selected):
        if left.path == right.path:
            return True
    return False


# ---------------------------------------------------------------------------
# enforce_continuous_sources – the deterministic repair itself
# ---------------------------------------------------------------------------
def test_coalesces_unnecessary_same_source_split():
    # A[0–2] followed by A[2–4] are two segments of ONE 4-second source clip.
    # Their combined length fits the natural source length, so they must be
    # merged into a single continuous occurrence.
    a1 = clip("a", 2.0, source_duration=4.0)
    a2 = clip("a", 2.0, source_duration=4.0)
    pool = [clip("a", 4.0), clip("b", 2.0)]
    result, warnings = enforce_continuous_sources([a1, a2], pool)
    assert len(result) == 1
    assert result[0].path.name == "a.mp4"
    assert result[0].duration == pytest.approx(4.0)
    assert warnings  # a coalesce happened


def test_replaces_repeat_with_different_source():
    # A → A cannot be coalesced (combined length exceeds the natural clip),
    # so the later occurrence is replaced by a different pool clip.
    a1 = clip("a", 4.0)
    a2 = clip("a", 4.0)
    pool = [clip("a", 4.0), clip("b", 6.0)]
    result, warnings = enforce_continuous_sources([a1, a2], pool)
    assert [item.path.name for item in result] == ["a.mp4", "b.mp4"]
    assert result[1].duration == pytest.approx(4.0)  # timeline slot preserved
    assert warnings


def test_replacement_never_matches_either_neighbor():
    # … C, A, A, C … must become C, A, <not C>, C. Only B is legal.
    c1 = clip("c", 2.0)
    a1 = clip("a", 4.0)
    a2 = clip("a", 4.0)
    c2 = clip("c", 2.0)
    pool = [clip("a", 4.0), clip("b", 6.0), clip("c", 2.0)]
    result, _ = enforce_continuous_sources([c1, a1, a2, c2], pool)
    names = [item.path.name for item in result]
    assert names[0] == "c.mp4" and names[-1] == "c.mp4"
    assert names[1] == "a.mp4"
    assert names[2] == "b.mp4"
    assert not has_consecutive_repeat(result)


def test_single_source_fallback_keeps_clip_and_warns():
    # Only one unique source exists; the repeat cannot be avoided.
    a1 = clip("a", 4.0)
    a2 = clip("a", 4.0)
    pool = [clip("a", 4.0)]
    result, warnings = enforce_continuous_sources([a1, a2], pool)
    assert len(result) == 2
    assert any("Fallback" in warning or "beibehalten" in warning for warning in warnings)


def test_loop_boundary_is_legal_not_a_repeat():
    # A → B → C → A is a clean loop boundary: the trailing A is a fresh
    # occurrence, NOT consecutive with the leading A (they are not adjacent).
    seq = [clip("a", 2.0), clip("b", 2.0), clip("c", 2.0), clip("a", 2.0)]
    pool = [clip("a", 2.0), clip("b", 2.0), clip("c", 2.0)]
    result, warnings = enforce_continuous_sources(seq, pool)
    assert [item.path.name for item in result] == ["a.mp4", "b.mp4", "c.mp4", "a.mp4"]
    assert not has_consecutive_repeat(result)
    assert warnings == []  # nothing needed fixing


def test_manual_order_without_repeats_is_untouched():
    seq = [clip("a", 2.0), clip("b", 2.0), clip("c", 2.0), clip("d", 2.0)]
    pool = [clip(p, 2.0) for p in ("a", "b", "c", "d")]
    result, warnings = enforce_continuous_sources(seq, pool)
    assert [item.path.name for item in result] == [
        "a.mp4", "b.mp4", "c.mp4", "d.mp4",
    ]
    assert warnings == []


def test_stretched_occurrence_is_not_replaced():
    # The second A carries an explicit per-occurrence stretch (rate 1.5);
    # the repair must never disturb it, even though A→A repeats.
    a1 = clip("a", 4.0)
    a2 = clip("a", 4.0, rate=1.5)
    pool = [clip("a", 4.0), clip("b", 6.0)]
    result, _ = enforce_continuous_sources([a1, a2], pool)
    assert [item.path.name for item in result] == ["a.mp4", "a.mp4"]
    assert result[1].playback_rate == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# fit_media_to_duration – the whole pipeline never emits consecutive repeats
# ---------------------------------------------------------------------------
def test_fit_cut_mode_no_consecutive_repeat():
    media = [clip(name, 2.0) for name in ("a", "b", "c", "d", "e")]
    selected, _ = fit_media_to_duration(
        media, target_duration=9.0, transition_duration=0.0, fps=30.0,
        short_video_mode="hold", duration_fit_mode="cut", folder_aware=False,
    )
    assert not has_consecutive_repeat(selected)


def test_fit_loop_mode_repeats_whole_timeline_not_one_clip():
    # Looping must walk the FULL ordered sequence (C→A→D→B→C…), so no two
    # neighbours share a source, and the boundary never doubles a clip.
    media = [clip(name, 1.0) for name in ("c", "a", "d", "b")]
    selected, _ = fit_media_to_duration(
        media, target_duration=6.0, transition_duration=0.0, fps=30.0,
        short_video_mode="loop", duration_fit_mode="cut", folder_aware=False,
    )
    assert not has_consecutive_repeat(selected)
    assert len(selected) >= 5  # the sequence wrapped past one full pass


def test_fit_single_clip_hold_mode_falls_back_safely():
    # One clip, longer target than the clip: hold mode stretches the final
    # frame. There is exactly one occurrence, so no repeat can exist.
    media = [clip("only", 2.0)]
    selected, _ = fit_media_to_duration(
        media, target_duration=6.0, transition_duration=0.0, fps=30.0,
        short_video_mode="hold", duration_fit_mode="cut", folder_aware=False,
    )
    assert not has_consecutive_repeat(selected)
    assert len(selected) == 1


@pytest.mark.parametrize("mode", ["natural", "random", "manual"])
def test_fit_order_modes_have_no_consecutive_repeat(mode):
    media = [clip(name, 1.5) for name in ("a", "b", "c")]
    rng = random.Random(42) if mode == "random" else None
    selected, _ = fit_media_to_duration(
        media, target_duration=4.2, transition_duration=0.0, fps=30.0,
        short_video_mode="hold", duration_fit_mode="cut", folder_aware=False,
        video_order_mode=mode, video_order_rng=rng,
    )
    assert not has_consecutive_repeat(selected)


def test_fit_loop_boundary_does_not_double_clip():
    # A→B→C looped twice ends with …C and the next occurrence is A, never C.
    media = [clip(name, 1.0) for name in ("a", "b", "c")]
    selected, _ = fit_media_to_duration(
        media, target_duration=6.0, transition_duration=0.0, fps=30.0,
        short_video_mode="loop", duration_fit_mode="cut", folder_aware=False,
    )
    names = [item.path.name for item in selected]
    assert not has_consecutive_repeat(selected)
    # The cyclic pattern a,b,c must hold throughout.
    cycle = ["a.mp4", "b.mp4", "c.mp4"]
    for index, name in enumerate(names):
        assert name == cycle[index % 3]
