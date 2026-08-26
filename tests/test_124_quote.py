"""1.2.4: Quote Card – Layout, Filter-Chain, Stage-2-Wiring, Stille, E2E-Render.

Die Quote-Karte ist ein synthetischer Abschnitt: komplett im Filtergraph
generiert (color + Vignette + drawtext), immer stumm, nie mit ``-i``-Input,
nie auf der SRT/VTT/Burn-in-, Voiceover- oder Musik-Timeline.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.models import ExportSettings, MediaInfo, ResolvedExport, ValidationReport
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.errors import VideoMergerError
from app.video_merger import quote
from app.video_merger.quote import layout_quote, quote_video_chain
from app.video_merger.font_manager import resolve_font
from tests.conftest import fake_media, make_clip

DE_10 = "Klarer Fokus auf dem wichtigsten Gedanken"  # 6 Wörter
DE_LONG = ("Wenige Wörter, viel Luft – der Satz bleibt ein einziger, ruhiger "
           "Fokus auf dem Format, den Raum und den Blick des Zuschauers.")  # 20 Wörter
DE_UMLAUT = "Größe, Mühe und Größe der Ökonomie: äöü und ÄÖÜ bleiben ganz."
EN_LONG = ("The quiet frame holds one idea, lets the room breathe, and keeps "
           "the viewer's attention on a single, carefully balanced statement.")

DT_BIN = Path("/tmp/ffdev-dt/bin")  # BtbN-Build mit drawtext (Suite-Build hat keins)


def _quote_item(duration: float = 2.0) -> MediaInfo:
    return MediaInfo(
        path=Path("<generated:quote-card>"),
        duration=duration, width=0, height=0, fps=30.0,
        effective_width=0, effective_height=0, fps_fraction="30/1",
        video_codec="generated", pixel_format="yuv420p", sar="1:1", dar="",
        source_duration=duration, is_generated_quote=True,
    )


# --------------------------------------------------------------------------- #
# Layout: Resolution, Sprachen, Zeilenbalance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("width,height,expected_size", [
    (1920, 1080, 56),   # 1080p  -> 1080 * 0.052
    (2560, 1440, 75),   # 1440p  -> 1440 * 0.052
    (3840, 2160, 112),  # 4K     -> 2160 * 0.052
    (1080, 1920, 56),   # 9:16   -> 1080 * 0.052 (min-Edge)
    (2160, 3840, 112),  # 9:16 4K
])
def test_quote_font_size_scales_with_resolution(width, height, expected_size):
    lay = layout_quote(DE_UMLAUT, "", "inter", width, height)
    assert lay.font_size == expected_size
    assert lay.width == width and lay.height == height
    assert lay.center_x == width // 2


def test_quote_never_splits_or_drops_words_acres_matrix():
    font = resolve_font("inter", bold=True)
    for text in (DE_10, DE_LONG, DE_UMLAUT, EN_LONG):
        for width, height in ((1920, 1080), (3840, 2160), (1080, 1920)):
            lay = layout_quote(text, "", "inter", width, height)
            lines = list(lay.lines)
            assert lines, (text, width, height)
            # Keine Wort-Brüche, keine Wort-Verluste, keine Erfindungen:
            assert " ".join(lines) == " ".join(text.split())
            words = text.split()
            assert [w for line in lines for w in line.split(" ")] == words
            assert len(lines) <= quote.MAX_QUOTE_LINES
            # Keine Zeile darf die erlaubte Breite überschreiten.
            limit = width * (quote.QUOTE_MAX_LINE_RATIO_LANDSCAPE if width >= height
                             else quote.QUOTE_MAX_LINE_RATIO_PORTRAIT)
            for line in lines:
                assert font.text_width(line, lay.font_size) <= limit + 2.0, (line, width, height)


def test_quote_single_line_short_text():
    lay = layout_quote("Klarer Fokus.", "", "inter", 1920, 1080)
    assert list(lay.lines) == ["Klarer Fokus."]


def test_quote_long_text_balanced_and_within_four_lines():
    lay = layout_quote(DE_LONG, "", "inter", 1920, 1080)
    counts = [len(line.split(" ")) for line in lay.lines]
    assert len(lay.lines) <= 4
    assert sum(counts) == len(DE_LONG.split())
    # Balance: keine Zeile ist mehr als 1.6x breiter als die breiteste
    # Nachbar-Kombination zulässt – praktisch: Max-Min-Verhältnis der
    # Wortanzahlen bleibt moderat (kein 1-Wort-Stub neben vollen Zeilen).
    if len(lay.lines) >= 3:
        assert max(counts) - min(counts) <= 3, counts


def test_quote_no_lone_word_final_line_invariant():
    """Eine 1-Wort-Schlusszeile darf nur stehen bleiben, wenn kein Wort
    der Nachbarzeile mehr hineinpasst (Singleton-Repair)."""
    font = resolve_font("inter", bold=True)
    for text in (DE_10, DE_LONG, DE_UMLAUT, EN_LONG):
        for width, height in ((1920, 1080), (2560, 1440), (1080, 1920)):
            lay = layout_quote(text, "", "inter", width, height)
            lines = list(lay.lines)
            if len(lines) < 3:
                continue
            limit = width * (quote.QUOTE_MAX_LINE_RATIO_LANDSCAPE if width >= height
                             else quote.QUOTE_MAX_LINE_RATIO_PORTRAIT)
            if len(lines[-1].split(" ")) == 1:
                second = lines[-2].split(" ")
                assert len(second) == 1  # sonst hätte das Repair gewirkt
                candidate = f"{lines[-1]} {second[-1]}"
                assert font.text_width(candidate, lay.font_size) > limit


def test_quote_positioned_slightly_above_mathematical_center():
    for width, height in ((1920, 1080), (3840, 2160), (1080, 1920)):
        lay = layout_quote(DE_LONG, "VideoMerger Studio", "inter", width, height)
        center_of_block = lay.line_top + lay.total_block_height / 2
        assert center_of_block < height / 2, (width, height, center_of_block)
        # aber nicht zu weit oben: maximal ~15 % der Höhe über Zentrum
        assert center_of_block > height / 2 - height * 0.15


def test_quote_attribution_shares_font_family_never_ultra_thin():
    lay = layout_quote(DE_10, "Studio Example", "inter", 1920, 1080)
    assert lay.font_path is not None and lay.font_path.is_file()
    assert "inter" in lay.font_path.name.lower()
    assert lay.attribution_size > 0
    assert lay.attribution_size < lay.font_size
    assert lay.attribution_y is not None
    assert lay.font_size >= quote.QUOTE_FONT_SIZE_FLOOR + 4  # kein Ultra-Thin
    plain = layout_quote(DE_10, "", "inter", 1920, 1080)
    assert plain.attribution_y is None and plain.attribution_size == 0


def test_quote_empty_text_layout_is_safe():
    lay = layout_quote("", "", "inter", 1920, 1080)
    assert list(lay.lines) == [""]


# --------------------------------------------------------------------------- #
# Filter-Chain: color + Vignette + drawtext, kein Input
# --------------------------------------------------------------------------- #


def test_quote_video_chain_structure():
    lay = layout_quote(DE_UMLAUT, "Studio", "inter", 1920, 1080)
    chain = quote_video_chain(lay, 1920, 1080, 30, 2.0, "base1")
    assert len(chain) == 1
    text = chain[0]
    assert text.startswith(f"color=c={quote.BACKGROUND_HEX}:s=1920x1080")
    assert "d=2" in text.split(",")[0]
    assert "vignette=" in text
    assert "format=yuv420p,setsar=1" in text
    # Eine drawtext-Zeile pro Layout-Zeile + Attribution:
    assert text.count("drawtext=") == len(lay.lines) + 1
    assert f"fontsize={lay.font_size}" in text
    assert text.count("expansion=none") == text.count("drawtext=")  # % bleibt literal
    assert text.rstrip("]").endswith("base1") or text.endswith("base1]")
    assert "<generated" not in text


def test_quote_drawtext_contains_umlaut_lines():
    """Jede Layout-Zeile muss byte-genau (mit Umlauten) im drawtext-Value
    vorkommen – geprüft über dieselbe Escape-Funktion, die der Builder nutzt.
    Einfaches Wort-Vergleichen geht nicht: Kommas/Zeichen werden zweistufig
    escaped (`,` -> `\\\\,`), sodass naive Text-Extraktion scheitert."""
    from app.video_merger.filter_escape import escape_drawtext_text

    lay = layout_quote(DE_UMLAUT, "", "inter", 1920, 1080)
    chain = quote_video_chain(lay, 1920, 1080, 30, 2.0, "base0")[0]
    # drawtext-Count passt: exakt so viele Text-Optionen wie Layout-Zeilen.
    # (Trennungs-anker, weil der Filtername ``drawtext=`` selbst ``text=`` enthält)
    assert len(re.findall(r"[,:]text=", chain)) == len(lay.lines)
    for line in lay.lines:
        escaped = escape_drawtext_text(line)
        assert f"text={escaped}" in chain, (
            f"Zeile fehlt byte-genau im drawtext-Chain: {line!r} "
            f"(erwartet {escaped!r})"
        )


# --------------------------------------------------------------------------- #
# Command-Builder: kein -i, stumm, keine Sub/VO/Musik auf Quote
# --------------------------------------------------------------------------- #


def _stage2_media_and_settings(tmp_path, with_quote: bool, quote_duration=2.0):
    media = [fake_media(str(tmp_path / "intro.mp4"), duration=1.0)]
    if with_quote:
        media.append(_quote_item(quote_duration))
    media.append(fake_media(str(tmp_path / "main.mp4"), duration=3.0))
    media.append(fake_media(str(tmp_path / "outro.mp4"), duration=1.0))
    modes = ["original"]
    if with_quote:
        modes.append("mute")
    modes += ["original", "original"]
    settings = ExportSettings(
        workflow_stage="outro",
        resolution="1920x1080",
        aspect="16:9",
        transition_duration=0.5,
        quote_enabled=with_quote,
        quote_text=DE_UMLAUT if with_quote else "",
        quote_duration=float(quote_duration),
        stage2_audio_modes=modes,
        subtitle_enabled=False,
        watermark_enabled=False,
        voiceover_paths=[],
        music_path="",
        crf=28,
        preset="fast",
        encoding="CPU",
        normalize_audio=False,
    )
    n = len(media)
    resolved = ResolvedExport(
        width=1920, height=1080, fps=30.0, fps_expr="30",
        effective_durations=[1.0, 2.0, 3.0, 1.0][:(n)],
        transitions=[0.5] * (n - 1),
        expected_duration=5.0,
        warnings=[],
    )
    return media, settings, resolved


def test_stage2_command_has_no_input_for_quote_and_is_silent(tmp_path):
    media, settings, resolved = _stage2_media_and_settings(tmp_path, with_quote=True)
    builder = FFmpegCommandBuilder("/nonexistent/ffmpeg")
    graph = builder.build_filter_graph(media, settings, resolved)
    built = builder.build(media, settings, resolved, tmp_path / "out.mp4")
    command_text = " ".join(built.command)

    # 1) Kein -i-Input für die Karte – nur Intro, Main, Outro:
    assert "<generated:quote-card>" not in command_text
    assert command_text.count("-i ") == 3
    assert "intro.mp4" in command_text and "main.mp4" in command_text and "outro.mp4" in command_text

    # 2) Die Karte wird im Graph generiert:
    assert f"color=c={quote.BACKGROUND_HEX}:s=1920x1080" in graph
    assert "vignette=" in graph

    # 3) Stumm: exakt ein anullsrc-Zweig (das Audio der Karte, Label a1):
    anullsrc = [seg for seg in graph.split(";") if "anullsrc=" in seg]
    assert len(anullsrc) == 1
    assert "[a1]" in anullsrc[0]
    assert "volume=0" not in graph  # kein anderer Abschnitt ist gemutet

    # 4) Übergänge durch das bestehende Transition-System (3xfade):
    assert graph.count("xfade=transition=custom") == 3

    # 5) Keine Subtitles, kein Voiceover, keine Musik in Stage 2:
    assert "subtitles=" not in graph
    assert "ass=" not in graph
    assert "voiceover" not in command_text.lower().replace("voiceover_paths", "")
    assert "stream_loop" not in command_text


def test_stage2_without_quote_is_unchanged(tmp_path):
    media, settings, resolved = _stage2_media_and_settings(tmp_path, with_quote=False)
    builder = FFmpegCommandBuilder("/nonexistent/ffmpeg")
    graph = builder.build_filter_graph(media, settings, resolved)
    built = builder.build(media, settings, resolved, tmp_path / "out.mp4")
    assert " ".join(built.command).count("-i ") == 3
    assert "color=c=" not in graph
    assert "drawtext=" not in graph
    assert graph.count("xfade=transition=custom") == 2


# --------------------------------------------------------------------------- #
# Stage-2-Wiring in MainProjectEngine.add_outro
# --------------------------------------------------------------------------- #


def _dummy_mp4(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x00fake")
    return path


def _captured_add_outro(tmp_path, quote_enabled: bool, quote_text: str = DE_UMLAUT,
                        quote_duration: float = 2.0):
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    project = MainProjectEngine(engine)
    intro, main, outro = _dummy_mp4(tmp_path, "intro.mp4"), _dummy_mp4(tmp_path, "main.mp4"), _dummy_mp4(tmp_path, "outro.mp4")

    analyzed: list[MediaInfo] = []

    def fake_analyze(paths, log=None):
        items = [
            fake_media(str(p), duration=2.0)
            for p in paths
        ]
        analyzed[:] = items
        return items

    engine.analyze = fake_analyze
    captured: dict = {}

    def fake_make_plan(media, settings, log=None):
        captured["media"] = list(media)
        captured["settings"] = settings
        return ResolvedExport(
            width=1920, height=1080, fps=30.0, fps_expr="30",
            effective_durations=[m.duration for m in media],
            transitions=[0.5] * (len(media) - 1),
            expected_duration=6.0, warnings=[],
        )

    engine.make_plan = fake_make_plan
    engine.export = lambda *a, **k: ValidationReport(ok=True, details=[], path=a[3])

    settings = ExportSettings(
        intro_path=str(intro),
        main_video_path=str(main),
        outro_path=str(outro),
        transition_duration=0.5,
        quote_enabled=quote_enabled,
        quote_text=quote_text,
        quote_duration=quote_duration,
        crf=28, preset="fast", encoding="CPU", normalize_audio=False,
    )
    project.add_outro(settings, tmp_path)
    return captured


def test_add_outro_inserts_quote_between_intro_and_main(tmp_path):
    captured = _captured_add_outro(tmp_path, quote_enabled=True)
    media = captured["media"]
    settings = captured["settings"]

    assert len(media) == 4
    assert not media[0].is_generated_quote
    assert media[1].is_generated_quote
    assert media[1].duration == pytest.approx(2.0)
    assert not media[2].is_generated_quote  # Main
    assert not media[3].is_generated_quote  # Outro

    # Quote ist von der Subtitle-/Voiceover-/Musik-Timeline isoliert:
    assert settings.subtitle_enabled is False
    assert settings.voiceover_paths == []
    assert settings.voiceover_path == ""
    assert settings.music_path == ""
    assert settings.script_paths == []

    # Pro-Abschnitt-Audio: Intro Original, Quote stumm, Main/Outro Original:
    assert settings.stage2_audio_modes == ["original", "mute", "original", "original"]

    # Transition-System bleibt aktiv (Quote→Main + Intro→Quote + Main→Outro):
    assert settings.transition_duration == 0.5


def test_add_outro_without_quote_keeps_three_sections(tmp_path):
    captured = _captured_add_outro(tmp_path, quote_enabled=False)
    media = captured["media"]
    assert len(media) == 3
    assert not any(m.is_generated_quote for m in media)
    assert captured["settings"].stage2_audio_modes == ["original", "original", "original"]


def _add_outro_expect_error(tmp_path, quote_text: str, quote_duration: float) -> None:
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    project = MainProjectEngine(engine)
    intro, main = _dummy_mp4(tmp_path, "intro.mp4"), _dummy_mp4(tmp_path, "main.mp4")
    engine.analyze = lambda paths, log=None: [fake_media(str(p), duration=2.0) for p in paths]
    settings = ExportSettings(
        intro_path=str(intro),
        main_video_path=str(main),
        quote_enabled=True, quote_text=quote_text, quote_duration=quote_duration,
        crf=28, preset="fast", encoding="CPU", normalize_audio=False,
    )
    return project, settings


def test_add_outro_quote_empty_text_is_clear_error(tmp_path):
    project, settings = _add_outro_expect_error(tmp_path, "   ", 2.0)
    with pytest.raises(VideoMergerError, match="Quote-Text ist leer"):
        project.add_outro(settings, tmp_path)


def test_add_outro_quote_invalid_duration_is_clear_error(tmp_path):
    project, settings = _add_outro_expect_error(tmp_path, DE_UMLAUT, 2.2)
    with pytest.raises(VideoMergerError, match="1\\.0 / 1\\.5 / 2\\.0 / 2\\.5 / 3\\.0"):
        project.add_outro(settings, tmp_path)


@pytest.mark.parametrize("duration", [1.0, 1.5, 2.0, 2.5, 3.0])
def test_add_outro_all_allowed_quote_durations_pass_validation(tmp_path, duration):
    project, settings = _add_outro_expect_error(tmp_path, DE_UMLAUT, duration)
    # Die fünf erlaubten Dauern dürfen an der Validierung nicht scheitern.
    # (Der Export selbst wird per Sentinel abgefangen.)
    from app.video_merger.models import ResolvedExport
    project.engine.make_plan = lambda media, settings, log=None: ResolvedExport(
        width=320, height=180, fps=30.0, fps_expr="30",
        effective_durations=[m.duration for m in media],
        transitions=[0.5] * (len(media) - 1), expected_duration=3.0, warnings=[],
    )
    sentinel = RuntimeError("EXPORT REACHED")
    project.engine.export = lambda *a, **k: (_ for _ in ()).throw(sentinel)
    with pytest.raises(RuntimeError, match="EXPORT REACHED"):
        project.add_outro(settings, tmp_path)


def test_create_complete_gate_accepts_quote_without_intro_outro(tmp_path):
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    project = MainProjectEngine(engine)

    # Ohne Quote und ohne Intro/Outro: klare Fehlermeldung.
    blocked = ExportSettings(quote_enabled=False, crf=28, preset="fast", encoding="CPU", normalize_audio=False)
    with pytest.raises(VideoMergerError, match="One-Click benötigt"):
        project.create_complete([], blocked, tmp_path)

    # Mit aktiver Quote (Text + gültige Dauer) passiert das Gate:
    # Stage 1 würde starten (hier per Sentinel abgefangen).
    sentinel = RuntimeError("GATE PASSED")
    project.create_main = lambda *a, **k: (_ for _ in ()).throw(sentinel)
    quoted = ExportSettings(
        quote_enabled=True, quote_text=DE_UMLAUT, quote_duration=2.0,
        crf=28, preset="fast", encoding="CPU", normalize_audio=False,
    )
    with pytest.raises(RuntimeError, match="GATE PASSED"):
        project.create_complete([], quoted, tmp_path)


# --------------------------------------------------------------------------- #
# E2E: Echter Render Intro → Quote → Main (FFmpeg mit drawtext)
# --------------------------------------------------------------------------- #


def _dt_binaries() -> tuple[Path, Path] | None:
    ffmpeg, ffprobe = DT_BIN / "ffmpeg", DT_BIN / "ffprobe"
    if ffmpeg.is_file() and ffprobe.is_file():
        return ffmpeg, ffprobe
    return None


@pytest.mark.e2e
def test_intro_quote_main_renders_silent_quote_section(tmp_path):
    binaries = _dt_binaries()
    if binaries is None:
        pytest.skip("drawtext-fähiger FFmpeg-Build nicht vorhanden (/tmp/ffdev-dt/bin)")
    ffmpeg, ffprobe = binaries

    intro, main = tmp_path / "intro.mp4", tmp_path / "main.mp4"
    make_clip(ffmpeg, intro, size="320x180", fps=30, duration=1.0, color="red")
    make_clip(ffmpeg, main, size="320x180", fps=30, duration=3.0, color="green")

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    project = MainProjectEngine(engine)
    settings = ExportSettings(
        workflow_stage="outro",
        intro_path=str(intro),
        main_video_path=str(main),
        transition_duration=0.5,
        quote_enabled=True,
        quote_text=DE_UMLAUT,
        quote_duration=2.0,
        resolution="Auto",
        aspect="16:9",
        crf=28, preset="fast", encoding="CPU", normalize_audio=False,
        subtitle_enabled=False,
    )
    output, report = project.add_outro(settings, tmp_path)
    assert output.is_file()
    assert report.ok, report.details
    # 1.0 + 2.0 + 3.0 - (0.45 + 0.5) = 5.05 s
    # (Intro-Clip 1.0s begrenzt seinen Übergang auf 0.45s)
    assert report.duration == pytest.approx(5.05, abs=0.15)

    # Frame in der Mitte der Quote (t=2.0s): heller Text auf dunklem Grund.
    probe = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "info",
         "-ss", "2.0", "-i", str(output), "-frames:v", "1",
         "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YMAX",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    y_max = re.search(r"YMAX=([\d.]+)", probe.stdout + probe.stderr)
    assert y_max is not None, probe.stderr
    assert float(y_max.group(1)) >= 120  # Text ist sichtbar gerendert

    # Reine Quote-Stille: Audio-Timeline = Intro [0,1.0] + Acrossfade 0.45 s
    # + Quote [1.0,3.0] + Acrossfade 0.5 s + Main [3.0,6.0]. Das Fenster
    # 1.1–1.5 s liegt komplett im reinen Quote-Bereich (ohne Crossfade):
    detect = subprocess.run(
        [str(ffmpeg), "-hide_banner",
         "-ss", "1.1", "-i", str(output),
         "-t", "0.4", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    match = re.search(r"mean_volume: ([-\d.]+|-inf) dB", detect.stderr)
    assert match is not None, detect.stderr
    mean = float(match.group(1)) if match.group(1) != "-inf" else float("-inf")
    assert mean <= -60.0, f"Quote-Audio muss stumm sein, war {mean} dB"

    # Vergleich: Hauptabschnitt (t=3.5s) hat echtes Audio:
    detect_main = subprocess.run(
        [str(ffmpeg), "-hide_banner",
         "-ss", "3.5", "-i", str(output),
         "-t", "1.0", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    match_main = re.search(r"mean_volume: ([-\d.]+|-inf) dB", detect_main.stderr)
    assert match_main is not None
    mean_main = float(match_main.group(1)) if match_main.group(1) != "-inf" else float("-inf")
    assert mean_main > mean  # Main (Sine-Ton) ist hörbar, Quote nicht


@pytest.mark.e2e
def test_quote_with_outro_renders_all_transitions(tmp_path):
    binaries = _dt_binaries()
    if binaries is None:
        pytest.skip("drawtext-fähiger FFmpeg-Build nicht vorhanden (/tmp/ffdev-dt/bin)")
    ffmpeg, ffprobe = binaries

    intro, main, outro = (tmp_path / n for n in ("intro.mp4", "main.mp4", "outro.mp4"))
    make_clip(ffmpeg, intro, size="320x180", fps=30, duration=1.0, color="red")
    make_clip(ffmpeg, main, size="320x180", fps=30, duration=3.0, color="green")
    make_clip(ffmpeg, outro, size="320x180", fps=30, duration=1.0, color="blue")

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    project = MainProjectEngine(engine)
    settings = ExportSettings(
        workflow_stage="outro",
        intro_path=str(intro),
        main_video_path=str(main),
        outro_path=str(outro),
        transition_duration=0.5,
        outro_transition_enabled=True,
        quote_enabled=True,
        quote_text="Zwei Zeilen für das Format – mit Bindestrich und Punkt.",
        quote_duration=1.5,
        resolution="Auto",
        aspect="16:9",
        crf=28, preset="fast", encoding="CPU", normalize_audio=False,
        subtitle_enabled=False,
    )
    output, report = project.add_outro(settings, tmp_path)
    assert report.ok, report.details
    # 1.0 + 1.5 + 3.0 + 1.0 - (0.45 + 0.5 + 0.45) = 5.1 s
    # (1.0s-Clips begrenzen benachbarte Übergänge auf 45 % = 0.45s)
    assert report.duration == pytest.approx(5.1, abs=0.15)
