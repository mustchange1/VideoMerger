"""1.3.0 – Quote Card style system: 5 styles, manual controls, zoom, isolation,
16:9 / 9:16 / 1080p / 4K and the Intro → Quote → Main → Outro sequence.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.video_merger import quote
from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.errors import VideoMergerError
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings, MediaInfo, ResolvedExport
from app.video_merger.quote import (
    QUOTE_STYLES, layout_quote, normalize_color, quote_video_chain,
)
from app.video_merger.font_manager import resolve_font
from tests.conftest import fake_media, make_clip

DE_TEXT = "Wenige Wörter, viel Luft – der Satz bleibt ein ruhiger Fokus."
DE_UMLAUT = "Größe, Mühe und Größe der Ökonomie: äöü und ÄÖÜ bleiben ganz."
DT_BIN = Path("/tmp/ffdev-dt/bin")

RESOLUTIONS = [(1920, 1080), (3840, 2160), (1080, 1920), (2160, 3840)]


# --------------------------------------------------------------------------- #
# The five polished styles
# --------------------------------------------------------------------------- #


def test_exactly_five_distinct_polished_styles_with_default():
    assert len(QUOTE_STYLES) == 5
    keys = list(QUOTE_STYLES)
    assert keys == [
        "clean_editorial", "warm_cinematic", "soft_paper",
        "minimal_film", "elegant_contrast",
    ]
    palettes = {(spec.background_hex, spec.text_hex) for spec in QUOTE_STYLES.values()}
    assert len(palettes) == 5  # visually distinct
    # The default is the cleanest/most readable (warm white) style.
    assert quote.get_quote_style("clean_editorial").background_hex == "0xF6F1E7"
    assert quote.get_quote_style("clean_editorial").text_hex == "0x232019"
    # Unknown keys fall back to the clean default.
    assert quote.get_quote_style("nonsense").key == "clean_editorial"


def test_default_style_is_warm_white_soft_beige_with_serif_default():
    spec = QUOTE_STYLES["clean_editorial"]
    bg = [int(spec.background_hex[i:i + 2], 16) for i in (2, 4, 6)]
    text = [int(spec.text_hex[i:i + 2], 16) for i in (2, 4, 6)]
    assert all(channel > 225 for channel in bg)   # warm white / soft beige
    assert all(channel < 70 for channel in text)  # dark elegant type
    assert spec.default_font == "lora"            # elegant serif
    assert spec.hairline_hex is not None          # editorial hairline accent


@pytest.mark.parametrize("width,height", RESOLUTIONS)
def test_every_style_lays_out_at_every_resolution(width, height):
    for key in QUOTE_STYLES:
        layout = layout_quote(DE_TEXT, "Studio", "inter", width, height,
                              style_key=key, zoom_percent=4.0)
        words = DE_TEXT.split()
        flat = [word for line in layout.lines for word in line.split(" ")]
        assert flat == words
        assert len(layout.lines) <= quote.MAX_QUOTE_LINES
        font = resolve_font("inter", bold=True)
        limit = width * (quote.QUOTE_MAX_LINE_RATIO_LANDSCAPE if width >= height
                         else quote.QUOTE_MAX_LINE_RATIO_PORTRAIT)
        for line in layout.lines:
            assert font.text_width(line, layout.font_size) <= limit + 2.0
        # Style + resolution flow into the rendered layout identity.
        assert layout.style_key == key
        assert layout.width == width and layout.height == height


@pytest.mark.parametrize("width,height", RESOLUTIONS)
@pytest.mark.parametrize("key", list(QUOTE_STYLES))
def test_quote_video_chain_renders_every_style_at_every_resolution(width, height, key):
    layout = layout_quote(DE_UMLAUT, "Studio", "lora", width, height,
                          style_key=key, zoom_percent=4.0)
    chain = quote_video_chain(layout, width, height, 30.0, 2.0, "base0")[0]
    assert chain.startswith(f"color=c={layout.background_hex}:s={width}x{height}")
    assert "vignette=" in chain
    assert f"fontcolor={layout.text_hex}" in chain
    # drawtext: one per line + attribution, all expansion=none (literal text).
    assert len(re.findall(r"[:,]text=", chain)) == len(layout.lines) + 1
    assert chain.count("expansion=none") == len(layout.lines) + 1
    if layout.grain:
        assert "noise=alls=" in chain
    assert chain.endswith("[base0]")


def test_grain_only_in_the_grain_styles():
    for key, spec in QUOTE_STYLES.items():
        layout = layout_quote("Kurz.", "", "inter", 1920, 1080, style_key=key)
        chain = quote_video_chain(layout, 1920, 1080, 30, 2.0, "b")[0]
        assert ("noise=alls=" in chain) == spec.grain


# --------------------------------------------------------------------------- #
# Manual controls
# --------------------------------------------------------------------------- #


def test_manual_font_size_percent_scales_the_type():
    small = layout_quote(DE_TEXT, "", "lora", 1920, 1080, font_size_percent=60)
    normal = layout_quote(DE_TEXT, "", "lora", 1920, 1080, font_size_percent=100)
    big = layout_quote(DE_TEXT, "", "lora", 1920, 1080, font_size_percent=160)
    assert small.font_size < normal.font_size < big.font_size
    assert normal.font_size == round(1080 * quote.QUOTE_FONT_SIZE_RATIO)


def test_manual_weight_and_colors_override_the_style():
    bold = layout_quote(DE_TEXT, "", "inter", 1920, 1080, font_weight="bold")
    regular = layout_quote(DE_TEXT, "", "inter", 1920, 1080, font_weight="regular")
    assert bold.font_path != regular.font_path
    assert bold.font_path is not None and "Bold" in bold.font_path.name
    assert regular.font_path is not None and "Regular" in regular.font_path.name
    custom = layout_quote(DE_TEXT, "", "inter", 1920, 1080,
                          text_color="#102030", background_color="#FEDCBA")
    assert custom.text_hex == "0x102030"
    assert custom.background_hex == "0xFEDCBA"
    assert normalize_color("garbage", "0xABCDEF") == "0xABCDEF"  # invalid → default
    assert normalize_color("#aabbcc", "0x111111") == "0xAABBCC"


def test_manual_position_and_safe_area_padding():
    center = layout_quote(DE_TEXT, "A", "lora", 1920, 1080, position="center")
    upper = layout_quote(DE_TEXT, "A", "lora", 1920, 1080, position="upper")
    lower = layout_quote(DE_TEXT, "A", "lora", 1920, 1080, position="lower")
    assert upper.line_top < center.line_top < lower.line_top
    # Safe-area padding keeps the block inside the padded area.
    tight = layout_quote(DE_TEXT, "A", "lora", 1920, 1080, safe_padding_percent=15)
    assert tight.line_top >= round(1080 * 0.15) - 1
    assert tight.safe_padding_percent == 15.0


def test_subtle_zoom_uses_bounded_zoompan_and_preserves_duration():
    layout = layout_quote(DE_TEXT, "", "lora", 1920, 1080, zoom_percent=4.0)
    chain = quote_video_chain(layout, 1920, 1080, 30.0, 2.0, "b")[0]
    assert "zoompan=" in chain
    match = re.search(r"min\(zoom\+[\d.]+,([\d.]+)\)", chain)
    assert match is not None
    assert float(match.group(1)) == pytest.approx(1.04, abs=0.001)  # 4 % cap
    assert "d=1:fps=30" in chain  # one output frame per input frame
    assert chain.split(",")[0].endswith("d=2")  # duration untouched
    # Zoom 0 → no zoompan at all.
    plain = quote_video_chain(
        layout_quote(DE_TEXT, "", "lora", 1920, 1080, zoom_percent=0.0),
        1920, 1080, 30.0, 2.0, "b",
    )[0]
    assert "zoompan=" not in plain


def test_builder_passes_quote_style_controls_into_stage2_graph(tmp_path):
    media = [fake_media(str(tmp_path / "main.mp4"), duration=3.0)]
    settings = ExportSettings(
        workflow_stage="outro", resolution="1920x1080", aspect="16:9",
        transition_duration=0.5, quote_enabled=True, quote_text=DE_UMLAUT,
        quote_duration=2.0, quote_style="warm_cinematic", quote_font="lora",
        quote_font_size_percent=120, quote_font_weight="regular",
        quote_text_color="#123456", quote_background_color="#234567",
        quote_zoom_percent=6.0, quote_position="upper",
        quote_safe_padding_percent=10.0, subtitle_enabled=False,
        stage2_audio_modes=["original", "mute"],
    )
    resolved = ResolvedExport(
        width=1920, height=1080, fps=30.0, fps_expr="30",
        effective_durations=[2.0, 3.0], transitions=[0.5],
        expected_duration=4.5, warnings=[],
    )
    # Replace the main clip with a generated quote section in front of it.
    quote_item = MediaInfo(
        path=Path("<generated:quote-card>"), duration=2.0, width=0, height=0,
        fps=30.0, effective_width=0, effective_height=0, fps_fraction="30/1",
        video_codec="generated", pixel_format="yuv420p", sar="1:1", dar="",
        source_duration=2.0, is_generated_quote=True,
    )
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
        [quote_item, *media], settings, resolved,
    )
    assert f"color=c=0x234567:s=1920x1080" in graph  # manual background color
    assert "fontcolor=0x123456" in graph              # manual text color
    assert "zoompan=" in graph                        # manual zoom
    # Manual size 120 % scales the font size beyond the default 56 px.
    layout = layout_quote(DE_UMLAUT, "", "lora", 1920, 1080,
                          style_key="warm_cinematic", font_size_percent=120,
                          font_weight="regular")
    assert f"fontsize={layout.font_size}" in graph


# --------------------------------------------------------------------------- #
# Quote transition duration + duration validation
# --------------------------------------------------------------------------- #


def _quote_stage2_settings(tmp_path, quote_transition=0.0):
    return ExportSettings(
        workflow_stage="outro", resolution="320x180", aspect="16:9",
        transition_duration=0.5, quote_enabled=True, quote_text=DE_UMLAUT,
        quote_duration=2.0, quote_transition_duration=quote_transition,
        intro_path=str(tmp_path / "intro.mp4"),
        main_video_path=str(tmp_path / "main.mp4"),
        outro_path=str(tmp_path / "outro.mp4"),
        subtitle_enabled=False, crf=28, preset="fast", encoding="CPU",
        normalize_audio=False,
    )


def test_quote_transition_duration_only_around_the_quote(tmp_path, monkeypatch):
    for name in ("intro.mp4", "main.mp4", "outro.mp4"):
        (tmp_path / name).write_bytes(b"\x00fake")
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    project = MainProjectEngine(engine)
    monkeypatch.setattr(
        engine, "analyze",
        lambda paths, log=None: [fake_media(str(p), duration=2.0) for p in paths],
    )
    captured: dict[str, object] = {}

    def fake_make_plan(media, settings, log=None):
        resolved = ResolvedExport(
            width=320, height=180, fps=30.0, fps_expr="30",
            effective_durations=[m.duration for m in media],
            transitions=[0.5] * (len(media) - 1),
            expected_duration=sum(m.duration for m in media) - 0.5 * (len(media) - 1),
        )
        captured["plan"] = resolved
        return resolved

    def fake_export(media, settings, resolved, output_path, **_kwargs):
        captured["transitions"] = list(resolved.transitions)
        captured["expected"] = resolved.expected_duration
        from app.video_merger.models import ValidationReport
        return ValidationReport(
            ok=True, details=[], path=Path(output_path), duration=resolved.expected_duration,
            width=320, height=180, fps=30.0, has_video=True, has_audio=True,
        )

    monkeypatch.setattr(engine, "make_plan", fake_make_plan)
    monkeypatch.setattr(engine, "export", fake_export)
    output, report = project.add_outro(_quote_stage2_settings(tmp_path, 0.25), tmp_path)
    assert report.ok
    # Intro → Quote → Main → Outro: three boundaries; only the two around the
    # quote use the dedicated 0.25 s duration.
    transitions = captured["transitions"]
    assert len(transitions) == 3
    assert transitions[0] == pytest.approx(0.25)
    assert transitions[1] == pytest.approx(0.25)
    assert transitions[2] == pytest.approx(0.5)
    # expected duration recomputed from the adjusted boundaries
    assert captured["expected"] == pytest.approx(2.0 + 2.0 + 2.0 + 2.0 - 0.25 - 0.25 - 0.5)


def test_quote_duration_validation_is_free_range(tmp_path, monkeypatch):
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    project = MainProjectEngine(engine)
    for name in ("intro.mp4", "main.mp4", "outro.mp4"):
        (tmp_path / name).write_bytes(b"\x00fake")
    monkeypatch.setattr(
        engine, "analyze",
        lambda paths, log=None: [fake_media(str(p), duration=2.0) for p in paths],
    )
    settings = _quote_stage2_settings(tmp_path)
    settings.quote_duration = 0.2
    with pytest.raises(VideoMergerError, match="0\\.5–5\\.0"):
        project.add_outro(settings, tmp_path)
    settings.quote_duration = 5.5
    with pytest.raises(VideoMergerError, match="0\\.5–5\\.0"):
        project.add_outro(settings, tmp_path)


# --------------------------------------------------------------------------- #
# E2E: real renders with the new style system
# --------------------------------------------------------------------------- #


def _real_binaries() -> tuple[Path, Path] | None:
    ffmpeg, ffprobe = DT_BIN / "ffmpeg", DT_BIN / "ffprobe"
    if ffmpeg.is_file() and ffprobe.is_file():
        return ffmpeg, ffprobe
    return None


def _mean_volume(ffmpeg: Path, media: Path, start: float, duration: float) -> float:
    detect = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-ss", f"{start:.3f}", "-i", str(media),
         "-t", f"{duration:.3f}", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    match = re.search(r"mean_volume: ([-\d.]+|-inf) dB", detect.stderr)
    assert match is not None
    return float(match.group(1)) if match.group(1) != "-inf" else float("-inf")


def _stats(ffmpeg: Path, media: Path, start: float) -> tuple[float, float]:
    probe = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "info", "-ss", f"{start:.3f}",
         "-i", str(media), "-frames:v", "1", "-vf", "signalstats,metadata=print",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    text = probe.stdout + probe.stderr
    low = re.search(r"YMIN=([\d.]+)", text)
    high = re.search(r"YMAX=([\d.]+)", text)
    assert low and high, text
    return float(low.group(1)), float(high.group(1))


@pytest.mark.e2e
@pytest.mark.parametrize("style_key,expect_dark_text", [
    ("clean_editorial", True),
    ("warm_cinematic", False),
    ("minimal_film", False),
    ("elegant_contrast", False),
    ("soft_paper", True),
])
def test_every_style_renders_a_real_visible_card(tmp_path, style_key, expect_dark_text):
    binaries = _real_binaries()
    if binaries is None:
        pytest.skip("drawtext-fähiger FFmpeg-Build nicht vorhanden (/tmp/ffdev-dt/bin)")
    ffmpeg, ffprobe = binaries
    main = tmp_path / "main.mp4"
    make_clip(ffmpeg, main, size="320x180", fps=30, duration=3.0, color="green")
    settings = ExportSettings(
        workflow_stage="outro", main_video_path=str(main), resolution="320x180",
        aspect="16:9", transition_duration=0.5, quote_enabled=True,
        quote_text=DE_UMLAUT, quote_attribution="Studio Test",
        quote_duration=2.0, quote_style=style_key, quote_font="lora",
        quote_zoom_percent=4.0, crf=28, preset="fast", encoding="CPU",
        normalize_audio=False, subtitle_enabled=False,
    )
    output, report = MainProjectEngine(VideoMergerEngine(ffmpeg, ffprobe)).add_outro(
        settings, tmp_path,
    )
    assert output.is_file() and report.ok, report.details
    # Frame in the middle of the quote (t≈1.25 s): visible typography.
    y_min, y_max = _stats(ffmpeg, output, 1.25)
    if expect_dark_text:
        assert y_min < 90, f"{style_key}: dunkler Text fehlt (YMIN={y_min})"
        assert y_max > 200, f"{style_key}: heller Karten-Hintergrund fehlt (YMAX={y_max})"
    else:
        assert y_max > 120, f"{style_key}: heller Text fehlt (YMAX={y_max})"
        assert y_min < 90, f"{style_key}: dunkler Karten-Hintergrund fehlt (YMIN={y_min})"


@pytest.mark.e2e
def test_nine_by_sixteen_4k_quote_with_zoom_renders_and_stays_silent(tmp_path):
    binaries = _real_binaries()
    if binaries is None:
        pytest.skip("drawtext-fähiger FFmpeg-Build nicht vorhanden (/tmp/ffdev-dt/bin)")
    ffmpeg, ffprobe = binaries
    main = tmp_path / "main.mp4"
    make_clip(ffmpeg, main, size="360x640", fps=30, duration=3.0, color="gray")
    settings = ExportSettings(
        workflow_stage="outro", main_video_path=str(main), resolution="360x640",
        aspect="9:16", transition_duration=0.5, quote_enabled=True,
        quote_text=DE_TEXT, quote_attribution="VideoMerger",
        quote_duration=2.0, quote_style="clean_editorial", quote_font="lora",
        quote_zoom_percent=4.0, crf=28, preset="fast", encoding="CPU",
        normalize_audio=False, subtitle_enabled=False,
    )
    output, report = MainProjectEngine(VideoMergerEngine(ffmpeg, ffprobe)).add_outro(
        settings, tmp_path,
    )
    assert output.is_file() and report.ok, report.details
    assert report.width == 360 and report.height == 640
    # Pure quote window must be acoustically silent (no voiceover/music bleed).
    assert _mean_volume(ffmpeg, output, 0.8, 0.4) <= -60.0
    # …and the card is visible in portrait as well.
    y_min, y_max = _stats(ffmpeg, output, 1.2)
    assert y_min < 90 and y_max > 200


@pytest.mark.e2e
def test_full_sequence_intro_quote_main_outro_renders(tmp_path):
    binaries = _real_binaries()
    if binaries is None:
        pytest.skip("drawtext-fähiger FFmpeg-Build nicht vorhanden (/tmp/ffdev-dt/bin)")
    ffmpeg, ffprobe = binaries
    intro, main, outro = (tmp_path / n for n in ("intro.mp4", "main.mp4", "outro.mp4"))
    make_clip(ffmpeg, intro, size="320x180", fps=30, duration=1.0, color="red")
    make_clip(ffmpeg, main, size="320x180", fps=30, duration=2.5, color="green", audio_rate=None)
    make_clip(ffmpeg, outro, size="320x180", fps=30, duration=1.0, color="blue")
    settings = ExportSettings(
        workflow_stage="outro", intro_path=str(intro), main_video_path=str(main),
        outro_path=str(outro), resolution="320x180", aspect="16:9",
        transition_duration=0.5, quote_enabled=True, quote_text=DE_UMLAUT,
        quote_duration=2.0, quote_style="elegant_contrast", quote_font="lora",
        crf=28, preset="fast", encoding="CPU", normalize_audio=False,
        subtitle_enabled=False,
    )
    output, report = MainProjectEngine(VideoMergerEngine(ffmpeg, ffprobe)).add_outro(
        settings, tmp_path,
    )
    assert output.is_file() and report.ok, report.details
    # 1.0 + 2.0 + 2.5 + 1.0 = 6.5 minus three clamped transitions
    # (0.45 / 0.5 / 0.45) = 5.1 s
    assert report.duration == pytest.approx(5.1, abs=0.2)
    # Quote window silent; outro window carries real outro audio.
    assert _mean_volume(ffmpeg, output, 1.8, 0.5) <= -60.0
    assert _mean_volume(ffmpeg, output, report.duration - 0.4, 0.3) > -60.0
