from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.project_assets import probe_audio
from tests.conftest import make_clip


BENCHMARK_SENTENCES = [
    "Today we measure the complete local video workflow with clear acoustic speech and reliable timing.",
    "Every spoken word receives a real timestamp derived from the supplied voiceover audio.",
    "The exact script remains authoritative for spelling punctuation capitalization and visible subtitle wording.",
    "Four manually ordered video clips are analyzed once and then combined with smooth transitions.",
    "Background music repeats only during the spoken program and ends before the quiet pause.",
    "Voiceover audio is never duplicated replaced or stretched by the application during final rendering.",
    "The subtitle timeline produces valid S R T and web V T T files locally.",
    "Burned captions are applied inside the same final filter graph as video and audio processing.",
    "Expensive speech recognition results are cached independently from the selected subtitle visual style.",
    "Changing a font color position or preset cannot trigger another transcription of identical audio.",
    "Media metadata is cached safely using the full path file size and modification timestamp.",
    "Transition blur runs only near visible boundaries instead of processing every frame for two minutes.",
    "Portrait background blur uses a reduced working canvas before returning to the final resolution.",
    "The output keeps synchronized video speech music and original audio at forty eight kilohertz.",
    "A quiet final interval remains free from generated music speech and burned subtitle events.",
    "Validation checks duration dimensions frame rate codecs pixel format aspect ratio and audio streams.",
    "The application reports separate timings for analysis voice processing recognition alignment and subtitles.",
    "It also measures the final encode output validation visual verification and complete pipeline duration.",
    "All processing remains on this computer without a cloud transcription service or rendering provider.",
    "This benchmark represents approximately two minutes of real program material with captions and music.",
    "The final frames verify the first middle and last spoken portions directly from the encoded movie.",
    "A successful result must be measurable reproducible and visibly different from a captionless export.",
]


def _run(command, timeout=600):
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _atempo_filters(factor: float) -> str:
    values: list[float] = []
    while factor < .5:
        values.append(.5)
        factor /= .5
    while factor > 2:
        values.append(2.0)
        factor /= 2.0
    values.append(factor)
    return ",".join(f"atempo={value:.8f}" for value in values)


@pytest.mark.benchmark
@pytest.mark.e2e
def test_measured_two_minute_voice_subtitle_music_final_render(ffmpeg_paths, tmp_path):
    if os.environ.get("VIDEOMERGER_RUN_2MIN_BENCHMARK") != "1":
        pytest.skip("set VIDEOMERGER_RUN_2MIN_BENCHMARK=1 for measured two-minute benchmark")
    espeak = shutil.which("espeak-ng")
    ffmpeg, ffprobe = ffmpeg_paths
    raw_voice = tmp_path / "benchmark_raw.wav"
    voice = tmp_path / "benchmark_voice_119s.wav"
    if espeak:
        script = " ".join(BENCHMARK_SENTENCES)
        subprocess.run(
            [espeak, "-v", "en", "-s", "132", "-w", str(raw_voice), script],
            check=True, timeout=120,
        )
        raw_duration = probe_audio(ffprobe, raw_voice).duration
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", raw_voice,
            "-af", _atempo_filters(raw_duration / 119.0), "-t", "119", "-ar", "48000", voice,
        ])
    else:
        # Native Windows does not normally provide espeak. The release carries
        # a short known spoken evidence fixture; repeat that *voiceover* and the
        # exact matching script solely to make the opt-in benchmark reproducible.
        evidence = Path(__file__).resolve().parents[1] / "test_evidence" / "1.2.1" / "subtitle_workflow" / "assets"
        seed_voice = evidence / "KnownVoiceover.wav"
        seed_script = (evidence / "script.txt").read_text(encoding="utf-8").strip()
        if not seed_voice.is_file():
            pytest.skip("benchmark voice fixture and espeak-ng are both unavailable")
        repeats = 24
        script = " ".join([seed_script] * repeats)
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-stream_loop", "-1",
            "-i", seed_voice, "-t", "119", "-ar", "48000", voice,
        ])
    script_path = tmp_path / "benchmark_script.txt"
    script_path.write_text(script, encoding="utf-8")
    voice_duration = probe_audio(ffprobe, voice).duration
    assert voice_duration == pytest.approx(119.0, abs=.08)

    folder = tmp_path / "clips"
    ordered = []
    for name, color in (("C", "navy"), ("A", "maroon"), ("D", "teal"), ("B", "purple")):
        path = folder / f"{name}.mp4"
        make_clip(ffmpeg, path, size="1280x720", duration=30.0, color=color, audio_rate=None)
        ordered.append(path)
    music = tmp_path / "music.mp3"
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "sine=f=220:r=48000:d=8", "-c:a", "libmp3lame", "-b:a", "160k", music,
    ])

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    analysis_started = time.perf_counter()
    media = engine.analyze(ordered)
    analysis_seconds = time.perf_counter() - analysis_started
    model = os.environ.get("VIDEOMERGER_TEST_ALIGNMENT_MODEL", "small")
    result = MainProjectEngine(engine).create_main(
        media,
        ExportSettings(
            resolution="1280x720", encoding="CPU", preset="slow", crf=18,
            normalize_audio=True, transition_type="smooth_blur", transition_duration=.5,
            voiceover_path=str(voice), script_path=str(script_path), music_path=str(music),
            original_audio_mode="mute", music_volume=22, ducking_enabled=True,
            final_pause=1.0, short_video_mode="hold", subtitle_enabled=True,
            subtitle_language="English", subtitle_model=model, subtitle_style="long_1",
            allow_alignment_warnings=True,
        ),
        tmp_path / "output",
        aligner=LocalWordAligner(model, cache_dir=tmp_path / "benchmark-alignment-cache"),
    )
    assert result.report.duration == pytest.approx(120.0, abs=.10)
    assert result.srt.is_file() and result.vtt.is_file()
    assert len(result.verification_frames) == 3
    assert "subtitles=filename=" in engine.last_filter_graph

    timings = dict(result.timings)
    timings["video_analysis_seconds"] = analysis_seconds
    measured_total = analysis_seconds + float(timings["total_pipeline_seconds"])
    timings["measured_analysis_to_final_seconds"] = measured_total
    timings["measured_analysis_to_final_minutes"] = measured_total / 60.0
    timings["benchmark_resolution"] = "1280x720"
    timings["benchmark_duration_seconds"] = result.report.duration
    timings["benchmark_encoder"] = result.report.details[-1] if result.report.details else "CPU libx264"
    timings["baseline_user_observation_minutes"] = 60.0
    timings["improvement_vs_observed_percent"] = (60.0 - measured_total / 60.0) / 60.0 * 100.0
    evidence = Path(os.environ.get(
        "VIDEOMERGER_BENCHMARK_RESULT",
        str(tmp_path / "benchmark_2min_result.json"),
    ))
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(timings, indent=2, ensure_ascii=False), encoding="utf-8")
    assert float(timings["ffmpeg_rendering_seconds"]) > 0
    assert float(timings["asr_seconds"]) > 0
