from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from tests.conftest import make_clip


def _run(command: list[object], timeout: int = 180) -> bytes:
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _tone(ffmpeg: Path, path: Path, frequency: int, duration: float, volume: float = .55) -> None:
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=f={frequency}:r=48000:d={duration}", "-af", f"volume={volume}",
        "-c:a", "pcm_s16le", path,
    ])


def _watermark(ffmpeg: Path, path: Path) -> None:
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "color=c=yellow@0.92:s=90x36:d=0.1", "-frames:v", "1", "-update", "1", path,
    ])


def _samples(ffmpeg: Path, media: Path, start: float, duration: float = .20) -> list[float]:
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", media,
        "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1",
    ])
    values = array.array("f")
    values.frombytes(raw)
    return list(values)


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def _frequency_strength(values: list[float], frequency: float, sample_rate: int = 16000) -> float:
    # Correlation at a known deterministic test frequency; sufficient to prove
    # outro-only audio without an optional NumPy dependency.
    sine = 0.0
    cosine = 0.0
    for index, value in enumerate(values):
        angle = 2.0 * math.pi * frequency * index / sample_rate
        sine += value * math.sin(angle)
        cosine += value * math.cos(angle)
    return math.hypot(sine, cosine) / max(1, len(values))


def _rgb_frame(ffmpeg: Path, media: Path, start: float, width: int, height: int) -> bytes:
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", media,
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    assert len(raw) == width * height * 3
    return raw


def _pixel(raw: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    index = (y * width + x) * 3
    return tuple(raw[index:index + 3])


@pytest.mark.e2e
def test_actual_one_click_stage1_handoff_stage2_validation_and_outro_isolation(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    blue = tmp_path / "Blue.mp4"
    red = tmp_path / "Red.mp4"
    make_clip(ffmpeg, blue, size="320x180", duration=.85, color="blue", audio_rate=None)
    make_clip(ffmpeg, red, size="320x180", duration=.85, color="red", audio_rate=None)
    voice = tmp_path / "voice.wav"
    music = tmp_path / "music.wav"
    _tone(ffmpeg, voice, 850, 1.20, .65)
    _tone(ffmpeg, music, 220, .40, .40)
    script_text = "One click uses the actual generated main video."
    script = tmp_path / "script.txt"
    script.write_text(script_text, encoding="utf-8")
    watermark = tmp_path / "watermark.png"
    _watermark(ffmpeg, watermark)
    outro = tmp_path / "outro.mp4"
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30:d=0.80",
        "-f", "lavfi", "-i", "sine=f=1200:r=48000:d=0.80",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", outro,
    ])

    timings = [
        ("One", .08, .20), ("click", .22, .34), ("uses", .36, .48),
        ("the", .50, .58), ("actual", .60, .72), ("generated", .74, .88),
        ("main", .90, 1.01), ("video", 1.03, 1.14),
    ]
    calls: list[Path] = []

    def recognize(path: Path, _language: str):
        calls.append(Path(path).resolve())
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    aligner = LocalWordAligner("one-click-fixture", recognize, cache_dir=tmp_path / "alignment-cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    # This list is the active manual order and must remain exact.
    media = engine.analyze([blue, red])
    settings = ExportSettings(
        resolution="320x180", encoding="CPU", preset="fast", crf=24, normalize_audio=False,
        transition_type="cross_dissolve", transition_duration=.10,
        voiceover_path=str(voice), script_path=str(script), music_path=str(music),
        original_audio_mode="mute", music_volume=24, ducking_enabled=True, final_pause=.50,
        subtitle_enabled=True, subtitle_language="English", subtitle_style="long_1",
        subtitle_animation="word_highlight", subtitle_font="modern_sans_bold",
        subtitle_position="Bottom", subtitle_debug_overlay=False,
        watermark_enabled=True, watermark_path=str(watermark), watermark_position="top_right",
        watermark_scope="both", watermark_size=12, watermark_opacity=82,
        outro_path=str(outro), outro_audio_mode="original", outro_transition_enabled=False,
    )
    logs: list[str] = []
    progress = []
    result = MainProjectEngine(engine).create_complete(
        media, settings, tmp_path / "output", aligner=aligner,
        log=logs.append, progress=progress.append,
    )

    assert calls == [voice.resolve()]
    assert result.main.video.is_file() and result.main.report.ok
    assert result.final_video.is_file() and result.final_report.ok
    assert result.main.video != result.final_video
    assert result.main.srt and result.main.srt.is_file()
    assert result.main.vtt and result.main.vtt.is_file()
    assert result.main.canonical_timeline and result.main.canonical_timeline.is_file()
    assert any(f"actual MainVideo input = {result.main.video}" in line for line in logs)
    assert any(f"Actual Stage 1 input used by Stage 2: {result.main.video}" in line for line in logs)
    assert any("ONE-CLICK COMPLETE WORKFLOW – PASS" in line for line in logs)
    assert progress and max(item.percent for item in progress) == pytest.approx(100.0)
    assert any(item.stage.startswith("One-Click 1/2") for item in progress)
    assert any(item.stage.startswith("One-Click 2/2") for item in progress)

    # Stage 1 duration is voiceover 1.20 s plus the configured quiet .50 s.
    assert result.main.report.duration == pytest.approx(1.70, abs=.08)
    quiet = _samples(ffmpeg, result.final_video, 1.35, .20)
    assert _rms(quiet) < .004

    # With transition disabled, the outro begins after the intact quiet gap.
    outro_audio = _samples(ffmpeg, result.final_video, 1.82, .25)
    strength_1200 = _frequency_strength(outro_audio, 1200)
    assert strength_1200 > .015
    assert strength_1200 > _frequency_strength(outro_audio, 850) * 8
    assert strength_1200 > _frequency_strength(outro_audio, 220) * 8

    # Configured active order begins blue. Outro is green and still has the
    # configured watermark, but no generated subtitle glyphs.
    first = _rgb_frame(ffmpeg, result.final_video, .03, 320, 180)
    assert _pixel(first, 320, 160, 90)[2] > 120
    outro_frame = _rgb_frame(ffmpeg, result.final_video, 1.90, 320, 180)
    center = _pixel(outro_frame, 320, 160, 90)
    assert center[1] > center[0] * 1.5 and center[1] > center[2] * 1.5
    mark = _pixel(outro_frame, 320, 300, 9)
    assert mark[0] > 120 and mark[1] > 100
    bright_bottom = 0
    for y in range(115, 175):
        for x in range(25, 295):
            red_value, green_value, blue_value = _pixel(outro_frame, 320, x, y)
            if min(red_value, green_value, blue_value) > 185:
                bright_bottom += 1
    assert bright_bottom == 0
