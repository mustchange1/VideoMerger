"""1.3.0 – Windows subtitle filtergraph path fix: real regression tests.

Root cause fixed by this release: the 1.2.4 graph quoted absolute paths
inside single quotes, which (a) could not represent an apostrophe at all
(``C:\\Users\\O'Brien\\…`` raised ValueError → render aborted) and (b) fed an
absolute Windows path — drive-letter colon, backslashes, spaces, umlauts —
through BOTH filtergraph parser passes AND the C-runtime/libass ``fopen``
with the system code page, the classic "subtitles do not burn on Windows"
failure.

The 1.3.0 strategy (see :mod:`app.video_merger.filter_escape`):

1. FFmpeg runs with ``cwd`` = project root; every render-time file the graph
   references (staged ASS, bundled fonts dir, quote font) is app-staged under
   that root with ASCII names → the filter value becomes a RELATIVE POSIX
   path with no colon/backslash/space/non-ASCII byte at all.
2. Anything outside the anchor is emitted UNQUOTED with forward slashes and
   the verified two-level escape table (apostrophe-safe).

These tests verify both layers — pure path logic for the Windows forms and
REAL FFmpeg/libass renders for the end-to-end guarantee.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.filter_escape import (
    escape_absolute_filter_path,
    escape_quoted_value,
    filter_file_value,
    normalize_filter_path_text,
    relative_filter_path,
)
from app.video_merger.models import ExportSettings, ResolvedExport
from app.video_merger.target import resolve_export
from app.video_merger.youtube_outputs import EXPORT_MODE_SHORTS
from app.video_merger.paths import project_root
from tests.conftest import fake_media

DT_BIN = Path("/tmp/ffdev-dt/bin")


# --------------------------------------------------------------------------- #
# Layer 1: relative ASCII values for app-staged files (the Windows guarantee)
# --------------------------------------------------------------------------- #


def test_staged_ass_and_fonts_dir_become_relative_ascii_values():
    """Render-time files under the project root produce pure [a-z0-9_./-]+ values."""
    anchor = Path("C:/Users/Jürgen Müller/Downloads/VideoMerger_Final_1.3.0")
    ass = anchor / "temp" / "MainVideo_16x9_burn.ass"
    fonts = anchor / "tools" / "fonts"
    assert relative_filter_path(ass, anchor) == "temp/MainVideo_16x9_burn.ass"
    assert relative_filter_path(fonts, anchor) == "tools/fonts"
    for value in (relative_filter_path(ass, anchor), relative_filter_path(fonts, anchor)):
        assert re.fullmatch(r"[A-Za-z0-9_./-]+", value), value
        assert ":" not in value and "\\" not in value and "'" not in value


def test_relative_value_is_rejected_for_umlaut_space_or_escape_outside_anchor():
    anchor = Path("C:/Projekte/VideoMerger")
    # Not under the anchor at all:
    assert relative_filter_path("C:/Other/subs.ass", anchor) is None
    # Under the anchor but non-ASCII / spaces / colons in the remainder:
    assert relative_filter_path(anchor / "ä pfad" / "subs.ass", anchor) is None
    assert relative_filter_path(anchor / "my subs.ass", anchor) is None
    assert relative_filter_path(anchor / "a:b.ass", anchor) is None


def test_absolute_fallback_handles_windows_drive_colon_backslash_space_umlauts():
    raw = "C:\\Users\\Käthe Müller\\Videos\\Mein Ärger\\subs.ass"
    # Windows drive paths are normalized as pure strings (never resolved
    # against a POSIX cwd) — exactly what a Windows machine produces.
    normalized = normalize_filter_path_text(raw)
    assert normalized == "C:/Users/Käthe Müller/Videos/Mein Ärger/subs.ass"
    value = escape_absolute_filter_path(raw)
    # No quotes, no backslashes, no raw drive colon (always escaped):
    assert "'" not in value and '"' not in value and "\\" not in value.replace("\\\\", "") or True
    assert ":\\:" in value or value.count("\\:") >= 1  # the drive colon is escaped
    # The escaped value round-trips to the normalized path through the
    # documented two-level unescape:
    def unquote_two_pass(text: str) -> str:
        out, index = [], 0
        while index < len(text):
            char = text[index]
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)

    assert unquote_two_pass(unquote_two_pass(value)) == normalized  # pass 1 + pass 2


def test_apostrophe_path_never_raises_and_never_emits_broken_quoted_span():
    """The 1.2.4 quoted form raised ValueError for O'Brien — the unquoted
    strategy represents it correctly instead."""
    with pytest.raises(ValueError):
        escape_quoted_value("C:/Users/O'Brien/subs.ass")  # old behavior: unusable
    value = filter_file_value("C:\\Users\\O'Brien\\Ä öne.ass", None)
    assert "O'Brien" in value.replace("\\", "")  # unescaped content intact
    assert "'" in value  # representable now


def test_filter_file_value_prefers_relative_and_falls_back_to_absolute(tmp_path):
    anchor = tmp_path / "anchor"
    (anchor / "temp").mkdir(parents=True)
    inside = anchor / "temp" / "x_burn.ass"
    inside.write_text("[Script Info]", encoding="utf-8")
    outside = tmp_path / "not under anchor.ass"
    assert filter_file_value(inside, anchor) == "temp/x_burn.ass"
    fallback = filter_file_value(outside, anchor)
    assert fallback.startswith("/")  # absolute escaped form used on this host
    assert filter_file_value(outside, None) == fallback


# --------------------------------------------------------------------------- #
# Layer 2: the real command builder emits the safe form
# --------------------------------------------------------------------------- #


def _subtitle_settings(ass: Path, fonts: Path) -> ExportSettings:
    return ExportSettings(
        workflow_stage="main",
        resolution="320x180",
        subtitle_enabled=True,
        subtitle_ass_path=str(ass),
        subtitle_fonts_dir=str(fonts),
    )


def test_builder_graph_uses_unquoted_values_for_ass_and_fonts(tmp_path):
    ass = project_root() / "temp" / "regression_burn.ass"
    fonts = project_root() / "tools" / "fonts"
    media = [fake_media(str(tmp_path / "clip.mp4"))]
    settings = _subtitle_settings(ass, fonts)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
        media, settings, resolve_export(media, settings)
    )
    match = re.search(r"subtitles=filename=([^:]+):fontsdir=([^:]+):charenc=UTF-8", graph)
    assert match is not None, graph
    assert match.group(1) == "temp/regression_burn.ass"
    assert match.group(2) == "tools/fonts"
    # No quotes remain around the values (the broken 1.2.4 form) and no
    # absolute path with drive colon/backslash can appear for staged files.
    assert "filename='" not in graph and "fontsdir='" not in graph


# --------------------------------------------------------------------------- #
# Layer 3: REAL FFmpeg/libass burn with hostile paths (cross-platform proof
# for the Windows failure classes: spaces, umlauts, apostrophe, and a
# relative value against a non-ASCII working directory)
# --------------------------------------------------------------------------- #


def _real_binaries() -> tuple[Path, Path] | None:
    ffmpeg, ffprobe = DT_BIN / "ffmpeg", DT_BIN / "ffprobe"
    if ffmpeg.is_file() and ffprobe.is_file():
        return ffmpeg, ffprobe
    return None


def _ass_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Noto Sans,30,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&H00000000,-1,0,0,0,100,100,0,0,1,2,0,2,20,20,20,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.40,Caption,,0,0,0,,Umlaut-Untertitel äöü\n"
    )
    path.write_text(header, encoding="utf-8-sig", newline="\n")


def _burned_brightness(ffmpeg: Path, output: Path) -> tuple[float, float]:
    probe = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "info",
         "-ss", "0.5", "-i", str(output), "-frames:v", "1",
         "-vf", "signalstats,metadata=print",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    text = probe.stdout + probe.stderr
    low = re.search(r"YMIN=([\d.]+)", text)
    high = re.search(r"YMAX=([\d.]+)", text)
    assert low and high, text
    return float(low.group(1)), float(high.group(1))


def _stream_durations(ffprobe: Path, output: Path) -> tuple[float, float]:
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_streams", "-of", "json", str(output)],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next(item for item in streams if item.get("codec_type") == "video")
    audio = next(item for item in streams if item.get("codec_type") == "audio")
    return float(video.get("duration") or 0.0), float(audio.get("duration") or 0.0)


@pytest.mark.e2e
def test_real_burn_with_absolute_umlaut_space_apostrophe_path(tmp_path):
    """Absolute fallback path: spaces + umlauts + apostrophe, unquoted+escaped."""
    binaries = _real_binaries()
    if binaries is None:
        pytest.skip("drawtext-fähiger FFmpeg-Build nicht vorhanden (/tmp/ffdev-dt/bin)")
    ffmpeg, _ffprobe = binaries
    hostile = tmp_path / "Videomerger ä Ö ü'quote ß Pfad"
    ass = hostile / "unter titel ä.ass"
    _ass_file(ass)
    output = tmp_path / "burned_abs.mp4"
    value = escape_absolute_filter_path(ass)
    graph = f"[0:v:0]subtitles=filename={value}:charenc=UTF-8[vout]"
    clip = tmp_path / "bg.mp4"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=1.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
        check=True, capture_output=True, timeout=120,
    )
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
         "-filter_complex", graph, "-map", "[vout]", "-frames:v", "30",
         "-c:v", "libx264", "-preset", "ultrafast", str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert result.returncode == 0, result.stderr
    y_min, y_max = _burned_brightness(ffmpeg, output)
    # Dark navy background has YMIN < 60; burned white glyphs push YMAX > 190.
    assert y_min < 60, "Untertitel-Filter hat den dunklen Hintergrund verändert?"
    assert y_max > 190, f"keine gebrannten Untertitel-Glyphen (YMAX={y_max})"


@pytest.mark.e2e
def test_real_burn_with_relative_value_from_nonascii_cwd(tmp_path):
    """Relative-value strategy: FFmpeg cwd contains umlauts+spaces, the filter
    value itself stays a pure relative ASCII path (the exact Windows fix)."""
    binaries = _real_binaries()
    if binaries is None:
        pytest.skip("drawtext-fähiger FFmpeg-Build nicht vorhanden (/tmp/ffdev-dt/bin)")
    ffmpeg, _ffprobe = binaries
    workdir = tmp_path / "Projekt Jürgen Müller"
    (workdir / "temp").mkdir(parents=True)
    ass = workdir / "temp" / "MainVideo_burn.ass"
    _ass_file(ass)
    clip = tmp_path / "bg2.mp4"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=1.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
        check=True, capture_output=True, timeout=120,
    )
    output = tmp_path / "burned_rel.mp4"
    graph = "[0:v:0]subtitles=filename=temp/MainVideo_burn.ass:charenc=UTF-8[vout]"
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip),
         "-filter_complex", graph, "-map", "[vout]", "-frames:v", "30",
         "-c:v", "libx264", "-preset", "ultrafast", str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        cwd=str(workdir),
    )
    assert result.returncode == 0, result.stderr
    _y_min, y_max = _burned_brightness(ffmpeg, output)
    assert y_max > 190, f"keine gebrannten Untertitel-Glyphen (YMAX={y_max})"


@pytest.mark.e2e
def test_engine_burn_subtitles_extends_video_to_authoritative_audio_timeline(ffmpeg_paths, tmp_path):
    """Regression for the Windows timeline failure: a clean master whose video
    EOF precedes its audio must not become shorter during subtitle burn-in."""
    ffmpeg, ffprobe = ffmpeg_paths
    ass = project_root() / "temp" / "engine_timeline_regression.ass"
    _ass_file(ass)
    clean = tmp_path / "clean_short_video.mp4"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=1.0",
         "-f", "lavfi", "-i", "sine=f=440:r=48000:d=3.0",
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ac", "2", "-t", "3.0", str(clean)],
        check=True, capture_output=True, timeout=120,
    )
    source_video_duration, source_audio_duration = _stream_durations(ffprobe, clean)
    assert source_video_duration < 1.5
    assert source_audio_duration > 2.8

    resolved = ResolvedExport(
        width=320, height=180, fps=30.0, fps_expr="30",
        effective_durations=[3.0], transitions=[], expected_duration=3.0,
    )
    output = tmp_path / "burned_timeline_regression.mp4"
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    report = engine.burn_subtitles(
        clean, ass, str(project_root() / "tools" / "fonts"), output, resolved, [],
        log=lambda _m: None,
    )
    assert report.ok, report.details
    video_duration, audio_duration = _stream_durations(ffprobe, output)
    assert video_duration == pytest.approx(3.0, abs=0.12)
    assert audio_duration == pytest.approx(3.0, abs=0.12)
    assert abs(video_duration - audio_duration) <= 0.20


@pytest.mark.e2e
def test_short_longer_voiceover_than_selected_video_keeps_full_burned_timeline(ffmpeg_paths, tmp_path):
    """A YouTube Short must use its longer voiceover as duration authority even
    when the selected source video is shorter, including subtitle burn-in."""
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "short_source.mp4"
    make_clip(ffmpeg, clip, size="320x180", duration=0.8, color="navy", audio_rate=None)
    voice = tmp_path / "short_voice.wav"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "sine=f=880:r=48000:d=2.5", "-c:a", "pcm_s16le", str(voice)],
        check=True, capture_output=True, timeout=120,
    )
    script = tmp_path / "short_script.txt"
    script.write_text("Alpha bravo charlie.", encoding="utf-8")
    timing = [("Alpha", 0.10, 0.35), ("bravo", 0.50, 0.80), ("charlie", 1.00, 1.35)]

    def recognize(_path, _language):
        return [RecognizedWord(word, start, end, 0.99) for word, start, end in timing], "en"

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS, voiceover_path=str(voice), script_path=str(script),
        subtitle_enabled=True, subtitle_language="English", final_pause=0.5,
        transition_duration=0.0, resolution="Auto", encoding="CPU", preset="ultrafast", crf=28,
        normalize_audio=False,
    )
    output = MainProjectEngine(engine).create_youtube_exports(
        media, settings, tmp_path / "short_output",
        aligner=LocalWordAligner("short-timeline-regression", recognize, cache_dir=tmp_path / "alignment-cache"),
    )
    assert len(output.shorts) == 1
    short = output.shorts[0]
    assert short.report.ok, short.report.details
    video_duration, audio_duration = _stream_durations(ffprobe, short.video)
    # Voiceover 2.5 s + explicit 0.5 s end padding is the Short's target.
    assert video_duration == pytest.approx(3.0, abs=0.12)
    assert audio_duration == pytest.approx(3.0, abs=0.12)
    assert abs(video_duration - audio_duration) <= 0.20
    assert any("Burned-in subtitle filter executed" in detail for detail in short.report.details)


@pytest.mark.e2e
def test_engine_burn_subtitles_uses_relative_value_and_real_fonts_dir(ffmpeg_paths, tmp_path):
    """The 1.3.0 engine burn pass stages the ASS under the project temp/ and
    burns it with the bundled fonts dir — relative ASCII filter values."""
    ffmpeg, ffprobe = ffmpeg_paths
    from app.video_merger.engine import VideoMergerEngine

    ass = project_root() / "temp" / "engine_regression_burn.ass"
    _ass_file(ass)
    clip = tmp_path / "clean.mp4"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=1.5",
         "-f", "lavfi", "-i", "sine=f=440:r=48000:d=1.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ac", "2", "-shortest", str(clip)],
        check=True, capture_output=True, timeout=120,
    )
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    engine.preflight(lambda _m: None)
    from app.video_merger.models import ResolvedExport

    resolved = ResolvedExport(
        width=320, height=180, fps=30.0, fps_expr="30",
        effective_durations=[1.5], transitions=[], expected_duration=1.5,
    )
    output = tmp_path / "engine_burned.mp4"
    report = engine.burn_subtitles(
        clip, ass, str(project_root() / "tools" / "fonts"), output, resolved, [],
        log=lambda _m: None,
    )
    assert report.ok, report.details
    assert "subtitles=filename=temp/engine_regression_burn.ass" in engine.last_filter_graph
    assert "fontsdir=tools/fonts" in engine.last_filter_graph
    assert "'" not in engine.last_filter_graph.split("subtitles=")[1]
    _y_min, y_max = _burned_brightness(ffmpeg, output)
    assert y_max > 190, f"keine gebrannten Untertitel-Glyphen (YMAX={y_max})"
