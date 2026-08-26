from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.video_merger.discovery import discover_videos
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env


def _run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result


def _encode_rgb_frames(ffmpeg: Path, path: Path, frames: list[bytes], width: int, height: int, fps: int = 30) -> None:
    raw = path.with_suffix(".rgb")
    raw.write_bytes(b"".join(frames))
    _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps),
        "-i", str(raw), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
    ])


def _rgb_frame(ffmpeg: Path, video: Path, at: float, width: int, height: int) -> bytes:
    result = _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(video),
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    assert len(result.stdout) == width * height * 3
    return result.stdout


def _pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * width + x) * 3
    return frame[offset], frame[offset + 1], frame[offset + 2]


def _dominant(pixel: tuple[int, int, int], channel: int) -> bool:
    selected = pixel[channel]
    others = [pixel[index] for index in range(3) if index != channel]
    return selected > 70 and selected > max(others) * 1.5


def _pattern_frame(width: int, height: int, interior: tuple[int, int, int]) -> bytes:
    data = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            color = interior
            if x < 18:
                color = (0, 255, 0)       # left marker
            elif x >= width - 18:
                color = (0, 0, 255)       # right marker
            elif y < 14:
                color = (255, 255, 255)   # top marker
            elif y >= height - 14:
                color = (255, 255, 0)     # bottom marker
            offset = (y * width + x) * 3
            data[offset:offset + 3] = bytes(color)
    return bytes(data)


@pytest.mark.e2e
def test_real_1080x1920_self_video_background_and_complete_foreground(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "vertical-proof"
    folder.mkdir()
    red_pattern = _pattern_frame(320, 180, (230, 15, 15))
    cyan_pattern = _pattern_frame(320, 180, (15, 210, 210))
    source = folder / "wide_dynamic.mp4"
    _encode_rgb_frames(ffmpeg, source, [red_pattern] * 9 + [cyan_pattern] * 9, 320, 180)

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(discover_videos(folder))
    settings = ExportSettings(
        aspect="9:16", resolution="1080x1920", fit_mode="contain_blur",
        background_blur=30, background_darkness=10, encoding="CPU",
        crf=28, preset="fast", normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    output = tmp_path / "vertical_1080x1920.mp4"
    report = engine.export(media, settings, resolved, output)
    assert report.ok and (report.width, report.height) == (1080, 1920)

    red_frame = _rgb_frame(ffmpeg, output, 0.10, 1080, 1920)
    cyan_frame = _rgb_frame(ffmpeg, output, 0.45, 1080, 1920)
    # The 16:9 foreground fits to 1080x608 at y approximately 656..1263.
    # All four source-edge markers remain visible: no crop and no stretching.
    assert _dominant(_pixel(red_frame, 1080, 12, 960), 1)       # full left edge
    assert _dominant(_pixel(red_frame, 1080, 1067, 960), 2)     # full right edge
    top_marker = _pixel(red_frame, 1080, 540, 665)
    bottom_marker = _pixel(red_frame, 1080, 540, 1254)
    assert min(top_marker) > 150
    assert bottom_marker[0] > 150 and bottom_marker[1] > 150 and bottom_marker[2] < 100
    # Outside the foreground is a non-black, frame-synchronous version of the
    # same source: it changes from red to cyan together with the foreground.
    red_background = _pixel(red_frame, 1080, 540, 200)
    cyan_background = _pixel(cyan_frame, 1080, 540, 200)
    assert sum(red_background) > 50 and _dominant(red_background, 0)
    assert sum(cyan_background) > 50 and cyan_background[1] > cyan_background[0] * 2 and cyan_background[2] > cyan_background[0] * 2
    assert _dominant(_pixel(red_frame, 1080, 540, 960), 0)
    cyan_foreground = _pixel(cyan_frame, 1080, 540, 960)
    assert cyan_foreground[1] > cyan_foreground[0] * 2 and cyan_foreground[2] > cyan_foreground[0] * 2


def _checker_frame(width: int, height: int, block: int = 3) -> bytes:
    data = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            level = 245 if ((x // block + y // block) % 2) else 10
            offset = (y * width + x) * 3
            data[offset:offset + 3] = bytes((level, level, level))
    return bytes(data)


def _edge_energy(frame: bytes, width: int, height: int) -> float:
    total = 0
    count = 0
    for y in range(1, height - 1):
        row = y * width * 3
        for x in range(1, width - 1):
            here = frame[row + x * 3]
            total += abs(here - frame[row + (x - 1) * 3])
            total += abs(here - frame[(y - 1) * width * 3 + x * 3])
            count += 2
    return total / count


@pytest.mark.e2e
def test_transition_has_real_time_varying_blur_not_only_xfade(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "blur-transition"
    folder.mkdir()
    pattern = _checker_frame(160, 90)
    first = folder / "video_B.mp4"
    _encode_rgb_frames(ffmpeg, first, [pattern] * 30, 160, 90)
    second = folder / "video_A.mp4"
    shutil.copyfile(first, second)

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(discover_videos(folder))
    settings = ExportSettings(
        aspect="16:9", resolution="160x90", transition_duration=0.4,
        encoding="CPU", crf=24, preset="fast", normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    graph = engine.builder.build_filter_graph(media, settings, resolved)
    # Both prepared target-format streams are dynamically blended with gblur
    # before the xfade combines them.
    assert graph.index("scale=w=160:h=90") < graph.index("blend=all_expr") < graph.index("xfade=transition=custom")
    output = tmp_path / "actual_smooth_blur_crossfade.mp4"
    report = engine.export(media, settings, resolved, output)
    assert report.ok
    sharp = _rgb_frame(ffmpeg, output, 0.25, 160, 90)
    transition_middle = _rgb_frame(ffmpeg, output, 0.80, 160, 90)
    sharp_energy = _edge_energy(sharp, 160, 90)
    blurred_energy = _edge_energy(transition_middle, 160, 90)
    assert sharp_energy > 25
    assert blurred_energy < sharp_energy * 0.55
