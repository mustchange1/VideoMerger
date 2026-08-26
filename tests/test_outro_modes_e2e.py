from __future__ import annotations

import array
import math
import subprocess

import pytest

from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from tests.conftest import make_clip


def _run(command):
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=120,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _tail_rms(ffmpeg, path):
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-sseof", "-0.25", "-i", path,
        "-map", "0:a:0", "-t", "0.2", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1",
    ])
    values = array.array("f"); values.frombytes(raw)
    return math.sqrt(sum(v*v for v in values) / max(1, len(values)))


@pytest.mark.e2e
def test_outro_audio_original_low_mute_and_missing_audio(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    main = tmp_path / "MainVideo.mp4"
    outro = tmp_path / "Outro.mp4"
    silent_outro = tmp_path / "SilentOutro.mp4"
    make_clip(ffmpeg, main, "160x90", duration=1.2, color="red", audio_rate=48000)
    make_clip(ffmpeg, outro, "160x90", duration=.8, color="blue", audio_rate=48000)
    make_clip(ffmpeg, silent_outro, "160x90", duration=.8, color="blue", audio_rate=None)
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    levels = {}
    for mode in ("original", "low", "mute"):
        settings = ExportSettings(
            resolution="160x90", encoding="CPU", preset="fast", crf=28, normalize_audio=False,
            main_video_path=str(main), outro_path=str(outro), outro_audio_mode=mode,
            transition_type="film_dissolve", transition_duration=.2,
            # These assigned Stage-1 roles must never leak into Stage 2.
            voiceover_path="must_not_be_opened.wav", music_path="must_not_be_opened.mp3",
            subtitle_enabled=True, subtitle_ass_path="must_not_be_used.ass",
        )
        output, report = MainProjectEngine(engine).add_outro(settings, tmp_path / mode)
        assert report.ok
        levels[mode] = _tail_rms(ffmpeg, output)
    assert levels["original"] > levels["low"] * 2.5
    assert levels["low"] > levels["mute"] * 5
    assert levels["mute"] < .001

    settings = ExportSettings(
        resolution="160x90", encoding="CPU", preset="fast", crf=28, normalize_audio=False,
        main_video_path=str(main), outro_path=str(silent_outro), outro_audio_mode="original",
        outro_transition_enabled=False,
    )
    output, report = MainProjectEngine(engine).add_outro(settings, tmp_path / "silent")
    assert report.ok and report.has_audio  # main track plus generated outro silence
    assert report.duration == pytest.approx(2.0, abs=.08)
