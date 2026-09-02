from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.models import ExportSettings
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export, safe_transition_durations
from app.video_merger.timeline import fit_media_to_duration
from tests.conftest import fake_media


def _main_graph(tmp_path, **changes):
    media = [fake_media(str(tmp_path / "A.mp4"), duration=2), fake_media(str(tmp_path / "B.mp4"), duration=2)]
    values = dict(
        resolution="160x90", workflow_stage="main", program_duration=3,
        voiceover_path=str(tmp_path / "voice.wav"), music_path=str(tmp_path / "music.mp3"),
        original_audio_mode="mute", normalize_audio=False,
    )
    values.update(changes)
    settings = ExportSettings(**values)
    resolved = resolve_export(media, settings)
    builder = FFmpegCommandBuilder("ffmpeg")
    return builder.build(media, settings, resolved, tmp_path / "out.mp4"), settings, resolved


def test_voiceover_target_trims_ordered_prefix_exactly():
    media = [fake_media(f"{name}.mp4", duration=3) for name in "ABCD"]
    fitted, warnings = fit_media_to_duration(media, 5.0, .5, 30)
    effective, transitions = safe_transition_durations([m.duration for m in fitted], .5, 30)
    assert [m.path.name for m in fitted] == ["A.mp4", "B.mp4"]
    assert sum(effective) - sum(transitions) == pytest.approx(5.0, abs=.002)
    assert fitted[-1].duration < fitted[-1].source_duration
    assert any("länger" in warning for warning in warnings)


def test_short_visual_material_extends_last_clip_and_preserves_source_duration():
    media = [fake_media("A.mp4", duration=.8), fake_media("B.mp4", duration=.8)]
    fitted, warnings = fit_media_to_duration(media, 4.0, .4, 30)
    effective, transitions = safe_transition_durations([m.duration for m in fitted], .4, 30)
    assert sum(effective) - sum(transitions) == pytest.approx(4.0, abs=.002)
    assert fitted[-1].duration > fitted[-1].source_duration
    assert any("kürzer" in warning for warning in warnings)


def test_authoritative_video_window_is_padded_before_final_trim(tmp_path):
    """The final visual label must cover the audio-driven target even when the
    selected source sequence is shorter than that target."""
    media = [fake_media(str(tmp_path / "short.mp4"), duration=0.8, audio=False)]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", program_duration=3.0,
        timeline_target_duration=3.0, normalize_audio=False,
    )
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "out.mp4")
    assert "tpad=stop_mode=clone:stop_duration=3.033333" in built.filter_graph
    assert "trim=duration=3" in built.filter_graph
    output_duration_index = built.command.index("-t")
    assert built.command[output_duration_index + 1] == "3"


def test_music_loops_but_voiceover_never_loops_and_gap_is_quiet(tmp_path):
    built, _settings, resolved = _main_graph(tmp_path)
    command = built.command
    voice_index = command.index(str(tmp_path / "voice.wav"))
    music_index = command.index(str(tmp_path / "music.mp3"))
    assert command[voice_index - 1] == "-i"
    assert command[music_index - 3:music_index] == ["-stream_loop", "-1", "-i"]
    graph = built.filter_graph
    assert "[voice_pre]" in graph
    assert "atrim=duration=3" in graph
    assert f"atrim=duration={resolved.expected_duration:g}" in graph or "atrim=duration=3.5" in graph
    assert "sidechaincompress=" in graph


@pytest.mark.parametrize("mode,gain", [("mute", "volume=0"), ("low", "volume=0.22"), ("original", "volume=1")])
def test_original_video_audio_modes_are_explicit(tmp_path, mode, gain):
    built, _, _ = _main_graph(tmp_path, original_audio_mode=mode)
    assert gain in built.filter_graph


def test_music_optional_voice_optional_and_music_only_graph(tmp_path):
    media = [fake_media("A.mp4")]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", music_path=str(tmp_path / "music.mp3"),
        program_duration=2, normalize_audio=False,
    )
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "out.mp4")
    assert "music_pre" in built.filter_graph
    assert "voice_pre" not in built.filter_graph
    assert "sidechaincompress" not in built.filter_graph


@pytest.mark.parametrize("position", ["top_left", "top_right", "bottom_left", "bottom_right"])
def test_watermark_positions_use_relative_scale_opacity_and_safe_margin(tmp_path, position):
    media = [fake_media("A.mp4")]
    settings = ExportSettings(
        resolution="1920x1080", workflow_stage="main", watermark_enabled=True,
        watermark_path=str(tmp_path / "mark.png"), watermark_position=position,
        watermark_size=12, watermark_opacity=63, watermark_margin=3,
    )
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "out.mp4")
    assert "scale=w=230:h=-1" in built.filter_graph
    assert "colorchannelmixer=aa=0.63" in built.filter_graph
    assert "overlay=x=" in built.filter_graph
    assert built.command[-1] == str(tmp_path / "out.mp4")


def test_outro_graph_has_own_audio_mode_and_no_stage1_assets_or_subtitles(tmp_path):
    media = [fake_media("MainVideo.mp4", duration=3), fake_media("Outro.mp4", duration=1)]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="outro", outro_audio_mode="low",
        voiceover_path="voice.wav", music_path="music.mp3", subtitle_enabled=True,
        subtitle_ass_path="captions.ass", transition_type="cross_dissolve",
    )
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "final.mp4")
    assert "volume=0.22" in built.filter_graph
    assert "voice.wav" not in built.command and "music.mp3" not in built.command
    assert "subtitles=" not in built.filter_graph
    assert "xfade=transition=custom" in built.filter_graph


def test_outro_transition_can_be_disabled_with_true_concat():
    effective, transitions = safe_transition_durations([3, 1], 0, 30)
    assert transitions == [0.0]
    settings = ExportSettings(resolution="160x90", workflow_stage="outro", transition_duration=0)
    resolved = resolve_export([fake_media("main.mp4", duration=3), fake_media("outro.mp4", duration=1)], settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
        [fake_media("main.mp4", duration=3), fake_media("outro.mp4", duration=1)], settings, resolved
    )
    assert "concat=n=2:v=1:a=0" in graph
    assert "xfade=" not in graph


def test_all_new_project_settings_persist(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    selected = ExportSettings(
        voiceover_path="Ä voice.wav", script_path="script.txt", music_path="music.mp3",
        original_audio_mode="low", music_volume=31, subtitle_enabled=True,
        subtitle_language="English", subtitle_style="short_4", subtitle_position="Medium-Low",
        watermark_enabled=True, watermark_path="mark.png", watermark_scope="both",
        main_video_path="Main.mp4", outro_path="Outro.mp4", outro_audio_mode="original",
    )
    store.save(selected)
    loaded = SettingsStore(tmp_path / "settings.json").load()
    assert loaded == selected
