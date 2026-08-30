"""Phase 1 – Stage-1 fingerprinting and One-Click Main Video reuse."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import AudioAssetInfo, ExportSettings, ResolvedExport, ValidationReport
from app.video_merger.render_cache import Stage1RenderCache, stage1_fingerprint
from tests.conftest import fake_media, make_clip


def _resolved(count: int = 2) -> ResolvedExport:
    return ResolvedExport(
        width=320,
        height=180,
        fps=30.0,
        fps_expr="30",
        effective_durations=[2.0] * count,
        transitions=[0.2] * max(0, count - 1),
        expected_duration=count * 2.0 - max(0, count - 1) * 0.2,
        encoder="libx264",
        encoder_label="CPU (libx264)",
        crf=24,
        preset="fast",
        quality_label="Custom",
    )


def _fingerprint_inputs(tmp_path: Path):
    clip_a = tmp_path / "A.mp4"
    clip_b = tmp_path / "B.mp4"
    voice = tmp_path / "voice.wav"
    script = tmp_path / "script.txt"
    music = tmp_path / "music.mp3"
    watermark = tmp_path / "watermark.png"
    for path, content in (
        (clip_a, b"clip-a"), (clip_b, b"clip-b"), (voice, b"voice-a"),
        (script, b"The authoritative script."), (music, b"music-a"),
        (watermark, b"watermark-a"),
    ):
        path.write_bytes(content)
    media = [fake_media(str(clip_a), duration=2.0), fake_media(str(clip_b), duration=2.0)]
    voice_asset = AudioAssetInfo(voice, 1.5, 48000, 2, "pcm_s16le")
    music_asset = AudioAssetInfo(music, 3.0, 48000, 2, "mp3")
    settings = ExportSettings(
        resolution="320x180",
        encoding="CPU",
        quality_preset="custom",
        crf=24,
        preset="fast",
        transition_type="cross_dissolve",
        transition_duration=0.2,
        voiceover_paths=[str(voice)],
        script_paths=[str(script)],
        subtitle_enabled=True,
        subtitle_language="English",
        subtitle_style="long_1",
        subtitle_animation="static_phrase",
        subtitle_font="modern_sans_bold",
        subtitle_position="Bottom",
        music_path=str(music),
        watermark_enabled=True,
        watermark_path=str(watermark),
        music_volume=30,
        video_speed=1.0,
    )
    return media, settings, voice_asset, music_asset, script, watermark


def test_stage1_fingerprint_is_deterministic_and_excludes_stage2(tmp_path):
    media, settings, voice, music, script, watermark = _fingerprint_inputs(tmp_path)
    kwargs = dict(
        voice_assets=[voice],
        script_files=[script],
        subtitle_requested=True,
        music_asset=music,
        watermark_path=watermark,
    )
    first, payload = stage1_fingerprint(media, settings, _resolved(), **kwargs)
    second, _ = stage1_fingerprint(media, settings, _resolved(), **kwargs)
    assert first == second
    assert "intro_path" not in payload["settings"]
    assert "quote_text" not in payload["settings"]
    assert "outro_path" not in payload["settings"]

    stage2_changed = replace(
        settings,
        intro_path=str(tmp_path / "intro.mp4"),
        outro_path=str(tmp_path / "outro.mp4"),
        quote_enabled=True,
        quote_text="A Stage-2-only quote",
        quote_duration=4.0,
        quote_style="warm_cinematic",
        quote_input_mode="artwork",
        quote_artwork_path=str(tmp_path / "flyer.pdf"),
        quote_pdf_page=3,
        quote_artwork_fit_mode="crop",
        quote_transition_duration=1.0,
    )
    stage2_digest, _ = stage1_fingerprint(media, stage2_changed, _resolved(), **kwargs)
    assert stage2_digest == first


def test_stage1_fingerprint_changes_for_sequence_selection_and_stage1_settings(tmp_path):
    media, settings, voice, music, script, watermark = _fingerprint_inputs(tmp_path)
    kwargs = dict(
        voice_assets=[voice], script_files=[script], subtitle_requested=True,
        music_asset=music, watermark_path=watermark,
    )
    baseline, _ = stage1_fingerprint(media, settings, _resolved(), **kwargs)
    reordered, _ = stage1_fingerprint(media[::-1], settings, _resolved(), **kwargs)
    selected, _ = stage1_fingerprint(media[:1], settings, _resolved(1), **kwargs)
    assert reordered != baseline
    assert selected != baseline

    for changed in (
        replace(settings, video_speed=0.7),
        replace(settings, transition_duration=0.8),
        replace(settings, subtitle_animation="type_reveal"),
        replace(settings, duration_fit_mode="stretch"),
        replace(settings, max_stretch_percent=20.0),
        replace(settings, final_pause=2.0),
        replace(settings, resolution="640x360"),
    ):
        digest, _ = stage1_fingerprint(media, changed, _resolved(), **kwargs)
        assert digest != baseline


def test_stage1_fingerprint_changes_for_script_voiceover_and_media_identity(tmp_path):
    media, settings, voice, music, script, watermark = _fingerprint_inputs(tmp_path)
    kwargs = dict(
        voice_assets=[voice], script_files=[script], subtitle_requested=True,
        music_asset=music, watermark_path=watermark,
    )
    baseline, _ = stage1_fingerprint(media, settings, _resolved(), **kwargs)

    script.write_text("The changed authoritative script.", encoding="utf-8")
    changed_script, _ = stage1_fingerprint(media, settings, _resolved(), **kwargs)
    assert changed_script != baseline

    voice.path.write_bytes(b"voice-b")
    changed_voice, _ = stage1_fingerprint(media, settings, _resolved(), **kwargs)
    assert changed_voice != changed_script

    media[0].path.write_bytes(b"clip-a-changed")
    changed_clip, _ = stage1_fingerprint(media, settings, _resolved(), **kwargs)
    assert changed_clip != changed_voice


def _custom_settings(**overrides) -> ExportSettings:
    values = dict(
        resolution="160x90",
        aspect="16:9",
        encoding="CPU",
        quality_preset="custom",
        crf=32,
        preset="ultrafast",
        normalize_audio=False,
        transition_type="cross_dissolve",
        transition_duration=0.1,
    )
    values.update(overrides)
    return ExportSettings(**values)


def test_cache_hit_skips_stage1_and_explicit_main_stays_fresh(tmp_path, monkeypatch):
    from app.video_merger import main_project

    output = tmp_path / "Output"
    media = [fake_media(str(tmp_path / "clip.mp4"), width=160, height=90, duration=1.0)]
    resolved = ResolvedExport(
        width=160, height=90, fps=30.0, fps_expr="30",
        effective_durations=[1.0], transitions=[], expected_duration=1.0,
        encoder="libx264", encoder_label="CPU (libx264)", crf=32,
        preset="ultrafast", quality_label="Custom",
    )

    class StubEngine:
        ffprobe_path = tmp_path / "ffprobe"

        def __init__(self):
            self.export_count = 0

        def make_plan(self, *_args, **_kwargs):
            return resolved

        def export(self, _media, _settings, _resolved, output_path, **_kwargs):
            self.export_count += 1
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"valid-main-video")
            return ValidationReport(
                True, [], output_path, duration=1.0, width=160, height=90,
                fps=30.0, has_video=True,
            )

    def validate(path, _ffprobe, _expected):
        valid = Path(path).is_file() and Path(path).read_bytes() == b"valid-main-video"
        return ValidationReport(
            valid, [] if valid else ["invalid"], Path(path), duration=1.0,
            width=160, height=90, fps=30.0, has_video=valid,
        )

    monkeypatch.setattr(main_project, "validate_output", validate)
    engine = StubEngine()
    project = MainProjectEngine(engine, Stage1RenderCache(tmp_path / "render-cache"))
    settings = _custom_settings()

    first = project.create_main(media, settings, output)
    second = project.create_main(media, settings, output, reuse_cached=True)
    assert first.timings["render_reused"] is False
    assert second.timings["render_reused"] is True
    assert engine.export_count == 1

    # Missing and corrupt cached artifacts are both fail-closed and rendered again.
    first.video.unlink()
    missing = project.create_main(media, settings, output, reuse_cached=True)
    assert missing.timings["render_reused"] is False
    assert engine.export_count == 2

    missing.video.write_bytes(b"corrupt")
    corrupt = project.create_main(media, settings, output, reuse_cached=True)
    assert corrupt.timings["render_reused"] is False
    assert engine.export_count == 3

    # Explicit Main Video creation never consults the cache.
    explicit = project.create_main(media, settings, output)
    assert explicit.timings["render_reused"] is False
    assert engine.export_count == 4


def test_cache_sidecar_snapshots_restore_missing_subtitle_artifacts(tmp_path):
    cache = Stage1RenderCache(tmp_path / "render-cache")
    video = tmp_path / "Output" / "MainVideo.mp4"
    clean = tmp_path / "Output" / "MainVideo_no_subtitles.mp4"
    srt = tmp_path / "Output" / "MainVideo.srt"
    vtt = tmp_path / "Output" / "MainVideo.vtt"
    timeline = tmp_path / "temp" / "MainVideo.json"
    for path, content in (
        (video, b"video"), (clean, b"clean"), (srt, b"srt"),
        (vtt, b"vtt"), (timeline, b"timeline"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    fingerprint, payload = stage1_fingerprint([], ExportSettings(), _resolved(0))
    cache.save(
        fingerprint,
        payload,
        video=video,
        video_no_subtitles=clean,
        srt=srt,
        vtt=vtt,
        canonical_timeline=timeline,
        subtitle_requested=True,
    )
    srt.unlink()
    vtt.unlink()
    timeline.unlink()
    record = cache.load(fingerprint)
    assert record is not None
    cache.restore_sidecars(record)
    assert srt.read_bytes() == b"srt"
    assert vtt.read_bytes() == b"vtt"
    assert timeline.read_bytes() == b"timeline"
    manifest_path = cache.root / fingerprint / "manifest.json"
    assert manifest_path.is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["payload"]["settings"]["aspect"] = "9:16"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert cache.load(fingerprint) is None


@pytest.mark.e2e
def test_real_one_click_reuses_stage1_for_unchanged_and_stage2_only_changes(
    ffmpeg_paths, tmp_path
):
    """First call renders; unchanged and Stage-2-only calls reuse Stage 1;
    a Stage-1 setting change renders again; explicit Create Main remains fresh."""
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "A.mp4"
    outro = tmp_path / "outro.mp4"
    outro_changed = tmp_path / "outro_changed.mp4"
    make_clip(ffmpeg, clip, size="160x90", duration=1.0, color="red", audio_rate=None)
    make_clip(ffmpeg, outro, size="160x90", duration=0.7, color="blue", audio_rate=None)
    make_clip(ffmpeg, outro_changed, size="160x90", duration=0.8, color="green", audio_rate=None)

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    cache = Stage1RenderCache(tmp_path / "render-cache")
    project = MainProjectEngine(engine, render_cache=cache)
    settings = _custom_settings(outro_path=str(outro))
    output = tmp_path / "Output"

    first = project.create_complete(media, settings, output)
    assert first.main.timings["render_reused"] is False
    assert first.main.timings["cache_hit"] is False
    assert first.main.video.is_file()

    second = project.create_complete(media, settings, output)
    assert second.main.timings["render_reused"] is True
    assert second.main.timings["cache_hit"] is True
    assert second.main.video == first.main.video

    stage2_only = replace(settings, outro_path=str(outro_changed))
    third = project.create_complete(media, stage2_only, output)
    assert third.main.timings["render_reused"] is True
    assert third.main.video == first.main.video

    stage1_changed = replace(settings, transition_duration=0.2)
    fourth = project.create_complete(media, stage1_changed, output)
    assert fourth.main.timings["render_reused"] is False
    assert fourth.main.video != first.main.video

    explicit = project.create_main(media, stage1_changed, output)
    assert explicit.timings["render_reused"] is False
    assert explicit.timings["cache_hit"] is False


@pytest.mark.e2e
def test_missing_and_corrupt_cached_main_fall_back_to_fresh_render(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "A.mp4"
    outro = tmp_path / "outro.mp4"
    make_clip(ffmpeg, clip, size="160x90", duration=0.9, color="red", audio_rate=None)
    make_clip(ffmpeg, outro, size="160x90", duration=0.6, color="blue", audio_rate=None)
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    project = MainProjectEngine(engine, render_cache=Stage1RenderCache(tmp_path / "cache"))
    settings = _custom_settings(outro_path=str(outro))
    output = tmp_path / "Output"

    first = project.create_complete(media, settings, output)
    first.main.video.unlink()
    missing = project.create_complete(media, settings, output)
    assert missing.main.timings["render_reused"] is False

    missing.main.video.write_bytes(b"not an mp4")
    corrupt = project.create_complete(media, settings, output)
    assert corrupt.main.timings["render_reused"] is False
    assert corrupt.main.report.ok


@pytest.mark.e2e
def test_reuse_restores_subtitle_sidecars_without_repeating_alignment_or_metadata_work(
    ffmpeg_paths, tmp_path
):
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "A.mp4"
    outro = tmp_path / "outro.mp4"
    voice = tmp_path / "voice.wav"
    script = tmp_path / "script.txt"
    make_clip(ffmpeg, clip, size="160x90", duration=1.2, color="red", audio_rate=None)
    make_clip(ffmpeg, outro, size="160x90", duration=0.6, color="blue", audio_rate=None)
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=f=880:r=48000:d=0.8", "-c:a", "pcm_s16le", str(voice)],
        check=True, capture_output=True, timeout=120,
    )
    script.write_text("Cached subtitles remain available.", encoding="utf-8")
    words = [
        ("Cached", 0.05, 0.18), ("subtitles", 0.20, 0.36),
        ("remain", 0.38, 0.53), ("available.", 0.55, 0.72),
    ]

    def recognize(_path, _language):
        return [RecognizedWord(word, start, end, 0.99) for word, start, end in words], "en"

    first_aligner = LocalWordAligner("phase1", recognize, cache_dir=tmp_path / "alignment")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    project = MainProjectEngine(engine, render_cache=Stage1RenderCache(tmp_path / "render-cache"))
    settings = _custom_settings(
        outro_path=str(outro),
        voiceover_path=str(voice),
        voiceover_paths=[str(voice)],
        script_path=str(script),
        script_paths=[str(script)],
        subtitle_enabled=True,
        subtitle_language="English",
        subtitle_style="long_1",
        subtitle_font="modern_sans_bold",
        subtitle_position="Bottom",
        final_pause=0.3,
    )
    output = tmp_path / "Output"
    first = project.create_complete(media, settings, output, aligner=first_aligner)
    assert first.main.timings["render_reused"] is False
    assert first.main.srt is not None and first.main.vtt is not None
    assert first.main.canonical_timeline is not None
    for path in (first.main.srt, first.main.vtt, first.main.canonical_timeline):
        path.unlink()

    def should_not_align(*_args, **_kwargs):
        raise AssertionError("ASR/alignment must not run on a valid Stage-1 cache hit")

    second_aligner = LocalWordAligner("phase1", should_not_align, cache_dir=tmp_path / "alignment")
    second = project.create_complete(media, settings, output, aligner=second_aligner)
    assert second.main.timings["render_reused"] is True
    assert second.main.srt is not None and second.main.srt.is_file()
    assert second.main.vtt is not None and second.main.vtt.is_file()
    assert second.main.canonical_timeline is not None and second.main.canonical_timeline.is_file()
    assert second.youtube_metadata is not None and second.youtube_metadata.is_file()
    assert "Cached subtitles" in second.main.srt.read_text(encoding="utf-8")
