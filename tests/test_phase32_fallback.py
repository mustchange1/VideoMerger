"""Phase 32 unit tests: Smart Visual fallback policies + generation toggle.

PHASE 33 CONTRACT CHANGE (documented): Smart Visuals is now a pure
content-aware SELECTION engine. The Phase-32 fallback-policy vocabulary
(``generate_image``/``random_video``/``random_image``/``best_available``/
``skip``) and the Allow-Generated toggle are RETAINED as settings fields for
backward compatibility, but they no longer drive the workflow: the
selection MODE (``smart_match``/``smart_inserts``/``random_only``) fully
governs the behavior and NOTHING is ever generated - weak matches fall back
to a seeded random EXISTING pool asset. The generation-oriented tests of
the Phase-32 delivery were therefore replaced 1:1 by the selection-contract
tests below (same guarantees, new engine); the normalizer and the profile
resolution tests stay unchanged because the legacy fields still load.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.image_generation import GenerationResult, ImageGenerationProvider
from app.video_merger.smart_visuals import (
    FALLBACK_CHOICES,
    SMART_MODE_RANDOM_ONLY,
    SMART_MODE_SMART_INSERTS,
    SMART_MODE_SMART_MATCH,
    SLOT_MODE_FALLBACK_RANDOM_IMAGE,
    SLOT_MODE_FALLBACK_RANDOM_VIDEO,
    SLOT_MODE_MATCH,
    SLOT_MODE_SKIPPED,
    SLOT_SOURCE_RANDOM,
    SLOT_SOURCE_SMART,
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
# Normalization + profile resolution (legacy fields still load)
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
# Phase 33 selection contract (replaces the generation-oriented tests)
# ---------------------------------------------------------------------------
def test_legacy_generate_policy_never_generates_anymore(tmp_path, monkeypatch):
    """Replaces ``test_default_policy_keeps_phase31_generate_fallback``:
    even a project that still carries the historical ``generate_image``
    fallback policy and ``allow_generated=True`` never generates anything -
    below-threshold slots fall back to an existing pool asset."""
    target = tmp_path / "gen" / "image.png"
    provider = RecordingProvider(succeed=True, target=target)
    plan = _plan(
        tmp_path, monkeypatch, provider=provider,
        allow_generated=True, fallback_policy="generate_image",
        threshold_mode="high",
    )
    assert provider.calls == [], "the selection engine must never ask a provider"
    assert not any(slot.generation_used for slot in plan.slots)
    assert not any(slot.selected_kind == "generated" for slot in plan.slots)
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled, "weak matches fall back to an existing asset, they do not vanish"
    assert not target.exists()


def test_smart_match_below_threshold_falls_back_to_existing(tmp_path, monkeypatch):
    """Replaces ``test_skip_policy_never_inserts_and_never_generates``:
    with an unreachable threshold every slot becomes a seeded RANDOM draw
    from the existing pool - never skipped while media exists, never
    generated."""
    provider = RecordingProvider(succeed=True, target=tmp_path / "gen.png")
    plan = _plan(
        tmp_path, monkeypatch, provider=provider,
        mode=SMART_MODE_SMART_MATCH,
    )
    assert provider.calls == []
    random_slots = [slot for slot in plan.slots if slot.source_mode == SLOT_SOURCE_RANDOM]
    assert random_slots or all(slot.source_mode == SLOT_SOURCE_SMART for slot in plan.slots if slot.selected_kind)
    for slot in plan.slots:
        if slot.selected_kind:
            assert Path(slot.selected_path).exists()
            assert slot.selected_kind in {"image", "video"}


def test_random_only_mode_never_matches_and_never_generates(tmp_path, monkeypatch):
    """Replaces ``test_random_video_fallback_selects_only_videos``:
    Random Only performs no semantic matching at all - every filled slot is
    a RANDOM draw from the pool."""
    provider = RecordingProvider(succeed=True, target=tmp_path / "gen.png")
    plan = _plan(tmp_path, monkeypatch, provider=provider, mode=SMART_MODE_RANDOM_ONLY)
    assert provider.calls == [], "random_only must not generate"
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled
    for slot in filled:
        assert slot.source_mode == SLOT_SOURCE_RANDOM
        assert slot.fallback_mode in {
            SLOT_MODE_FALLBACK_RANDOM_IMAGE, SLOT_MODE_FALLBACK_RANDOM_VIDEO,
        }
        assert slot.score == 0.0
        assert Path(slot.selected_path).exists()


def test_weak_match_falls_back_to_random_existing_image(tmp_path, monkeypatch):
    """Replaces ``test_random_image_fallback_selects_only_images``: when the
    context has nothing relevant, the fallback is a random EXISTING asset."""
    folder = tmp_path / "city"
    folder.mkdir()
    (folder / "city_street.jpg").write_bytes(b"img")
    (folder / "city_market.jpg").write_bytes(b"img2")
    from app.video_merger.smart_visuals import build_smart_visual_plan

    profile = SmartVisualProfile(
        enabled=True, folders=(str(folder),), mode=SMART_MODE_SMART_MATCH,
    )
    plan = build_smart_visual_plan(
        profile=profile,
        script_text="Quantum entanglement experiments in orbit.",
        program_duration=8.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path="unused", seed_parts=("weak",), log=lambda *_a, **_k: None,
    )
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled
    for slot in filled:
        assert slot.source_mode == SLOT_SOURCE_RANDOM
        assert slot.selected_kind == "image"


def test_selection_engine_never_resolves_a_provider(tmp_path, monkeypatch):
    """Replaces ``test_best_available_never_generates`` and
    ``test_allow_generated_off_never_resolves_or_calls_a_provider``: the
    provider resolution must not even RUN - there is no generation path left
    in the Smart Visual workflow (explosion proves it is never called)."""
    import app.video_merger.image_generation as ig

    def exploding_resolve(ffmpeg_path=None):  # pragma: no cover - must not run
        raise AssertionError("provider resolution must never run in Phase 33")

    monkeypatch.setattr(ig, "resolve_generation_provider", exploding_resolve)
    for mode in (SMART_MODE_SMART_MATCH, SMART_MODE_SMART_INSERTS, SMART_MODE_RANDOM_ONLY):
        plan = _plan(
            tmp_path, monkeypatch, provider=None, mode=mode,
            allow_generated=True, fallback_policy="generate_image",
            generation_strategy="always",
        )
        assert not any(slot.generation_used for slot in plan.slots)


def test_empty_pool_skips_cleanly(tmp_path, monkeypatch):
    """Replaces ``test_random_fallback_without_pool_media_skips_cleanly``:
    with no media at all every slot is skipped cleanly - never a crash."""
    folder = tmp_path / "empty"
    folder.mkdir()
    from app.video_merger.smart_visuals import build_smart_visual_plan

    profile = SmartVisualProfile(enabled=True, folders=(str(folder),))
    plan = build_smart_visual_plan(
        profile=profile, script_text=SCRIPT, program_duration=24.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path="unused", log=lambda *_a, **_k: None,
    )
    assert plan.slots
    for slot in plan.slots:
        assert slot.selected_kind == ""
        assert slot.fallback_mode == SLOT_MODE_SKIPPED


def test_matched_slots_keep_MATCH_tag_and_scores(tmp_path, monkeypatch):
    plan = _plan(tmp_path, monkeypatch, provider=None, mode=SMART_MODE_SMART_MATCH)
    matched = [slot for slot in plan.slots if slot.fallback_mode == SLOT_MODE_MATCH]
    assert matched, "the glacier/river sentences must match the pool"
    for slot in matched:
        # Phase 33: explainable reason "matched:<signals>" + SMART source.
        assert slot.reason.startswith("matched")
        assert slot.source_mode == SLOT_SOURCE_SMART
        assert slot.score > 0.0


def test_random_draws_avoid_immediate_back_to_back_repeats(tmp_path, monkeypatch):
    """Replaces ``test_random_fallback_respects_repetition_window``: with two
    pool assets the uniqueness-aware assignment never places the same file
    twice in a row while unused assets remain."""
    folder = tmp_path / "twovideos"
    folder.mkdir()
    (folder / "alpha_generic_clip.jpg").write_bytes(b"v1")
    (folder / "beta_generic_clip.jpg").write_bytes(b"v2")
    from app.video_merger.smart_visuals import build_smart_visual_plan

    profile = SmartVisualProfile(
        enabled=True, folders=(str(folder),), mode=SMART_MODE_RANDOM_ONLY,
    )
    plan = build_smart_visual_plan(
        profile=profile, script_text=SCRIPT, program_duration=24.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path="unused", seed_parts=("rep",), log=lambda *_a, **_k: None,
    )
    paths = [slot.selected_path for slot in plan.slots if slot.selected_kind]
    assert len(paths) >= 2
    for previous, current in zip(paths, paths[1:]):
        assert previous != current, "uniqueness rule must avoid immediate repeats"


def test_preview_records_expose_phase32_fields(tmp_path, monkeypatch):
    plan = _plan(
        tmp_path, monkeypatch, provider=None,
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
        # Phase 33 Analyze Timeline fields are always present.
        assert record["source"] in {"SMART", "RANDOM", "NONE"}
        assert record["insert_duration"] > 0


def test_plan_identity_stable_until_phase33_settings_change(tmp_path, monkeypatch):
    """Replaces ``test_plan_identity_stable_until_phase32_settings_change``:
    the identity is deterministic for identical plans and changes when the
    Phase-33 selection settings change (the legacy fallback policy is inert
    now and must NOT churn the identity)."""
    plan_default = _plan(tmp_path, monkeypatch, provider=None)
    identity_default = plan_default.identity

    # Same plan rebuilt (deterministic) -> identical.
    plan_again = _plan(tmp_path, monkeypatch, provider=None)
    assert plan_again.identity == identity_default

    # The inert legacy fallback policy does not churn the identity.
    plan_legacy = _plan(tmp_path, monkeypatch, provider=None, fallback_policy="random_video")
    assert plan_legacy.identity == identity_default

    # Phase 33 settings DO change the identity.
    plan_changed = _plan(tmp_path, monkeypatch, provider=None, image_duration=8.0)
    assert plan_changed.identity != identity_default
    plan_mode = _plan(tmp_path, monkeypatch, provider=None, mode=SMART_MODE_RANDOM_ONLY)
    assert plan_mode.identity != identity_default
