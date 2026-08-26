from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video_merger.discovery import discover_videos
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.project_order import ProjectOrderStore
from tests.conftest import make_clip


def _export(ffmpeg_paths, folder: Path, output: Path, aspect: str, resolution: str, transition=0.1):
    ffmpeg, ffprobe = ffmpeg_paths
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(discover_videos(folder))
    settings = ExportSettings(
        aspect=aspect, resolution=resolution, transition_duration=transition,
        background_blur=5, background_darkness=10, encoding="CPU",
        crf=28, preset="fast", normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    report = engine.export(media, settings, resolved, output)
    return report, resolved, media


@pytest.mark.e2e
@pytest.mark.parametrize(
    "case,aspect,resolution,specs",
    [
        ("01_three_landscape", "16:9", "160x90", [("160x90", 30, 48000), ("160x90", 30, 48000), ("160x90", 30, 48000)]),
        ("02_three_portrait", "9:16", "90x160", [("90x160", 30, 48000), ("90x160", 30, 48000), ("90x160", 30, 48000)]),
        ("04_mixed_to_landscape", "16:9", "160x90", [("160x90", 30, 48000), ("90x160", 30, 48000), ("160x90", 30, 48000)]),
        ("06_mixed_resolutions", "16:9", "160x90", [("128x72", 30, 48000), ("320x180", 30, 48000), ("160x90", 30, 48000)]),
        ("07_missing_audio", "16:9", "160x90", [("160x90", 30, 48000), ("160x90", 30, None), ("160x90", 30, 48000)]),
        ("08_unicode", "16:9", "160x90", [("160x90", 30, 48000), ("160x90", 30, 48000), ("160x90", 30, 48000)]),
        ("11_audio_rates", "16:9", "160x90", [("160x90", 30, 22050), ("160x90", 30, 44100), ("160x90", 30, 48000)]),
    ],
)
def test_required_export_cases(ffmpeg_paths, tmp_path, case, aspect, resolution, specs):
    ffmpeg, _ = ffmpeg_paths
    folder = tmp_path / case / "Eingabe mit Leerzeichen ä"
    names = ["01 Clip ä.mp4", "02 Clip ö.mp4", "03 Clip ü.mp4"] if case == "08_unicode" else ["01.mp4", "02.mp4", "03.mp4"]
    for index, (size, fps, audio_rate) in enumerate(specs):
        make_clip(ffmpeg, folder / names[index], size=size, fps=fps, duration=0.55, color=["red", "green", "blue"][index], audio_rate=audio_rate)
    width, height = map(int, resolution.split("x"))
    report, _, _ = _export(ffmpeg_paths, folder, tmp_path / f"{case}.mp4", aspect, resolution)
    assert report.ok
    assert (report.width, report.height) == (width, height)
    assert report.has_audio


@pytest.mark.e2e
def test_03_mixed_vertical_uses_non_black_blurred_background(ffmpeg_paths, tmp_path):
    ffmpeg, _ = ffmpeg_paths
    folder = tmp_path / "mixed_vertical"
    make_clip(ffmpeg, folder / "01.mp4", "90x160", duration=0.7, color="red")
    make_clip(ffmpeg, folder / "02.mp4", "160x90", duration=0.7, color="lime")
    make_clip(ffmpeg, folder / "03.mp4", "90x160", duration=0.7, color="blue")
    output = tmp_path / "mixed_vertical.mp4"
    report, _, _ = _export(ffmpeg_paths, folder, output, "9:16", "90x160")
    assert report.ok
    # At 0.95 s clip 2 is fully active. Its 16:9 foreground does not reach the
    # top edge, so a non-black top-left pixel proves the self-video background.
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", "0.95", "-i", str(output),
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ]
    frame = subprocess.run(command, capture_output=True, timeout=30, creationflags=hidden_process_flags(), env=safe_subprocess_env()).stdout
    assert len(frame) == 90 * 160 * 3
    assert sum(frame[:3]) > 20


@pytest.mark.e2e
def test_05_mixed_framerates_resolve_to_30(ffmpeg_paths, tmp_path):
    ffmpeg, _ = ffmpeg_paths
    folder = tmp_path / "fps"
    make_clip(ffmpeg, folder / "01.mp4", fps=24, duration=0.6)
    make_clip(ffmpeg, folder / "02.mp4", fps=60, duration=0.6)
    report, resolved, _ = _export(ffmpeg_paths, folder, tmp_path / "fps.mp4", "16:9", "160x90")
    assert resolved.fps == 30
    assert report.fps == pytest.approx(30, abs=0.1)


@pytest.mark.e2e
def test_09_manual_persistent_order_controls_the_actual_timeline(ffmpeg_paths, tmp_path):
    ffmpeg, _ = ffmpeg_paths
    folder = tmp_path / "first_in"
    # Intentionally non-alphabetical creation/detection order.
    for name, color in [("video_B.mp4", "red"), ("video_A.mp4", "lime"), ("video_C.mp4", "blue")]:
        make_clip(ffmpeg, folder / name, duration=0.5, color=color)
    detected_first_in = [folder / "video_B.mp4", folder / "video_A.mp4", folder / "video_C.mp4"]
    store_path = tmp_path / "order.json"
    store = ProjectOrderStore(store_path)
    ordered = store.order(folder, detected_first_in)
    assert [path.name for path in ordered] == ["video_A.mp4", "video_B.mp4", "video_C.mp4"]
    # Manual C → B → A becomes the active order. A restart plus a different
    # detector order must preserve it exactly rather than silently sorting.
    manual = [folder / "video_C.mp4", folder / "video_B.mp4", folder / "video_A.mp4"]
    store.set_active_order(folder, manual)
    ordered_after_restart = ProjectOrderStore(store_path).order(folder, list(reversed(detected_first_in)))
    assert [path.name for path in ordered_after_restart] == ["video_C.mp4", "video_B.mp4", "video_A.mp4"]

    _, ffprobe = ffmpeg_paths
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(ordered_after_restart)
    settings = ExportSettings(
        aspect="16:9", resolution="160x90", transition_duration=0.1,
        background_blur=5, encoding="CPU", crf=28, preset="fast", normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    output = tmp_path / "first_in_timeline.mp4"
    report = engine.export(media, settings, resolved, output)
    assert report.ok
    assert [item.path.name for item in media] == ["video_C.mp4", "video_B.mp4", "video_A.mp4"]

    def center_rgb(at: float) -> tuple[int, int, int]:
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(output),
            "-frames:v", "1", "-vf", "crop=1:1:80:45,format=rgb24", "-f", "rawvideo", "pipe:1",
        ]
        pixel = subprocess.run(command, capture_output=True, timeout=30, creationflags=hidden_process_flags(), env=safe_subprocess_env()).stdout
        assert len(pixel) == 3
        return pixel[0], pixel[1], pixel[2]

    first, second, third = center_rgb(0.15), center_rgb(0.55), center_rgb(0.95)
    assert first[2] > first[0] * 2 and first[2] > first[1] * 2       # video_C = blue
    assert second[0] > second[1] * 2 and second[0] > second[2] * 2   # video_B = red
    assert third[1] > third[0] * 2 and third[1] > third[2] * 2       # video_A = lime


@pytest.mark.e2e
def test_11_direct_filter_complex_render_eliminates_script_option_failure(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "ÄÖÜ (Direct Graph)"
    make_clip(ffmpeg, folder / "video_B clip.mp4", duration=0.45, color="red")
    make_clip(ffmpeg, folder / "video_A clip.mp4", duration=0.45, color="blue")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    messages: list[str] = []
    engine.preflight(messages.append)
    media = engine.analyze([folder / "video_B clip.mp4", folder / "video_A clip.mp4"], messages.append)
    settings = ExportSettings(
        aspect="16:9", resolution="160x90", transition_duration=0.1,
        encoding="CPU", crf=28, preset="fast", normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    output = tmp_path / "direct_filter_complex.mp4"
    report = engine.export(media, settings, resolved, output, log=messages.append)
    assert report.ok
    command_logs = "\n".join(messages)
    assert "FFmpeg executable:" in command_logs
    assert "FFmpeg version:" in command_logs
    assert "Rendering command:" in command_logs
    assert "-filter_complex" in command_logs
    assert "Unrecognized option" not in command_logs


@pytest.mark.e2e
def test_10_extremely_short_clip_is_extended_safely(ffmpeg_paths, tmp_path):
    ffmpeg, _ = ffmpeg_paths
    folder = tmp_path / "short"
    make_clip(ffmpeg, folder / "01.mp4", duration=0.04, color="red", audio_rate=None)
    make_clip(ffmpeg, folder / "02.mp4", duration=0.5, color="blue", audio_rate=None)
    report, resolved, _ = _export(ffmpeg_paths, folder, tmp_path / "short.mp4", "16:9", "160x90", transition=1.0)
    assert report.ok
    assert resolved.effective_durations[0] >= 0.12
    assert resolved.transitions[0] < resolved.effective_durations[0] / 2
