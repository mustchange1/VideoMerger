"""Phase 32 unit tests: persistence, legacy loading and cache identity.

Verifies that every new Phase-32 setting survives the settings store
round trip, that legacy projects (files without the new keys) load with the
documented defaults, that the shipped example configuration stays valid,
and that the render cache only sees the new fields when they are active.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.models import ExportSettings
from app.video_merger.settings_store import SettingsStore
from tests.conftest import fake_media

PHASE32_SETTINGS = dict(
    # Smart Visual fallback + generation toggle (Feature A, F)
    smart_visual_enabled=True,
    smart_visual_allow_generated=False,
    smart_visual_fallback="random_video",
    shorts_smart_visual_enabled=True,
    shorts_smart_visual_allow_generated=True,
    shorts_smart_visual_fallback="skip",
    # Image transitions (Feature B)
    long_form_image_transition_type="film_dissolve",
    long_form_image_transition_duration=0.6,
    shorts_image_transition_type="smooth_blur",
    shorts_image_transition_duration=0.3,
    timeline_image_transition_type="additive_dissolve",
    timeline_image_transition_duration=0.9,
    # Image visual effects (Feature C, D)
    long_form_image_visual_effect="soft_shimmer",
    long_form_image_visual_effect_intensity="medium",
    shorts_image_visual_effect="crt_broadcast",
    shorts_image_visual_effect_intensity="high",
    timeline_image_visual_effect="film_flicker",
    timeline_image_visual_effect_intensity="low",
    # Typewriter completion sound (Feature H)
    typewriter_completion_sound_enabled=True,
    typewriter_completion_sound_preset="enter_return",
    typewriter_completion_sound_volume=55,
    short_typewriter_completion_sound_enabled=False,
    short_typewriter_completion_sound_preset="typewriter_return",
    short_typewriter_completion_sound_volume=10,
)


def test_round_trip_preserves_every_phase32_setting(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(ExportSettings(**PHASE32_SETTINGS))
    loaded = store.load()
    for key, expected in PHASE32_SETTINGS.items():
        assert getattr(loaded, key) == expected, key


def test_legacy_settings_file_loads_with_phase32_defaults(tmp_path):
    """A project saved before Phase 32 must load cleanly with the new
    defaults: fallback=generate_image (the historical behavior), generation
    allowed, project-default image transitions, no effects, completion
    sound ON."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "aspect": "16:9",
        "smart_visual_enabled": True,
        "timeline_image_mode": "every_n",
        "typewriter_intro_enabled": True,
        "typewriter_hook_text": "hook",
    }), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.smart_visual_allow_generated is True
    assert loaded.smart_visual_fallback == "generate_image"
    assert loaded.shorts_smart_visual_fallback == "generate_image"
    assert loaded.long_form_image_transition_type == "project"
    assert loaded.long_form_image_transition_duration is None
    assert loaded.shorts_image_transition_type == "project"
    assert loaded.timeline_image_visual_effect == "none"
    assert loaded.timeline_image_visual_effect_intensity == "low"
    assert loaded.typewriter_completion_sound_enabled is True
    assert loaded.typewriter_completion_sound_preset == "enter_return"
    assert loaded.typewriter_completion_sound_volume == 40
    # Existing fields keep their saved values.
    assert loaded.aspect == "16:9"
    assert loaded.smart_visual_enabled is True
    assert loaded.typewriter_hook_text == "hook"


def test_shipped_example_configuration_loads():
    example = Path(__file__).resolve().parents[1] / "config" / "example_settings.json"
    loaded = SettingsStore(example).load()
    assert loaded.smart_visual_fallback == "generate_image"
    assert loaded.long_form_image_transition_type == "project"
    assert loaded.timeline_image_visual_effect == "none"
    assert loaded.typewriter_completion_sound_enabled is True


def test_example_configuration_contains_all_phase32_keys():
    example = Path(__file__).resolve().parents[1] / "config" / "example_settings.json"
    data = json.loads(example.read_text(encoding="utf-8"))
    for key in PHASE32_SETTINGS:
        assert key in data, f"example_settings.json must document {key}"


def test_render_cache_media_payload_only_carries_image_visuals_when_active():
    from app.video_merger.render_cache import _media_payload

    timeline_image = replace(
        fake_media("img.jpg"),
        image_timeline_insertion=True,
        image_transition_type="film_dissolve",
    )
    plain_video = fake_media("clip.mp4")
    effect_image = replace(
        timeline_image,
        image_visual_effect="soft_shimmer",
        image_visual_effect_intensity="high",
    )

    default_payload = _media_payload(timeline_image)
    assert "image_visual_effect" not in default_payload
    assert "image_visual_effect_intensity" not in default_payload

    effect_payload = _media_payload(effect_image)
    assert effect_payload["image_visual_effect"] == "soft_shimmer"
    assert effect_payload["image_visual_effect_intensity"] == "high"

    # Effect keys on a non-timeline item never leak into the identity.
    video_payload = _media_payload(
        replace(plain_video, image_visual_effect="soft_shimmer")
    )
    assert "image_visual_effect" not in video_payload


def test_stage1_fingerprint_changes_only_for_effective_image_visual_changes():
    """Two projects differing only in an image visual effect on a timeline
    image must have different Stage-1 fingerprints; unrelated defaults must
    not churn them."""
    from app.video_merger.render_cache import stage1_fingerprint
    from app.video_merger.target import resolve_export

    timeline_image = replace(
        fake_media("img.jpg"),
        image_timeline_insertion=True,
        image_transition_type="film_dissolve",
    )
    base = ExportSettings(aspect="16:9", timeline_image_mode="every_n")

    def fingerprint(image):
        resolved = resolve_export([image], base)
        return stage1_fingerprint([image], base, resolved)[0]

    identity_default = fingerprint(timeline_image)
    identity_shimmer = fingerprint(
        replace(timeline_image, image_visual_effect="soft_shimmer",
                image_visual_effect_intensity="low")
    )
    assert identity_default != identity_shimmer

    # Same effective inputs again -> stable fingerprint.
    assert fingerprint(timeline_image) == identity_default


def test_default_phase32_settings_never_change_the_stage1_fingerprint():
    """The decisive guarantee: an existing project rendered before Phase 32
    and rendered after it (all new settings at their defaults) produce the
    exact same Stage-1 fingerprint."""
    from app.video_merger.render_cache import stage1_fingerprint
    from app.video_merger.target import resolve_export

    media = [fake_media("a.mp4"), fake_media("b.mp4")]
    settings = ExportSettings(aspect="16:9")
    resolved = resolve_export(media, settings)
    legacy_fingerprint = stage1_fingerprint(media, settings, resolved)[0]

    # Re-save through the settings store once (adds Phase-32 keys at
    # defaults) and fingerprint again.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory) / "settings.json")
        store.save(settings)
        reloaded = store.load()
    resolved_again = resolve_export(media, reloaded)
    phase32_fingerprint = stage1_fingerprint(media, reloaded, resolved_again)[0]
    assert phase32_fingerprint == legacy_fingerprint
