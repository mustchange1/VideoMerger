from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.media_analyzer import MediaAnalyzer
from app.video_merger.models import ExportSettings
from app.video_merger.target import resolve_export
from tests.conftest import fake_media, make_clip


def test_alignment_and_transcription_cache_prevent_repeated_asr(tmp_path, monkeypatch):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"deterministic voice fixture")
    cache = tmp_path / "alignment-cache"
    calls = {"count": 0}

    def recognize(_path, _language):
        calls["count"] += 1
        return [
            RecognizedWord("Alpha", .10, .31, .96),
            RecognizedWord("bravo", .55, .81, .95),
            RecognizedWord("charlie", 1.20, 1.55, .94),
        ], "en"

    first = LocalWordAligner("cache-fixture", cache_dir=cache)
    monkeypatch.setattr(first, "_recognize", recognize)
    result1 = first.align("Alpha bravo charlie.", audio, "English")
    assert calls["count"] == 1 and not first.last_timings["cache_hit"]

    second = LocalWordAligner("cache-fixture", cache_dir=cache)
    monkeypatch.setattr(
        second, "_recognize",
        lambda *_args: (_ for _ in ()).throw(AssertionError("ASR must not rerun")),
    )
    result2 = second.align("Alpha bravo charlie.", audio, "English")
    assert second.last_timings["cache_hit"] is True
    assert second.last_timings["cache_level"] == "alignment"
    assert [(w.text, w.start, w.end) for w in result2.words] == [
        (w.text, w.start, w.end) for w in result1.words
    ]

    # Changing script punctuation/content remaps the cached transcript but still
    # does not reload the model or transcribe the same voiceover.
    third = LocalWordAligner("cache-fixture", cache_dir=cache)
    monkeypatch.setattr(
        third, "_recognize",
        lambda *_args: (_ for _ in ()).throw(AssertionError("transcription cache expected")),
    )
    third.align("Alpha bravo charlie!", audio, "English")
    assert third.last_timings["cache_hit"] is True
    assert third.last_timings["cache_level"] == "transcription"


@pytest.mark.e2e
def test_identical_media_analysis_uses_safe_stat_cache(ffmpeg_paths, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "clip.mp4"
    make_clip(ffmpeg, clip, duration=.4, audio_rate=None)
    cache = tmp_path / "media-cache.json"
    first = MediaAnalyzer(ffprobe, cache_path=cache)
    original = first.analyze(clip)
    assert first.last_cache_hit is False

    second = MediaAnalyzer(ffprobe, cache_path=cache)
    monkeypatch.setattr(
        second, "probe_raw",
        lambda _path: (_ for _ in ()).throw(AssertionError("FFprobe must not rerun")),
    )
    cached = second.analyze(clip)
    assert second.last_cache_hit is True
    assert cached.duration == pytest.approx(original.duration)

    # A changed size/mtime invalidates the cache safely.
    clip.write_bytes(clip.read_bytes() + b"cache invalidation")
    third = MediaAnalyzer(ffprobe, cache_path=cache)
    called = {"value": False}
    monkeypatch.setattr(third, "probe_raw", lambda _path: called.update(value=True) or first.probe_raw(clip))
    third.analyze(clip)
    assert called["value"] is True


def test_long_program_transition_blur_is_timeline_enabled_and_background_blur_is_reduced():
    media = [
        fake_media("portrait.mp4", width=1080, height=1920, duration=60),
        fake_media("portrait2.mp4", width=1080, height=1920, duration=60),
    ]
    settings = ExportSettings(
        aspect="16:9", resolution="1920x1080", transition_type="smooth_blur",
        transition_duration=.5, background_blur=30, workflow_stage="main",
    )
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, Path("final.mp4"))
    graph = built.filter_graph
    assert "gblur=sigma=15:steps=2,scale=w=1920:h=1080:flags=bicubic" in graph
    assert "gblur=sigma=12:steps=2:enable='between(t," in graph
    assert "blend=all_expr=" in graph and ":enable='between(t," in graph
    assert "-filter_complex_script" not in built.command
    assert built.command.count("-filter_complex") == 1
    assert built.command[-1] == "final.mp4"


@pytest.mark.e2e
def test_reduced_background_blur_path_renders_real_720p_output(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "portrait.mp4"
    make_clip(ffmpeg, clip, size="90x160", duration=.35, color="navy", audio_rate=None)
    from app.video_merger.engine import VideoMergerEngine
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    settings = ExportSettings(
        aspect="16:9", resolution="1280x720", encoding="CPU", preset="fast", crf=28,
        background_blur=30, normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    report = engine.export(media, settings, resolved, tmp_path / "blurred.mp4")
    assert report.ok and (report.width, report.height) == (1280, 720)
    assert "gblur=sigma=15:steps=2,scale=w=1280:h=720:flags=bicubic" in engine.last_filter_graph


def test_full_loop_repeats_inputs_in_order_without_stream_looping_single_video(tmp_path):
    media = [fake_media(str(tmp_path / name), duration=.8) for name in ("C.mp4", "A.mp4", "D.mp4", "B.mp4")]
    from app.video_merger.timeline import fit_media_to_duration
    repeated, _ = fit_media_to_duration(media, 5.0, .2, 30, "loop")
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", short_video_mode="loop",
        timeline_target_duration=5.0, transition_duration=.2, normalize_audio=False,
    )
    resolved = resolve_export(repeated, settings)
    command = FFmpegCommandBuilder("ffmpeg").build(repeated, settings, resolved, tmp_path / "out.mp4").command
    video_inputs = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-i"]
    assert [Path(value).name for value in video_inputs] == ["C.mp4", "A.mp4", "D.mp4", "B.mp4"] * 2
    assert "-stream_loop" not in command
