from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import script_word_spans
from app.video_merger.font_manager import bundled_fonts_dir, resolve_font
from app.video_merger.models import AlignmentResult, ExportSettings, WordTiming
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.settings_store import SettingsStore
from app.video_merger.subtitles import ANIMATION_OPTIONS, build_cues, write_ass


def _run(command: list[object], timeout: int = 180) -> bytes:
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _alignment(script: str, starts: list[float] | None = None, step: float = .45) -> AlignmentResult:
    spans = script_word_spans(script)
    starts = starts or [0.10 + index * step for index in range(len(spans))]
    words = [
        WordTiming(
            text=token, start=starts[index], end=starts[index] + min(.31, step * .70),
            confidence=.98, script_start=start, script_end=end,
        )
        for index, (token, start, end) in enumerate(spans)
    ]
    return AlignmentResult(words, "de", "acoustic-test-timeline", 1.0, .98)


def _render_ass(ffmpeg: Path, ass: Path, output: Path, width: int, height: int, duration: float = 2.7) -> None:
    font_dir = bundled_fonts_dir().as_posix().replace("'", r"\'")
    ass_path = ass.as_posix().replace("'", r"\'")
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x08152f:s={width}x{height}:r=30:d={duration}",
        "-vf", f"subtitles=filename='{ass_path}':fontsdir='{font_dir}'",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-an", output,
    ])


def _raw_frame(ffmpeg: Path, video: Path, seconds: float, width: int, height: int) -> bytes:
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}",
        "-i", video, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    assert len(raw) == width * height * 3
    return raw


def _render_ass_image(ffmpeg: Path, ass: Path, output: Path, width: int, height: int, at: float = .20) -> bytes:
    font_dir = bundled_fonts_dir().as_posix().replace("'", r"\'")
    ass_path = ass.as_posix().replace("'", r"\'")
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x08152f:s={width}x{height}:r=30:d=1.5",
        "-vf", f"subtitles=filename='{ass_path}':fontsdir='{font_dir}'",
        "-ss", f"{at:.3f}", "-frames:v", "1", "-update", "1", output,
    ])
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", output,
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    assert len(raw) == width * height * 3
    return raw


def _caption_bbox(raw: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    points: list[tuple[int, int]] = []
    for y in range(height):
        row = y * width * 3
        for x in range(width):
            offset = row + x * 3
            red, green, blue = raw[offset:offset + 3]
            # Known background is dark blue. White and yellow/accent caption
            # pixels have strong red+green and are isolated reliably.
            if red + green > 255 and max(red, green, blue) > 150:
                points.append((x, y))
    if not points:
        return None
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)


def _mean_abs_difference(left: bytes, right: bytes) -> float:
    assert len(left) == len(right)
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def _plain_ass_text(event: str) -> str:
    text = event.rsplit(",,", 1)[-1]
    text = re.sub(r"\{[^}]*\}", "", text)
    return text.replace(r"\N", " ").strip()


def test_phrase_grouping_is_metric_punctuation_and_timing_rate_independent():
    cases = [
        "Präzise Untertitel bleiben ruhig, auch bei längeren deutschen Sätzen mit Ä, Ö und Ü. Ein Wort folgt.",
        "Professional captions remain stable, even when a longer English sentence contains careful punctuation. One follows.",
    ]
    resolutions = [(1920, 1080), (3840, 2160), (1080, 1920)]
    for script in cases:
        slow = _alignment(script, step=.72)
        fast = _alignment(script, step=.18)
        for width, height in resolutions:
            slow_cues = build_cues(script, slow, "long_1", width=width, height=height, font_key="modern_sans_bold")
            fast_cues = build_cues(script, fast, "long_1", width=width, height=height, font_key="modern_sans_bold")
            assert [cue.text for cue in fast_cues] == [cue.text for cue in slow_cues]
            assert [cue.line_break_after for cue in fast_cues] == [cue.line_break_after for cue in slow_cues]
            assert all(cue.line_count in {1, 2} for cue in slow_cues)
            assert all(len(cue.words) >= 2 for cue in slow_cues)
            assert sum(len(cue.words) for cue in slow_cues) == len(slow.words)


def test_font_discovery_is_legal_metric_sensitive_and_eveleth_is_detection_only(monkeypatch):
    monkeypatch.delenv("VIDEOMERGER_INSTALLED_FONTS", raising=False)
    eveleth = resolve_font("eveleth_clean")
    assert eveleth.proprietary is True
    assert eveleth.fallback_used is True
    assert eveleth.family == "Noto Sans"
    assert (bundled_fonts_dir() / "NotoSans-Regular.ttf").is_file()
    assert (bundled_fonts_dir() / "NotoSans-Bold.ttf").is_file()
    assert (bundled_fonts_dir() / "OFL.txt").is_file()
    assert not any("eveleth" in item.name.casefold() for item in bundled_fonts_dir().iterdir())

    monkeypatch.setenv("VIDEOMERGER_INSTALLED_FONTS", "Eveleth Clean Regular")
    licensed = resolve_font("eveleth_clean")
    assert licensed.installed is True and licensed.fallback_used is False
    assert licensed.family == "Eveleth Clean Regular"

    script = "MMMM breite Wörter und iii schmale Wörter bleiben messbar stabil."
    alignment = _alignment(script)
    modern = build_cues(script, alignment, "long_1", width=1080, height=1920, font_key="modern_sans_bold")
    clean = build_cues(script, alignment, "long_1", width=1080, height=1920, font_key="clean_sans")
    # Both choices remain valid, and each layout records its own measured break.
    assert all(cue.line_count <= 2 for cue in modern + clean)
    assert sum(len(cue.words) for cue in modern) == sum(len(cue.words) for cue in clean)


def test_new_presentation_settings_round_trip_without_changing_professional_defaults(tmp_path):
    defaults = ExportSettings()
    assert defaults.aspect == "16:9"
    assert defaults.subtitle_style == "long_1"
    # 1.2.4: Default-Animation ist "Static Phrase" (vor 1.2.4 "type_reveal").
    assert defaults.subtitle_animation == "static_phrase"
    assert defaults.subtitle_font == "modern_sans_bold"
    assert defaults.subtitle_position == "Bottom"
    assert defaults.subtitle_debug_overlay is False

    path = tmp_path / "settings.json"
    changed = ExportSettings(
        subtitle_animation="outline_highlight", subtitle_font="eveleth_clean",
        subtitle_position="Top", subtitle_debug_overlay=True,
    )
    SettingsStore(path).save(changed)
    loaded = SettingsStore(path).load()
    assert loaded.subtitle_animation == "outline_highlight"
    assert loaded.subtitle_font == "eveleth_clean"
    assert loaded.subtitle_position == "Top"
    assert loaded.subtitle_debug_overlay is True


@pytest.mark.e2e
@pytest.mark.parametrize("font_key", ["eveleth_clean", "modern_sans_bold", "clean_sans"])
def test_each_font_choice_executes_real_burn_with_selected_or_legal_fallback(ffmpeg_paths, tmp_path, font_key, monkeypatch):
    ffmpeg, _ffprobe = ffmpeg_paths
    monkeypatch.delenv("VIDEOMERGER_INSTALLED_FONTS", raising=False)
    script = "Ausgewählte Schriftmetriken bleiben sichtbar."
    alignment = _alignment(script)
    cues = build_cues(script, alignment, "long_1", width=960, height=540, font_key=font_key)
    ass = tmp_path / f"font_{font_key}.ass"
    image = tmp_path / f"font_{font_key}.png"
    write_ass(script, cues, ass, "long_1", "Bottom", 960, 540, animation="static_phrase", font_key=font_key)
    resolved = resolve_font(font_key)
    assert f"Style: Caption,{resolved.family}," in ass.read_text(encoding="utf-8-sig")
    assert _caption_bbox(_render_ass_image(ffmpeg, ass, image, 960, 540), 960, 540) is not None
    if font_key == "eveleth_clean":
        assert resolved.proprietary and resolved.fallback_used and resolved.family == "Noto Sans"


@pytest.mark.e2e
@pytest.mark.parametrize("preset_key", ["long_1", "long_2", "long_3", "long_4", "long_5"])
def test_each_improved_long_form_preset_executes_real_libass_burn(ffmpeg_paths, tmp_path, preset_key):
    ffmpeg, _ffprobe = ffmpeg_paths
    script = "Ruhige professionelle Untertitel bleiben stabil."
    alignment = _alignment(script)
    cues = build_cues(script, alignment, preset_key, width=960, height=540, font_key="modern_sans_bold")
    assert cues and all(cue.line_count <= 2 for cue in cues)
    assert all(len(cue.words) >= 2 for cue in cues)
    ass = tmp_path / f"{preset_key}.ass"
    image = tmp_path / f"{preset_key}.png"
    write_ass(
        script, cues, ass, preset_key, "Bottom", 960, 540,
        animation="type_reveal", font_key="modern_sans_bold",
    )
    raw = _render_ass_image(ffmpeg, ass, image, 960, 540)
    box = _caption_bbox(raw, 960, 540)
    assert box is not None and box[1] > 330 and box[3] < 535


@pytest.mark.e2e
def test_all_four_safe_positions_execute_and_keep_ordered_stable_regions(ffmpeg_paths, tmp_path):
    ffmpeg, _ffprobe = ffmpeg_paths
    script = "Positionen bleiben sicher und stabil."
    alignment = _alignment(script)
    cues = build_cues(script, alignment, "long_1", width=960, height=540, font_key="modern_sans_bold")
    vertical_centers: dict[str, float] = {}
    for position in ("Top", "Middle", "Medium-Low", "Bottom"):
        ass = tmp_path / f"position_{position}.ass"
        image = tmp_path / f"position_{position}.png"
        write_ass(
            script, cues, ass, "long_1", position, 960, 540,
            animation="static_phrase", font_key="modern_sans_bold",
        )
        box = _caption_bbox(_render_ass_image(ffmpeg, ass, image, 960, 540), 960, 540)
        assert box is not None
        vertical_centers[position] = (box[1] + box[3]) / 2
    assert vertical_centers["Top"] < vertical_centers["Middle"]
    assert vertical_centers["Middle"] < vertical_centers["Medium-Low"]
    assert vertical_centers["Medium-Low"] < vertical_centers["Bottom"]
    assert vertical_centers["Top"] > 20
    assert vertical_centers["Bottom"] < 520


@pytest.mark.e2e
def test_all_five_animations_burn_real_timestamp_changes_with_stable_complete_phrase_geometry(ffmpeg_paths, tmp_path):
    ffmpeg, _ffprobe = ffmpeg_paths
    script = "Präzise Wörter bleiben räumlich stabil."
    alignment = _alignment(script, starts=[.10, .55, 1.05, 1.55, 2.05])
    cues = build_cues(script, alignment, "long_1", width=960, height=540, font_key="modern_sans_bold")
    assert len(cues) == 1 and cues[0].line_count <= 2
    expected_starts = ["0:00:00.10", "0:00:00.55", "0:00:01.05", "0:00:01.55", "0:00:02.05"]

    bboxes: dict[str, list[tuple[int, int, int, int]]] = {}
    for animation, _label in ANIMATION_OPTIONS:
        ass = tmp_path / f"{animation}.ass"
        video = tmp_path / f"{animation}.mp4"
        write_ass(
            script, cues, ass, "long_1", "Bottom", 960, 540,
            animation=animation, font_key="modern_sans_bold",
        )
        text = ass.read_text(encoding="utf-8-sig")
        events = [line for line in text.splitlines() if line.startswith("Dialogue: 0,")]
        assert events
        assert all(_plain_ass_text(event) == script for event in events)
        if animation == "static_phrase":
            assert len(events) == 1 and events[0].split(",", 3)[1] == expected_starts[0]
        else:
            assert [event.split(",", 3)[1] for event in events] == expected_starts

        _render_ass(ffmpeg, ass, video, 960, 540)
        frames = [
            _raw_frame(ffmpeg, video, at, 960, 540)
            for at in (.20, 1.15, 2.15)
        ]
        boxes = [_caption_bbox(frame, 960, 540) for frame in frames]
        assert all(box is not None for box in boxes)
        bboxes[animation] = [box for box in boxes if box is not None]
        assert all(330 <= box[1] <= 500 for box in bboxes[animation])

        if animation != "static_phrase":
            assert _mean_abs_difference(frames[0], frames[1]) > .025
            assert _mean_abs_difference(frames[1], frames[2]) > .025
        else:
            assert _mean_abs_difference(frames[0], frames[1]) < .20
            assert _mean_abs_difference(frames[1], frames[2]) < .20

        if animation != "type_reveal":
            centers = [((box[0] + box[2]) / 2, (box[1] + box[3]) / 2) for box in bboxes[animation]]
            assert max(x for x, _ in centers) - min(x for x, _ in centers) <= 5
            assert max(y for _, y in centers) - min(y for _, y in centers) <= 4

    # Type Reveal keeps the already visible first word at the same phrase-based
    # x position while transparent future glyphs reserve complete geometry.
    assert max(box[0] for box in bboxes["type_reveal"]) - min(box[0] for box in bboxes["type_reveal"]) <= 5


@pytest.mark.e2e
@pytest.mark.parametrize("width,height", [(1920, 1080), (3840, 2160), (1080, 1920)])
def test_actual_libass_layout_scales_at_1080p_4k_and_vertical_without_unsafe_wrap(ffmpeg_paths, tmp_path, width, height):
    ffmpeg, _ffprobe = ffmpeg_paths
    script = "Professionelle Untertitel mit Umlauten bleiben in höchstens zwei gut balancierten Zeilen."
    alignment = _alignment(script, starts=[.05 + index * .25 for index in range(len(script_word_spans(script)))], step=.25)
    cues = build_cues(script, alignment, "long_1", width=width, height=height, font_key="modern_sans_bold")
    assert cues and all(cue.line_count <= 2 for cue in cues)
    assert all(len(cue.words) >= 2 for cue in cues)
    ass = tmp_path / f"layout_{width}x{height}.ass"
    write_ass(script, cues, ass, "long_1", "Bottom", width, height, animation="static_phrase", font_key="modern_sans_bold")
    text = ass.read_text(encoding="utf-8-sig")
    expected_size = round(min(width, height) * .046)
    expected_family = resolve_font("modern_sans_bold").family
    assert f"Style: Caption,{expected_family},{expected_size}," in text
    assert all(event.count(r"\N") <= 1 for event in text.splitlines() if event.startswith("Dialogue: 0,"))

    image = tmp_path / f"layout_{width}x{height}.png"
    font_dir = bundled_fonts_dir().as_posix()
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"color=c=0x08152f:s={width}x{height}:r=30:d=0.5",
        "-vf", f"subtitles=filename='{ass.as_posix()}':fontsdir='{font_dir}'",
        "-ss", "0.15", "-frames:v", "1", "-update", "1", image,
    ], timeout=300)
    assert image.is_file() and image.stat().st_size > 1000


@pytest.mark.e2e
def test_debug_overlay_burns_current_word_and_timestamp_in_actual_frame(ffmpeg_paths, tmp_path):
    ffmpeg, _ffprobe = ffmpeg_paths
    script = "Debug zeigt Zeit."
    alignment = _alignment(script, starts=[.1, .5, .9])
    cues = build_cues(script, alignment, "long_1")
    ass = tmp_path / "debug_on.ass"
    image = tmp_path / "debug_on.png"
    write_ass(
        script, cues, ass, "long_1", "Bottom", 960, 540,
        animation="static_phrase", font_key="modern_sans_bold", debug_overlay=True,
    )
    raw = _render_ass_image(ffmpeg, ass, image, 960, 540, at=.60)
    box = _caption_bbox(raw, 960, 540)
    assert box is not None and box[1] < 80 and box[3] > 400
    text = ass.read_text(encoding="utf-8-sig")
    assert "CURRENT WORD: zeigt" in text and "START: 00000.500" in text


def test_debug_overlay_is_default_off_and_tracks_words_for_every_animation(tmp_path):
    script = "Debug zeigt Zeit."
    alignment = _alignment(script, starts=[.1, .5, .9])
    cues = build_cues(script, alignment, "long_1")
    for animation, _label in ANIMATION_OPTIONS:
        off = tmp_path / f"{animation}_off.ass"
        on = tmp_path / f"{animation}_on.ass"
        write_ass(script, cues, off, "long_1", "Bottom", 1920, 1080, animation=animation, font_key="clean_sans")
        write_ass(
            script, cues, on, "long_1", "Bottom", 1920, 1080,
            animation=animation, font_key="clean_sans", debug_overlay=True,
        )
        assert "CURRENT WORD:" not in off.read_text(encoding="utf-8-sig")
        on_text = on.read_text(encoding="utf-8-sig")
        assert on_text.count("CURRENT WORD:") == 3
        assert "START: 00000.100" in on_text and "END: 00001.210" in on_text
