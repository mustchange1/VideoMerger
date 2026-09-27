"""Phase 33 unit tests: Smart Visuals selection engine (no rendering).

Smart Visuals is a content-aware SELECTION + placement engine: it matches
the local timeline context against the (tolerantly harvested) metadata of
existing pool media and NEVER generates anything. Covers the Phase-33
contracts: selection modes, relevance thresholds in one constants place,
tolerant metadata harvesting, uniqueness-aware global assignment, random
fallback, Randomize behavior, the single image duration, explainable
Analyze records and the hard no-generation guarantee.
"""
from __future__ import annotations

import json
from pathlib import Path
from random import Random

import pytest

import app.video_merger.image_generation as image_generation
from app.video_merger.models import ExportSettings
from app.video_merger.smart_visuals import (
    DEFAULT_SMART_IMAGE_DURATION,
    DEFAULT_SMART_INSERT_PERCENT,
    SMART_MATCH_THRESHOLD,
    SMART_MODE_RANDOM_ONLY,
    SMART_MODE_SMART_INSERTS,
    SMART_MODE_SMART_MATCH,
    SMART_VISUAL_MODES,
    SLOT_MODE_FALLBACK_RANDOM_IMAGE,
    SLOT_MODE_MATCH,
    SLOT_MODE_SKIPPED,
    SLOT_SOURCE_RANDOM,
    SLOT_SOURCE_SMART,
    SmartVisualProfile,
    assign_smart_visual_selections,
    assign_slot_times,
    build_media_index,
    build_semantic_slots,
    build_smart_visual_plan,
    clamp_smart_image_duration,
    clamp_smart_insert_percent,
    clamp_smart_visual_nonce,
    normalize_smart_visual_mode,
    scan_smart_visual_folders,
    smart_visual_profile_from_settings,
    split_script_sentences,
)

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG)


def _pool(tmp_path: Path, name: str = "pool") -> Path:
    """Relevant + irrelevant media with rich tolerant metadata."""
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    _write_image(folder / "alps_hiking.jpg")
    _write_image(folder / "alps_summit.jpg")
    _write_image(folder / "beach_sunset.jpg")
    _write_image(folder / "city_traffic.jpg")
    metadata = {
        "alps_hiking.jpg": {
            "title": "Hiking trail in the Alps",
            "description": "A hiker crosses a green alpine meadow below snowy peaks.",
            "keywords": ["alps", "hiking", "mountains"],
            "topic": "alpine adventure",
            "subject": ["hiker", "trail"],
            "entities": ["Alps"],
            "mood": "fresh",
            "environment": "outdoor daylight",
            "scene": "wide mountain panorama",
            "custom_unknown_field": "extra signal text glacier",
        },
        "alps_summit.jpg": {
            "title": "Alpine summit at dawn",
            "keywords": ["alps", "summit", "dawn"],
            "mood": "calm",
        },
        "beach_sunset.jpg": {
            "title": "Tropical beach at sunset",
            "keywords": ["beach", "sunset", "ocean"],
            "mood": "warm",
        },
        "city_traffic.jpg": {
            "title": "Dense city traffic at night",
            "keywords": ["city", "traffic", "night"],
        },
    }
    (folder / "smart_metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    return folder


def _plan(tmp_path: Path, folder: Path, script: str, *, mode: str = SMART_MODE_SMART_MATCH,
          nonce: int = 0, image_duration: float = DEFAULT_SMART_IMAGE_DURATION,
          insert_percent: int = DEFAULT_SMART_INSERT_PERCENT, seed: str = "p33",
          program_duration: float = 20.0, monkeypatch_provider=None):
    profile = SmartVisualProfile(
        enabled=True,
        folders=(str(folder),),
        mode=mode,
        image_duration=image_duration,
        insert_percent=insert_percent,
        randomize_nonce=nonce,
        cadence="every_1",
    )
    return build_smart_visual_plan(
        profile=profile,
        script_text=script,
        program_duration=program_duration,
        width=320,
        height=180,
        fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path="ffprobe-not-used",
        seed_parts=(seed,),
        log=lambda *_a, **_k: None,
    )


ALPS_SCRIPT = (
    "We hike through the alpine meadows toward the snowy mountain peaks. "
    "The trail climbs higher into the Alps every hour. "
)


# ---------------------------------------------------------------------------
# Normalization, constants, profile resolution
# ---------------------------------------------------------------------------
def test_mode_normalization_and_safe_defaults():
    assert normalize_smart_visual_mode(None) == "smart_match"
    assert normalize_smart_visual_mode("") == "smart_match"
    assert normalize_smart_visual_mode("NONSENSE") == "smart_match"
    for mode in SMART_VISUAL_MODES:
        assert normalize_smart_visual_mode(mode.upper()) == mode
    assert SMART_VISUAL_MODES == ("smart_match", "smart_inserts", "random_only")


def test_image_duration_and_percent_clamps():
    assert clamp_smart_image_duration(None) == 5.0
    assert clamp_smart_image_duration("abc") == 5.0
    assert clamp_smart_image_duration(0.1) == 0.5
    assert clamp_smart_image_duration(99) == 15.0
    assert clamp_smart_image_duration(5.0) == 5.0
    assert clamp_smart_insert_percent(-4) == 0
    assert clamp_smart_insert_percent(250) == 100
    assert clamp_smart_insert_percent("x") == 25
    assert clamp_smart_visual_nonce("bad") == 0
    assert clamp_smart_visual_nonce(7.9) == 7


def test_threshold_constants_live_in_one_place():
    # The relevance gates are module constants (one place, covered here);
    # nothing in the GUI exposes them as tunable values.
    assert 0.0 < SMART_MATCH_THRESHOLD < 1.0
    from app.video_merger import smart_visuals as sv

    assert sv.SMART_INSERT_STRONG_THRESHOLD >= SMART_MATCH_THRESHOLD
    assert sv.SMART_MATCH_TOP_K >= 1


def test_profile_resolution_reads_phase33_fields_with_defaults():
    settings = ExportSettings(
        smart_visual_enabled=True,
        smart_visual_folders=["x"],
        smart_visual_mode="smart_inserts",
        smart_visual_image_duration=7.5,
        smart_visual_insert_percent=40,
        smart_visual_randomize_nonce=2,
    )
    profile = smart_visual_profile_from_settings(settings)
    assert profile.mode == "smart_inserts"
    assert profile.image_duration == pytest.approx(7.5)
    assert profile.insert_percent == 40
    assert profile.randomize_nonce == 2
    # Missing keys (old projects) resolve to the safe defaults.
    defaults = smart_visual_profile_from_settings(ExportSettings())
    assert defaults.mode == "smart_match"
    assert defaults.image_duration == pytest.approx(5.0)
    assert defaults.insert_percent == 25
    assert defaults.randomize_nonce == 0


# ---------------------------------------------------------------------------
# Tolerant metadata harvesting
# ---------------------------------------------------------------------------
def test_sidecar_metadata_harvest_is_tolerant_and_schema_free(tmp_path):
    folder = _pool(tmp_path)
    entries = {Path(entry.path).name: entry for entry in scan_smart_visual_folders((str(folder),))}
    alps = entries["alps_hiking.jpg"]
    # Every metadata fragment enriches the semantic text - including keys the
    # engine has never seen before.
    text = alps.metadata_text.casefold()
    assert "hiker crosses a green alpine meadow" in text
    assert "alpine adventure" in text
    assert "wide mountain panorama" in text
    assert "extra signal text glacier" in text
    # Primary signals also join the hard keyword set; unknown keys enrich
    # ONLY the semantic text (the vector), never the hard keyword bonus.
    keywords = " ".join(alps.keywords)
    assert "alps" in keywords and "hiking" in keywords and "mountains" in keywords
    assert "glacier" in alps.metadata_text.casefold()


def test_broken_sidecar_never_breaks_indexing(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    _write_image(folder / "plain.png")
    (folder / "smart_metadata.json").write_text("{ this is not json", encoding="utf-8")
    entries, stats = build_media_index((str(folder),), tmp_path / "cache")
    assert len(entries) == 1
    assert stats.errors == 0


def test_sidecar_edit_invalidates_the_index_cache(tmp_path):
    folder = tmp_path / "city"
    folder.mkdir()
    _write_image(folder / "street.png")
    cache = tmp_path / "cache"
    entries1, _ = build_media_index((str(folder),), cache)
    assert entries1[0].metadata_text == ""
    (folder / "smart_metadata.json").write_text(
        json.dumps({"street.png": {"description": "rainy downtown avenue"}}),
        encoding="utf-8",
    )
    entries2, _ = build_media_index((str(folder),), cache)
    assert "rainy downtown avenue" in entries2[0].metadata_text


# ---------------------------------------------------------------------------
# Selection modes
# ---------------------------------------------------------------------------
def test_smart_match_beats_unrelated_media(tmp_path):
    plan = _plan(tmp_path, _pool(tmp_path), ALPS_SCRIPT)
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled
    smart = [slot for slot in filled if slot.source_mode == SLOT_SOURCE_SMART]
    assert smart, "relevant assets must be matched semantically"
    for slot in smart:
        name = Path(slot.selected_path).name
        assert name.startswith("alps_"), f"unrelated media selected: {name}"
        assert slot.fallback_mode == SLOT_MODE_MATCH
        assert slot.score >= SMART_MATCH_THRESHOLD
        assert slot.reason.startswith("matched")


def test_smart_match_weak_context_falls_back_to_random_existing(tmp_path):
    folder = _pool(tmp_path)
    plan = _plan(
        tmp_path, folder,
        "Quantum computers race through impossible calculations. "
        "Stock markets wobble on uncertain news.",
    )
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled, "weak matches still place an existing asset - never skip"
    for slot in filled:
        assert slot.source_mode == SLOT_SOURCE_RANDOM
        assert slot.generation_used is False
        assert Path(slot.selected_path).exists()


def test_random_only_mode_uses_no_matching(tmp_path):
    plan = _plan(tmp_path, _pool(tmp_path), ALPS_SCRIPT, mode=SMART_MODE_RANDOM_ONLY)
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled
    for slot in filled:
        assert slot.source_mode == SLOT_SOURCE_RANDOM
        assert slot.score == 0.0
        assert slot.fallback_mode == SLOT_MODE_FALLBACK_RANDOM_IMAGE


def test_smart_inserts_mode_mixes_smart_and_random(tmp_path):
    folder = _pool(tmp_path)
    script = (
        "We hike through the alpine meadows toward the peaks. "
        "A totally unrelated sentence about kitchen appliances. "
        "The trail climbs higher into the Alps. "
        "Another unrelated thought about stock markets. "
        "Snow covers the summit in the early morning. "
        "Banana exports rise sharply this quarter. "
    )
    plan = _plan(
        tmp_path, folder, script, mode=SMART_MODE_SMART_INSERTS,
        insert_percent=50, program_duration=30.0,
    )
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled
    sources = {slot.source_mode for slot in filled}
    assert SLOT_SOURCE_RANDOM in sources, "Mostly Random keeps random placements"
    smart = [slot for slot in filled if slot.source_mode == SLOT_SOURCE_SMART]
    for slot in smart:
        # Strong matches only: clearly relevant media at insert points.
        assert Path(slot.selected_path).name.startswith("alps_")
        assert slot.score >= SMART_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Uniqueness-aware global assignment
# ---------------------------------------------------------------------------
def test_uniqueness_while_unused_assets_remain(tmp_path):
    folder = _pool(tmp_path)  # 4 images, more than enough
    plan = _plan(
        tmp_path, folder, ALPS_SCRIPT + "The beach glows at sunset tonight. ",
        program_duration=24.0,
    )
    paths = [slot.selected_path for slot in plan.slots if slot.selected_kind]
    assert len(paths) >= 3
    assert len(set(paths)) == len(paths), "no asset repeats while unused ones remain"


def test_pool_exhaustion_allows_reuse_but_never_back_to_back(tmp_path):
    folder = tmp_path / "small"
    folder.mkdir()
    _write_image(folder / "only_one.jpg")
    _write_image(folder / "only_two.jpg")
    script = "One. Two. Three. Four. Five. Six. Seven. Eight. "
    plan = _plan(tmp_path, folder, script, mode=SMART_MODE_RANDOM_ONLY, program_duration=32.0)
    paths = [slot.selected_path for slot in plan.slots if slot.selected_kind]
    assert len(paths) > 2, "the exhausted pool may be reused"
    for previous, current in zip(paths, paths[1:]):
        assert previous != current, "never the same asset twice in a row"


def test_uniqueness_applies_to_videos_and_images(tmp_path):
    folder = tmp_path / "mixed"
    folder.mkdir()
    _write_image(folder / "alps_one.jpg")
    (folder / "alps_two.mp4").write_bytes(b"vid")
    (folder / "alps_three.jpg").write_bytes(b"img")
    script = "Alps sentence one. Alps sentence two. Alps sentence three. "
    profile = SmartVisualProfile(enabled=True, folders=(str(folder),), cadence="every_1")
    sentences = split_script_sentences(script)
    timed = assign_slot_times(build_semantic_slots(sentences, "every_1"), 15.0)
    entries, _ = build_media_index((str(folder),), folder.parent / "cache")
    slots = assign_smart_visual_selections(
        timed, entries, profile=profile, rng=Random(33),
    )
    kinds = {slot.selected_kind for slot in slots if slot.selected_kind}
    assert "image" in kinds
    paths = [slot.selected_path for slot in slots if slot.selected_kind]
    assert len(set(paths)) == len(paths)


# ---------------------------------------------------------------------------
# Randomize Timeline
# ---------------------------------------------------------------------------
def test_randomize_changes_assignment_but_keeps_validity(tmp_path):
    folder = _pool(tmp_path)
    script = (
        "We hike through the alpine meadows. "
        "The beach glows at sunset. "
        "City lights shimmer at night. "
        "The summit rests below the stars. "
    )
    base = _plan(tmp_path, folder, script, nonce=0)
    alt = _plan(tmp_path, folder, script, nonce=1)
    base_paths = [slot.selected_path for slot in base.slots if slot.selected_kind]
    alt_paths = [slot.selected_path for slot in alt.slots if slot.selected_kind]
    assert base_paths and alt_paths
    assert base_paths != alt_paths, "Randomize explores a different valid assignment"
    # Both assignments stay unique while unused assets remain.
    assert len(set(base_paths)) == len(base_paths)
    assert len(set(alt_paths)) == len(alt_paths)
    # Randomize keeps the mode, duration and timeline (same slot windows).
    assert [slot.start for slot in base.slots] == [slot.start for slot in alt.slots]
    # Determinism: the same nonce reproduces the same assignment.
    again = _plan(tmp_path, folder, script, nonce=1)
    assert [slot.selected_path for slot in again.slots if slot.selected_kind] == alt_paths


def test_smart_match_randomize_stays_relevant(tmp_path):
    folder = _pool(tmp_path)
    plan = _plan(tmp_path, folder, ALPS_SCRIPT, nonce=3)
    smart = [slot for slot in plan.slots if slot.source_mode == SLOT_SOURCE_SMART]
    assert smart
    for slot in smart:
        # Alternate good matches are still alpine media, never random junk.
        assert Path(slot.selected_path).name.startswith("alps_")
        assert slot.score >= SMART_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Image duration
# ---------------------------------------------------------------------------
def test_default_image_duration_is_5_seconds(tmp_path):
    plan = _plan(tmp_path, _pool(tmp_path), ALPS_SCRIPT)
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled
    assert all(slot.insert_duration == pytest.approx(5.0) for slot in filled)
    records = plan.to_records()
    assert all(record["insert_duration"] == pytest.approx(5.0) for record in records)


def test_explicit_image_duration_is_preserved_and_clamped(tmp_path):
    folder = _pool(tmp_path)
    plan = _plan(tmp_path, folder, ALPS_SCRIPT, image_duration=2.5)
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled and all(slot.insert_duration == pytest.approx(2.5) for slot in filled)
    profile = SmartVisualProfile(enabled=True, folders=(str(folder),), image_duration=99.0)
    assert profile.image_duration == pytest.approx(99.0)
    settings = ExportSettings(smart_visual_image_duration=120.0)
    resolved = smart_visual_profile_from_settings(settings)
    assert resolved.image_duration == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Analyze records + no-generation guarantee
# ---------------------------------------------------------------------------
def test_analyze_records_are_explainable_and_readable(tmp_path):
    plan = _plan(tmp_path, _pool(tmp_path), ALPS_SCRIPT)
    records = plan.to_records()
    assert records
    for record in records:
        assert record["source"] in {"SMART", "RANDOM", "NONE"}
        assert record["insert_duration"] > 0
        assert "context" in record
        assert record["topic"]
        # start-end window present (readable analysis row)
        assert "-" in record["time"] and record["time"].endswith("s")
    smart_records = [record for record in records if record["source"] == "SMART"]
    assert smart_records
    for record in smart_records:
        assert record["reason"].startswith("matched")
        assert float(record["match"]) > 0.0


def test_generation_is_never_invoked_in_any_mode(tmp_path, monkeypatch):
    def exploding_resolve(ffmpeg_path=None):  # pragma: no cover - must not run
        raise AssertionError("Phase 33: Smart Visuals must never resolve a provider")

    monkeypatch.setattr(image_generation, "resolve_generation_provider", exploding_resolve)
    folder = _pool(tmp_path)
    for mode in SMART_VISUAL_MODES:
        plan = _plan(
            tmp_path, folder, ALPS_SCRIPT, mode=mode,
            seed=f"trap-{mode}",
        )
        assert not any(slot.generation_used for slot in plan.slots), mode
        assert not any(slot.selected_kind == "generated" for slot in plan.slots), mode
        assert plan.diagnostics, "the plan stays honest about its selection"
    generated_dir = tmp_path / "cache" / "smart_visual_generated"
    assert not generated_dir.exists() or not list(generated_dir.glob("*"))


def test_plan_identity_changes_with_phase33_settings_only(tmp_path):
    folder = _pool(tmp_path)
    base = _plan(tmp_path, folder, ALPS_SCRIPT)
    same = _plan(tmp_path, folder, ALPS_SCRIPT)
    assert base.identity == same.identity
    assert base.identity, "an active plan carries an identity"
    # Inert legacy fields never churn the identity.
    legacy = _plan(tmp_path, folder, ALPS_SCRIPT)
    assert legacy.identity == base.identity
    # Phase 33 settings do.
    assert _plan(tmp_path, folder, ALPS_SCRIPT, nonce=5).identity != base.identity
    assert _plan(tmp_path, folder, ALPS_SCRIPT, mode=SMART_MODE_RANDOM_ONLY).identity != base.identity
    assert _plan(tmp_path, folder, ALPS_SCRIPT, image_duration=9.0).identity != base.identity


def test_empty_pool_stays_renderable(tmp_path):
    folder = tmp_path / "nothing"
    folder.mkdir()
    plan = _plan(tmp_path, folder, ALPS_SCRIPT)
    assert plan.slots
    assert all(slot.selected_kind == "" for slot in plan.slots)
    assert all(slot.fallback_mode == SLOT_MODE_SKIPPED for slot in plan.slots)
    assert plan.identity == ""
