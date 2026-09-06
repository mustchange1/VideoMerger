"""Phase 23 – the music timeline and the bounded verification on REAL renders.

The fast suite proves the generated filter graph; this module proves the measured
media produced by the real product pipeline (``create_youtube_exports``):

* background music starts at ``0.000 s`` and stays continuous through the visual
  intro, the voiceover and the visual outro up to the final frame,
* voiceover audio and captions stay inside the spoken section only,
* the Short reuses its single 0.7 s outro instead of stacking a second ending and
  uses only its own track,
* per-output music volume and transition settings are really applied in one
  combined run,
* a completed, probed and validated render is never discarded because an optional
  verification PNG failed – the bounded verification succeeds exactly where the
  old seek at the file end produced no PNG at all.

The music tracks are deliberately far shorter than the video (0.6 s for a 4.4 s /
6.0 s program), so every render here really loops. FFmpeg 6.0 deadlocks at 0 %
CPU when a ``-stream_loop -1`` input has to feed a branch right up to the output
end, which is why the product reads the looped source only for the spoken program
and covers the visual outro with a bounded in-graph ``aloop`` repeat of that
trimmed music. Each render is therefore wall-clock bounded: a regression of that
shape fails with a clear message instead of hanging the suite.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import subprocess
import time
from pathlib import Path

import pytest

from app.video_merger.alignment import (
    LocalWordAligner,
    RecognizedWord,
    script_word_spans,
)
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import AlignmentResult, ExportSettings, WordTiming
from app.video_merger.render_cache import Stage1RenderCache
from app.video_merger.subtitle_verification import (
    frame_safe_margin,
    png_frame_status,
    verify_subtitle_frames,
)
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
)

SAMPLE_RATE = 48000
VOICE_HZ = 900
LONG_FORM_MUSIC_HZ = 220
SHORTS_MUSIC_HZ = 260
SPOKEN_SECONDS = 3.0
LONG_FORM_INTRO = 1.5
LONG_FORM_OUTRO = 1.5
SHORT_INTRO = 0.7
SHORT_OUTRO = 0.7
# A real render of these 160x90 / 90x160 test clips finishes in about a second;
# the bound exists so a deadlocked FFmpeg (0 % CPU, no progress) fails the test
# instead of hanging, which is the exact regression the loop shape guards against.
RENDER_BUDGET_SECONDS = 90.0
# The background is darkened but stays clearly above black, while burned caption
# glyphs stay white: bright pixels can only come from the subtitle track.
# The source's white point is clamped far below the caption threshold, so a
# bright pixel in a rendered frame can only come from the burned subtitle track;
# the clamped pattern still averages ~50/255, which is dark but never black.
CLIP_WHITE_POINT = 0.45
CAPTION_BRIGHTNESS = 170
MINIMUM_SECTION_BRIGHTNESS = 8
# A burned cue leaves ~150-190 bright pixels in these frames, while the darkened
# test pattern leaves at most a handful of them: the two cases are unambiguous.
MINIMUM_CAPTION_PIXELS = 40
MAXIMUM_BACKGROUND_PIXELS = 8
SCRIPT = "Dies ist ein Test der Musik und der visuellen Abschnitte."


def _run(command: list[str], timeout: int = 180) -> bytes:
    result = subprocess.run(
        command, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False
    )
    assert result.returncode == 0, (
        f"{' '.join(str(item) for item in command[:6])}…\n"
        f"{result.stderr.decode('utf-8', 'replace')[-800:]}"
    )
    return result.stdout


def _tone(ffmpeg: Path, path: Path, frequency: int, seconds: float) -> Path:
    """A pure sine track; its measured amplitude identifies the source later."""
    _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=frequency={frequency}:sample_rate={SAMPLE_RATE}:duration={seconds}",
        "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", str(path),
    ])
    return path


def _clips(ffmpeg: Path, folder: Path, count: int, size: str) -> list[Path]:
    """Dark but moving test material.

    Motion is required to prove a visual section is not a frozen frame, and the
    deliberately dark background is required to prove burned captions really are
    present: caption glyphs are the only bright pixels in these renders.
    """
    folder.mkdir(parents=True, exist_ok=True)
    created = []
    for index in range(count):
        path = folder / f"clip_{index}.mp4"
        _run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            (
                f"testsrc2=size={size}:rate=30:duration=2.5,hue=h={index * 47},"
                f"colorlevels=romax={CLIP_WHITE_POINT}:gomax={CLIP_WHITE_POINT}"
                f":bomax={CLIP_WHITE_POINT}"
            ),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ])
        created.append(path)
    return created


def _probe(ffprobe: Path, path: Path) -> dict:
    return json.loads(_run([
        str(ffprobe), "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", str(path),
    ]).decode("utf-8"))


def _samples(ffmpeg: Path, path: Path, start: float, seconds: float) -> list[float]:
    raw = _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{start}", "-i", str(path),
        "-t", f"{seconds}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
    ])
    values = array.array("f")
    values.frombytes(raw)
    return list(values)


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def _strength(values: list[float], frequency: float) -> float:
    """Goertzel-style projection onto one frequency: which source is audible?"""
    sine = cosine = 0.0
    for index, value in enumerate(values):
        angle = 2.0 * math.pi * frequency * index / SAMPLE_RATE
        sine += value * math.sin(angle)
        cosine += value * math.cos(angle)
    return math.hypot(sine, cosine) / max(1, len(values))


def _bright_pixels(ffmpeg: Path, path: Path, at: float, threshold: int = CAPTION_BRIGHTNESS) -> int:
    """Count pixels brighter than the background: burned caption glyphs."""
    raw = _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{at}", "-i", str(path),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ])
    assert raw, f"no frame decoded at {at:.3f} s"
    return sum(1 for value in raw if value >= threshold)


def _frame_signature(ffmpeg: Path, path: Path, at: float) -> tuple[str, float]:
    """Decode one frame and return (hash, mean brightness) to detect frozen/black."""
    raw = _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{at}", "-i", str(path),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ])
    assert raw, f"no frame decoded at {at:.3f} s"
    return hashlib.sha256(raw).hexdigest(), sum(raw) / len(raw)


def _cue_window(srt: Path) -> tuple[float, float, int]:
    def seconds(value: str) -> float:
        hours, minutes, rest = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(rest.replace(",", "."))

    stamps = [
        (seconds(start), seconds(end))
        for start, end in (
            line.split(" --> ")
            for line in srt.read_text(encoding="utf-8").splitlines()
            if " --> " in line
        )
    ]
    assert stamps, f"{srt.name} contains no cue"
    return stamps[0][0], stamps[-1][1], len(stamps)


def _aligner(spoken: float) -> LocalWordAligner:
    """Deterministic word timing over the authoritative script (no faster-whisper)."""
    tokens = script_word_spans(SCRIPT)
    slot = spoken / len(tokens)
    words = [
        RecognizedWord(
            token.strip(".,") or token,
            round(index * slot + 0.05, 3),
            round(min(spoken - 0.02, index * slot + 0.05 + slot * 0.8), 3),
            0.95,
        )
        for index, (token, _start, _end) in enumerate(tokens)
    ]
    return LocalWordAligner("tiny", lambda _path, _language: (list(words), "de"), use_cache=False)


def _base_settings(**overrides) -> ExportSettings:
    values = {
        "voiceover_paths": [],
        "script_mode": "single",
        "subtitle_enabled": True,
        "subtitle_output_mode": "with_subtitles",
        "subtitle_language": "German",
        "original_audio_mode": "mute",
        "normalize_audio": False,
        "ducking_enabled": False,
        "video_order_mode": "natural",
        "workflow_stage": "main",
        "encoding": "CPU",
        "quality_preset": "custom",
        "crf": 32,
        "preset": "ultrafast",
    }
    values.update(overrides)
    return ExportSettings(**values)


def _render(ffmpeg: Path, ffprobe: Path, work: Path, settings: ExportSettings,
            clips: list[Path]):
    """Run the real product pipeline and return (result, logs, elapsed seconds)."""
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(clips, lambda _message: None)
    logs: list[str] = []
    project = MainProjectEngine(engine, render_cache=Stage1RenderCache(work / "stage1-cache"))
    started = time.perf_counter()
    result = project.create_youtube_exports(
        media, settings, work / "output", aligner=_aligner(SPOKEN_SECONDS), log=logs.append
    )
    elapsed = time.perf_counter() - started
    assert elapsed < RENDER_BUDGET_SECONDS, (
        f"render took {elapsed:.1f} s: a looped music input that has to reach the "
        "output end deadlocks FFmpeg instead of finishing"
    )
    return result, logs, elapsed


def _voice_and_script(work: Path, ffmpeg: Path) -> tuple[Path, Path]:
    voice = _tone(ffmpeg, work / "voice.wav", VOICE_HZ, SPOKEN_SECONDS)
    script = work / "script.txt"
    script.write_text(SCRIPT, encoding="utf-8")
    return voice, script


def _assert_streams(ffprobe: Path, video: Path, *, width: int, height: int,
                    duration: float, sar: str = "1:1") -> dict:
    info = _probe(ffprobe, video)
    stream = next(item for item in info["streams"] if item["codec_type"] == "video")
    audio = next(item for item in info["streams"] if item["codec_type"] == "audio")
    assert (stream["width"], stream["height"]) == (width, height)
    assert stream.get("sample_aspect_ratio") == sar
    numerator, denominator = stream["avg_frame_rate"].split("/")
    assert round(float(numerator) / float(denominator)) == 30
    assert abs(float(info["format"]["duration"]) - duration) <= 0.08
    assert float(audio["duration"]) >= duration - 0.15, "audio stops before the video end"
    return info


def _assert_music_contract(ffmpeg: Path, ffprobe: Path, video: Path, *, intro: float,
                           outro: float, music_hz: int, size: tuple[int, int],
                           label: str) -> dict:
    """Measure the complete audio contract of one real render."""
    expected = intro + SPOKEN_SECONDS + outro
    info = _assert_streams(ffprobe, video, width=size[0], height=size[1], duration=expected)
    duration = float(info["format"]["duration"])

    opening = _samples(ffmpeg, video, 0.0, 0.15)
    intro_audio = _samples(ffmpeg, video, 0.05, max(0.2, intro - 0.1))
    speech_audio = _samples(ffmpeg, video, intro + 0.2, 0.6)
    outro_audio = _samples(ffmpeg, video, expected - outro + 0.15, max(0.2, outro - 0.3))
    closing = _samples(ffmpeg, video, max(0.0, duration - 0.20), 0.15)

    # The video never starts silent: music is the only source at 0.000 s.
    assert _rms(opening) > 0.01, f"{label}: the video starts silent"
    assert _strength(opening, music_hz) > 4 * _strength(opening, VOICE_HZ), (
        f"{label}: the opening is not background music")
    # Music runs through the visual intro, which carries no voiceover.
    assert _strength(intro_audio, music_hz) > 0.01, f"{label}: no music in the visual intro"
    assert _strength(intro_audio, VOICE_HZ) < _strength(intro_audio, music_hz) / 4, (
        f"{label}: voiceover inside the visual intro")
    # Music stays subordinate while the voiceover speaks.
    assert _strength(speech_audio, VOICE_HZ) > 0.01, f"{label}: no voiceover after the intro"
    assert _strength(speech_audio, VOICE_HZ) > _strength(speech_audio, music_hz), (
        f"{label}: music is louder than the voiceover")
    assert _strength(speech_audio, music_hz) > 0.005, f"{label}: music stops during the voiceover"
    # Music runs through the visual outro to the final frame, without speech.
    assert _rms(outro_audio) > 0.01, f"{label}: the visual outro is silent"
    assert _strength(outro_audio, music_hz) > 0.01, f"{label}: no music in the visual outro"
    assert _strength(outro_audio, VOICE_HZ) < _strength(outro_audio, music_hz) / 4, (
        f"{label}: voiceover inside the visual outro")
    assert _rms(closing) > 0.01, f"{label}: music does not reach the final video frame"

    # Continuity inside the visual outro: not one 50 ms window may lose the track.
    holes = []
    cursor = expected - outro + 0.05
    while cursor < expected - 0.06:
        if _strength(_samples(ffmpeg, video, cursor, 0.05), music_hz) < 0.01:
            holes.append(round(cursor, 3))
        cursor += 0.05
    assert not holes, f"{label}: music has a hole in the visual outro at {holes}"

    # The visual sections must play moving material, never black or a frozen frame.
    first_signature, first_brightness = _frame_signature(ffmpeg, video, expected - outro + 0.2)
    later_signature, later_brightness = _frame_signature(ffmpeg, video, expected - 0.15)
    assert first_brightness > MINIMUM_SECTION_BRIGHTNESS, f"{label}: black frame in the outro"
    assert later_brightness > MINIMUM_SECTION_BRIGHTNESS, f"{label}: black frame at the video end"
    assert first_signature != later_signature, f"{label}: the visual outro is a frozen frame"

    # Burned captions are really visible while speaking, and really absent in
    # both visual sections (measured on decoded frames, not on the SRT alone).
    caption_pixels = _bright_pixels(ffmpeg, video, intro + SPOKEN_SECONDS * 0.5)
    assert caption_pixels > MINIMUM_CAPTION_PIXELS, (
        f"{label}: no burned caption visible while speaking ({caption_pixels} bright pixels)")
    intro_pixels = _bright_pixels(ffmpeg, video, intro * 0.5)
    assert intro_pixels <= MAXIMUM_BACKGROUND_PIXELS, (
        f"{label}: a caption is burned into the visual intro ({intro_pixels} bright pixels)")
    outro_pixels = _bright_pixels(ffmpeg, video, expected - outro * 0.5)
    assert outro_pixels <= MAXIMUM_BACKGROUND_PIXELS, (
        f"{label}: a caption is burned into the visual outro ({outro_pixels} bright pixels)")

    first_cue, last_cue, cues = _cue_window(video.with_suffix(".srt"))
    assert cues >= 1
    # Captions live exactly inside the spoken section.
    assert first_cue >= intro - 0.06, f"{label}: a caption starts inside the visual intro"
    assert last_cue <= intro + SPOKEN_SECONDS + 0.06, (
        f"{label}: a caption reaches into the visual outro")
    assert video.with_suffix(".vtt").read_text(encoding="utf-8").startswith("WEBVTT")
    return {"duration": duration, "cues": cues, "first_cue": first_cue, "last_cue": last_cue}


@pytest.fixture(scope="module")
def long_form_render(ffmpeg_paths, tmp_path_factory):
    """One real Long-Form render shared by the music and the verification tests."""
    ffmpeg, ffprobe = ffmpeg_paths
    work = tmp_path_factory.mktemp("phase23-long-form")
    voice, script = _voice_and_script(work, ffmpeg)
    music = _tone(ffmpeg, work / "long_form_theme.wav", LONG_FORM_MUSIC_HZ, 0.6)
    settings = _base_settings(
        export_mode=EXPORT_MODE_LONG_FORM, aspect="16:9", resolution="160x90",
        voiceover_paths=[str(voice)], voiceover_path=str(voice),
        global_script_path=str(script), script_paths=[str(script)], script_path=str(script),
        subtitle_style="long_1", subtitle_position="Bottom Center",
        music_path=str(music), long_form_music_volume=44,
        long_form_transition_type="cross_dissolve", long_form_transition_duration=2.0,
        opening_effect="zoom_out",
    )
    result, logs, elapsed = _render(
        ffmpeg, ffprobe, work, settings, _clips(ffmpeg, work / "clips", 6, "160x90")
    )
    return {
        "ffmpeg": ffmpeg, "ffprobe": ffprobe, "result": result, "logs": logs,
        "elapsed": elapsed, "video": result.long_form.video, "duration_expected":
        LONG_FORM_INTRO + SPOKEN_SECONDS + LONG_FORM_OUTRO,
    }


def test_long_form_music_covers_intro_voiceover_and_outro(long_form_render):
    render = long_form_render
    video = render["video"]
    assert video.is_file() and video.stat().st_size > 0
    measured = _assert_music_contract(
        render["ffmpeg"], render["ffprobe"], video,
        intro=LONG_FORM_INTRO, outro=LONG_FORM_OUTRO, size=(160, 90),
        music_hz=LONG_FORM_MUSIC_HZ, label="Long-Form (1.5 + 3.0 + 1.5)",
    )
    assert abs(measured["duration"] - 6.0) <= 0.08
    assert render["result"].long_form.verification_status == "PASS"
    timeline = [line for line in render["logs"] if line.startswith("Timeline:")]
    music_lines = [line for line in render["logs"] if line.startswith("Music:")]
    subtitle_lines = [line for line in render["logs"] if line.startswith("Subtitles:")]
    assert timeline and "Intro 1.500 s" in timeline[0] and "Outro 1.500 s" in timeline[0]
    assert any("start 0.000 s" in line and "end 6.000 s" in line for line in music_lines), (
        "the music window must be logged as 0.000 s → video end")
    assert subtitle_lines and "start 1.500 s" in subtitle_lines[0]


def test_bounded_verification_succeeds_where_an_eof_seek_produced_no_png(long_form_render):
    """PART A on a real file: the reported Windows failure must be impossible now."""
    render = long_form_render
    ffmpeg, ffprobe, video = render["ffmpeg"], render["ffprobe"], render["video"]
    info = _probe(ffprobe, video)
    duration = float(info["format"]["duration"])
    stream = next(item for item in info["streams"] if item["codec_type"] == "video")
    numerator, denominator = stream["avg_frame_rate"].split("/")
    fps = float(numerator) / float(denominator)

    alignment = AlignmentResult(
        words=[
            WordTiming("Erstes", 0.10, 0.60, 0.97),
            WordTiming("Mitte", 3.0, 3.4, 0.97),
            # The real failure: the last spoken word ends AT the video duration.
            WordTiming("Schluss", duration - 0.02, duration, 0.96),
        ],
        language="de", method="real-render", compatibility=1.0, average_confidence=0.97,
    )
    paths = {
        label: video.parent / f"{video.stem}.verify_{label}.png"
        for label in ("first", "middle", "final")
    }
    logs: list[str] = []
    verification = verify_subtitle_frames(
        ffmpeg, video, alignment, paths, duration=duration, fps=fps, log=logs.append
    )
    assert verification.status == "PASS", logs
    assert len(verification.paths) == 3
    for path in verification.paths:
        valid, reason = png_frame_status(path)
        assert valid, f"{path.name}: {reason}"
    final = verification.frames[-1]
    margin = frame_safe_margin(duration, fps)
    # The word timing of a real render ends at the file end, so the requested
    # timestamp lies beyond every decodable frame – exactly the reported failure.
    assert final.requested > duration - margin, "the test must reproduce an unsafe request"
    assert final.used is not None and final.used < duration, "the fallback stayed outside the file"
    assert final.used == pytest.approx(duration - margin, abs=1e-6), (
        "the request was not clamped to the bounded timestamp")
    assert final.used < final.requested
    assert final.attempts >= 1
    assert any("fallback=" in line for line in logs), logs

    # The legacy behaviour on the very same file: seeking at EOF decodes nothing.
    legacy = video.parent / "legacy_final.png"
    legacy.unlink(missing_ok=True)
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{duration:.6f}",
         "-i", str(video), "-map", "0:v:0", "-frames:v", "1", "-update", "1", str(legacy)],
        capture_output=True, timeout=120, stdin=subprocess.DEVNULL, check=False,
    )
    assert result.returncode == 0, "FFmpeg reports success at EOF"
    assert not legacy.is_file() or legacy.stat().st_size == 0, (
        "an EOF seek unexpectedly produced a frame; the bounded fallback would be unnecessary")


def test_short_reuses_one_0_7_outro_with_its_own_music(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    voice, script = _voice_and_script(tmp_path, ffmpeg)
    shorts_music = _tone(ffmpeg, tmp_path / "shorts_theme.wav", SHORTS_MUSIC_HZ, 0.6)
    long_form_music = _tone(ffmpeg, tmp_path / "long_form_theme.wav", LONG_FORM_MUSIC_HZ, 0.6)
    settings = _base_settings(
        export_mode=EXPORT_MODE_SHORTS, aspect="9:16", resolution="90x160",
        voiceover_paths=[str(voice)], voiceover_path=str(voice),
        global_script_path=str(script), script_paths=[str(script)], script_path=str(script),
        short_subtitle_style="short_2", short_subtitle_position="Top Center",
        short_subtitle_animation="phrase_focus",
        short_music_path=str(shorts_music), shorts_music_volume=44,
        shorts_transition_type="cross_dissolve", shorts_transition_duration=2.0,
        # A Long-Form track and a Long-Form opening effect must never leak in.
        music_path=str(long_form_music), opening_effect="zoom_in",
    )
    result, logs, _elapsed = _render(
        ffmpeg, ffprobe, tmp_path, settings, _clips(ffmpeg, tmp_path / "clips", 6, "90x160")
    )
    assert len(result.shorts) == 1
    short = result.shorts[0]
    measured = _assert_music_contract(
        ffmpeg, ffprobe, short.video, intro=SHORT_INTRO, outro=SHORT_OUTRO, size=(90, 160),
        music_hz=SHORTS_MUSIC_HZ, label="Short (0.7 + 3.0 + 0.7)",
    )
    # Exactly one 0.7 s ending: the outro replaces the legacy end padding.
    assert abs(measured["duration"] - 4.4) <= 0.08
    assert measured["duration"] < 4.4 + 0.4, "a second ending was stacked behind the outro"
    assert short.verification_status == "PASS"
    assert short.video.with_suffix(".txt").read_text(encoding="utf-8").strip() == SCRIPT
    opening = _samples(ffmpeg, short.video, 0.05, 0.4)
    assert _strength(opening, LONG_FORM_MUSIC_HZ) < _strength(opening, SHORTS_MUSIC_HZ) / 8, (
        "the Long-Form track leaked into the Short")
    assert any("Intro 0.700 s" in line for line in logs if line.startswith("Timeline:"))


def test_combined_run_applies_each_outputs_own_volume_and_transition(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    voice, script = _voice_and_script(tmp_path, ffmpeg)
    long_form_music = _tone(ffmpeg, tmp_path / "long_form_theme.wav", LONG_FORM_MUSIC_HZ, 0.6)
    shorts_music = _tone(ffmpeg, tmp_path / "shorts_theme.wav", SHORTS_MUSIC_HZ, 0.6)
    settings = _base_settings(
        export_mode=EXPORT_MODE_COMBINED, aspect="16:9", resolution="160x90",
        voiceover_paths=[str(voice)], voiceover_path=str(voice),
        global_script_path=str(script), script_paths=[str(script)], script_path=str(script),
        subtitle_style="long_1", short_subtitle_style="short_2",
        music_path=str(long_form_music), short_music_path=str(shorts_music),
        long_form_music_volume=30, shorts_music_volume=55,
        long_form_transition_type="cross_dissolve", long_form_transition_duration=2.0,
        shorts_transition_type="film_dissolve", shorts_transition_duration=1.0,
    )
    result, logs, _elapsed = _render(
        ffmpeg, ffprobe, tmp_path, settings, _clips(ffmpeg, tmp_path / "clips", 8, "160x90")
    )
    long_form = result.long_form.video
    short = result.shorts[0].video
    long_form_level = _strength(_samples(ffmpeg, long_form, 0.05, 0.5), LONG_FORM_MUSIC_HZ)
    short_level = _strength(_samples(ffmpeg, short, 0.05, 0.5), SHORTS_MUSIC_HZ)
    assert long_form_level > 0.01 and short_level > 0.01
    ratio = short_level / long_form_level
    assert abs(ratio - 55 / 30) <= 0.15 * (55 / 30), (
        f"each output must use its own music volume, measured ratio {ratio:.3f}")
    # Strict separation in both directions.
    assert _strength(_samples(ffmpeg, short, 0.05, 0.5), LONG_FORM_MUSIC_HZ) < long_form_level / 8
    assert _strength(_samples(ffmpeg, long_form, 0.05, 0.5), SHORTS_MUSIC_HZ) < short_level / 8
    assert abs(float(_probe(ffprobe, long_form)["format"]["duration"]) - 6.0) <= 0.08
    assert abs(float(_probe(ffprobe, short)["format"]["duration"]) - 4.4) <= 0.08
    assert result.long_form.verification_status == "PASS"
    assert result.shorts[0].verification_status == "PASS"

    output_lines = [line for line in logs if line.startswith("Output settings (")]
    assert len(output_lines) == 2, output_lines
    assert "Music volume 30 %" in output_lines[0] and "Cross Dissolve / 2.000 s" in output_lines[0]
    assert "Music volume 55 %" in output_lines[1] and "Film Dissolve / 1.000 s" in output_lines[1]
    # A different transition duration must reserve different material.
    material = [line for line in logs if line.startswith("Videomaterial:")]
    assert len(material) == 2 and material[0] != material[1], (
        "a 2.0 s transition must reserve different material than a 1.0 s transition")
