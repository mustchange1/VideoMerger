from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.project_order import ProjectOrderStore
from app.video_merger.project_assets import probe_audio
from tests.conftest import make_clip


def _run(command, timeout=120):
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _audio(ffmpeg, path, frequency, duration, rate=48000, codec="pcm_s16le"):
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=f={frequency}:r={rate}:d={duration}", "-c:a", codec, path,
    ])


def _watermark(ffmpeg, path):
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "color=yellow:s=40x20:d=0.1", "-frames:v", "1", path,
    ])


def _frame(ffmpeg, video, at, width=160, height=90):
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", video,
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    assert len(raw) == width * height * 3
    return raw


def _pixel(frame, width, x, y):
    offset = (y * width + x) * 3
    return tuple(frame[offset:offset + 3])


def _samples(ffmpeg, video, start=None, tail=None):
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if tail is not None:
        command += ["-sseof", str(-tail)]
    elif start is not None:
        command += ["-ss", str(start)]
    command += ["-i", video, "-map", "0:a:0", "-t", "0.35", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"]
    raw = _run(command)
    import array
    values = array.array("f")
    values.frombytes(raw)
    return values


def _rms(values):
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def _frequency_strength(values, frequency, sample_rate=48000):
    # Correlation at one deterministic test frequency; enough to tell the music
    # tone from the voiceover tone without an optional NumPy dependency.
    sine = 0.0
    cosine = 0.0
    for index, value in enumerate(values):
        angle = 2.0 * math.pi * frequency * index / sample_rate
        sine += value * math.sin(angle)
        cosine += value * math.cos(angle)
    return math.hypot(sine, cosine) / max(1, len(values))


def _tone(values, frequency, rate=48000):
    # Direct DFT at one frequency is sufficient for these short deterministic tones.
    real = imag = 0.0
    for index, value in enumerate(values):
        angle = 2 * math.pi * frequency * index / rate
        real += value * math.cos(angle)
        imag -= value * math.sin(angle)
    return math.hypot(real, imag) / max(1, len(values))


def _engine(ffmpeg_paths):
    return VideoMergerEngine(*ffmpeg_paths)


def _basic_assets(ffmpeg, tmp_path, clip_duration=1.0):
    folder = tmp_path / "videos"
    for name, color, tone in (("A.mp4", "red", 430), ("B.mp4", "lime", 540), ("C.mp4", "blue", 650)):
        make_clip(ffmpeg, folder / name, size="160x90", duration=clip_duration, color=color, audio_rate=48000)
    return folder


@pytest.mark.e2e
def test_complete_two_stage_workflow_with_exact_order_subtitles_music_watermark_and_clean_outro(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = _basic_assets(ffmpeg, tmp_path)
    # Persist a manual non-alphabetical order and pass that exact active order.
    store = ProjectOrderStore(tmp_path / "order.json")
    detected = [folder / "A.mp4", folder / "B.mp4", folder / "C.mp4"]
    store.order(folder, detected)
    manual = [folder / "C.mp4", folder / "A.mp4", folder / "B.mp4"]
    store.set_active_order(folder, manual)
    active = store.order(folder, list(reversed(detected)))
    assert active == manual

    voice = tmp_path / "voice.wav"
    script = "Das ist ein genauer Test mit Umlauten."
    # espeak is exercised separately by the real aligner test; this deterministic
    # voice fixture keeps the complete render fast while preserving actual audio.
    _audio(ffmpeg, voice, 900, 2.2, 32000)
    script_path = tmp_path / "script.txt"
    script_path.write_text(script, encoding="utf-8")
    music = tmp_path / "music.mp3"
    _audio(ffmpeg, music, 220, .45, 22050, "libmp3lame")
    mark = tmp_path / "watermark.png"
    _watermark(ffmpeg, mark)

    timings = [
        ("Das", .10, .30), ("ist", .34, .48), ("ein", .52, .68),
        ("genauer", .72, 1.02), ("Test", 1.08, 1.30), ("mit", 1.36, 1.52),
        ("Umlauten", 1.58, 1.95),
    ]
    aligner = LocalWordAligner(
        "tiny", lambda _p, _l: ([RecognizedWord(w, a, b, .97) for w, a, b in timings], "de")
    )
    engine = _engine(ffmpeg_paths)
    media = engine.analyze(active)
    settings = ExportSettings(
        resolution="160x90", encoding="CPU", preset="fast", crf=24, normalize_audio=False,
        transition_type="cross_dissolve", transition_duration=.2,
        voiceover_path=str(voice), script_path=str(script_path), music_path=str(music),
        original_audio_mode="mute", music_volume=28, ducking_enabled=True, final_pause=.5,
        subtitle_enabled=True, subtitle_language="German", subtitle_style="long_1",
        subtitle_position="Bottom", subtitle_model="tiny",
        watermark_enabled=True, watermark_path=str(mark), watermark_position="top_right",
        watermark_scope="both", watermark_size=12, watermark_opacity=80,
    )
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "output", aligner=aligner
    )
    assert result.report.ok and result.srt.is_file() and result.vtt.is_file()
    assert result.report.duration == pytest.approx(2.7, abs=.08)
    assert script in result.srt.read_text(encoding="utf-8")
    assert "WEBVTT" in result.vtt.read_text(encoding="utf-8")
    assert result.alignment.words[0].start == pytest.approx(.10)
    # The voiceover stops at the spoken-program boundary, but the looped music
    # covers the COMPLETE video: it is still audible in the configured final
    # half-second (music tone 220 Hz) and no voiceover (900 Hz) reaches into it,
    # so the visual outro is never silent and never contains speech.
    outro_audio = _samples(ffmpeg, result.video, start=2.35)
    assert _rms(outro_audio) > .003
    assert _frequency_strength(outro_audio, 220) > _frequency_strength(outro_audio, 900) * 4

    frame = _frame(ffmpeg, result.video, .2)
    center = _pixel(frame, 160, 80, 45)
    assert center[2] > center[0] * 2  # manual order starts with blue C
    mark_pixel = _pixel(frame, 160, 150, 8)
    assert mark_pixel[0] > 120 and mark_pixel[1] > 100
    # Burned captions produce bright pixels in the safe bottom region.
    bright = 0
    for y in range(68, 87):
        for x in range(20, 140):
            if min(_pixel(frame, 160, x, y)) > 180:
                bright += 1
    assert bright > 5

    outro = tmp_path / "Outro.mp4"
    make_clip(ffmpeg, outro, size="160x90", duration=.8, color="magenta", audio_rate=None)
    # Replace its silence with a deterministic 1200 Hz original audio track.
    outro_audio = tmp_path / "outro_audio.wav"
    _audio(ffmpeg, outro_audio, 1200, .8)
    with_audio = tmp_path / "Outro_with_audio.mp4"
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", outro, "-i", outro_audio,
        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", with_audio,
    ])
    settings.main_video_path = str(result.video)
    settings.outro_path = str(with_audio)
    settings.outro_audio_mode = "original"
    final, final_report = MainProjectEngine(engine).add_outro(settings, tmp_path / "output")
    assert final_report.ok
    assert final_report.duration > result.report.duration
    assert result.srt.read_text(encoding="utf-8").count("-->") == 1

    tail = _samples(ffmpeg, final, tail=.30)
    assert _tone(tail, 1200) > _tone(tail, 900) * 3
    assert _tone(tail, 1200) > _tone(tail, 220) * 3
    outro_frame = _frame(ffmpeg, final, final_report.duration - .12)
    bottom = _pixel(outro_frame, 160, 80, 78)
    assert bottom[0] > 100 and bottom[2] > 100  # magenta, no white subtitle


@pytest.mark.e2e
def test_voiceover_only_music_only_short_loop_long_trim_and_missing_roles(ffmpeg_paths, tmp_path):
    ffmpeg, _ = ffmpeg_paths
    folder = _basic_assets(ffmpeg, tmp_path, .8)
    engine = _engine(ffmpeg_paths)
    media = engine.analyze(sorted(folder.glob("*.mp4")))
    voice = tmp_path / "voice.wav"
    _audio(ffmpeg, voice, 900, 1.7)
    short_music = tmp_path / "short.mp3"
    long_music = tmp_path / "long.mp3"
    _audio(ffmpeg, short_music, 220, .25, codec="libmp3lame")
    _audio(ffmpeg, long_music, 330, 8.0, codec="libmp3lame")

    base = dict(resolution="160x90", encoding="CPU", preset="fast", crf=28, normalize_audio=False, final_pause=.5)
    voice_result = MainProjectEngine(engine).create_main(
        media, ExportSettings(**base, voiceover_path=str(voice)), tmp_path / "voice_only"
    )
    assert voice_result.report.duration == pytest.approx(2.2, abs=.08)
    short_result = MainProjectEngine(engine).create_main(
        media, ExportSettings(**base, music_path=str(short_music)), tmp_path / "music_short"
    )
    long_result = MainProjectEngine(engine).create_main(
        media, ExportSettings(**base, music_path=str(long_music)), tmp_path / "music_long"
    )
    basic_result = MainProjectEngine(engine).create_main(
        media, ExportSettings(**base), tmp_path / "missing_optional"
    )
    assert all(result.report.ok for result in (short_result, long_result, basic_result))
    # Short music is still audible near the end, proving loop; long music is
    # trimmed to exactly the visual program rather than extending the output.
    assert _rms(_samples(ffmpeg, short_result.video, start=short_result.report.duration - .45)) > .003
    assert long_result.report.duration == pytest.approx(basic_result.report.duration, abs=.08)


@pytest.mark.e2e
def test_original_audio_mute_low_original_gain_staging(ffmpeg_paths, tmp_path):
    ffmpeg, _ = ffmpeg_paths
    folder = tmp_path / "clips"
    make_clip(ffmpeg, folder / "A.mp4", size="160x90", duration=1.0, color="red", audio_rate=48000)
    engine = _engine(ffmpeg_paths)
    media = engine.analyze([folder / "A.mp4"])
    levels = {}
    for mode in ("mute", "low", "original"):
        settings = ExportSettings(
            resolution="160x90", encoding="CPU", preset="fast", crf=28,
            normalize_audio=False, original_audio_mode=mode,
        )
        result = MainProjectEngine(engine).create_main(media, settings, tmp_path / mode)
        levels[mode] = _rms(_samples(ffmpeg, result.video, start=.2))
    assert levels["mute"] < .001
    assert levels["low"] > levels["mute"] * 5
    assert levels["original"] > levels["low"] * 2.5


@pytest.mark.e2e
def test_voiceover_ducking_reduces_music_tone_smoothly(ffmpeg_paths, tmp_path):
    ffmpeg, _ = ffmpeg_paths
    folder = tmp_path / "clips"
    make_clip(ffmpeg, folder / "A.mp4", size="160x90", duration=2.0, color="red", audio_rate=None)
    voice, music = tmp_path / "voice.wav", tmp_path / "music.wav"
    _audio(ffmpeg, voice, 900, 1.8)
    _audio(ffmpeg, music, 220, .4)
    engine = _engine(ffmpeg_paths)
    media = engine.analyze([folder / "A.mp4"])
    outputs = {}
    for enabled in (False, True):
        settings = ExportSettings(
            resolution="160x90", encoding="CPU", preset="fast", crf=28, normalize_audio=False,
            voiceover_path=str(voice), music_path=str(music), original_audio_mode="mute",
            music_volume=60, ducking_enabled=enabled, final_pause=.5,
        )
        outputs[enabled] = MainProjectEngine(engine).create_main(
            media, settings, tmp_path / ("duck" if enabled else "plain")
        ).video
    plain_samples = _samples(ffmpeg, outputs[False], start=.5)
    ducked_samples = _samples(ffmpeg, outputs[True], start=.5)
    plain = _tone(plain_samples, 220)
    ducked = _tone(ducked_samples, 220)
    assert ducked < plain * .65
    assert max(abs(value) for value in ducked_samples) < 1.0
