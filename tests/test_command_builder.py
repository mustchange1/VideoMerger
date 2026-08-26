from pathlib import Path

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.models import ExportSettings
from app.video_merger.target import resolve_export
from tests.conftest import fake_media


def test_vertical_mixed_aspect_builds_blurred_background():
    media = [fake_media(width=1080, height=1920), fake_media("wide.mp4", 1920, 1080)]
    settings = ExportSettings(aspect="9:16", resolution="1080x1920", background_blur=30)
    resolved = resolve_export(media, settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolved)
    assert "force_original_aspect_ratio=increase" in graph
    assert "force_original_aspect_ratio=decrease" in graph
    assert "gblur=sigma=15" in graph
    assert "scale=w=1080:h=1920:flags=bicubic" in graph
    assert "overlay=x=(W-w)/2:y=(H-h)/2" in graph
    assert "]pad=" not in graph  # no black-bar pad filter (tpad is only timestamp safety)


def test_transition_is_after_target_format_processing():
    media = [fake_media(), fake_media("b.mp4")]
    settings = ExportSettings(resolution="1920x1080")
    resolved = resolve_export(media, settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolved)
    assert graph.index("scale=w=1920:h=1080") < graph.index("xfade=transition=custom")
    assert "blend=all_expr" in graph
    assert "gblur" in graph


def test_missing_audio_gets_silence_source():
    media = [fake_media(audio=False), fake_media("b.mp4")]
    settings = ExportSettings()
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolve_export(media, settings))
    assert "anullsrc=r=48000:cl=stereo" in graph
    assert "acrossfade=" in graph


def test_subprocess_command_keeps_unicode_path_as_one_argument(tmp_path):
    media = [fake_media(str(tmp_path / "ÄÖÜ (Mein Projekt)" / "Clip ä mit Leerzeichen.mp4"))]
    settings = ExportSettings(resolution="1920x1080")
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "ausgabe.mp4")
    assert str(media[0].path) in built.command
    assert "-filter_complex" in built.command
    assert built.command[built.command.index("-filter_complex") + 1] == built.filter_graph
    assert "-filter_complex_script" not in built.command
    assert not (tmp_path / "graph.txt").exists()
    # Paths and the complete graph are separate subprocess arguments; no shell
    # quoting is baked into path arguments.
    assert '"' not in str(media[0].path)
