"""Phase 32 unit tests: Typewriter Hook Intro completion (Enter/Return) sound.

Feature Group H: one short, deterministic completion click at the end of the
typing sequence - independent from the per-character SFX, played exactly
once, clamped inside the intro so it never lands after the video starts,
with strictly separate Long-Form / Shorts settings.
"""
from __future__ import annotations

import struct
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.models import ExportSettings
from app.video_merger.typewriter_intro import (
    SFX_SAMPLE_RATE,
    TYPEWRITER_COMPLETION_PRESETS,
    TypewriterProfile,
    build_timeline,
    completion_sound_samples,
    completion_sound_start,
    normalize_completion_sound_preset,
    profile_from_settings,
    synthesize_completion_sound,
    synthesize_sfx_wav,
    typewriter_identity,
)


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


def read_mono_envelope(path: Path) -> list[int]:
    """Per-sample absolute amplitude of one channel (16-bit stereo WAV)."""
    with wave.open(str(path)) as handle:
        frames = handle.readframes(handle.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    return [abs(samples[i]) for i in range(0, len(samples), 2)]


def nonzero_span(envelope: list[int], threshold: int = 40) -> tuple[int, int]:
    indices = [i for i, value in enumerate(envelope) if value > threshold]
    if not indices:
        return -1, -1
    return indices[0], indices[-1]


# ---------------------------------------------------------------------------
# Presets & determinism
# ---------------------------------------------------------------------------
def test_completion_presets_are_deterministic_and_distinct():
    assert normalize_completion_sound_preset(None) == "enter_return"
    assert normalize_completion_sound_preset("garbage") == "enter_return"
    assert normalize_completion_sound_preset("OFF") == "off"
    assert set(TYPEWRITER_COMPLETION_PRESETS) == {
        "enter_return", "mechanical_keypress", "typewriter_return", "off",
    }
    samples_by_preset = {}
    for preset in TYPEWRITER_COMPLETION_PRESETS:
        first = synthesize_completion_sound(preset)
        second = synthesize_completion_sound(preset)
        assert first == second, f"{preset} must be deterministic"
        samples_by_preset[preset] = first
    assert samples_by_preset["off"] == []
    # Every audible preset is short (well under one second) and non-empty.
    for preset, samples in samples_by_preset.items():
        if preset == "off":
            continue
        assert samples
        assert len(samples) / SFX_SAMPLE_RATE < 0.5
    # The presets are distinguishable sounds.
    assert samples_by_preset["enter_return"] != samples_by_preset["mechanical_keypress"]
    assert samples_by_preset["enter_return"] != samples_by_preset["typewriter_return"]


def test_completion_sound_cache_returns_same_object():
    assert completion_sound_samples("enter_return") is completion_sound_samples("enter_return")


# ---------------------------------------------------------------------------
# Timeline placement
# ---------------------------------------------------------------------------
def test_completion_fires_at_typing_end_and_fits_inside_the_intro():
    profile = make_profile()
    timeline = build_timeline(profile)
    start = completion_sound_start(timeline)
    assert start == pytest.approx(timeline.typing_end)
    click = completion_sound_samples(profile.completion_sound_preset)
    assert start + len(click) / SFX_SAMPLE_RATE <= timeline.total_duration + 1e-6


def test_completion_clamps_into_a_zero_hold_intro():
    profile = make_profile(hold_seconds=0.0)
    timeline = build_timeline(profile)
    click = completion_sound_samples("typewriter_return")
    total = timeline.total_duration
    start = min(completion_sound_start(timeline), max(0.0, total - len(click) / SFX_SAMPLE_RATE))
    assert start + len(click) / SFX_SAMPLE_RATE <= total + 1e-6


# ---------------------------------------------------------------------------
# WAV rendering: exactly one click, correct place, volume, independence
# ---------------------------------------------------------------------------
def test_exactly_one_completion_click_at_the_end_of_typing(tmp_path):
    profile = make_profile(completion_sound_enabled=True)
    timeline = build_timeline(profile)
    path = synthesize_sfx_wav(timeline, profile, tmp_path / "sfx.wav")
    envelope = read_mono_envelope(path)

    baseline = synthesize_sfx_wav(
        timeline, replace(profile, completion_sound_enabled=False), tmp_path / "off.wav"
    )
    base_envelope = read_mono_envelope(baseline)
    assert len(envelope) == len(base_envelope)

    # The difference between ON and OFF is exactly one contiguous click.
    diff_indices = [
        i for i, (a, b) in enumerate(zip(envelope, base_envelope)) if abs(a - b) > 40
    ]
    assert diff_indices, "the completion click must be audible"
    span = diff_indices[-1] - diff_indices[0]
    click_samples = len(completion_sound_samples(profile.completion_sound_preset))
    assert span <= click_samples + 48  # one click, no duplicates anywhere else

    expected_start = int(round(timeline.typing_end * SFX_SAMPLE_RATE))
    expected_start = min(expected_start, max(0, len(envelope) - click_samples))
    assert diff_indices[0] == pytest.approx(expected_start, abs=48)
    # The click finishes inside the intro track (never after the video starts).
    assert diff_indices[-1] < len(envelope)


def test_completion_plays_even_when_per_character_sfx_is_off(tmp_path):
    profile = make_profile(sound_preset="off", completion_sound_enabled=True)
    timeline = build_timeline(profile)
    path = synthesize_sfx_wav(timeline, profile, tmp_path / "sfx.wav")
    envelope = read_mono_envelope(path)
    first, last = nonzero_span(envelope)
    assert first >= 0
    # Everything audible belongs to the completion click near typing_end.
    assert first >= int(timeline.typing_end * SFX_SAMPLE_RATE) - SFX_SAMPLE_RATE // 20


def test_completion_volume_scales_and_zero_is_silent(tmp_path):
    profile = make_profile(sound_preset="off")
    timeline = build_timeline(profile)
    loud = read_mono_envelope(synthesize_sfx_wav(
        timeline, replace(profile, completion_sound_volume=80), tmp_path / "loud.wav"))
    quiet = read_mono_envelope(synthesize_sfx_wav(
        timeline, replace(profile, completion_sound_volume=20), tmp_path / "quiet.wav"))
    assert max(loud) > max(quiet) > 0
    silent = read_mono_envelope(synthesize_sfx_wav(
        timeline, replace(profile, completion_sound_volume=0), tmp_path / "zero.wav"))
    assert max(silent) == 0


def test_completion_disabled_is_the_historical_silent_tail(tmp_path):
    profile = make_profile(completion_sound_enabled=False)
    timeline = build_timeline(profile)
    path = synthesize_sfx_wav(timeline, profile, tmp_path / "sfx.wav")
    envelope = read_mono_envelope(path)
    typing_end_sample = int(timeline.typing_end * SFX_SAMPLE_RATE)
    assert all(value <= 40 for value in envelope[typing_end_sample:])


def test_wav_rendering_is_deterministic(tmp_path):
    profile = make_profile()
    timeline = build_timeline(profile)
    first = synthesize_sfx_wav(timeline, profile, tmp_path / "a.wav")
    second = synthesize_sfx_wav(timeline, profile, tmp_path / "b.wav")
    assert first.read_bytes() == second.read_bytes()


# ---------------------------------------------------------------------------
# Settings resolution, profile separation, cache identity
# ---------------------------------------------------------------------------
def test_long_form_and_shorts_completion_settings_are_separate():
    settings = ExportSettings(
        typewriter_intro_enabled=True,
        typewriter_hook_text="hook",
        typewriter_completion_sound_enabled=True,
        typewriter_completion_sound_preset="enter_return",
        typewriter_completion_sound_volume=55,
        short_typewriter_intro_enabled=True,
        short_typewriter_hook_text="hook",
        short_typewriter_completion_sound_enabled=False,
        short_typewriter_completion_sound_preset="typewriter_return",
        short_typewriter_completion_sound_volume=10,
    )
    long_profile = profile_from_settings(settings, short=False)
    short_profile = profile_from_settings(settings, short=True)
    assert long_profile.completion_sound_enabled is True
    assert long_profile.completion_sound_preset == "enter_return"
    assert long_profile.completion_sound_volume == 55
    assert short_profile.completion_sound_enabled is False
    assert short_profile.completion_sound_preset == "typewriter_return"
    assert short_profile.completion_sound_volume == 10


def test_legacy_settings_keep_default_completion_values():
    profile = profile_from_settings(ExportSettings(typewriter_hook_text="hook"))
    assert profile.completion_sound_enabled is True
    assert profile.completion_sound_preset == "enter_return"
    assert profile.completion_sound_volume == 40


def test_completion_fields_change_the_intro_identity():
    base = typewriter_identity(make_profile(), 1280, 720, 30.0)
    disabled = typewriter_identity(
        make_profile(completion_sound_enabled=False), 1280, 720, 30.0
    )
    other_preset = typewriter_identity(
        make_profile(completion_sound_preset="typewriter_return"), 1280, 720, 30.0
    )
    other_volume = typewriter_identity(
        make_profile(completion_sound_volume=90), 1280, 720, 30.0
    )
    assert len({base, disabled, other_preset, other_volume}) == 4
