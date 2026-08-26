from __future__ import annotations

import array
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord, script_word_spans
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.project_assets import probe_audio
from tests.conftest import make_clip


def _run(command, timeout=180):
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _tone(ffmpeg, path, duration):
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=f=850:r=48000:d={duration}", "-c:a", "pcm_s16le", path,
    ])


def _bright_caption_pixels(ffmpeg, image, width, height):
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", image,
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    count = 0
    for y in range(int(height * .62), height):
        for x in range(int(width * .08), int(width * .92)):
            offset = (y * width + x) * 3
            if min(raw[offset:offset + 3]) > 175:
                count += 1
    return count


def _speech_rms(ffmpeg, audio, start, end):
    midpoint = (start + end) / 2
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(max(0, midpoint - .08)),
        "-i", audio, "-t", "0.16", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1",
    ])
    values = array.array("f")
    values.frombytes(raw)
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


@pytest.mark.e2e
def test_voiceover_plus_script_auto_generates_tracks_burn_in_timeline_and_visual_frames(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "clip.mp4"
    make_clip(ffmpeg, clip, size="320x180", duration=3.2, color="navy", audio_rate=None)
    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, 2.5)
    script = "Alpha bravo charlie delta echo foxtrot."
    script_path = tmp_path / "script.txt"
    script_path.write_text(script, encoding="utf-8")
    measured = [
        ("Alpha", .10, .31), ("bravo", .43, .67), ("charlie", .79, 1.09),
        ("delta", 1.23, 1.49), ("echo", 1.64, 1.86), ("foxtrot", 2.02, 2.34),
    ]
    aligned_inputs = []
    def recognize_voice_only(path, _language):
        aligned_inputs.append(Path(path).resolve())
        return [RecognizedWord(w, a, b, .98) for w, a, b in measured], "en"
    aligner = LocalWordAligner("test", recognize_voice_only)
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    settings = ExportSettings(
        resolution="320x180", encoding="CPU", preset="fast", crf=28, normalize_audio=False,
        voiceover_path=str(voice), script_path=str(script_path), subtitle_enabled=False,
        subtitle_language="English", subtitle_style="long_1", final_pause=.5,
    )
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "output", aligner=aligner
    )
    assert result.report.ok
    assert aligned_inputs == [voice.resolve()]  # ASR receives voiceover, never video clips.
    assert result.srt.is_file() and result.vtt.is_file()
    assert result.canonical_timeline.is_file()
    assert len(result.verification_frames) == 3
    assert all(path.is_file() for path in result.verification_frames)
    assert "subtitles=filename=" in engine.last_filter_graph
    assert any("Burned-in subtitle filter executed" in detail for detail in result.report.details)
    assert "Alpha bravo charlie delta echo foxtrot." in result.srt.read_text(encoding="utf-8")
    assert result.vtt.read_text(encoding="utf-8").startswith("WEBVTT")
    canonical = json.loads(result.canonical_timeline.read_text(encoding="utf-8"))
    assert canonical["authoritative_script"] == script
    assert [word["text"] for word in canonical["words"]] == [
        token for token, _a, _b in script_word_spans(script)
    ]
    for index in (0, len(measured) // 2, len(measured) - 1):
        assert canonical["words"][index]["start"] == pytest.approx(measured[index][1])
    assert all(_bright_caption_pixels(ffmpeg, frame, 320, 180) > 8 for frame in result.verification_frames)


@pytest.mark.e2e
def test_subtitle_failure_is_explicit_and_leaves_no_captionless_video(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "clip.mp4"
    make_clip(ffmpeg, clip, size="160x90", duration=1, audio_rate=None)
    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, .8)
    script = tmp_path / "script.txt"
    script.write_text("This must fail visibly.", encoding="utf-8")
    aligner = LocalWordAligner("test", lambda _p, _l: ([], "en"))
    media = VideoMergerEngine(ffmpeg, ffprobe).analyze([clip])
    output = tmp_path / "output"
    with pytest.raises(Exception, match=r"^SUBTITLE GENERATION FAILED \[local ASR / word alignment\]"):
        MainProjectEngine(VideoMergerEngine(ffmpeg, ffprobe)).create_main(
            media,
            ExportSettings(
                resolution="160x90", encoding="CPU", preset="fast",
                voiceover_path=str(voice), script_path=str(script), subtitle_enabled=True,
                subtitle_language="English",
            ),
            output,
            aligner=aligner,
        )
    assert not list(output.glob("*.mp4"))
    assert not list(output.glob("*.srt"))
    assert not list(output.glob("*.vtt"))


@pytest.mark.e2e
def test_real_local_speech_first_middle_final_word_timing_and_burned_frames(ffmpeg_paths, tmp_path):
    if os.environ.get("VIDEOMERGER_TEST_REAL_ALIGNMENT") != "1":
        pytest.skip("set VIDEOMERGER_TEST_REAL_ALIGNMENT=1 for the real acoustic E2E")
    espeak = shutil.which("espeak-ng")
    if not espeak:
        pytest.skip("espeak-ng not available")
    ffmpeg, ffprobe = ffmpeg_paths
    script = "Alpha bravo charlie delta echo foxtrot golf hotel india."
    voice = tmp_path / "known_voice.wav"
    subprocess.run(
        [espeak, "-v", "en", "-s", "125", "-w", str(voice), script],
        check=True, timeout=30,
    )
    duration = probe_audio(ffprobe, voice).duration
    clip = tmp_path / "background.mp4"
    make_clip(ffmpeg, clip, size="640x360", duration=duration + .8, color="navy", audio_rate=None)
    script_path = tmp_path / "script.txt"
    script_path.write_text(script, encoding="utf-8")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    model = os.environ.get("VIDEOMERGER_TEST_ALIGNMENT_MODEL", "small")
    result = MainProjectEngine(engine).create_main(
        media,
        ExportSettings(
            resolution="640x360", encoding="CPU", preset="fast", crf=26, normalize_audio=False,
            voiceover_path=str(voice), script_path=str(script_path), subtitle_enabled=True,
            subtitle_language="English", subtitle_model=model, subtitle_style="long_1",
            allow_alignment_warnings=True, final_pause=.5,
        ),
        tmp_path / "output",
        aligner=LocalWordAligner(model, cache_dir=tmp_path / "alignment-cache"),
    )
    canonical = json.loads(result.canonical_timeline.read_text(encoding="utf-8"))
    words = canonical["words"]
    assert [item["text"] for item in words] == [token for token, _a, _b in script_word_spans(script)]
    indexes = [0, len(words) // 2, len(words) - 1]
    selected = [words[index] for index in indexes]
    assert 0 <= selected[0]["start"] < selected[1]["start"] < selected[2]["start"] < duration
    # Every selected acoustic timestamp points at actual non-silent speech.
    assert all(_speech_rms(ffmpeg, voice, item["start"], item["end"]) > .002 for item in selected)
    # Every corresponding frame was decoded from the final MP4 and visibly has
    # bright caption glyphs over the known dark background.
    assert all(_bright_caption_pixels(ffmpeg, frame, 640, 360) > 20 for frame in result.verification_frames)
    assert "subtitles=filename=" in engine.last_filter_graph
    assert result.srt.is_file() and result.vtt.is_file()
