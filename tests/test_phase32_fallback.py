"""Phase 32 unit tests: Smart Visual fallback policies + generation toggle.

The Phase-31 matching/threshold/generation behavior must stay intact; these
tests cover the new explicit fallback policy and the Allow Generated Images
toggle (Feature Groups A, F, G). No rendering happens here - planning only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.image_generation import GenerationResult, ImageGenerationProvider
from app.video_merger.smart_visuals import (
    FALLBACK_CHOICES,
    SLOT_MODE_FALLBACK_BEST_AVAILABLE,
    SLOT_MODE_FALLBACK_GENERATED,
    SLOT_MODE_FALLBACK_RANDOM_IMAGE,
    SLOT_MODE_FALLBACK_RANDOM_VIDEO,
    SLOT_MODE_MATCH,
    SLOT_MODE_SKIPPED,
    SmartVisualProfile,
    build_smart_visual_plan,
    normalize_fallback_policy,
    smart_visual_profile_from_settings,
)
from app.video_merger.models import ExportSettings

SCRIPT = (
    "The mountain glacier melts faster every year. "
    "Rivers carry the meltwater down into the valley. "
    "A totally unrelated sentence about zebras in the zoo. "
)


class RecordingProvider(ImageGenerationProvider):
    """Test double: records every generate() call and optionally succeeds."""

    name = "recording-test"
    model = "test"

    def __init__(self, *, succeed: bool, target: Path):
        self.calls: list[str] = []
        self.succeed = succeed
        self.target = target

    def is_available(self) -> bool:
        return True

    def diagnostics(self) -> list[str]:
        return ["recording test provider"]

    def generate(self, prompt, width, height, *, style="", seed=0, cache_dir, params=None):
        self.calls.append(prompt)
        if self.succeed:
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_bytes(b"fake-generated-image")
            return GenerationResult(self.target, ["test generation ok"])
        return GenerationResult(None, ["test generation refused"])


def _make_pool(tmp_path: Path) -> Path:
    """One smart-visual folder with matching + non-matching images/videos."""
    folder = tmp_path / "pool"
    folder.mkdir(exist_ok=True)
    (folder / "glacier_mountain_ice.jpg").write_bytes(b"img1")
    (folder / "river_valley_water.jpg").write_bytes(b"img2")
    (folder / "mountain_stream.mp4").write_bytes(b"vid1")
    (folder / "zebra_savanna_animal.mp4").write_bytes(b"vid2")
    return folder


_IMAGE_KWARGS = {
    "image_transition_type", "image_transition_duration",
    "image_visual_effect", "image_visual_effect_intensity",
}


def _plan(tmp_path, monkeypatch, *, provider=None, **overrides):
    """Build a plan with a stubbed provider resolution."""
    def fake_resolve(ffmpeg_path=None):
        if provider is None:
            return None, ["no provider in test"]
        return provider, provider.diagnostics()

    from app.video_merger import image_generation

    monkeypatch.setattr(image_generation, "resolve_generation_provider", fake_resolve)
    plan_kwargs = {key: overrides.pop(key) for key in list(overrides) if key in _IMAGE_KWARGS}
    profile = SmartVisualProfile(enabled=True, folders=(str(_make_pool(tmp_path)),), **overrides)
    return build_smart_visual_plan(
        profile=profile,
        script_text=SCRIPT,
        program_duration=24.0,
        width=320,
        height=180,
        fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path="ffprobe-not-used",
        seed_parts=("phase32-test",),
        log=lambda *_a, **_k: None,
        **plan_kwargs,
    )


# ---------------------------------------------------------------------------
# Normalization + profile resolution
# ---------------------------------------------------------------------------
def test_normalize_fallback_policy_defaults_and_aliases():
    assert normalize_fallback_policy(None) == "generate_image"
    assert normalize_fallback_policy("") == "generate_image"
    assert normalize_fallback_policy("nonsense") == "generate_image"
    for choice in FALLBACK_CHOICES:
        assert normalize_fallback_policy(choice.upper()) == choice


def test_profile_from_settings_reads_phase32_fields():
    settings = ExportSettings(
        smart_visual_enabled=True,
        smart_visual_folders=["x"],
        smart_visual_allow_generated=False,
        smart_visual_fallback="random_video",
    )
    profile = smart_visual_profile_from_settings(settings)
    assert profile.allow_generated is False
    assert profile.fallback_policy == "random_video"
    # Historical defaults stay untouched.
    defaults = smart_visual_profile_from_settings(ExportSettings())
    assert defaults.allow_generated is True
    assert defaults.fallback_policy == "generate_image"
    assert defaults.phase32_active is False


# ---------------------------------------------------------------------------
# Feature A/F: policies + generation toggle
# ---------------------------------------------------------------------------
def test_default_policy_keeps_phase31_generate_fallback(tmp_path, monkeypatch):
    """generate_image = historical flow: generate below the threshold."""
    target = tmp_path / "gen" / "image.png"
    provider = RecordingProvider(succeed=True, target=target)
    plan = _plan(tmp_path, monkeypatch, provider=provider, threshold_mode="high")
    generated = [slot for slot in plan.slots if slot.selected_kind == "generated"]
    assert generated, "below-threshold slots must use the generation path by default"
    assert provider.calls, "the provider must be asked under the default policy"
    assert all(slot.fallback_mode == SLOT_MODE_FALLBACK_GENERATED for slot in generated)


def test_skip_policy_never_inserts_and_never_generates(tmp_path, monkeypatch):
    provider = RecordingProvider(succeed=True, target=tmp_path / "gen.png")
    plan = _plan(
        tmp_path, monkeypatch, provider=provider,
        threshold_mode="custom", threshold_custom=0.99,
        fallback_policy="skip",
    )
    assert provider.calls == []
    below = [slot for slot in plan.slots if slot.fallback_mode == SLOT_MODE_SKIPPED]
    assert below, "at least one slot must be below the 0.99 threshold"
    for slot in below:
        assert slot.selected_kind == ""


def test_random_video_fallback_selects_only_videos(tmp_path, monkeypatch):
    provider = RecordingProvider(succeed=True, target=tmp_path / "gen.png")
    plan = _plan(
        tmp_path, monkeypatch, provider=provider,
        threshold_mode="custom", threshold_custom=0.99,
        fallback_policy="random_video",
    )
    assert provider.calls == [], "random fallback must not generate"
    picked = [slot for slot in plan.slots if slot.fallback_mode == SLOT_MODE_FALLBACK_RANDOM_VIDEO]
    assert picked, "at least one below-threshold slot must receive a random video"
    for slot in picked:
        assert slot.selected_kind == "video"
        assert slot.selected_path.endswith(".mp4")
        assert slot.reason == "fallback_random_video"


def test_random_image_fallback_selects_only_images(tmp_path, monkeypatch):
    plan = _plan(
        tmp_path, monkeypatch, provider=None,
        threshold_mode="custom", threshold_custom=0.99,
        fallback_policy="random_image",
    )
    picked = [slot for slot in plan.slots if slot.fallback_mode == SLOT_MODE_FALLBACK_RANDOM_IMAGE]
    assert picked
    for slot in picked:
        assert slot.selected_kind == "image"
        assert slot.selected_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))


def test_best_available_never_generates(tmp_path, monkeypatch):
    provider = RecordingProvider(succeed=True, target=tmp_path / "gen.png")
    plan = _plan(
        tmp_path, monkeypatch, provider=provider,
        threshold_mode="custom", threshold_custom=0.99,
        fallback_policy="best_available",
    )
    assert provider.calls == []
    picked = [
        slot for slot in plan.slots
        if slot.fallback_mode == SLOT_MODE_FALLBACK_BEST_AVAILABLE
    ]
    assert picked
    for slot in picked:
        assert slot.selected_kind in {"image", "video"}


def test_random_fallback_without_pool_media_skips_cleanly(tmp_path, monkeypatch):
    """A folder with only images cannot serve Random Video -> skip, never crash."""
    folder = tmp_path / "images_only"
    folder.mkdir()
    (folder / "glacier_mountain_ice.jpg").write_bytes(b"img")
    from app.video_merger.smart_visuals import build_smart_visual_plan

    profile = SmartVisualProfile(
        enabled=True, folders=(str(folder),),
        threshold_mode="custom", threshold_custom=0.99,
        fallback_policy="random_video",
    )
    plan = build_smart_visual_plan(
        profile=profile, script_text=SCRIPT, program_duration=24.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path="unused", log=lambda *_a, **_k: None,
    )
    assert plan.slots
    for slot in plan.slots:
        assert slot.selected_kind == ""
        assert slot.fallback_mode == SLOT_MODE_SKIPPED
    assert any("Random-Video-Fallback" in note for note in plan.diagnostics)


def test_allow_generated_off_never_resolves_or_calls_a_provider(tmp_path, monkeypatch):
    import app.video_merger.image_generation as ig

    def exploding_resolve(ffmpeg_path=None):  # pragma: no cover - must not run
        raise AssertionError("provider resolution must not run when generation is OFF")

    monkeypatch.setattr(ig, "resolve_generation_provider", exploding_resolve)
    plan = _plan(
        tmp_path, monkeypatch, provider=None,
        allow_generated=False,
        threshold_mode="custom", threshold_custom=0.99,
        fallback_policy="generate_image",
    )
    # generate_image policy + generation OFF behaves like the historical
    # unavailable-provider path: best existing media, never generation.
    below = [
        slot for slot in plan.slots
        if slot.reason == "fallback_best_existing"
    ]
    assert below
    assert all(slot.fallback_mode == SLOT_MODE_FALLBACK_BEST_AVAILABLE for slot in below)
    assert any("Erzeugung deaktiviert" in note for note in plan.diagnostics)


def test_allow_generated_off_blocks_strategy_generation_too(tmp_path, monkeypatch):
    provider = RecordingProvider(succeed=True, target=tmp_path / "gen.png")
    plan = _plan(
        tmp_path, monkeypatch, provider=provider,
        allow_generated=False,
        generation_strategy="always",
    )
    assert provider.calls == [], "'always' strategy must not generate when the toggle is OFF"
    assert not any(slot.generation_used for slot in plan.slots)


def test_matched_slots_keep_MATCH_tag_and_scores(tmp_path, monkeypatch):
    plan = _plan(tmp_path, monkeypatch, provider=None, threshold_mode="low")
    matched = [slot for slot in plan.slots if slot.fallback_mode == SLOT_MODE_MATCH]
    assert matched, "the glacier/river sentences must match the pool at a low threshold"
    for slot in matched:
        assert slot.reason == "matched"
        assert slot.score > 0.0


def test_random_fallback_respects_repetition_window(tmp_path, monkeypatch):
    """With one video in the pool the same file may repeat, but with two or
    more alternatives the window avoids immediate back-to-back repeats."""
    folder = tmp_path / "twovideos"
    folder.mkdir()
    (folder / "alpha_generic_clip.mp4").write_bytes(b"v1")
    (folder / "beta_generic_clip.mp4").write_bytes(b"v2")
    from app.video_merger.smart_visuals import build_smart_visual_plan

    profile = SmartVisualProfile(
        enabled=True, folders=(str(folder),),
        threshold_mode="custom", threshold_custom=0.99,
        fallback_policy="random_video", repetition_window=3,
    )
    plan = build_smart_visual_plan(
        profile=profile, script_text=SCRIPT, program_duration=24.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path="unused", log=lambda *_a, **_k: None,
    )
    paths = [slot.selected_path for slot in plan.slots if slot.selected_kind]
    assert len(paths) >= 2
    for previous, current in zip(paths, paths[1:]):
        assert previous != current, "repetition protection must avoid immediate repeats"


def test_preview_records_expose_phase32_fields(tmp_path, monkeypatch):
    plan = _plan(
        tmp_path, monkeypatch, provider=None,
        threshold_mode="low",
        image_transition_type="film_dissolve",
        image_transition_duration=0.8,
        image_visual_effect="soft_shimmer",
        image_visual_effect_intensity="medium",
    )
    records = plan.to_records()
    assert records
    for record in records:
        assert record["image_transition"] == "film_dissolve (0.80s)"
        assert record["image_effect"] == "soft_shimmer"
        assert record["image_effect_intensity"] == "medium"
        assert record["fallback_mode"]
        assert record["duration"] > 0


def test_plan_identity_stable_until_phase32_settings_change(tmp_path, monkeypatch):
    """Default Phase-32 settings keep the exact Phase-31 identity."""
    plan_default = _plan(tmp_path, monkeypatch, provider=None, threshold_mode="low")
    identity_default = plan_default.identity

    # Same plan rebuilt (deterministic) -> identical.
    plan_again = _plan(tmp_path, monkeypatch, provider=None, threshold_mode="low")
    assert plan_again.identity == identity_default

    # A non-default fallback policy changes the identity.
    plan_changed = _plan(
        tmp_path, monkeypatch, provider=None, threshold_mode="low",
        fallback_policy="random_video",
    )
    assert plan_changed.identity != identity_default

    # A non-default image transition changes it too when image visuals exist
    # (image_first guarantees at least one image selection here).
    assert any(slot.selected_kind == "image" for slot in plan_default.slots)
    plan_transition = _plan(
        tmp_path, monkeypatch, provider=None, threshold_mode="low",
        image_transition_type="smooth_blur",
    )
    assert plan_transition.identity != identity_default
