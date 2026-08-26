"""1.2.4: Standardwerte, zusätzliche Schriftarten und die echte Vorschau.

Abdeckte Anforderungspunkte:

* Intro/Main/Outro Audio-Treffer: alle drei Tracks default „Original",
  unabhängig wählbar (Mute/Low/Original).
* Untertitel-Animation default „Static Phrase" (Long-Form / YouTube
  Landscape); alle fünf Animationen + das komplette Style-System bleiben
  erhalten.
* Output default YouTube Landscape (16:9) + Qualität „Maximum".
* Vier zusätzliche, lizenzrechtlich vertriebbar gebundene Schriftarten
  (Inter, Manrope, Lora, Roboto – Regular + Bold) mit OFL/Apache-2.0;
  proprietäre Eveleth nur Erkennung mit legalen Fallback; der Font-
  Selector listet alle verfügbaren Schriftarten.
* Die Untertitel-Preview rechnet mit denselben Funktionen wie der
  eingebrannte Renderer (Font-Metriken, Zeilenumbruch, Font-Größe,
  Safe-Area/Position) – Preview ≈ Final Render, keine Deko-QLabel,
  kein FFmpeg-Render, sofort auf jede Änderung.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video_merger.font_manager import FONT_OPTIONS, bundled_fonts_dir, font_status, resolve_font
from app.video_merger.models import AlignmentResult, ExportSettings, WordTiming
from app.video_merger import subtitles
from app.video_merger.subtitle_presets import SUBTITLE_PRESETS, get_preset
from app.video_merger.subtitle_preview import preview_cue
from app.video_merger.quality import effective_quality

DE_SAMPLE = "Untertitel folgen exakt der gesprochenen Stimme."


# --------------------------------------------------------------------------- #
# Standardwerte (1.2.4)
# --------------------------------------------------------------------------- #


def test_three_audio_tracks_default_to_original():
    settings = ExportSettings()
    assert settings.original_audio_mode == "original"
    assert settings.intro_audio_mode == "original"
    assert settings.outro_audio_mode == "original"


def test_audio_modes_remain_independently_settable():
    settings = ExportSettings(
        original_audio_mode="original",
        intro_audio_mode="mute",
        outro_audio_mode="low",
    )
    assert settings.original_audio_mode == "original"
    assert settings.intro_audio_mode == "mute"
    assert settings.outro_audio_mode == "low"
    # Jeder der drei Modi bleibt frei wählbar.
    for value in ("mute", "low", "original"):
        assert ExportSettings(intro_audio_mode=value).intro_audio_mode == value
        assert ExportSettings(original_audio_mode=value).original_audio_mode == value
        assert ExportSettings(outro_audio_mode=value).outro_audio_mode == value


def test_default_animation_is_static_phrase_and_all_five_remain():
    assert ExportSettings().subtitle_animation == "static_phrase"
    keys = {key for key, _label in subtitles.ANIMATION_OPTIONS}
    assert keys == {"static_phrase", "word_highlight", "color_change", "outline_highlight", "type_reveal"}
    # Komplettes Style-System intakt: exakt zehn Presets wie in 1.2.3.
    assert len(SUBTITLE_PRESETS) == 10


def test_youtube_landscape_and_maximum_quality_defaults():
    settings = ExportSettings()
    assert settings.aspect == "16:9"
    assert settings.quality_preset == "maximum"
    crf, preset, label = effective_quality(settings)
    from app.video_merger.quality import QUALITY_PRESETS
    assert crf == int(QUALITY_PRESETS["maximum"]["crf"])
    assert preset == str(QUALITY_PRESETS["maximum"]["preset"])
    assert label == str(QUALITY_PRESETS["maximum"]["label"])
    # Long-Form-Stil bleibt der YouTube-Landscape-Standard.
    assert settings.subtitle_style == "long_1"
    assert get_preset("long_1").collection == "long"


# --------------------------------------------------------------------------- #
# Zusätzliche Schriftarten (1.2.4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["inter", "manrope", "lora", "roboto"])
def test_four_new_families_bundled_with_regular_and_bold(key):
    regular = resolve_font(key, bold=False)
    bold = resolve_font(key, bold=True)
    for font, weight in ((regular, "regular"), (bold, "bold")):
        assert font.path is not None, f"{key}/{weight}: Datei fehlt"
        assert Path(font.path).is_file(), f"{key}/{weight}: {font.path} existiert nicht"
        assert font.weight == weight
        assert not font.fallback_used
    # Regular und Bold sind wirklich zwei unterschiedliche Gewichtsdateien.
    assert str(regular.path) != str(bold.path)
    # Auflösung funktioniert ohne Umgebungs-Fonts (portabler Einsatz).
    assert regular.family
    # Die deutsche Großbuchstaben-/Umlautmenge ist im cmap vertreten.
    from app.video_merger.font_manager import _get_font_table
    for font in (regular, bold):
        _units, cmap, _metrics, _fallback = _get_font_table(str(font.path))
        for char in "ÄÖÜäöüßBdghklmnpqrstz0123456789":
            assert ord(char) in cmap, f"{key}: {char!r} nicht im cmap"


def test_bundled_fonts_carry_valid_redistribution_licenses():
    fonts_dir = bundled_fonts_dir()
    license_files = {p.name for p in fonts_dir.iterdir() if p.suffix == ".txt"}
    assert "OFL.txt" in license_files, "OFL-Lizenzdatei fehlt"
    assert "Apache-2.0.txt" in license_files, "Apache-2.0-Lizenzdatei fehlt"
    for key in ("inter", "manrope", "lora", "roboto"):
        for bold in (False, True):
            font = resolve_font(key, bold=bold)
            assert font.path is not None and Path(font.path).is_file()
    # Proprietäre Schriftarten dürfen NICHT gebunden werden.
    bundled_names = {p.name.casefold() for p in fonts_dir.iterdir() if p.suffix in {".ttf", ".otf"}}
    assert not any("eveleth" in name for name in bundled_names)


def test_eveleth_is_detection_only_with_legal_fallback(monkeypatch):
    monkeypatch.delenv("VIDEOMERGER_INSTALLED_FONTS", raising=False)
    font = resolve_font("eveleth_clean")
    # Ohne installierte Lizenz: Noto Sans (OFL) als legale Alternative.
    assert font.fallback_used
    assert font.proprietary
    assert font.family == "Noto Sans"
    assert Path(font.path).is_file()
    assert "legal" in font_status("eveleth_clean")
    # Erkennbare Installation wird genutzt, ohne die Datei zu kopieren.
    monkeypatch.setenv("VIDEOMERGER_INSTALLED_FONTS", "Eveleth Clean Regular")
    installed = resolve_font("eveleth_clean")
    assert installed.family == "Eveleth Clean Regular"
    assert not installed.fallback_used


def test_font_selector_offers_all_available_families():
    keys = {key for key, _label in FONT_OPTIONS}
    assert {"inter", "manrope", "lora", "roboto"} <= keys
    assert {"modern_sans_bold", "clean_sans", "eveleth_clean"} <= keys
    for key in sorted(keys):
        status = font_status(key)
        assert status and len(status) > 0


# --------------------------------------------------------------------------- #
# Echte Untertitel-Preview (1.2.4): dieselbe Logik wie der Renderer
# --------------------------------------------------------------------------- #


def test_preview_uses_renderer_font_size_and_safe_area():
    cases = [
        ("inter", "long_1", "Bottom", 1920, 1080),
        ("lora", "long_3", "Top", 1080, 1920),
        ("manrope", "short_5", "Medium-Low", 3840, 2160),
    ]
    for font_key, preset_key, position, width, height in cases:
        layout = preview_cue(DE_SAMPLE, font_key, preset_key, position, width, height)
        preset = get_preset(preset_key)
        assert layout.font_size == subtitles._font_size(width, height, preset)
        alignment, margin_v = subtitles._position(position, width, height, preset.collection)
        assert (layout.alignment, layout.margin_v) == (alignment, margin_v)
        expected_margin_h = round(width * (.07 if preset.collection == "long" else .055))
        assert layout.margin_h == expected_margin_h
        assert 0 < layout.font_size <= max(width, height)


def test_preview_line_break_is_exactly_the_renderer_split():
    width, height, font_key, preset_key = 1080, 1920, "inter", "long_1"
    preset = get_preset(preset_key)
    size = subtitles._font_size(width, height, preset)
    available = max(40.0, width * .86)
    tokens = DE_SAMPLE.split()
    ok, split = subtitles._layout_words(
        [WordTiming(text=t, start=0.0, end=0.4) for t in tokens], font_key, size, available
    )
    assert ok and split is not None  # 9:16 zerlegt die 6-Wort-Zeile in zwei Zeilen
    layout = preview_cue(DE_SAMPLE, font_key, preset_key, "Medium-Low", width, height)
    assert layout.lines == [tokens[:split], tokens[split:]]
    assert not layout.truncated
    # Wortverlustfreiheit: alle Wörter exakt einmal.
    assert [word for line in layout.lines for word in line] == tokens


def test_preview_never_exceeds_two_lines_and_flags_truncation():
    layout = preview_cue(DE_SAMPLE, "inter", "long_1", "Bottom", 1920, 1080)
    assert len(layout.lines) == 1 and not layout.truncated

    short = preview_cue(DE_SAMPLE, "inter", "short_5", "Bottom", 1920, 1080)
    assert short.truncated
    assert sum(len(line) for line in short.lines) == get_preset("short_5").max_words

    many = preview_cue(" ".join(f"Wort{i}" for i in range(50)), "inter", "long_1", "Bottom", 1920, 1080)
    assert len(many.lines) <= 2
    assert many.truncated


def test_preview_matches_burned_in_cue_layout_exactly():
    """Die Vorschau-Geometrie muss Cue-für-Cue dem ASS-Renderer entsprechen."""
    words = [
        WordTiming(text=token, start=0.0 + i * 0.4, end=0.4 + i * 0.4, script_start=i, script_end=i + 1)
        for i, token in enumerate(DE_SAMPLE.split())
    ]
    alignment = AlignmentResult(words=words, language="de", method="test", compatibility=1.0, average_confidence=1.0)
    cues = subtitles.build_cues(DE_SAMPLE, alignment, "long_1", width=1080, height=1920, font_key="inter")
    two_line = [cue for cue in cues if cue.line_count == 2]
    assert two_line, "Testvoraussetzung: mindestens eine zweizeilige Cue in 9:16"
    for cue in two_line:
        tokens = [word.text for word in cue.words]
        layout = preview_cue(" ".join(tokens), "inter", "long_1", "Medium-Low", 1080, 1920)
        assert layout.lines == [tokens[: cue.line_break_after], tokens[cue.line_break_after :]]
        assert not layout.truncated


def test_preview_reacts_instantly_to_font_style_and_position():
    base = preview_cue(DE_SAMPLE, "inter", "long_1", "Bottom", 1920, 1080)
    # Fontwechsel: Metrik bleibt determiniert durch die echte TTF-Datei.
    for key in ("lora", "manrope", "roboto", "modern_sans_bold"):
        other = preview_cue(DE_SAMPLE, key, "long_1", "Bottom", 1920, 1080)
        assert other.font_family == resolve_font(key).family
        assert 0 < other.font_size == base.font_size  # selbe Auflösung, selbes Preset
        assert len(other.lines) <= 2
    # Stilwechsel ändert Größe/Geometrie entsprechend dem Preset.
    other_style = preview_cue(DE_SAMPLE, "inter", "long_3", "Bottom", 1920, 1080)
    assert other_style.font_size == subtitles._font_size(1920, 1080, get_preset("long_3"))
    assert other_style.font_size != base.font_size
    # Positionswechsel: identische Safe-Area-Werte wie der Renderer.
    top = preview_cue(DE_SAMPLE, "inter", "long_1", "Top", 1920, 1080)
    alignment, margin_v = subtitles._position("Top", 1920, 1080, "long")
    assert (top.alignment, top.margin_v) == (alignment, margin_v)
    assert top.margin_v != base.margin_v


# --------------------------------------------------------------------------- #
# GUI: Defaults + Live-Update ohne Export
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PySide6 nicht verfügbar: {exc}")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_gui_defaults_and_live_preview_updates_without_export(qapp, monkeypatch):
    """GUI-Defaults 1.2.4 und Sofort-Update der echten Vorschau (kein FFmpeg)."""
    from app.video_merger.gui.main_window import MainWindow
    from app.video_merger import quote
    from app.video_merger.subtitle_preview import SAMPLE_TEXTS

    def forbidden(*_args, **_kwargs):  # pragma: no cover - darf nie aufgerufen werden
        raise AssertionError("Die Vorschau darf keinen FFmpeg-Prozess starten.")

    monkeypatch.setattr(subprocess, "run", forbidden)

    window = MainWindow()
    try:
        # --- Defaults 1.2.4 ---
        assert window.radio_16.isChecked()
        assert str(window.subtitle_style_combo.currentData()) == "long_1"
        assert str(window.subtitle_animation_combo.currentData()) == "static_phrase"
        assert str(window.quality_combo.currentData()) == "maximum"
        assert str(window.subtitle_font_combo.currentData()) == "modern_sans_bold"

        sample = SAMPLE_TEXTS["German"]
        layout = window.subtitle_live_preview.current_layout()
        assert layout is not None
        assert layout.preset_key == "long_1"
        # Exakt die Renderer-Geometrie für 16:9.
        assert layout.font_size == subtitles._font_size(1920, 1080, get_preset("long_1"))
        alignment, margin_v = subtitles._position("Bottom", 1920, 1080, "long")
        assert (layout.alignment, layout.margin_v) == (alignment, margin_v)

        # --- Fontwechsel: sofort, ohne Export, echte Metrik ---
        window.subtitle_font_combo.setCurrentIndex(window.subtitle_font_combo.findData("inter"))
        layout = window.subtitle_live_preview.current_layout()
        assert layout.font_key == "inter"
        assert layout.font_size == subtitles._font_size(1920, 1080, get_preset("long_1"))
        joined = " ".join(" ".join(line) for line in layout.lines)
        assert sample.split()[0] in joined

        # --- Animationswechsel: alle fünf, ohne Export ---
        for key in ("word_highlight", "color_change", "outline_highlight", "type_reveal", "static_phrase"):
            window.subtitle_animation_combo.setCurrentIndex(window.subtitle_animation_combo.findData(key))
            layout = window.subtitle_live_preview.current_layout()
            assert layout is not None and len(layout.lines) <= 2

        # --- Positionswechsel: Renderer-Safe-Area ---
        window.subtitle_position_combo.setCurrentText("Top")
        layout = window.subtitle_live_preview.current_layout()
        alignment, margin_v = subtitles._position("Top", 1920, 1080, "long")
        assert (layout.alignment, layout.margin_v) == (alignment, margin_v)

        # --- Quote-Preview: exakt die layout_quote()-Geometrie ---
        window.quote_check.setChecked(True)
        quote_text = "„Klarer Fokus.\nEchte Qualität.\n– ohne leere Worte“"
        window.quote_text_edit.setPlainText(quote_text)
        window.quote_attribution_edit.setText("– Test")
        font_key = str(window.quote_font_combo.currentData())
        expected = quote.layout_quote(quote_text, "– Test", font_key, 1920, 1080)
        got = window.quote_preview.current_layout()
        assert got is not None
        assert list(got.lines) == list(expected.lines)
        assert got.font_size == expected.font_size
        assert got.line_top == expected.line_top
        assert got.line_height == expected.line_height
        assert got.attribution_size == expected.attribution_size
    finally:
        window.close()
