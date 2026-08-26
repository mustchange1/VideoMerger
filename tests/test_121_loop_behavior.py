from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.target import safe_transition_durations
from app.video_merger.timeline import fit_media_to_duration
from tests.conftest import fake_media, make_clip


def _run(command):
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=120,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _tone(ffmpeg: Path, output: Path, duration: float) -> None:
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=f=700:r=48000:d={duration}", "-c:a", "pcm_s16le", output,
    ])


def _center_pixel(ffmpeg: Path, video: Path, at: float) -> tuple[int, int, int]:
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", video,
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    offset = (45 * 160 + 80) * 3
    return tuple(raw[offset:offset + 3])


def test_full_timeline_loop_restarts_exact_manual_order_and_trims_final_occurrence():
    manual = [fake_media(f"{name}.mp4", duration=.8) for name in ("C", "A", "D", "B")]
    fitted, warnings = fit_media_to_duration(manual, 4.6, .2, 30, "loop")
    names = [item.path.stem for item in fitted]
    assert names == ["C", "A", "D", "B", "C", "A", "D", "B"]
    assert fitted[-1].duration < fitted[-1].source_duration
    effective, transitions = safe_transition_durations([item.duration for item in fitted], .2, 30)
    assert sum(effective) - sum(transitions) == pytest.approx(4.6, abs=.003)
    # Boundary 3 is B -> C, and it must use the selected transition.
    assert transitions[3] > 0
    assert any("Full-Timeline Loop" in warning for warning in warnings)


def test_hold_last_frame_is_separate_and_never_restarts_timeline():
    manual = [fake_media(f"{name}.mp4", duration=.8) for name in ("C", "A", "D", "B")]
    fitted, warnings = fit_media_to_duration(manual, 4.6, .2, 30, "hold")
    assert [item.path.stem for item in fitted] == ["C", "A", "D", "B"]
    assert fitted[-1].duration > fitted[-1].source_duration
    effective, transitions = safe_transition_durations([item.duration for item in fitted], .2, 30)
    assert sum(effective) - sum(transitions) == pytest.approx(4.6, abs=.003)
    assert any("Hold Last Frame" in warning for warning in warnings)


@pytest.mark.e2e
def test_real_loop_and_hold_outputs_follow_manual_order_and_exact_target(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "clips"
    colors = {"C": "blue", "A": "red", "D": "yellow", "B": "lime"}
    manual_paths = []
    for name in ("C", "A", "D", "B"):
        path = folder / f"{name}.mp4"
        make_clip(ffmpeg, path, size="160x90", duration=.8, color=colors[name], audio_rate=None)
        manual_paths.append(path)
    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, 4.1)
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(manual_paths)
    base = dict(
        resolution="160x90", encoding="CPU", preset="fast", crf=28,
        normalize_audio=False, voiceover_path=str(voice), final_pause=.5,
        transition_type="cross_dissolve", transition_duration=.2,
    )
    loop_result = MainProjectEngine(engine).create_main(
        media, ExportSettings(**base, short_video_mode="loop"), tmp_path / "loop"
    )
    assert loop_result.report.duration == pytest.approx(4.6, abs=.08)
    # 2.85 s is in the repeated C clip, proving restart at the first manual item.
    loop_pixel = _center_pixel(ffmpeg, loop_result.video, 2.85)
    assert loop_pixel[2] > loop_pixel[0] * 2 and loop_pixel[2] > loop_pixel[1] * 2
    assert engine.last_filter_graph.count(":v:0]fps=") == 8
    assert "xfade=transition=custom" in engine.last_filter_graph

    hold_result = MainProjectEngine(engine).create_main(
        media, ExportSettings(**base, short_video_mode="hold"), tmp_path / "hold"
    )
    assert hold_result.report.duration == pytest.approx(4.6, abs=.08)
    hold_pixel = _center_pixel(ffmpeg, hold_result.video, 4.25)
    assert hold_pixel[1] > 140 and hold_pixel[0] < 90  # final B/lime frame held
    assert engine.last_filter_graph.count(":v:0]fps=") == 4
