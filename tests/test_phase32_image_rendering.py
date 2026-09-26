"""Phase 32 unit tests: dedicated image transitions + image visual effects.

Covers Feature Groups B, C, D at the unit level: normalization, filter
chain determinism, boundary duration resolution, profile mapping, LF/Shorts
separation and the cache-identity guarantees (default == historical).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.image_timeline import (
    IMAGE_TRANSITION_CHOICES,
    IMAGE_VISUAL_EFFECTS,
    IMAGE_VISUAL_INTENSITIES,
    ImageTimelineProfile,
    clamp_image_transition_duration,
    image_plan_identity,
    image_visual_effect_chain,
    make_image_media,
    normalize_image_transition_choice,
    normalize_visual_effect,
    normalize_visual_effect_intensity,
    profile_from_settings,
)
from app.video_merger.models import ExportSettings
from app.video_merger.target import resolve_export
from tests.conftest import fake_media


def _image_item(name="img.jpg", duration=2.5, **overrides):
    item = fake_media(name, duration=duration)
    return replace(
        item,
        is_image_insertion=True,
        image_timeline_insertion=True,
        image_fit_mode="fill",
        video_codec="image2",
        image_motion="zoom_in",
        **overrides,
    )


def _settings(**overrides) -> ExportSettings:
    return ExportSettings(aspect="16:9", **overrides)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def test_normalizers_and_clamps():
    assert normalize_image_transition_choice(None) == "project"
    assert normalize_image_transition_choice("garbage") == "project"
    for choice in IMAGE_TRANSITION_CHOICES:
        assert normalize_image_transition_choice(choice.upper()) == choice
    assert normalize_visual_effect("nonsense") == "none"
    for effect in IMAGE_VISUAL_EFFECTS:
        assert normalize_visual_effect(effect) == effect
    for level in IMAGE_VISUAL_INTENSITIES:
        assert normalize_visual_effect_intensity(level.upper()) == level
    assert clamp_image_transition_duration(None) is None
    assert clamp_image_transition_duration(0) is None
    assert clamp_image_transition_duration(-2) is None
    assert clamp_image_transition_duration("bad") is None
    assert clamp_image_transition_duration(0.4) == pytest.approx(0.4)
    assert clamp_image_transition_duration(9.0) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Feature C: visual effect chains
# ---------------------------------------------------------------------------
def test_effect_chain_none_is_empty_for_every_intensity():
    for level in IMAGE_VISUAL_INTENSITIES:
        assert image_visual_effect_chain("none", level, 720) == ""
        assert image_visual_effect_chain("", level, 720) == ""


def test_every_effect_produces_a_deterministic_chain():
    for effect in IMAGE_VISUAL_EFFECTS:
        if effect == "none":
            continue
        chain_a = image_visual_effect_chain(effect, "medium", 720)
        chain_b = image_visual_effect_chain(effect, "medium", 720)
        assert chain_a == chain_b
        assert chain_a, f"effect {effect} must produce a chain"


def test_effect_intensity_scales_the_chain():
    for effect in ("soft_shimmer", "gentle_flicker", "film_flicker", "crt_broadcast", "soft_glow_pulse"):
        low = image_visual_effect_chain(effect, "low", 720)
        high = image_visual_effect_chain(effect, "high", 720)
        assert low != high, f"intensity must change the {effect} chain"


def test_crt_broadcast_reuses_phase30_scanline_engine():
    chain = image_visual_effect_chain("crt_broadcast", "medium", 720)
    assert "mod(Y" in chain  # the Phase-30 scanline geq expression


# ---------------------------------------------------------------------------
# Feature B: per-boundary transition resolution
# ---------------------------------------------------------------------------
def test_project_default_keeps_historical_transitions():
    media = [fake_media("a.mp4"), _image_item(), fake_media("b.mp4"), fake_media("c.mp4")]
    base = resolve_export(media, _settings(transition_duration=1.0))
    override = resolve_export(
        media,
        _settings(
            transition_duration=1.0,
            timeline_image_transition_type="project",
            timeline_image_transition_duration=None,
        ),
    )
    assert base.transitions == override.transitions
    assert base.expected_duration == override.expected_duration


def test_explicit_image_transition_duration_applies_only_at_image_boundaries():
    media = [fake_media("a.mp4"), _image_item(), fake_media("b.mp4"), fake_media("c.mp4")]
    settings = _settings(
        transition_duration=1.0,
        timeline_image_transition_type="film_dissolve",
        timeline_image_transition_duration=0.4,
    )
    resolved = resolve_export(media, settings)
    # boundaries: 0=a→img, 1=img→b, 2=b→c. The global 1.0 s transition is
    # clamped to 45 % of the adjacent 2.0 s clips (0.9 s) everywhere.
    assert resolved.transitions[0] == pytest.approx(0.4)
    assert resolved.transitions[1] == pytest.approx(0.4)
    assert resolved.transitions[2] == pytest.approx(0.9)  # video→video unchanged


def test_image_transition_none_forces_hard_cut():
    media = [fake_media("a.mp4"), _image_item(), fake_media("b.mp4")]
    resolved = resolve_export(
        media,
        _settings(transition_duration=1.0, timeline_image_transition_type="none"),
    )
    assert resolved.transitions[0] == 0.0
    assert resolved.transitions[1] == 0.0


def test_image_to_image_boundary_uses_the_image_transition():
    media = [fake_media("a.mp4"), _image_item("i1.jpg"), _image_item("i2.jpg"), fake_media("b.mp4")]
    resolved = resolve_export(
        media,
        _settings(
            transition_duration=1.0,
            timeline_image_transition_type="smooth_blur",
            timeline_image_transition_duration=0.7,
        ),
    )
    assert resolved.transitions[0] == pytest.approx(0.7)
    assert resolved.transitions[1] == pytest.approx(0.7)  # image→image
    assert resolved.transitions[2] == pytest.approx(0.7)


def test_stage2_add_image_is_not_affected_by_timeline_override():
    """Stage-2 Add Image items are not image_timeline_insertion elements."""
    stage2_image = replace(fake_media("add.jpg"), is_image_insertion=True)
    media = [fake_media("a.mp4"), stage2_image, fake_media("b.mp4")]
    resolved = resolve_export(
        media,
        _settings(
            transition_duration=1.0,
            timeline_image_transition_type="smooth_blur",
            timeline_image_transition_duration=0.3,
        ),
    )
    assert resolved.transitions == pytest.approx([0.9, 0.9])  # 45 % clamp of the 2.0 s clips


def test_short_clamp_keeps_boundary_safe():
    media = [fake_media("a.mp4", duration=0.5), _image_item(duration=0.5), fake_media("b.mp4")]
    resolved = resolve_export(
        media,
        _settings(
            transition_duration=0.2,
            timeline_image_transition_type="cross_dissolve",
            timeline_image_transition_duration=4.0,
        ),
    )
    # 45 % clamp per adjacent element still protects very short sections.
    assert resolved.transitions[0] <= 0.5 * 0.45 + 1e-6
    assert resolved.transitions[1] <= 0.5 * 0.45 + 1e-6


# ---------------------------------------------------------------------------
# Profile resolution + element factory
# ---------------------------------------------------------------------------
def test_profile_reads_canonical_phase32_fields():
    settings = _settings(
        timeline_image_transition_type="additive_dissolve",
        timeline_image_transition_duration=0.9,
        timeline_image_visual_effect="film_flicker",
        timeline_image_visual_effect_intensity="high",
    )
    profile = profile_from_settings(settings)
    assert profile.transition_type == "additive_dissolve"
    assert profile.transition_duration == pytest.approx(0.9)
    assert profile.visual_effect == "film_flicker"
    assert profile.visual_effect_intensity == "high"
    assert profile.phase32_active is True
    assert profile_from_settings(_settings()).phase32_active is False


def test_make_image_media_keeps_historical_item_when_default():
    profile = profile_from_settings(_settings())
    item = make_image_media(
        path=Path("shot.jpg"), duration=2.5, width=1280, height=720, fps=30.0,
        size=(1920, 1080), transition_type="cross_dissolve", profile=profile,
    )
    assert item.image_transition_type == "cross_dissolve"  # follows the job
    assert item.image_visual_effect == ""
    assert item.image_visual_effect_intensity == ""
    assert item.image_timeline_insertion is True


def test_make_image_media_applies_phase32_overrides():
    profile = profile_from_settings(_settings(
        timeline_image_transition_type="film_dissolve",
        timeline_image_visual_effect="soft_shimmer",
        timeline_image_visual_effect_intensity="medium",
    ))
    item = make_image_media(
        path=Path("shot.jpg"), duration=2.5, width=1280, height=720, fps=30.0,
        size=(1920, 1080), transition_type="cross_dissolve", profile=profile,
    )
    assert item.image_transition_type == "film_dissolve"
    assert item.image_visual_effect == "soft_shimmer"
    assert item.image_visual_effect_intensity == "medium"


def test_image_plan_identity_unchanged_by_default_phase32_fields():
    profile_default = profile_from_settings(_settings(timeline_image_mode="every_n"))
    images = [Path("a.jpg"), Path("b.jpg")]
    base = image_plan_identity(profile_default, images, [1, 3], (1280, 720, 30.0))
    again = image_plan_identity(profile_default, images, [1, 3], (1280, 720, 30.0))
    assert base == again
    changed = image_plan_identity(
        profile_from_settings(_settings(
            timeline_image_mode="every_n",
            timeline_image_visual_effect="gentle_flicker",
        )),
        images, [1, 3], (1280, 720, 30.0),
    )
    assert changed != base


def test_command_builder_applies_visual_effect_only_to_timeline_images():
    from app.video_merger.command_builder import _timeline_image_framing

    plain = _timeline_image_framing(_image_item(), 320, 180, 2.5)
    shimmered = _timeline_image_framing(
        _image_item(image_visual_effect="soft_shimmer", image_visual_effect_intensity="low"),
        320, 180, 2.5,
    )
    assert shimmered != plain
    assert "geq" in shimmered
    # A normal video item never goes through this chain at all; an item
    # without the effect keeps the exact historical chain.
    assert image_visual_effect_chain("", "low", 180) == ""


# ---------------------------------------------------------------------------
# Long-Form / Shorts separation
# ---------------------------------------------------------------------------
def test_long_form_and_shorts_image_settings_stay_separate():
    from app.video_merger.youtube_outputs import long_form_settings, short_settings, ShortJob

    settings = ExportSettings(
        long_form_image_transition_type="film_dissolve",
        long_form_image_transition_duration=0.6,
        long_form_image_visual_effect="soft_shimmer",
        long_form_image_visual_effect_intensity="medium",
        shorts_image_transition_type="smooth_blur",
        shorts_image_transition_duration=0.3,
        shorts_image_visual_effect="crt_broadcast",
        shorts_image_visual_effect_intensity="high",
    )
    lf = long_form_settings(settings)
    assert lf.timeline_image_transition_type == "film_dissolve"
    assert lf.timeline_image_transition_duration == pytest.approx(0.6)
    assert lf.timeline_image_visual_effect == "soft_shimmer"
    assert lf.timeline_image_visual_effect_intensity == "medium"

    job = ShortJob(
        index=1, voiceover_path=Path("vo.mp3"), script_path=None,
        output_name="Short1", cache_key="short-1",
    )
    short = short_settings(settings, job)
    assert short.timeline_image_transition_type == "smooth_blur"
    assert short.timeline_image_transition_duration == pytest.approx(0.3)
    assert short.timeline_image_visual_effect == "crt_broadcast"
    assert short.timeline_image_visual_effect_intensity == "high"
    # No leakage: the Short job must not carry the Long-Form values.
    assert short.timeline_image_transition_type != lf.timeline_image_transition_type


def test_fallback_settings_stay_separate_per_output():
    from app.video_merger.youtube_outputs import long_form_settings, short_settings, ShortJob

    settings = ExportSettings(
        smart_visual_fallback="random_video",
        smart_visual_allow_generated=False,
        shorts_smart_visual_fallback="skip",
        shorts_smart_visual_allow_generated=True,
    )
    lf = long_form_settings(settings)
    assert lf.smart_visual_fallback == "random_video"
    assert lf.smart_visual_allow_generated is False
    job = ShortJob(
        index=1, voiceover_path=Path("vo.mp3"), script_path=None,
        output_name="Short1", cache_key="short-1",
    )
    short = short_settings(settings, job)
    assert short.smart_visual_fallback == "skip"
    assert short.smart_visual_allow_generated is True
