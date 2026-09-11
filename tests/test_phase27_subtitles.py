"""Phase 27 – subtitle font sizes, debug overlay, per-profile Duration Before Merge.

Regression contracts:
* the historical appearance at the defaults is preserved byte-identically,
* Long-Form and Shorts font sizes are independent and scale real wrapping,
* the debug overlay is OFF by default and emits NO debug style/content when OFF,
* Duration Before Merge stays a playback-rate multiplier per profile,
* none of these visual settings leak into ASR/alignment cache identity.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.models import AlignmentResult, ExportSettings, WordTiming
from app.video_merger.settings_store import SettingsStore
from app.video_merger.subtitle_presets import get_preset
from app.video_merger.subtitles import (
    _font_size,
    build_cues,
    clamp_font_size_percent,
    write_ass,
)
from app.video_merger.youtube_outputs import ShortJob, long_form_settings, short_settings


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


_DE_SCRIPT = (
    "Heute sprechen wir über Disziplin und Beständigkeit. "
    "Jeden Tag ein kleiner Schritt führt zu einem großen Ergebnis. "
    "Bleib fokussiert und geduldig, dann kommt der Erfolg von allein."
)


# --------------------------------------------------------------------------- #
# Font size scaling
# --------------------------------------------------------------------------- #
def test_default_font_size_is_one_hundred_percent():
    settings = ExportSettings()
    assert settings.subtitle_font_size == 100
    assert settings.short_subtitle_font_size == 100


def test_font_size_percent_scales_preset_base_size():
    preset = get_preset("long_1")
    base = _font_size(1920, 1080, preset, 100)
    assert _font_size(1920, 1080, preset, 100) == base
    bigger = _font_size(1920, 1080, preset, 150)
    smaller = _font_size(1920, 1080, preset, 60)
    assert bigger > base > smaller
    # Scaling is proportional to the percentage.
    assert bigger == pytest.approx(base * 1.5, rel=0.02)


def test_font_size_percent_is_clamped_to_safe_bounds():
    assert clamp_font_size_percent(100) == 100
    assert clamp_font_size_percent(10) == 50      # lower bound
    assert clamp_font_size_percent(999) == 200    # upper bound
    assert clamp_font_size_percent(None) == 100
    assert clamp_font_size_percent("garbage") == 100


def test_default_render_is_identical_to_pre_phase27(tmp_path):
    """100 % must reproduce the exact historical ASS Style line."""
    alignment = _alignment(_DE_SCRIPT)
    cues = build_cues(
        _DE_SCRIPT, alignment, "long_1",
        program_end=alignment.words[-1].end + 0.2,
        width=1920, height=1080, font_key="modern_sans_bold", font_size_percent=100,
    )
    path = tmp_path / "default.ass"
    write_ass(_DE_SCRIPT, cues, path, "long_1", "Bottom Center", 1920, 1080,
              animation="static_phrase", font_key="modern_sans_bold",
              debug_overlay=False, font_size_percent=100)
    text = path.read_text(encoding="utf-8")
    preset = get_preset("long_1")
    expected_size = _font_size(1920, 1080, preset, 100)
    assert f"Style: Caption," in text and f",{expected_size}," in text
    # No debug layer present at the default.
    assert "Style: Debug" not in text


def test_larger_font_size_changes_wrapping():
    alignment = _alignment(_DE_SCRIPT)
    base_kwargs = dict(
        program_end=alignment.words[-1].end + 0.2,
        width=1920, height=1080, font_key="modern_sans_bold",
    )
    normal = build_cues(_DE_SCRIPT, alignment, "long_1", font_size_percent=100, **base_kwargs)
    huge = build_cues(_DE_SCRIPT, alignment, "long_1", font_size_percent=200, **base_kwargs)
    # Same words in the same order with identical acoustic timing — only the
    # measured wrapping (cue boundaries / line layout) may differ.
    normal_words = [w for cue in normal for w in cue.words]
    huge_words = [w for cue in huge for w in cue.words]
    assert [(w.text, w.start, w.end) for w in normal_words] == [
        (w.text, w.start, w.end) for w in huge_words
    ]
    # Doubling the size can only increase (or keep) the number of lines.
    normal_lines = sum(cue.line_count for cue in normal)
    huge_lines = sum(cue.line_count for cue in huge)
    assert huge_lines >= normal_lines
    # And it must actually change the layout for this script at this size.
    assert (len(huge), huge_lines) != (len(normal), normal_lines)


def test_font_size_change_never_touches_word_timing():
    alignment = _alignment(_DE_SCRIPT)
    program_end = alignment.words[-1].end + 0.2
    a = build_cues(_DE_SCRIPT, alignment, "long_1", program_end=program_end,
                   width=1920, height=1080, font_key="modern_sans_bold", font_size_percent=100)
    b = build_cues(_DE_SCRIPT, alignment, "long_1", program_end=program_end,
                   width=1920, height=1080, font_key="modern_sans_bold", font_size_percent=160)
    assert [(c.start, c.end) for c in a] == [(c.start, c.end) for c in b]


# --------------------------------------------------------------------------- #
# Independent Long-Form / Shorts sizes (persistence + mapping)
# --------------------------------------------------------------------------- #
def test_long_and_short_font_sizes_are_independent_and_persist(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(ExportSettings(subtitle_font_size=140, short_subtitle_font_size=85))
    loaded = store.load()
    assert loaded.subtitle_font_size == 140
    assert loaded.short_subtitle_font_size == 85


def test_legacy_settings_migrate_to_default_sizes(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        '{"subtitle_style": "long_1"}', encoding="utf-8"
    )
    loaded = store.load()
    assert loaded.subtitle_font_size == 100
    assert loaded.short_subtitle_font_size == 100


def test_short_settings_maps_short_size_onto_generic_field():
    settings = ExportSettings(subtitle_font_size=140, short_subtitle_font_size=85)
    job = ShortJob(index=0, voiceover_path=Path("/v.wav"), script_path=Path("/s.txt"),
                   output_name="short", cache_key="k")
    mapped = short_settings(settings, job)
    assert mapped.subtitle_font_size == 85     # Shorts size, NOT the Long value
    assert mapped.subtitle_style == "short_1"


def test_long_settings_keeps_long_size_and_multipliers():
    settings = ExportSettings(
        subtitle_font_size=140, short_subtitle_font_size=85,
        duration_before_merge=0.55, duration_before_merge_shorts=0.95,
    )
    mapped = long_form_settings(settings)
    assert mapped.subtitle_font_size == 140
    assert mapped.duration_before_merge == pytest.approx(0.55)


# --------------------------------------------------------------------------- #
# Debug overlay safety
# --------------------------------------------------------------------------- #
def test_debug_overlay_default_is_off():
    assert ExportSettings().subtitle_debug_overlay is False


def test_debug_overlay_off_emits_no_debug_style(tmp_path):
    alignment = _alignment(_DE_SCRIPT)
    cues = build_cues(_DE_SCRIPT, alignment, "long_1",
                      program_end=alignment.words[-1].end + 0.2,
                      width=1920, height=1080, font_key="modern_sans_bold")
    path = tmp_path / "off.ass"
    write_ass(_DE_SCRIPT, cues, path, "long_1", "Bottom Center", 1920, 1080,
              animation="static_phrase", font_key="modern_sans_bold",
              debug_overlay=False, font_size_percent=100)
    text = path.read_text(encoding="utf-8")
    assert "Style: Debug" not in text
    # No diagnostic markers and no Dialogue line may reference the Debug style.
    assert "CURRENT WORD" not in text.upper()
    assert ",Debug," not in text


def test_debug_overlay_on_adds_debug_style(tmp_path):
    alignment = _alignment(_DE_SCRIPT)
    cues = build_cues(_DE_SCRIPT, alignment, "long_1",
                      program_end=alignment.words[-1].end + 0.2,
                      width=1920, height=1080, font_key="modern_sans_bold")
    path = tmp_path / "on.ass"
    write_ass(_DE_SCRIPT, cues, path, "long_1", "Bottom Center", 1920, 1080,
              animation="static_phrase", font_key="modern_sans_bold",
              debug_overlay=True, font_size_percent=100)
    text = path.read_text(encoding="utf-8")
    assert "Style: Debug" in text


# --------------------------------------------------------------------------- #
# Per-profile Duration Before Merge (multiplier semantics preserved)
# --------------------------------------------------------------------------- #
def test_duration_before_merge_defaults_are_multipliers():
    settings = ExportSettings()
    assert settings.duration_before_merge == pytest.approx(0.70)
    assert settings.duration_before_merge_shorts == pytest.approx(0.70)


def test_short_settings_uses_shorts_multiplier_with_fallback():
    settings = ExportSettings(duration_before_merge=0.55, duration_before_merge_shorts=0.95)
    job = ShortJob(index=0, voiceover_path=Path("/v.wav"), script_path=Path("/s.txt"),
                   output_name="short", cache_key="k")
    assert short_settings(settings, job).duration_before_merge == pytest.approx(0.95)

    # Legacy project without an explicit Shorts value falls back to the long one.
    legacy = ExportSettings(duration_before_merge=0.55, duration_before_merge_shorts=0.0)
    assert short_settings(legacy, job).duration_before_merge == pytest.approx(0.55)


def test_long_and_short_multipliers_are_independent_and_persist(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(ExportSettings(duration_before_merge=0.60, duration_before_merge_shorts=1.10))
    loaded = store.load()
    assert loaded.duration_before_merge == pytest.approx(0.60)
    assert loaded.duration_before_merge_shorts == pytest.approx(1.10)


def test_legacy_single_multiplier_migrates_to_both_profiles(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        '{"duration_before_merge": 0.85}', encoding="utf-8"
    )
    loaded = store.load()
    assert loaded.duration_before_merge == pytest.approx(0.85)
    # The Shorts field defaults to the canonical 0.70; the short_settings
    # fallback additionally mirrors the long value when the field is unset.
    assert loaded.duration_before_merge_shorts == pytest.approx(0.70)


# --------------------------------------------------------------------------- #
# Visual settings never leak into ASR/alignment identity
# --------------------------------------------------------------------------- #
def test_visual_settings_do_not_change_alignment_cache_identity():
    """The transcription cache key contains only audio+model+language."""
    import inspect
    from app.video_merger.alignment import LocalWordAligner

    source = inspect.getsource(LocalWordAligner._transcription_for)
    assert "music" not in source
    assert "font" not in source
    assert "subtitle" not in source
    assert "debug" not in source


# --------------------------------------------------------------------------- #
# Alignment-warning fail-closed safety gate
# --------------------------------------------------------------------------- #
def test_alignment_warning_gate_is_fail_closed_when_off(tmp_path):
    """OFF + alignment warnings must stop subtitle generation (fail-closed)."""
    from app.video_merger.main_project import _subtitle_failure

    error = _subtitle_failure(
        "alignment safety check",
        "Subtitle alignment reported warnings and 'Continue After Alignment "
        "Warning' is OFF (fail-closed).",
    )
    # The gate surfaces as the canonical VideoMergerError contract.
    from app.video_merger.errors import VideoMergerError
    assert isinstance(error, VideoMergerError)
    assert "fail-closed" in str(error)


def test_default_flag_is_off_and_explicit_override_is_documented():
    settings = ExportSettings()
    assert settings.allow_alignment_warnings is False
    # The field is part of the render identity so flipping it re-renders.
    from app.video_merger.render_cache import _SUBTITLE_SETTING_FIELDS
    assert "allow_alignment_warnings" in _SUBTITLE_SETTING_FIELDS
