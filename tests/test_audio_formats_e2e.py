from __future__ import annotations

import subprocess

import pytest

from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.project_assets import probe_audio


@pytest.mark.e2e
@pytest.mark.parametrize(
    "extension,codec",
    [("wav", "pcm_s16le"), ("mp3", "libmp3lame"), ("m4a", "aac"), ("aac", "aac"), ("flac", "flac")],
)
def test_voiceover_music_required_audio_formats_decode(ffmpeg_paths, tmp_path, extension, codec):
    ffmpeg, ffprobe = ffmpeg_paths
    path = tmp_path / f"voice.{extension}"
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=f=440:r=44100:d=0.4", "-c:a", codec, str(path)],
        capture_output=True, timeout=60, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    info = probe_audio(ffprobe, path)
    assert info.duration > .3 and info.sample_rate == 44100 and info.channels == 1
