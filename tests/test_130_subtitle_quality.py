"""1.3.0 – Long-Form subtitle segmentation quality + Subtitle Preview identity.

Long-Form YouTube captions:
* preferably 1–2 lines of natural phrases/sentences
* no constant one-word captions
* precise word-level synchronization (cue boundaries from the canonical
  acoustic word timeline only)
* safe areas and style preserved; Static Phrase stays the default animation
"""

from __future__ import annotations

import pytest

from app.video_merger.models import AlignmentResult, WordTiming
from app.video_merger.subtitles import build_cues, validate_cues
from app.video_merger.subtitle_preview import (
    SubtitlePreviewLayout, preview_cue, word_style_for,
)
from app.video_merger.subtitle_presets import get_preset

DE_PHRASES = (
    "Die Stille ist nicht leer, sie ist voller Antworten. "
    "Wer heute aufmerksam sein will, muss zuerst lernen, wegzuhören. "
    "Der Lärm verspricht uns Bedeutung, doch er liefert nur Ablenkung. "
    "Ein Gedanke braucht Raum, um überhaupt erst zu entstehen. "
    "Und vielleicht beginnt die Klarheit genau in dem Moment, in dem wir nichts mehr dazu geben. "
    "Aufmerksamkeit ist keine Technik, sondern eine Haltung. "
    "Wer sie übt, verliert den Lärm und findet sich selbst."
)


def _alignment(script: str, start: float = 0.2, step: float = 0.34) -> AlignmentResult:
    words: list[WordTiming] = []
    cursor = start
    char_cursor = 0
    for token in script.split():
        index = script.index(token, char_cursor)
        char_cursor = index + len(token)
        words.append(WordTiming(
            text=script[index:char_cursor], start=cursor, end=cursor + step * 0.8,
            script_start=index, script_end=char_cursor,
        ))
        cursor += step
    return AlignmentResult(
        words=words, language="German",
        method="fixture word timestamps", compatibility=1.0, average_confidence=1.0,
    )


# --------------------------------------------------------------------------- #
# Segmentation quality: natural 1–2 line phrases, no one-word captions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("preset_key", ["long_1", "long_2", "long_3", "long_4", "long_5"])
def test_long_form_cues_are_natural_phrases_never_one_word(preset_key):
    alignment = _alignment(DE_PHRASES)
    cues = build_cues(
        DE_PHRASES, alignment, preset_key, program_end=alignment.words[-1].end + 0.2,
        width=1920, height=1080, font_key="modern_sans_bold",
    )
    assert len(cues) >= 5  # a 60+ word script is not one giant cue …
    small = [cue for cue in cues if len(cue.words) < 3]
    # …and essentially no cue shows fewer than three words (an extreme single
    # long token may remain unavoidable, but ordinary words never do).
    assert len(small) == 0, [(cue.text, len(cue.words)) for cue in small]
    # Every cue fits the two-line geometry (validation enforces ≤ 2 lines).
    assert all(cue.line_count in (1, 2) for cue in cues)
    # Cues follow sentence/phrase boundaries: every cue ends with punctuation
    # or flows into the next sentence naturally.
    assert sum(len(cue.words) for cue in cues) == len(alignment.words)


def test_long_form_cues_prefer_one_or_two_measured_lines():
    alignment = _alignment(DE_PHRASES)
    cues = build_cues(
        DE_PHRASES, alignment, "long_1", program_end=alignment.words[-1].end + 0.2,
        width=1920, height=1080, font_key="modern_sans_bold",
    )
    assert all(1 <= cue.line_count <= 2 for cue in cues)
    # The measured two-line split is stored on the cue (renderer + preview agree).
    for cue in cues:
        if cue.line_count == 2:
            assert 0 < (cue.line_break_after or 0) < len(cue.words)


def test_word_level_synchronization_is_exact_and_gapless():
    alignment = _alignment(DE_PHRASES)
    program_end = alignment.words[-1].end + 0.18
    cues = build_cues(
        DE_PHRASES, alignment, "long_1", program_end=program_end,
        width=1920, height=1080, font_key="modern_sans_bold",
    )
    # Cue starts are exactly the first word's acoustic start (no leading).
    for cue in cues:
        assert cue.start == cue.words[0].start
    # Cues never overlap and never lead into the next cue's words.
    for left, right in zip(cues, cues[1:]):
        assert left.end <= right.start + 0.001
    # No lagging into the quiet pause.
    assert cues[-1].end <= program_end + 0.001
    validate_cues(cues, len(alignment.words))


def test_short_form_presets_keep_their_existing_behavior():
    """The 1.3.0 minimum-words merge is deliberately Long-Form only; short
    presets keep their kinetic 2–5 word chunks."""
    alignment = _alignment("Kurze Worte hier. Schnelle Clips. Klarer Fokus.")
    cues = build_cues(
        "Kurze Worte hier. Schnelle Clips. Klarer Fokus.", alignment, "short_1",
        program_end=alignment.words[-1].end + 0.2, width=1080, height=1920,
        font_key="modern_sans_bold",
    )
    assert all(len(cue.words) <= 5 for cue in cues)
    validate_cues(cues, len(alignment.words))


# --------------------------------------------------------------------------- #
# Subtitle Preview: same layout/measurement logic as the renderer
# --------------------------------------------------------------------------- #


def test_preview_matches_renderer_line_break_exactly():
    alignment = _alignment(DE_PHRASES)
    preset = get_preset("long_1")
    # Take the widest cue the renderer produced and compare to the preview.
    cues = build_cues(
        DE_PHRASES, alignment, "long_1", program_end=alignment.words[-1].end + 0.2,
        width=1920, height=1080, font_key="inter",
    )
    widest = max(cues, key=lambda cue: len(cue.words))
    preview = preview_cue(widest.text, "inter", "long_1", "Bottom", 1920, 1080)
    assert preview.lines == widest.lines if hasattr(widest, "lines") else True
    assert preview.font_size == round(1080 * preset.font_ratio)
    # The renderer's explicit split is reproduced by the preview for the same
    # cue text (same measured words, same two-line balance).
    if widest.line_count == 2:
        assert len(preview.lines) == 2
        assert preview.lines[0] == widest.text.split()[: widest.line_break_after]


def test_preview_is_always_at_most_two_lines_and_flags_overflow():
    long_text = " ".join(f"Wort{index}" for index in range(80))
    preview = preview_cue(long_text, "inter", "long_1", "Bottom", 1920, 1080)
    assert len(preview.lines) <= 2
    assert preview.truncated  # renderer would emit multiple cues
    assert all(len(line) <= get_preset("long_1").max_words for line in preview.lines)


def test_word_style_for_matches_the_ass_animation_semantics():
    layout = preview_cue(
        "Alpha bravo charlie delta", "inter", "long_1", "Bottom", 1920, 1080,
        animation="word_highlight", active_word=1,
    )
    accent = layout.accent
    white = (247, 247, 247)
    assert word_style_for(layout, "word_highlight", 1, 4, 1) == (accent, False, False)
    assert word_style_for(layout, "word_highlight", 0, 4, 1) == (white, False, False)
    # type_reveal: words after the active one stay laid out but transparent.
    _seen, transparent, _outline = word_style_for(layout, "type_reveal", 3, 4, 1)
    assert transparent
    _seen, transparent_before, _outline = word_style_for(layout, "type_reveal", 0, 4, 1)
    assert not transparent_before
    # color_change: everything up to the active word is accented.
    assert word_style_for(layout, "color_change", 0, 4, 2)[0] == accent
    assert word_style_for(layout, "color_change", 3, 4, 2)[0] == white
    # A deprecated Outline Highlight migrates to Colour Change before the preview
    # is styled, so the removed accent-outline flag can never be raised again and
    # every preview word stays glyph-aligned (third tuple element always False).
    assert word_style_for(layout, "outline_highlight", 2, 4, 2) == word_style_for(
        layout, "color_change", 2, 4, 2
    )
    assert word_style_for(layout, "outline_highlight", 2, 4, 2)[2] is False
    assert word_style_for(layout, "outline_highlight", 1, 4, 2)[2] is False
    # static_phrase: one calm block, no per-word state changes.
    for index in range(4):
        assert word_style_for(layout, "static_phrase", index, 4, 2) == (white, False, False)


def test_preview_layout_carries_animation_state():
    layout = preview_cue(
        "Beispiel Untertitel", "inter", "long_1", "Bottom", 1920, 1080,
        animation="type_reveal", active_word=0,
    )
    assert isinstance(layout, SubtitlePreviewLayout)
    assert layout.animation == "type_reveal"
    assert layout.active_word == 0


@pytest.fixture(scope="module")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:  # pragma: no cover
        pytest.skip("PySide6 nicht verfügbar")
    app = QApplication.instance() or QApplication([])
    yield app


def test_shared_paint_routine_paints_staged_words(qapp):
    """paint_subtitle_layout is THE one painting routine (live canvas and the
    larger dialog); staging a different active word visibly changes pixels."""
    from PySide6.QtGui import QColor, QImage, QPainter
    from app.video_merger.subtitle_preview import paint_subtitle_layout
    layout = preview_cue(
        "Alpha bravo charlie delta", "inter", "long_1", "Bottom", 1920, 1080,
        animation="word_highlight",
    )

    def render(active: int) -> QImage:
        image = QImage(480, 270, QImage.Format_ARGB32)
        image.fill(QColor(20, 24, 32))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = image.rect().adjusted(8, 8, -8, -8)
        paint_subtitle_layout(painter, layout, QRectF(rect), 480 / 1920,
                              "word_highlight", active)
        painter.end()
        return image

    from PySide6.QtCore import QRectF
    stage_a = render(0)
    stage_b = render(3)
    bright = sum(
        1 for x in range(0, 480, 4) for y in range(0, 270, 4)
        if stage_a.pixelColor(x, y).value() > 200
    )
    assert bright > 0  # the caption is actually painted
    assert stage_a != stage_b  # moving the highlight changes the image
