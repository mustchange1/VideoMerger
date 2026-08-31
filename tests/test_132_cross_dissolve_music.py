"""Phase 2 – calmer Cross Dissolve and safer music defaults."""

from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.render_cache import Stage1RenderCache
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export, safe_transition_durations
from app.video_merger.transition_effects import TRANSITION_OPTIONS, normalize_transition, xfade_expression
from tests.conftest import fake_media, make_clip


def _main_graph(tmp_path: Path, **changes):
    values = dict(
        resolution="160x90",
        workflow_stage="main",
        program_duration=3.5,
        timeline_target_duration=4.0,
        voiceover_path=str(tmp_path / "voice.wav"),
        music_path=str(tmp_path / "music.wav"),
        original_audio_mode="original",
        normalize_audio=False,
    )
    values.update(changes)
    settings = ExportSettings(**values)
    media = [
        fake_media(str(tmp_path / "A.mp4"), width=160, height=90, duration=3.0),
        fake_media(str(tmp_path / "B.mp4"), width=160, height=90, duration=3.0),
    ]
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "out.mp4")
    return built, settings, resolved


def test_new_project_defaults_are_cross_dissolve_calm_and_balanced():
    settings = ExportSettings()
    assert settings.transition_type == "cross_dissolve"
    assert settings.transition_duration == pytest.approx(1.0)
    assert settings.music_volume == 44
    assert settings.music_preset == "balanced"
    assert 20 * math.log10(settings.music_volume / 22) == pytest.approx(6.02, abs=0.05)
    assert settings.music_volume < settings.voiceover_volume


def test_explicit_saved_legacy_values_are_preserved(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    saved = ExportSettings(
        transition_type="smooth_blur", transition_duration=0.5,
        music_volume=22, music_preset="quiet",
    )
    store.save(saved)
    loaded = SettingsStore(tmp_path / "settings.json").load()
    assert loaded.transition_type == "smooth_blur"
    assert loaded.transition_duration == pytest.approx(0.5)
    assert loaded.music_volume == 22
    assert loaded.music_preset == "quiet"


def test_all_alternative_transitions_remain_available_and_unknown_uses_new_default():
    keys = [key for key, _label, _description in TRANSITION_OPTIONS]
    assert keys == ["smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"]
    assert normalize_transition("not-a-transition") == "cross_dissolve"


def test_default_cross_dissolve_duration_reaches_pipeline_and_is_visible_in_graph(tmp_path):
    media = [fake_media(str(tmp_path / "A.mp4"), duration=3.0), fake_media(str(tmp_path / "B.mp4"), duration=3.0)]
    settings = ExportSettings(resolution="160x90")
    resolved = resolve_export(media, settings)
    assert resolved.transitions == [pytest.approx(1.0)]
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolved)
    assert "xfade=transition=custom:duration=1:offset=2" in graph
    expression = xfade_expression("cross_dissolve", "ease_in_out")
    assert f":expr='{expression}'" in graph
    # The custom expression contains both sources throughout the ramp, rather
    # than replacing A with B at a hard cut.
    assert "A*(1-" in expression and "B*" in expression


def test_one_second_default_is_clamped_safely_for_short_clips():
    effective, transitions = safe_transition_durations([0.20, 0.30, 0.06], 1.0, 30.0)
    assert all(duration >= 0.12 for duration in effective)
    assert len(transitions) == 2
    assert all(0.0 < value < 0.5 for value in transitions)
    assert transitions[0] <= effective[0] * 0.45 + 1e-9
    assert transitions[1] <= effective[1] * 0.45 + 1e-9


def test_music_pipeline_keeps_voiceover_dominant_ducking_and_limiter(tmp_path):
    built, settings, resolved = _main_graph(tmp_path)
    graph = built.filter_graph
    command = built.command
    music_index = command.index(str(tmp_path / "music.wav"))
    assert command[music_index - 3:music_index] == ["-stream_loop", "-1", "-i"]
    assert "volume=1" in graph  # original video audio remains in the mix
    assert "volume=0.44" in graph  # 44 % linear gain, below 100 % voiceover
    assert "sidechaincompress=threshold=0.025:ratio=8" in graph
    assert "attack=25:release=450" in graph
    assert "amix=inputs=3:normalize=0" in graph
    assert "alimiter=limit=0.95" in graph
    assert f"atrim=duration={settings.program_duration:g}" in graph
    assert f"atrim=duration={resolved.expected_duration:g}" in graph
    normalized, _normalized_settings, _ = _main_graph(tmp_path, normalize_audio=True)
    assert "loudnorm=I=-16:LRA=11:TP=-1.5:linear=true" in normalized.filter_graph


def test_music_loop_and_ducking_can_still_be_manually_changed(tmp_path):
    built, _settings, _resolved = _main_graph(tmp_path, music_volume=60, ducking_enabled=False)
    graph = built.filter_graph
    assert "volume=0.6" in graph
    assert "sidechaincompress" not in graph
    assert "-stream_loop" in built.command


@pytest.mark.e2e
def test_real_default_cross_dissolve_and_music_mix_are_safe(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    clip_a = tmp_path / "A.mp4"
    clip_b = tmp_path / "B.mp4"
    make_clip(ffmpeg, clip_a, size="160x90", duration=2.5, color="red", audio_rate=None)
    make_clip(ffmpeg, clip_b, size="160x90", duration=2.5, color="blue", audio_rate=None)
    voice = tmp_path / "voice.wav"
    music = tmp_path / "music.wav"
    for path, frequency, duration, volume in (
        (voice, 900, 3.3, 0.40),
        (music, 220, 0.40, 0.50),
    ):
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
             f"sine=f={frequency}:r=48000:d={duration}", "-af", f"volume={volume}",
             "-c:a", "pcm_s16le", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip_a, clip_b])
    settings = ExportSettings(
        resolution="160x90", encoding="CPU", quality_preset="custom", crf=32,
        preset="ultrafast", normalize_audio=False,
        voiceover_path=str(voice), music_path=str(music),
        original_audio_mode="mute", final_pause=0.2,
        # Use the new defaults deliberately: no transition or music override.
        ducking_enabled=True,
    )
    project = MainProjectEngine(engine, render_cache=Stage1RenderCache(tmp_path / "stage1-cache"))
    result = project.create_main(media, settings, tmp_path / "output")
    assert result.report.ok
    assert result.report.duration == pytest.approx(3.5, abs=0.12)
    assert result.timings["render_reused"] is False

    def samples(start: float, duration: float) -> list[float]:
        raw_result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", str(start),
             "-i", str(result.video), "-t", str(duration), "-vn", "-ac", "1", "-ar", "16000",
             "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=120,
        )
        assert raw_result.returncode == 0, raw_result.stderr.decode("utf-8", errors="replace")
        values = array.array("f")
        values.frombytes(raw_result.stdout)
        return list(values)

    def tone_strength(values: list[float], frequency: float) -> float:
        real = imag = 0.0
        for index, value in enumerate(values):
            angle = 2.0 * math.pi * frequency * index / 16000.0
            real += value * math.cos(angle)
            imag += value * math.sin(angle)
        return math.hypot(real, imag) / max(1, len(values))

    speech = samples(0.8, 0.5)
    quiet_pause = samples(3.34, 0.12)
    assert math.sqrt(sum(value * value for value in speech) / len(speech)) > 0.02
    assert tone_strength(speech, 900) > tone_strength(speech, 220)
    assert max(abs(value) for value in speech) <= 0.98
    assert math.sqrt(sum(value * value for value in quiet_pause) / max(1, len(quiet_pause))) < 0.02

    # The two source colors are both visible during the calculated transition,
    # while the beginning and end remain dominated by their respective clips.
    def frame_rgb(at: float) -> tuple[int, int, int]:
        raw_result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", str(at),
             "-i", str(result.video), "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=120,
        )
        assert raw_result.returncode == 0, raw_result.stderr.decode("utf-8", errors="replace")
        return tuple(raw_result.stdout[(45 * 160 + 80) * 3:(45 * 160 + 80) * 3 + 3])

    start = frame_rgb(0.25)
    middle = frame_rgb(2.1)
    end = frame_rgb(3.25)
    assert start[0] > start[2] * 2
    assert end[2] > end[0] * 2
    assert middle[0] > 40 and middle[2] > 40
