from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video_merger.models import AudioInfo, MediaInfo
from app.video_merger.paths import locate_ffmpeg
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env


@pytest.fixture(scope="session")
def ffmpeg_paths():
    try:
        return locate_ffmpeg()
    except Exception as exc:
        pytest.skip(f"FFmpeg nicht installiert: {exc}")


def fake_media(path: str = "clip.mp4", width: int = 1920, height: int = 1080, duration: float = 2.0, fps: float = 30.0, audio: bool = True) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=width, height=height,
        effective_width=width, effective_height=height, fps=fps, fps_fraction=f"{int(fps)}/1",
        video_codec="h264", pixel_format="yuv420p", sar="1:1", dar="",
        audio=AudioInfo(present=audio, codec="aac" if audio else "", sample_rate=48000 if audio else 0, channels=2 if audio else 0),
    )


def make_clip(ffmpeg: Path, path: Path, size="160x90", fps=30, duration=0.7, color="red", audio_rate: int | None = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}:d={duration}",
    ]
    if audio_rate:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={audio_rate}:duration={duration}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio_rate:
        command += ["-c:a", "aac", "-shortest"]
    command.append(str(path))
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=hidden_process_flags(), env=safe_subprocess_env())
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
