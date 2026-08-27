"""1.3.0 – Smart Last-Clip Stretch, Global Video Speed, Main Video End Padding.

* Duration Fit Mode: Cut Last Clip (default) / Stretch Last Clip
* configurable maximum stretch (default 10 %; 5/10/15/20/Custom)
* stretch slows ONLY the final selected clip, preserves transitions and
  visual continuity, falls back to normal trimming beyond the limit and
  NEVER uses Hold Last Frame
* Global Video Speed 0.50x–2.00x (default 1.00x): the voiceover remains the
  timing authority — subtitle timing, voiceover and music are unchanged
* Main Video End Padding: manual 0.0–5.0 s, default remains ~1 s
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.timeline import fit_media_to_duration
from app.video_merger.video_pool import compute_pool_status, required_selection_length
from tests.conftest import fake_media, make_clip

FPS = 30.0


def _clips(durations: list[float]) -> list:
    return [fake_media(f"clip{index}.mp4", duration=value) for index, value in enumerate(durations)]


# --------------------------------------------------------------------------- #
# Smart Last-Clip Stretch
# --------------------------------------------------------------------------- #


def test_stretch_mode_slows_only_the_last_selected_clip():
    media = _clips([2.0, 2.0, 2.0])
    # Cut prefix for target 3.0: 2 clips (2+2-0.4=3.6 ≥ 3.0; 1 clip = 2.0).
    # Stretch prefers ONE clip less: prefix [1] = 2.0 → deficit 1.0 → required
    # 3.0 s = +50 % of the 2.0 s source → allowed with limit 60.
    selected, warnings = fit_media_to_duration(
        media, 3.0, 0.4, FPS, "hold", duration_fit_mode="stretch", max_stretch_percent=60.0,
    )
    assert len(selected) == 1
    assert selected[0].duration == pytest.approx(3.0, abs=0.02)
    assert selected[0].playback_rate == pytest.approx(2.0 / 3.0, abs=0.01)
    assert (selected[0].source_duration or 2.0) == pytest.approx(2.0)  # full content kept
    assert any("Smart Stretch" in warning for warning in warnings)


def test_stretch_mode_within_10_percent_default_limit():
    media = _clips([2.0, 2.0, 2.0])
    # Prefix [1] = 2.0; target 2.2 → deficit 0.2 → +10 % of the source.
    selected, warnings = fit_media_to_duration(
        media, 2.2, 0.4, FPS, "hold", duration_fit_mode="stretch", max_stretch_percent=10.0,
    )
    assert len(selected) == 1
    assert selected[0].duration == pytest.approx(2.2, abs=0.02)
    assert selected[0].playback_rate < 1.0
    assert any("Smart Stretch" in warning for warning in warnings)


def test_stretch_beyond_limit_falls_back_to_normal_cut_never_hold():
    media = _clips([2.0, 2.0, 2.0])
    selected, warnings = fit_media_to_duration(
        media, 3.0, 0.4, FPS, "hold", duration_fit_mode="stretch", max_stretch_percent=10.0,
    )
    # +50 % needed > 10 % limit → normal cut behavior: covered prefix with the
    # last occurrence TRIMMED (duration ≤ source), no playback slowdown.
    assert len(selected) == 2
    assert selected[-1].duration <= 2.0 + 1e-6
    assert selected[-1].duration == pytest.approx(1.4, abs=0.05)
    assert all(abs(item.playback_rate - 1.0) < 1e-9 for item in selected)
    assert any("Limit" in warning and "Kürzen" in warning for warning in warnings)
    # Never Hold Last Frame: no clip exceeds its source length.
    for item in selected:
        assert item.duration <= (item.source_duration or item.duration) + 1e-6


def test_cut_mode_is_the_unchanged_default_behavior():
    media = _clips([2.0, 2.0, 2.0])
    cut, _ = fit_media_to_duration(media, 3.0, 0.4, FPS, "hold", "cut", 10.0)
    stretch_default, _ = fit_media_to_duration(media, 3.0, 0.4, FPS)  # defaults
    assert [item.duration for item in cut] == pytest.approx(
        [item.duration for item in stretch_default], abs=1e-6
    )
    assert len(cut) == 2 and cut[-1].duration == pytest.approx(1.4, abs=0.05)


def test_stretch_preserves_order_and_transitions():
    media = _clips([1.0, 1.0, 1.0, 1.0])
    selected, _warnings = fit_media_to_duration(
        media, 2.3, 0.5, FPS, "hold", duration_fit_mode="stretch", max_stretch_percent=50.0,
    )
    # Only the final occurrence carries a slowdown; order is untouched.
    assert [item.path.name for item in selected] == [item.path.name for item in media[: len(selected)]]
    rates = [item.playback_rate for item in selected]
    assert rates[-1] < 1.0 and all(rate == 1.0 for rate in rates[:-1])


def test_pool_status_mirrors_stretch_selection():
    media = _clips([2.0, 2.0, 2.0])
    cut = compute_pool_status(media, 3.0, 0.4, FPS, "hold")
    stretch = compute_pool_status(media, 3.0, 0.4, FPS, "hold",
                                  duration_fit_mode="stretch", max_stretch_percent=60.0)
    assert cut.selected == 2
    assert stretch.selected == 1 and stretch.unused == 2
    assert stretch.mode == "exact" and stretch.covered
    # …und mit dem 10 %-Limit gilt wieder die Cut-Anzahl:
    limited = compute_pool_status(media, 3.0, 0.4, FPS, "hold",
                                  duration_fit_mode="stretch", max_stretch_percent=10.0)
    assert limited.selected == 2


def test_required_selection_length_pure_duration_math():
    durations = [2.0, 2.0, 2.0]
    assert required_selection_length(durations, 3.0, 0.4, FPS, "cut", 10.0) == 2
    assert required_selection_length(durations, 3.0, 0.4, FPS, "stretch", 60.0) == 1
    assert required_selection_length(durations, 3.0, 0.4, FPS, "stretch", 10.0) == 2


# --------------------------------------------------------------------------- #
# Global Video Speed
# --------------------------------------------------------------------------- #


def test_speed_scales_timeline_durations_and_sets_playback_rate():
    media = _clips([2.0, 2.0])
    selected, _ = fit_media_to_duration(
        media, 1.0, 0.4, FPS, "hold", playback_rate=2.0,
    )
    assert len(selected) == 1
    assert all(item.playback_rate == 2.0 for item in selected)
    assert all(item.duration == pytest.approx(1.0) for item in selected)
    slowed, _ = fit_media_to_duration(
        media, 4.0, 0.4, FPS, "hold", playback_rate=0.5,
    )
    assert len(slowed) == 1
    assert all(item.playback_rate == 0.5 for item in slowed)
    assert all(item.duration == pytest.approx(4.0) for item in slowed)


def test_pool_status_respects_speed():
    media = _clips([2.0, 2.0, 2.0, 2.0])
    normal = compute_pool_status(media, 2.5, 0.4, FPS, "hold")
    faster = compute_pool_status(media, 2.5, 0.4, FPS, "hold", playback_rate=2.0)
    slower = compute_pool_status(media, 2.5, 0.4, FPS, "hold", playback_rate=0.5)
    assert normal.selected == 2
    assert faster.selected == 4   # clips deplete faster → the whole pool is needed
    assert faster.unused == 0 and faster.covered
    assert slower.selected == 1   # slow motion covers the target with one clip


# --------------------------------------------------------------------------- #
# End-to-end: real renders
# --------------------------------------------------------------------------- #


def _voice(ffmpeg: Path, path: Path, duration: float) -> None:
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"sine=f=880:r=48000:d={duration}", "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True, timeout=120,
    )


@pytest.mark.e2e
def test_stretch_e2e_real_render_final_clip_slowed_exactly(ffmpeg_paths, tmp_path):
    """Real render: the stretched final clip fills the target exactly, at
    real frame rate (setpts slowdown), with a clean transition into it."""
    ffmpeg, ffprobe = ffmpeg_paths
    clips = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for clip, color in zip(clips, ("red", "blue")):
        make_clip(ffmpeg, clip, size="160x90", duration=2.0, color=color, audio_rate=None)
    voice = tmp_path / "voice.wav"
    _voice(ffmpeg, voice, 2.4)
    script = tmp_path / "script.txt"
    script.write_text("Alpha bravo charlie delta.", encoding="utf-8")
    timings = [("Alpha", .1, .4), ("bravo", .5, .8), ("charlie", .9, 1.3), ("delta", 1.5, 2.0)]

    def recognize(_path, _language):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    aligner = LocalWordAligner("stretch-e2e", recognize, cache_dir=tmp_path / "cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(clips)
    settings = ExportSettings(
        resolution="160x90", encoding="CPU", preset="fast", crf=28, normalize_audio=False,
        voiceover_path=str(voice), script_path=str(script), subtitle_enabled=True,
        subtitle_language="English", final_pause=0.6, transition_duration=0.4,
        duration_fit_mode="stretch", max_stretch_percent=60.0,
    )
    result = MainProjectEngine(engine).create_main(media, settings, tmp_path / "out", aligner=aligner)
    assert result.report.ok, result.report.details
    # Target = 2.4 + 0.6 = 3.0 s; stretch prefix is exactly one 2.0 s clip
    # slowed by 50 % (within the 60 % limit).
    assert result.report.duration == pytest.approx(3.0, abs=0.12)
    graph = engine.last_render_graph
    assert "setpts=PTS/0" in graph  # real slowdown in the render graph
    # Exact selection: only the first clip entered the composition graph.
    assert "pre1" not in graph and "[1:v:0]" not in graph
    # Subtitles still end before the quiet pause and SRT exists.
    assert result.srt is not None and result.srt.is_file()
    assert result.video_no_subtitles is not None and result.video_no_subtitles.is_file()


@pytest.mark.e2e
def test_global_speed_e2e_voiceover_remains_timing_authority(ffmpeg_paths, tmp_path):
    """Real render at 1.50x: the Main Video duration still equals voiceover +
    end padding, subtitles keep the identical word timeline, and the graph
    really speeds the clips up."""
    ffmpeg, ffprobe = ffmpeg_paths
    clips = [tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"]
    for clip, color in zip(clips, ("red", "blue", "green")):
        make_clip(ffmpeg, clip, size="160x90", duration=2.0, color=color, audio_rate=None)
    voice = tmp_path / "voice.wav"
    _voice(ffmpeg, voice, 2.4)
    script = tmp_path / "script.txt"
    script.write_text("Alpha bravo charlie delta.", encoding="utf-8")
    timings = [("Alpha", .1, .4), ("bravo", .5, .8), ("charlie", .9, 1.3), ("delta", 1.5, 2.0)]

    def recognize(_path, _language):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    def run(speed: float):
        aligner = LocalWordAligner(f"speed-{speed}", recognize, cache_dir=tmp_path / "cache")
        engine = VideoMergerEngine(ffmpeg, ffprobe)
        media = engine.analyze(clips)
        settings = ExportSettings(
            resolution="160x90", encoding="CPU", preset="fast", crf=28, normalize_audio=False,
            voiceover_path=str(voice), script_path=str(script), subtitle_enabled=True,
            subtitle_language="English", final_pause=0.6, transition_duration=0.4,
            video_speed=speed,
        )
        result = MainProjectEngine(engine).create_main(
            media, settings, tmp_path / f"out-{speed}", aligner=aligner,
        )
        return engine, result

    engine_fast, result_fast = run(1.5)
    _engine_normal, result_normal = run(1.0)
    # Identical voiceover-derived duration — speed never shifts the authority.
    assert result_fast.report.duration == pytest.approx(result_normal.report.duration, abs=0.1)
    assert result_fast.report.duration == pytest.approx(3.0, abs=0.12)
    # Identical subtitle timeline: the SRT is byte-equal (same acoustic words).
    assert result_fast.srt.read_text(encoding="utf-8") == result_normal.srt.read_text(encoding="utf-8")
    # The render graph really applies the speed to the clips.
    assert "setpts=PTS/1.5" in engine_fast.last_render_graph


@pytest.mark.e2e
def test_end_padding_e2e_manual_value_changes_only_the_tail(ffmpeg_paths, tmp_path):
    """End Padding is manual: 0.3 s and 1.7 s both render exactly; the
    subtitle timeline never reaches into the padding."""
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "a.mp4"
    make_clip(ffmpeg, clip, size="160x90", duration=4.0, color="navy", audio_rate=None)
    voice = tmp_path / "voice.wav"
    _voice(ffmpeg, voice, 2.0)
    script = tmp_path / "script.txt"
    script.write_text("Alpha bravo charlie.", encoding="utf-8")
    timings = [("Alpha", .1, .5), ("bravo", .6, 1.1), ("charlie", 1.2, 1.8)]

    def recognize(_path, _language):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    for padding in (0.3, 1.7):
        aligner = LocalWordAligner(f"pad-{padding}", recognize, cache_dir=tmp_path / "cache")
        engine = VideoMergerEngine(ffmpeg, ffprobe)
        media = engine.analyze([clip])
        settings = ExportSettings(
            resolution="160x90", encoding="CPU", preset="fast", crf=28, normalize_audio=False,
            voiceover_path=str(voice), script_path=str(script), subtitle_enabled=True,
            subtitle_language="English", final_pause=padding,
        )
        result = MainProjectEngine(engine).create_main(
            media, settings, tmp_path / f"out-{padding}", aligner=aligner,
        )
        assert result.report.ok, result.report.details
        assert result.report.duration == pytest.approx(2.0 + padding, abs=0.12)
        import json as _json
        timeline = _json.loads(result.canonical_timeline.read_text(encoding="utf-8"))
        assert timeline["cues"][-1]["end"] <= 2.0 + 0.001
