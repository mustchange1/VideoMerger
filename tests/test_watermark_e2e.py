from __future__ import annotations

import subprocess

import pytest

from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from tests.conftest import make_clip


def _run(command, data=None):
    result = subprocess.run(
        [str(item) for item in command], input=data, capture_output=True, timeout=120,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _transparent_png(ffmpeg, path):
    width, height = 20, 10
    raw = bytes((255, 255, 0, 180)) * width * height
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
        "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-i", "pipe:0",
        "-frames:v", "1", "-c:v", "png", path,
    ], raw)


def _pixel(ffmpeg, video, at, width, x, y):
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", video,
        "-frames:v", "1", "-vf", f"crop=1:1:{x}:{y},format=rgb24", "-f", "rawvideo", "pipe:1",
    ])
    return tuple(raw)


@pytest.mark.e2e
@pytest.mark.parametrize("aspect,resolution,size", [("16:9", "160x90", (160, 90)), ("9:16", "90x160", (90, 160))])
@pytest.mark.parametrize("position", ["top_left", "top_right", "bottom_left", "bottom_right"])
def test_real_transparent_watermark_all_corners_16x9_and_9x16(ffmpeg_paths, tmp_path, aspect, resolution, size, position):
    ffmpeg, ffprobe = ffmpeg_paths
    source = tmp_path / f"source_{aspect.replace(':','x')}.mp4"
    make_clip(ffmpeg, source, f"{size[0]}x{size[1]}", duration=.6, color="red", audio_rate=None)
    mark = tmp_path / "transparent.png"
    _transparent_png(ffmpeg, mark)
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([source])
    settings = ExportSettings(
        aspect=aspect, resolution=resolution, encoding="CPU", preset="fast", crf=24,
        normalize_audio=False, watermark_enabled=True, watermark_path=str(mark),
        watermark_position=position, watermark_size=20, watermark_opacity=70,
        watermark_margin=3, watermark_scope="main",
    )
    result = MainProjectEngine(engine).create_main(media, settings, tmp_path / f"out_{position}_{size[0]}")
    width, height = size
    x = 7 if position.endswith("left") else width - 7
    y = 6 if position.startswith("top") else height - 6
    pixel = _pixel(ffmpeg, result.video, .25, width, x, y)
    # Yellow transparent mark over red keeps red high and introduces green.
    assert pixel[0] > 140 and pixel[1] > 65 and pixel[2] < 90
