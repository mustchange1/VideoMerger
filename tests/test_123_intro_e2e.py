"""VideoMerger 1.2.3 e2e: optional Intro in the Stage-2 composition.

Verifies that the one-click pipeline renders Intro → Main → Outro, that the
Intro keeps only its own original audio (Mute/Low/Original), and that neither
Intro nor Outro receives subtitles, voiceover or background music. Mirrors the
acoustic probe technique of the 1.2.2 complete-workflow test.
"""

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


def _run(command: list[object], timeout: int = 240) -> bytes:
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
    sine = sum(value * math.sin(2 * math.pi * frequency * index / sample_rate) for index, value in enumerate(values))
    cosine = sum(value * math.cos(2 * math.pi * frequency * index / sample_rate) for index, value in enumerate(values))
    return math.sqrt(sine * sine + cosine * cosine) / max(1, len(values))


def _section_video(ffmpeg: Path, media: Path, start: float, duration: float, out: Path) -> Path:
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
        "-i", media, "-t", f"{duration:.3f}", "-c", "copy", out,
    ])
    return out


def _has_subtitle_stream(ffprobe: Path, media: Path) -> bool:
    output = _run([
        ffprobe, "-hide_banner", "-loglevel", "error", "-select_streams", "s",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(media),
    ])
    return bool(output.strip())


@pytest.mark.e2e
def test_one_click_intro_main_outro_audio_isolation_and_no_subtitles_on_intro_outro(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    main_a = tmp_path / "MainA.mp4"
    main_b = tmp_path / "MainB.mp4"
    make_clip(ffmpeg, main_a, size="320x180", duration=.7, color="blue", audio_rate=None)
    make_clip(ffmpeg, main_b, size="320x180", duration=.7, color="red", audio_rate=None)

    intro = tmp_path / "intro.mp4"
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=white:s=320x180:r=30:d=0.60",
        "-f", "lavfi", "-i", "sine=f=700:r=48000:d=0.60",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", intro,
    ])
    outro = tmp_path / "outro.mp4"
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30:d=0.60",
        "-f", "lavfi", "-i", "sine=f=1300:r=48000:d=0.60",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", outro,
    ])

    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, 850, 1.0, .65)
    script = tmp_path / "script.txt"
    script.write_text("Intro stays clean and keeps its own audio.", encoding="utf-8")
    timings = [
        ("Intro", .08, .18), ("stays", .20, .30), ("clean", .32, .44),
        ("and", .46, .54), ("keeps", .56, .68), ("its", .70, .78),
        ("own", .80, .88), ("audio", .90, .99),
    ]

    def recognize(path: Path, _language: str):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    aligner = LocalWordAligner("intro-fixture", recognize, cache_dir=tmp_path / "alignment-cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([main_a, main_b])
    settings = ExportSettings(
        resolution="320x180", encoding="CPU", preset="fast", crf=24, normalize_audio=False,
        transition_type="cross_dissolve", transition_duration=.10,
        voiceover_path=str(voice), script_path=str(script),
        original_audio_mode="mute", final_pause=.30,
        subtitle_enabled=True, subtitle_language="English", subtitle_style="long_1",
        intro_path=str(intro), intro_audio_mode="original",
        outro_path=str(outro), outro_audio_mode="original", outro_transition_enabled=False,
    )
    logs: list[str] = []
    result = MainProjectEngine(engine).create_complete(
        media, settings, tmp_path / "output", aligner=aligner, log=logs.append,
    )
    assert result.main.video.is_file() and result.main.report.ok
    assert result.final_video.is_file() and result.final_report.ok
    assert any(f"actual MainVideo input = {result.main.video}" in line for line in logs)
    assert any("Intro original audio mode: original" in line for line in logs)
    assert any("Intro/Outro receive no application voiceover" in line for line in logs)

    # Section durations: 0.60 intro + main (~1.40 + pause) + 0.60 outro with
    # one 0.10 transition; probe each section inside its own audio window.
    final = result.final_video
    duration = result.final_report.duration

    intro_window = _samples(ffmpeg, final, 0.10, 0.30)
    outro_window = _samples(ffmpeg, final, max(0.0, duration - 0.40), 0.30)
    main_window = _samples(ffmpeg, final, 1.30, 0.30)

    intro_700 = _frequency_strength(intro_window, 700)
    outro_1300 = _frequency_strength(outro_window, 1300)
    main_850 = _frequency_strength(main_window, 850)

    # Intro carries its own 700 Hz original audio (not muted, not replaced).
    assert intro_700 > 0.02, f"Intro original audio missing (700 Hz strength {intro_700:.4f})"
    # Outro carries its own 1300 Hz audio.
    assert outro_1300 > 0.02, f"Outro original audio missing (1300 Hz strength {outro_1300:.4f})"
    # Main section carries the voiceover tone.
    assert main_850 > 0.02, f"Main voiceover missing (850 Hz strength {main_850:.4f})"
    # The intro window must not contain the voiceover tone.
    assert _frequency_strength(intro_window, 850) < intro_700 * 0.6
    # The outro window must not contain the voiceover tone.
    assert _frequency_strength(outro_window, 850) < outro_1300 * 0.6

    # No subtitle stream inside the final composition (Intro/Outro and the
    # stage-2 composition never burn subtitles; SRT/VTT belong to Stage 1).
    assert not _has_subtitle_stream(ffprobe, final)

    # The canonical subtitle bundle still exists from Stage 1.
    assert result.main.srt and result.main.srt.is_file()
    assert result.main.vtt and result.main.vtt.is_file()


@pytest.mark.e2e
def test_stage2_intro_only_and_outro_only_both_render(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    main = tmp_path / "main.mp4"
    make_clip(ffmpeg, main, size="320x180", duration=.8, color="navy", audio_rate=None)
    intro = tmp_path / "intro_only.mp4"
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=white:s=320x180:r=30:d=0.50",
        "-f", "lavfi", "-i", "sine=f=700:r=48000:d=0.50",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", intro,
    ])
    engine = VideoMergerEngine(ffmpeg, ffprobe)

    # Intro only (no outro assigned).
    result = MainProjectEngine(engine).create_complete(
        [engine.analyze([main])[0]],
        ExportSettings(
            resolution="320x180", encoding="CPU", preset="fast", crf=24,
            normalize_audio=False, transition_duration=0.0,
            intro_path=str(intro), intro_audio_mode="mute",
        ),
        tmp_path / "intro_only_out",
    )
    assert result.final_video.is_file() and result.final_report.ok
    assert _has_subtitle_stream(ffprobe, result.final_video) is False

    # Outro only (no intro assigned) still works.
    outro = tmp_path / "outro_only.mp4"
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30:d=0.50",
        "-f", "lavfi", "-i", "sine=f=1300:r=48000:d=0.50",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", outro,
    ])
    result_outro = MainProjectEngine(engine).create_complete(
        [engine.analyze([main])[0]],
        ExportSettings(
            resolution="320x180", encoding="CPU", preset="fast", crf=24,
            normalize_audio=False, transition_duration=0.0,
            outro_path=str(outro), outro_audio_mode="low",
        ),
        tmp_path / "outro_only_out",
    )
    assert result_outro.final_video.is_file() and result_outro.final_report.ok
    assert _has_subtitle_stream(ffprobe, result_outro.final_video) is False
