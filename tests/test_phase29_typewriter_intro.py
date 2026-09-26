"""Phase 29 unit tests: Typewriter Hook Intro.

Covers the deterministic timeline, SFX synthesis, normalizers, layout,
cache identity, settings persistence/migration, LF/Shorts independence and
the GUI wiring. All production render path behavior is additionally covered
by tests/test_phase29_typewriter_e2e.py (real FFmpeg renders).
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.models import ExportSettings
from app.video_merger.typewriter_intro import (
    TYPEWRITER_H_ALIGNS,
    TYPEWRITER_POSITIONS,
    TYPEWRITER_SOUND_FREQUENCIES,
    TYPEWRITER_SOUND_PRESETS,
    TYPEWRITER_SPEEDS,
    TypewriterProfile,
    auto_interval,
    build_layout,
    build_timeline,
    clamp_hold_seconds,
    clamp_sound_volume,
    merge_intro_command,
    normalize_h_align,
    normalize_music_mode,
    normalize_position,
    normalize_sound_frequency,
    normalize_sound_preset,
    normalize_speed,
    parse_color,
    profile_from_settings,
    synthesize_sfx_wav,
    typewriter_identity,
)
from app.video_merger.render_cache import build_stage1_payload, stage1_fingerprint
from app.video_merger.youtube_outputs import long_form_settings, short_settings


def make_profile(**overrides) -> TypewriterProfile:
    base = dict(
        enabled=True, text="Why do 97% fail?",
        speed="auto", sound_frequency="every_character", sound_preset="typewriter_1",
        sound_volume=30, cursor_enabled=True, position="Center", h_align="Center",
        font="modern_sans_bold", font_size=100, bold=True, color=(255, 255, 255),
        outline_enabled=True, shadow_enabled=False, box_enabled=False, box_opacity=55,
        box_padding=40, background_image_enabled=False, background_image_path="",
        background_darken=0, background_blur=False, background_zoom=False,
        hold_seconds=0.5, transition="project", music_mode="start_with_video",
    )
    base.update(overrides)
    return TypewriterProfile(**base)


# --------------------------------------------------------------------------- #
# Normalizers (one canonical name per visual choice)
# --------------------------------------------------------------------------- #
def test_option_sets_are_canonical_and_complete():
    assert TYPEWRITER_SPEEDS == ("slow", "normal", "fast", "auto")
    assert TYPEWRITER_SOUND_FREQUENCIES == ("every_character", "every_word", "word_boundary", "off")
    assert TYPEWRITER_SOUND_PRESETS == ("typewriter_1", "typewriter_2", "mechanical", "soft_keyboard", "off")
    assert TYPEWRITER_POSITIONS == ("Top", "Upper-Middle", "Center", "Lower-Middle", "Bottom")
    assert TYPEWRITER_H_ALIGNS == ("Left", "Center", "Right")


def test_speed_normalization_defaults_to_auto():
    assert normalize_speed("fast") == "fast"
    assert normalize_speed("FAST ") == "fast"
    assert normalize_speed("bogus") == "auto"
    assert normalize_speed(None) == "auto"


def test_sound_frequency_and_preset_normalization():
    assert normalize_sound_frequency("word_boundary") == "word_boundary"
    assert normalize_sound_frequency("x") == "every_character"
    assert normalize_sound_preset("mechanical") == "mechanical"
    assert normalize_sound_preset("") == "typewriter_1"


def test_position_normalization_has_exactly_five_spots_and_migrates_aliases():
    for value in TYPEWRITER_POSITIONS:
        assert normalize_position(value) == value
    # Duplicated historical names collapse into the canonical spot.
    assert normalize_position("Middle") == "Center"
    assert normalize_position("centre") == "Center"
    assert normalize_position("upper middle") == "Upper-Middle"
    assert normalize_position("unknown") == "Center"


def test_h_align_normalization():
    assert normalize_h_align("Left") == "Left"
    assert normalize_h_align("r") == "Right"
    assert normalize_h_align("anything") == "Center"


def test_music_mode_normalization_defaults_to_historical_behavior():
    assert normalize_music_mode("continue_during_intro") == "continue_during_intro"
    assert normalize_music_mode("x") == "start_with_video"
    assert normalize_music_mode(None) == "start_with_video"


def test_color_parsing():
    assert parse_color("#FFFFFF") == (255, 255, 255)
    assert parse_color("#F00") == (255, 0, 0)
    assert parse_color("112233") == (0x11, 0x22, 0x33)
    assert parse_color("nonsense") == (255, 255, 255)


def test_volume_and_hold_clamps():
    assert clamp_sound_volume(150) == 100
    assert clamp_sound_volume(-5) == 0
    assert clamp_sound_volume("abc") == 30
    assert clamp_hold_seconds(25) == 10.0
    assert clamp_hold_seconds(-1) == 0.0
    assert clamp_hold_seconds("abc") == 0.5


# --------------------------------------------------------------------------- #
# Deterministic timeline
# --------------------------------------------------------------------------- #
def test_every_visible_character_appears_exactly_once_in_order():
    profile = make_profile(text="Hello, Wörld! 🚀")
    timeline = build_timeline(profile)
    assert "".join(event.char for event in timeline.events) == "Hello, Wörld! 🚀"
    assert timeline.char_count == len("Hello, Wörld! 🚀")


def test_timeline_is_deterministic():
    profile = make_profile(text="Why do 97% of creators fail?\nAnd what do winners do?")
    first = build_timeline(profile)
    second = build_timeline(profile)
    assert first == second


def test_manual_line_breaks_are_honored_but_never_rendered():
    profile = make_profile(text="Line one\nLine two\n\nLine four")
    timeline = build_timeline(profile)
    assert timeline.lines == ("Line one", "Line two", "", "Line four")
    typed = "".join(event.char for event in timeline.events)
    assert "\n" not in typed
    assert typed == "Line oneLine twoLine four"


def test_single_character_and_empty_text():
    one = build_timeline(make_profile(text="A"))
    assert one.char_count == 1
    assert one.total_duration >= 0.8  # MIN_TOTAL_SECONDS guarantees a real segment
    empty = build_timeline(make_profile(text=""))
    assert empty.char_count == 0
    assert not empty.events


def test_long_text_respects_the_typing_cap():
    timeline = build_timeline(make_profile(text="x" * 5000, speed="auto"))
    assert timeline.typing_end <= 20.0 + 0.001


def test_speed_modes_have_fixed_deterministic_intervals():
    text = "abcdefghij"
    slow = build_timeline(make_profile(text=text, speed="slow"))
    normal = build_timeline(make_profile(text=text, speed="normal"))
    fast = build_timeline(make_profile(text=text, speed="fast"))
    assert slow.interval == 0.13
    assert normal.interval == 0.075
    assert fast.interval == 0.042
    assert slow.typing_end > normal.typing_end > fast.typing_end


def test_auto_speed_is_bounded_and_monotonic_by_length():
    assert auto_interval(1) >= 0.03
    assert auto_interval(10_000) <= 0.12
    assert auto_interval(200) > auto_interval(20)


def test_hold_extends_total_duration():
    timeline = build_timeline(make_profile(text="abc", hold_seconds=2.0))
    assert timeline.total_duration == pytest.approx(timeline.typing_end + 2.0, abs=1e-6)


def test_sound_events_follow_frequency_mode():
    text = "ab cd"
    every_char = build_timeline(make_profile(text=text, sound_frequency="every_character"))
    every_word = build_timeline(make_profile(text=text, sound_frequency="every_word"))
    word_boundary = build_timeline(make_profile(text=text, sound_frequency="word_boundary"))
    off = build_timeline(make_profile(text=text, sound_frequency="off"))
    # a, b, SPACE, c, d -> every typed character (including the space) is audible.
    assert len(every_char.sound_times) == 5
    assert every_char.sound_is_space[2] is True
    assert len(every_word.sound_times) == 2  # 'a' and 'c'
    assert len(word_boundary.sound_times) == 2  # 'b' and 'd'
    assert off.sound_times == ()
    # Sound events line up exactly with character appearance times.
    for event in every_char.events:
        assert event.plays_sound
    assert every_word.sound_times[0] == every_word.events[0].time


def test_sound_off_via_preset_disables_events():
    timeline = build_timeline(make_profile(sound_preset="off"))
    assert timeline.sound_times == ()


def test_sfx_wav_is_deterministic_and_volume_scaled(tmp_path):
    timeline = build_timeline(make_profile(text="ab cd"))
    profile = make_profile(text="ab cd")
    first = synthesize_sfx_wav(timeline, profile, tmp_path / "a.wav")
    second = synthesize_sfx_wav(timeline, profile, tmp_path / "b.wav")
    assert first.read_bytes() == second.read_bytes()
    loud = synthesize_sfx_wav(timeline, replace(profile, sound_volume=90), tmp_path / "c.wav")
    assert loud.read_bytes() != first.read_bytes()
    # Phase 32: the completion click is an INDEPENDENT sound (tested below
    # and in the Phase-32 suite); a fully silent track requires the
    # per-character SFX AND the completion sound to be off.
    silent = synthesize_sfx_wav(
        timeline,
        replace(profile, sound_preset="off", completion_sound_enabled=False),
        tmp_path / "d.wav",
    )
    data = silent.read_bytes()
    assert all(byte == 0 for byte in data[44:])
    # Per-character SFX off with the (default) completion sound ON renders
    # exactly the completion click and nothing else.
    completion_only = synthesize_sfx_wav(
        timeline, replace(profile, sound_preset="off"), tmp_path / "e.wav"
    )
    assert completion_only.read_bytes() != silent.read_bytes()


def test_sfx_wav_length_matches_timeline(tmp_path):
    import wave

    profile = make_profile(text="hook text")
    timeline = build_timeline(profile)
    path = synthesize_sfx_wav(timeline, profile, tmp_path / "sfx.wav")
    with wave.open(str(path)) as handle:
        assert handle.getframerate() == 48000
        assert handle.getnchannels() == 2
        expected = timeline.total_duration
        assert handle.getnframes() / 48000 == pytest.approx(expected, abs=0.01)


# --------------------------------------------------------------------------- #
# Cache identity
# --------------------------------------------------------------------------- #
def test_identity_stable_and_sensitive_to_every_render_input():
    profile = make_profile()
    base = typewriter_identity(profile, 1920, 1080, 30.0)
    assert base == typewriter_identity(profile, 1920, 1080, 30.0)
    variants = [
        replace(profile, text="Other"),
        replace(profile, speed="fast"),
        replace(profile, sound_frequency="off"),
        replace(profile, sound_preset="mechanical"),
        replace(profile, font="inter"),
        replace(profile, font_size=120),
        replace(profile, position="Top"),
        replace(profile, h_align="Right"),
        replace(profile, hold_seconds=1.5),
        replace(profile, box_enabled=True),
        replace(profile, color=(0, 0, 0)),
        replace(profile, background_image_enabled=True, background_image_path="bg.jpg"),
        replace(profile, cursor_enabled=False),
        replace(profile, outline_enabled=False),
        replace(profile, shadow_enabled=True),
    ]
    for variant in variants:
        assert typewriter_identity(variant, 1920, 1080, 30.0) != base
    assert typewriter_identity(profile, 1080, 1920, 30.0) != base
    assert typewriter_identity(profile, 1920, 1080, 60.0) != base


# --------------------------------------------------------------------------- #
# Settings resolution: strict LF/Shorts independence + fail-safe
# --------------------------------------------------------------------------- #
def test_profiles_resolve_strictly_separate_fields():
    settings = ExportSettings()
    settings.typewriter_intro_enabled = True
    settings.typewriter_hook_text = "Long form hook"
    settings.typewriter_speed = "slow"
    settings.short_typewriter_intro_enabled = True
    settings.short_typewriter_hook_text = "Short hook"
    settings.short_typewriter_speed = "fast"
    long_profile = profile_from_settings(settings, short=False)
    short_profile = profile_from_settings(settings, short=True)
    assert long_profile.text == "Long form hook" and long_profile.speed == "slow"
    assert short_profile.text == "Short hook" and short_profile.speed == "fast"
    assert long_profile.active and short_profile.active


def test_empty_text_with_enabled_flag_behaves_disabled():
    settings = ExportSettings()
    settings.typewriter_intro_enabled = True
    settings.typewriter_hook_text = "   \n  "
    assert not profile_from_settings(settings).active
    settings.typewriter_hook_text = "Real text"
    assert profile_from_settings(settings).active


def test_missing_fields_fall_back_to_spec_defaults():
    settings = ExportSettings()
    profile = profile_from_settings(settings)
    assert not profile.enabled and not profile.active
    assert profile.speed == "auto"
    assert profile.sound_frequency == "every_character"
    assert profile.sound_preset == "typewriter_1"
    assert profile.sound_volume == 30
    assert profile.cursor_enabled
    assert profile.position == "Center" and profile.h_align == "Center"
    assert profile.bold and profile.color == (255, 255, 255)
    assert profile.outline_enabled
    assert not (profile.shadow_enabled or profile.box_enabled)
    assert not profile.background_image_enabled
    assert profile.background_darken == 0
    assert not (profile.background_blur or profile.background_zoom)
    assert profile.hold_seconds == 0.5
    assert profile.transition == "project"
    assert profile.music_mode == "start_with_video"


def test_short_settings_maps_short_fields_onto_canonical_ones():
    from app.video_merger.youtube_outputs import ShortJob

    settings = ExportSettings()
    settings.short_typewriter_intro_enabled = True
    settings.short_typewriter_hook_text = "Shorts hook"
    settings.short_typewriter_position = "Upper-Middle"
    settings.typewriter_hook_text = "LF hook"  # must never leak into a Short
    job = ShortJob(
        index=0, voiceover_path=Path("voice.wav"), script_path=None,
        output_name="001", cache_key="k",
    )
    resolved = short_settings(settings, job)
    assert resolved.typewriter_intro_enabled is True
    assert resolved.typewriter_hook_text == "Shorts hook"
    assert resolved.typewriter_position == "Upper-Middle"
    assert profile_from_settings(resolved).active


def test_short_settings_respects_explicit_zero_values():
    """A deliberate 0 (silent, zero hold, transparent box) is NEVER replaced
    by a default during the Shorts profile mapping."""
    from app.video_merger.youtube_outputs import ShortJob

    settings = ExportSettings()
    settings.short_typewriter_intro_enabled = True
    settings.short_typewriter_hook_text = "Silent zero hold"
    settings.short_typewriter_sound_volume = 0
    settings.short_typewriter_hold_seconds = 0.0
    settings.short_typewriter_box_opacity = 0
    settings.short_typewriter_box_padding = 0
    settings.short_typewriter_background_darken = 0
    job = ShortJob(
        index=0, voiceover_path=Path("voice.wav"), script_path=None,
        output_name="001", cache_key="k",
    )
    resolved = short_settings(settings, job)
    assert resolved.typewriter_sound_volume == 0
    assert resolved.typewriter_hold_seconds == 0.0
    assert resolved.typewriter_box_opacity == 0
    assert resolved.typewriter_box_padding == 0
    assert resolved.typewriter_background_darken == 0


def test_long_form_settings_keep_their_own_profile():
    settings = ExportSettings()
    settings.typewriter_intro_enabled = True
    settings.typewriter_hook_text = "LF hook"
    settings.short_typewriter_hook_text = "Shorts hook"
    resolved = long_form_settings(settings)
    assert resolved.typewriter_hook_text == "LF hook"
    assert resolved.short_typewriter_hook_text == "Shorts hook"
    assert profile_from_settings(resolved).text == "LF hook"


# --------------------------------------------------------------------------- #
# Stage-1 cache payload behavior
# --------------------------------------------------------------------------- #
def _minimal_stage1_payload(typewriter: str | None):
    from tests.conftest import fake_media

    settings = ExportSettings()
    from app.video_merger.target import resolve_export

    media = [fake_media()]
    resolved = resolve_export(media, settings)
    return build_stage1_payload(media, settings, resolved, typewriter_intro=typewriter)


def test_stage1_payload_unchanged_when_intro_inactive():
    baseline = _minimal_stage1_payload(None)
    assert "typewriter_intro" not in baseline
    disabled = _minimal_stage1_payload("")
    assert disabled == baseline


def test_stage1_payload_changes_only_when_intro_active():
    baseline = _minimal_stage1_payload(None)
    active = _minimal_stage1_payload("abc123")
    assert active != baseline
    assert active["typewriter_intro"] == "abc123"


def test_stage1_fingerprint_stable_with_intro_identity():
    from tests.conftest import fake_media
    from app.video_merger.target import resolve_export

    settings = ExportSettings()
    media = [fake_media()]
    resolved = resolve_export(media, settings)
    digest_one, _ = stage1_fingerprint(media, settings, resolved, typewriter_intro="ident")
    digest_two, _ = stage1_fingerprint(media, settings, resolved, typewriter_intro="ident")
    digest_none, _ = stage1_fingerprint(media, settings, resolved)
    assert digest_one == digest_two
    assert digest_one != digest_none


# --------------------------------------------------------------------------- #
# Layout: five positions, three alignments, wrapping, 16:9 & 9:16
# --------------------------------------------------------------------------- #
def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_all_five_positions_render_at_distinct_heights():
    _app()
    tops = {}
    for position in TYPEWRITER_POSITIONS:
        profile = make_profile(position=position)
        timeline = build_timeline(profile)
        layout = build_layout(profile, timeline, 1920, 1080)
        tops[position] = layout.block_top
    ordered = [tops[name] for name in TYPEWRITER_POSITIONS]
    assert ordered == sorted(ordered), ordered
    assert len(set(ordered)) == 5


def test_all_three_alignments_place_the_block_correctly():
    _app()
    profile = make_profile(text="Hook")
    timeline = build_timeline(profile)
    left = build_layout(replace(profile, h_align="Left"), timeline, 1920, 1080)
    center = build_layout(profile, timeline, 1920, 1080)
    right = build_layout(replace(profile, h_align="Right"), timeline, 1920, 1080)
    assert left.block_x < center.block_x < right.block_x
    assert left.block_x == pytest.approx(1920 * 0.07, abs=1)
    assert right.block_x + right.block_width == pytest.approx(1920 * 0.93, abs=1)


def test_layout_wraps_long_lines_and_maps_every_character():
    _app()
    profile = make_profile(
        text="This hook is deliberately long so that the production layout must wrap it."
    )
    timeline = build_timeline(profile)
    for size in ((1920, 1080), (1080, 1920)):
        layout = build_layout(profile, timeline, *size)
        assert len(layout.wrapped_lines) > 1
        assert len(layout.line_of_char) == timeline.char_count
        assert max(layout.line_of_char) == len(layout.wrapped_lines) - 1
        # No line may exceed the safe text width by construction.
        assert layout.block_width <= size[0] * 0.86 + 1


def test_layout_geometry_consistent_between_both_aspects():
    _app()
    profile = make_profile(text="Why?")
    timeline = build_timeline(profile)
    wide = build_layout(profile, timeline, 1920, 1080)
    tall = build_layout(profile, timeline, 1080, 1920)
    assert wide.block_top == pytest.approx(1080 * 0.5 - wide.block_height / 2, abs=1)
    assert tall.block_top == pytest.approx(1920 * 0.5 - tall.block_height / 2, abs=1)


# --------------------------------------------------------------------------- #
# Frame rendering determinism (production draw routine)
# --------------------------------------------------------------------------- #
def _render_frame(profile, timeline, width, height, time):
    from PySide6.QtGui import QImage, QPainter
    from app.video_merger.typewriter_intro import TypewriterBackground, draw_typewriter_frame

    layout = build_layout(profile, timeline, width, height)
    background = TypewriterBackground(profile, width, height)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0xFF000000)
    painter = QPainter(image)
    draw_typewriter_frame(painter, profile, timeline, layout, background, width, height, time)
    painter.end()
    # IMPORTANT: keep the converted QImage alive while copying its buffer -
    # constBits() points INTO the QImage, it is not a detached copy.
    converted = image.convertToFormat(QImage.Format.Format_RGB888)
    return bytes(converted.constBits().tobytes())


def test_frame_rendering_is_deterministic_and_progressive():
    _app()
    profile = make_profile(text="Watch this")
    timeline = build_timeline(profile)
    first = _render_frame(profile, timeline, 640, 360, timeline.typing_end * 0.5)
    again = _render_frame(profile, timeline, 640, 360, timeline.typing_end * 0.5)
    assert first == again
    early = _render_frame(profile, timeline, 640, 360, 0.0)
    late = _render_frame(profile, timeline, 640, 360, timeline.typing_end)
    assert early != late  # text visibly grows over time


def test_background_image_cover_fit_and_effects(tmp_path):
    _app()
    from PySide6.QtGui import QImage

    source = QImage(400, 200, QImage.Format.Format_RGB32)
    source.fill(0xFF224466)
    image_path = tmp_path / "bg.png"
    source.save(str(image_path))
    profile = make_profile(
        text="Hook", background_image_enabled=True, background_image_path=str(image_path),
        background_darken=40, background_blur=True, background_zoom=True,
    )
    timeline = build_timeline(profile)
    frame = _render_frame(profile, timeline, 640, 360, timeline.total_duration - 0.01)
    neutral = _render_frame(make_profile(text="Hook"), timeline, 640, 360, timeline.total_duration - 0.01)
    assert frame != neutral


def test_missing_background_image_falls_back_to_neutral():
    _app()
    profile = make_profile(
        text="Hook", background_image_enabled=True,
        background_image_path="/does/not/exist.png",
    )
    timeline = build_timeline(profile)
    with_image = _render_frame(profile, timeline, 320, 180, 0.5)
    without = _render_frame(make_profile(text="Hook"), timeline, 320, 180, 0.5)
    assert with_image == without


# --------------------------------------------------------------------------- #
# Merge command construction
# --------------------------------------------------------------------------- #
def test_merge_command_reuses_project_transition_system_and_clamps_duration():
    command, effective = merge_intro_command(
        "/usr/bin/ffmpeg", Path("intro.mp4"), Path("intro.wav"), Path("program.mp4"),
        Path("out.mp4"), fps=30.0, width=1920, height=1080,
        transition_key="cross_dissolve", transition_ease="ease_in_out",
        transition_duration=0.7, intro_duration=2.0, program_duration=10.0,
        encoder_args=["-c:v", "libx264"],
    )
    assert effective == pytest.approx(0.7)
    joined = " ".join(command)
    assert "xfade=transition=custom" in joined
    assert "acrossfade" in joined
    # A 0.9 s intro cannot carry a 2.0 s transition: it gets clamped.
    command, clamped = merge_intro_command(
        "/usr/bin/ffmpeg", Path("intro.mp4"), Path("intro.wav"), Path("program.mp4"),
        Path("out.mp4"), fps=30.0, width=1920, height=1080,
        transition_key="cross_dissolve", transition_ease="ease_in_out",
        transition_duration=2.0, intro_duration=0.9, program_duration=10.0,
        encoder_args=["-c:v", "libx264"],
    )
    assert clamped < 0.9


def test_merge_command_adds_music_only_in_continue_mode():
    base = dict(
        fps=30.0, width=1920, height=1080, transition_key="cross_dissolve",
        transition_ease="ease_in_out", transition_duration=0.5,
        intro_duration=2.0, program_duration=10.0, encoder_args=["-c:v", "libx264"],
    )
    default_command, _ = merge_intro_command(
        "ffmpeg", Path("i.mp4"), Path("i.wav"), Path("p.mp4"), Path("o.mp4"), **base,
    )
    assert "amix" not in " ".join(default_command)
    continue_command, _ = merge_intro_command(
        "ffmpeg", Path("i.mp4"), Path("i.wav"), Path("p.mp4"), Path("o.mp4"),
        music_path=Path("music.mp3"), music_volume=42, **base,
    )
    joined = " ".join(continue_command)
    assert "amix=inputs=2:normalize=0" in joined
    assert "volume=0.42" in joined


# --------------------------------------------------------------------------- #
# Model defaults + example settings + persistence
# --------------------------------------------------------------------------- #
def test_model_defaults_match_specification():
    settings = ExportSettings()
    assert settings.typewriter_intro_enabled is False
    assert settings.typewriter_hook_text == ""
    assert settings.typewriter_speed == "auto"
    assert settings.typewriter_sound_frequency == "every_character"
    assert settings.typewriter_sound_preset == "typewriter_1"
    assert settings.typewriter_sound_volume == 30
    assert settings.typewriter_cursor_enabled is True
    assert settings.typewriter_position == "Center"
    assert settings.typewriter_h_align == "Center"
    assert settings.typewriter_bold is True
    assert settings.typewriter_color == "#FFFFFF"
    assert settings.typewriter_hold_seconds == 0.5
    assert settings.typewriter_transition == "project"
    assert settings.typewriter_music_mode == "start_with_video"
    assert settings.short_typewriter_intro_enabled is False
    assert settings.short_typewriter_hook_text == ""
    assert settings.short_typewriter_hold_seconds == 0.5


def test_example_settings_contain_all_phase29_keys_with_defaults():
    example = json.loads(
        (Path(__file__).resolve().parents[1] / "config" / "example_settings.json").read_text(encoding="utf-8")
    )
    defaults = ExportSettings()
    for field_name in (
        "typewriter_intro_enabled", "typewriter_hook_text", "typewriter_speed",
        "typewriter_sound_frequency", "typewriter_sound_preset", "typewriter_sound_volume",
        "typewriter_cursor_enabled", "typewriter_position", "typewriter_h_align",
        "typewriter_font", "typewriter_font_size", "typewriter_bold", "typewriter_color",
        "typewriter_outline_enabled", "typewriter_shadow_enabled", "typewriter_box_enabled",
        "typewriter_box_opacity", "typewriter_box_padding",
        "typewriter_background_image_enabled", "typewriter_background_image_path",
        "typewriter_background_darken", "typewriter_background_blur",
        "typewriter_background_zoom", "typewriter_hold_seconds",
        "typewriter_transition", "typewriter_music_mode",
    ):
        assert field_name in example, field_name
        assert example[field_name] == getattr(defaults, field_name), field_name
        short_name = "short_" + field_name
        assert short_name in example, short_name
        assert example[short_name] == getattr(defaults, short_name), short_name


def test_settings_store_roundtrip_and_legacy_migration(tmp_path):
    from app.video_merger.settings_store import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    settings = ExportSettings()
    settings.typewriter_intro_enabled = True
    settings.typewriter_hook_text = "Persisted hook"
    settings.short_typewriter_hook_text = "Short hook"
    store.save(settings)
    loaded = store.load()
    assert loaded.typewriter_intro_enabled is True
    assert loaded.typewriter_hook_text == "Persisted hook"
    assert loaded.short_typewriter_hook_text == "Short hook"

    legacy = SettingsStore(tmp_path / "legacy.json")
    (tmp_path / "legacy.json").write_text(json.dumps({"output_folder": "x"}), encoding="utf-8")
    migrated = legacy.load()
    assert migrated.typewriter_intro_enabled is False
    assert migrated.typewriter_hook_text == ""
    assert migrated.short_typewriter_hook_text == ""


# --------------------------------------------------------------------------- #
# GUI wiring
# --------------------------------------------------------------------------- #
@pytest.fixture()
def window():
    from PySide6 import QtWidgets
    from app.video_merger.gui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    main = MainWindow()
    yield main
    main.close()
    app.processEvents()


def test_gui_has_strictly_separated_profile_sections(window):
    assert set(window._typewriter_widgets) == {"tw_long", "tw_short"}
    settings = window._settings()
    assert settings.typewriter_intro_enabled is False
    assert settings.short_typewriter_intro_enabled is False


def test_gui_roundtrip_and_profile_independence(window):
    long_widgets = window._typewriter_widgets["tw_long"]
    short_widgets = window._typewriter_widgets["tw_short"]
    long_widgets["enabled"].setChecked(True)
    long_widgets["hook_text"].setPlainText("Long form hook")
    long_widgets["position"].setCurrentIndex(long_widgets["position"].findData("Top"))
    short_widgets["enabled"].setChecked(True)
    short_widgets["hook_text"].setPlainText("Shorts hook")
    short_widgets["position"].setCurrentIndex(short_widgets["position"].findData("Bottom"))

    settings = window._settings()
    assert settings.typewriter_hook_text == "Long form hook"
    assert settings.short_typewriter_hook_text == "Shorts hook"
    assert settings.typewriter_position == "Top"
    assert settings.short_typewriter_position == "Bottom"

    long_profile = window._typewriter_profile_from_ui("tw_long")
    short_profile = window._typewriter_profile_from_ui("tw_short")
    assert long_profile.text == "Long form hook" and long_profile.position == "Top"
    assert short_profile.text == "Shorts hook" and short_profile.position == "Bottom"


def test_gui_loads_saved_settings_into_both_sections(window, tmp_path, monkeypatch):
    import app.video_merger.settings_store as settings_store_module

    settings_file = tmp_path / "config" / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings_store_module, "project_root", lambda: tmp_path)
    payload = {
        "typewriter_intro_enabled": True,
        "typewriter_hook_text": "Saved LF",
        "typewriter_position": "Lower-Middle",
        "short_typewriter_intro_enabled": True,
        "short_typewriter_hook_text": "Saved Short",
        "short_typewriter_speed": "fast",
    }
    settings_file.write_text(json.dumps(payload), encoding="utf-8")
    window.store = settings_store_module.SettingsStore()
    window.saved = window.store.load()
    window._load_typewriter_settings()
    long_widgets = window._typewriter_widgets["tw_long"]
    short_widgets = window._typewriter_widgets["tw_short"]
    assert long_widgets["enabled"].isChecked()
    assert long_widgets["hook_text"].toPlainText() == "Saved LF"
    assert long_widgets["position"].currentData() == "Lower-Middle"
    assert short_widgets["enabled"].isChecked()
    assert short_widgets["hook_text"].toPlainText() == "Saved Short"
    assert short_widgets["speed"].currentData() == "fast"
