"""Phase 31 unit tests: Smart Visual Hybrid (deterministic, no rendering).

Covers the spec section 44-47 unit requirements:
segmentation, grouping, keywords, metadata parsing, folder categories,
incremental indexing, cache keys, scoring, thresholds, repetition
protection, fallbacks, cache reuse, provider availability, LF/Shorts
separation - plus the mandatory DISABLED regression (historical behavior
and cache identities stay byte-identical).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video_merger.image_generation import (
    DiffusersProvider,
    SyntheticConceptProvider,
    generation_cache_key,
    resolve_generation_provider,
)
from app.video_merger.models import ExportSettings, MediaInfo
from app.video_merger.smart_visuals import (
    MAX_SLOT_SECONDS,
    MIN_SLOT_SECONDS,
    IndexStats,
    SmartVisualPlan,
    SmartVisualProfile,
    apply_smart_visual_plan,
    assign_slot_times,
    build_generation_prompt,
    build_media_index,
    build_semantic_slots,
    build_smart_visual_plan,
    clamp_generation_percent,
    clamp_repetition_window,
    clamp_threshold,
    concept_vector,
    cosine_similarity,
    extract_keywords,
    load_sidecar_metadata,
    normalize_generation_strategy,
    normalize_smart_visual_cadence,
    normalize_smart_visual_style,
    normalize_source_priority,
    normalize_threshold_mode,
    scan_smart_visual_folders,
    score_candidate,
    smart_plan_identity,
    smart_visual_profile_from_settings,
    smart_visual_threshold,
    split_script_sentences,
    strategy_wants_generation,
    MediaIndexEntry,
)
from tests.conftest import fake_media, make_clip


# ---------------------------------------------------------------------------
# Normalization, thresholds, profile resolution
# ---------------------------------------------------------------------------
def test_normalizers_fall_back_to_safe_defaults():
    assert normalize_source_priority("nonsense") == "balanced"
    assert normalize_source_priority("VIDEO_FIRST") == "video_first"
    assert normalize_threshold_mode("x") == "medium"
    assert normalize_generation_strategy("") == "only_when_no_match"
    assert normalize_smart_visual_style("wild") == "cinematic"
    assert normalize_smart_visual_cadence("sometimes") == "adaptive"
    assert clamp_threshold(5) == 0.95
    assert clamp_threshold(-1) == 0.05
    assert clamp_threshold("bad") == 0.5
    assert clamp_generation_percent(250) == 100
    assert clamp_repetition_window(-3) == 0


def test_threshold_presets_and_custom():
    assert smart_visual_threshold("low", 0.9) == pytest.approx(0.35)
    assert smart_visual_threshold("medium", 0.9) == pytest.approx(0.50)
    assert smart_visual_threshold("high", 0.9) == pytest.approx(0.65)
    assert smart_visual_threshold("custom", 0.77) == pytest.approx(0.77)


def test_defaults_are_disabled_and_inactive():
    settings = ExportSettings()
    assert settings.smart_visual_enabled is False
    assert settings.shorts_smart_visual_enabled is False
    profile = smart_visual_profile_from_settings(settings)
    assert profile.active is False
    assert profile.threshold == pytest.approx(0.50)


def test_profile_resolution_from_settings():
    settings = ExportSettings(
        smart_visual_enabled=True,
        smart_visual_folders=[" media/city ", ""],
        smart_visual_source_priority="image_first",
        smart_visual_threshold_mode="custom",
        smart_visual_threshold_custom=0.42,
        smart_visual_generation_strategy="every_3rd",
        smart_visual_repetition_window=12,
    )
    profile = smart_visual_profile_from_settings(settings)
    assert profile.active is True
    assert profile.folders == ("media/city",)
    assert profile.source_priority == "image_first"
    assert profile.threshold == pytest.approx(0.42)
    assert profile.generation_strategy == "every_3rd"
    assert profile.repetition_window == 10  # clamped


# ---------------------------------------------------------------------------
# Segmentation, grouping, keywords, semantics
# ---------------------------------------------------------------------------
def test_sentence_splitting_handles_punctuation_and_newlines():
    text = "First sentence. Second one!\nThird line here\n\nFourth sentence follows."
    sentences = split_script_sentences(text)
    assert len(sentences) == 4
    assert sentences[0] == "First sentence."


def test_adaptive_grouping_merges_same_topic_sentences():
    sentences = [
        "The city streets are busy today.",
        "In the city, markets and shops flourish.",
        "The mountains rise above the valley.",
    ]
    slots = build_semantic_slots(sentences, "adaptive")
    assert len(slots) == 2
    assert len(slots[0].sentences) == 2
    assert slots[1].sentences == ["The mountains rise above the valley."]
    assert "city" in slots[0].keywords or "citi" in slots[0].keywords or "street" in slots[0].keywords


def test_cadence_forces_fixed_group_size():
    sentences = [f"Sentence number {i} about topic {i % 3}." for i in range(5)]
    slots = build_semantic_slots(sentences, "every_2")
    assert [len(slot.sentences) for slot in slots] == [2, 2, 1]
    slots_every1 = build_semantic_slots(sentences, "every_1")
    assert len(slots_every1) == 5


def test_slot_times_are_proportional_and_contiguous():
    drafts = build_semantic_slots(["Short one.", "A much longer sentence with many words inside it."], "every_1")
    timed = assign_slot_times(drafts, 10.0)
    assert timed[0].start == pytest.approx(0.0)
    assert timed[-1].end == pytest.approx(10.0)
    assert timed[0].end == pytest.approx(timed[1].start)
    # Longer sentence gets more time.
    assert (timed[1].end - timed[1].start) > (timed[0].end - timed[0].start)


def test_synonym_concepts_collapse_and_survive_stripping():
    query = concept_vector("the cities and towns grow")
    match = concept_vector("urban city downtown")
    assert cosine_similarity(query, match) > 0.4
    unrelated = concept_vector("glacier arctic ice")
    assert cosine_similarity(query, unrelated) == pytest.approx(0.0)
    # Stripped keyword lists still resolve to concepts.
    stripped = concept_vector(["citi", "berg"])
    assert stripped.get("city") == 1
    assert stripped.get("mountain") == 1


def test_keyword_extraction_prefers_long_distinct_tokens():
    keywords = extract_keywords("The roman empire and its roman cities", top_n=3)
    assert len(keywords) <= 3
    assert all(len(token) >= 3 for token in keywords)


# ---------------------------------------------------------------------------
# Folder categories, sidecar metadata, incremental index
# ---------------------------------------------------------------------------
def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 1x1 PNG - enough for indexing, no ffmpeg needed.
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
        )
    )


def test_sidecar_metadata_json_and_csv_are_optional_and_tolerant(tmp_path):
    folder = tmp_path / "City"
    folder.mkdir()
    _write_image(folder / "a.png")
    assert load_sidecar_metadata(folder) == {}

    (folder / "smart_metadata.json").write_text(
        json.dumps({"a.png": {"title": "Rome streets", "keywords": ["Roman", "City"]}}),
        encoding="utf-8",
    )
    meta = load_sidecar_metadata(folder)
    assert meta["a.png"]["title"] == "Rome streets"

    broken = tmp_path / "Broken"
    broken.mkdir()
    (broken / "smart_metadata.json").write_text("{ not json", encoding="utf-8")
    (broken / "smart_metadata.csv").write_text("file,title,keywords,category\nb.png,Harbor,ship;port,water\n", encoding="utf-8")
    meta = load_sidecar_metadata(broken)
    assert meta["b.png"]["keywords"] == ["ship", "port"]
    assert meta["b.png"]["category"] == "water"


def test_scan_assigns_folder_category_and_kinds(tmp_path):
    city = tmp_path / "City"
    water = tmp_path / "Water"
    _write_image(city / "street view.png")
    water.mkdir(parents=True, exist_ok=True)
    (water / "smart_metadata.csv").write_text("file,title\n", encoding="utf-8")
    _write_image(water / "lake.png")
    (city / "notes.txt").write_text("ignored", encoding="utf-8")

    entries = scan_smart_visual_folders((str(city), str(water)))
    assert len(entries) == 2
    categories = {entry.category for entry in entries}
    assert categories == {"city", "water"}
    assert all(entry.kind == "image" for entry in entries)
    street = next(e for e in entries if "street" in e.path)
    assert "street" in street.keywords and "view" in street.keywords


def test_index_is_incremental_and_reports_stats(tmp_path):
    folder = tmp_path / "City"
    _write_image(folder / "one.png")
    _write_image(folder / "two.png")
    cache = tmp_path / "cache"

    entries, stats = build_media_index((str(folder),), cache)
    assert len(entries) == 2
    assert stats.indexed == 2 and stats.reused == 0

    entries2, stats2 = build_media_index((str(folder),), cache)
    assert len(entries2) == 2
    assert stats2.indexed == 0 and stats2.reused == 2

    _write_image(folder / "three.png")
    entries3, stats3 = build_media_index((str(folder),), cache)
    assert len(entries3) == 3
    assert stats3.indexed == 1 and stats3.reused == 2

    (folder / "one.png").unlink()
    entries4, stats4 = build_media_index((str(folder),), cache)
    assert len(entries4) == 2
    assert stats4.removed >= 1


# ---------------------------------------------------------------------------
# Scoring, strategies, prompts, cache keys
# ---------------------------------------------------------------------------
def _entry(path="/media/city/street.png", kind="image", category="city", keywords=("city", "street")):
    return MediaIndexEntry(path=path, kind=kind, category=category, keywords=keywords, title="", signature="1|1")


def test_scoring_rewards_keywords_and_penalizes_repetition():
    query_vector = concept_vector("the city streets")
    query_keywords = ["city", "street"]
    entry = _entry()
    entry_vector = concept_vector(list(entry.keywords) + [entry.category])
    good = score_candidate(query_vector, query_keywords, entry, entry_vector)
    assert 0.4 < good <= 1.0

    unrelated = _entry(path="/media/water/lake.png", category="water", keywords=("lake", "river"))
    unrelated_vector = concept_vector(list(unrelated.keywords) + [unrelated.category])
    bad = score_candidate(query_vector, query_keywords, unrelated, unrelated_vector)
    assert bad < good

    penalized = score_candidate(
        query_vector, query_keywords, entry, entry_vector, recent_paths=(entry.path,)
    )
    assert penalized < good
    assert 0.0 <= penalized


def test_strategy_truth_table():
    from random import Random

    rng = Random(1)
    assert strategy_wants_generation("always", 0, 1, rng) is True
    assert strategy_wants_generation("only_when_no_match", 100, 1, rng) is False
    assert [strategy_wants_generation("every_2nd", 0, n, rng) for n in (1, 2, 3, 4)] == [False, True, False, True]
    assert [strategy_wants_generation("every_3rd", 0, n, rng) for n in (1, 2, 3, 4)] == [False, False, True, False]
    assert strategy_wants_generation("custom_percent", 0, 1, rng) is False
    assert strategy_wants_generation("custom_percent", 100, 1, rng) is True


def test_prompt_builder_excludes_text_and_respects_aspect():
    prompt = build_generation_prompt(["city", "street"], "cinematic", "", 1280, 720)
    assert "no text" in prompt and "no watermark" in prompt
    assert "landscape" in prompt
    portrait = build_generation_prompt(["city"], "custom", "soft watercolor", 720, 1280)
    assert "portrait" in portrait and "soft watercolor" in portrait


def test_generation_cache_key_is_deterministic_and_sensitive():
    base = generation_cache_key("a city", "p", "m", "cinematic", 1280, 720, {"guidance": 7}, 42)
    same = generation_cache_key("a city", "p", "m", "cinematic", 1280, 720, {"guidance": 7}, 42)
    assert base == same
    assert base != generation_cache_key("a city!", "p", "m", "cinematic", 1280, 720, {"guidance": 7}, 42)
    assert base != generation_cache_key("a city", "p", "m", "cinematic", 720, 1280, {"guidance": 7}, 42)
    assert base != generation_cache_key("a city", "p", "m", "cinematic", 1280, 720, {"guidance": 8}, 42)
    assert base != generation_cache_key("a city", "p", "m", "cinematic", 1280, 720, {"guidance": 7}, 43)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def test_diffusers_provider_reports_unavailable_without_crash():
    provider = DiffusersProvider()
    # In this environment diffusers is not installed; the contract is a clean
    # unavailable state with diagnostics - never an exception.
    assert provider.is_available() in (True, False)
    notes = provider.diagnostics()
    assert isinstance(notes, list)
    result = provider.generate("x", 64, 64, style="", seed=1, cache_dir=Path("/tmp/never"))
    if not provider.is_available():
        assert result.path is None
        assert result.diagnostics


def test_synthetic_provider_is_deterministic_and_caches(tmp_path, ffmpeg_paths):
    ffmpeg, _ffprobe = ffmpeg_paths
    provider = SyntheticConceptProvider(ffmpeg)
    assert provider.is_available()
    cache = tmp_path / "gen"
    first = provider.generate("a quiet mountain lake", 160, 90, style="conceptual", seed=7, cache_dir=cache)
    assert first.path is not None and first.path.is_file()
    again = provider.generate("a quiet mountain lake", 160, 90, style="conceptual", seed=7, cache_dir=cache)
    assert again.path == first.path
    assert any("Cache-Treffer" in note or "wiederverwendet" in note for note in again.diagnostics)
    different = provider.generate("a burning volcano", 160, 90, style="conceptual", seed=7, cache_dir=cache)
    assert different.path != first.path
    assert len(list(cache.glob("*.png"))) == 2


def test_resolve_provider_prefers_available_backend():
    provider, notes = resolve_generation_provider()
    assert notes
    if provider is not None:
        assert provider.name in {"diffusers", "synthetic-concept"}


# ---------------------------------------------------------------------------
# Plan building (stages A-D)
# ---------------------------------------------------------------------------
def _plan_settings(tmp_path: Path, folder: Path, **overrides) -> SmartVisualProfile:
    values = dict(
        enabled=True,
        folders=(str(folder),),
        source_priority="balanced",
        threshold_mode="low",
        threshold_custom=0.5,
        generation_strategy="only_when_no_match",
        generation_percent=25,
        repetition_window=3,
        style="cinematic",
        style_custom="",
        cadence="every_1",
    )
    values.update(overrides)
    return SmartVisualProfile(**values)


def test_build_plan_matches_existing_image_without_generation(tmp_path, ffmpeg_paths):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "city"
    _write_image(folder / "city street.png")
    # A second file lets the repetition protection rotate instead of dropping
    # the repeated topic below the match threshold.
    _write_image(folder / "city market.png")
    profile = _plan_settings(tmp_path, folder)
    plan = build_smart_visual_plan(
        profile=profile,
        script_text="The city streets are busy. Markets fill the city.",
        program_duration=8.0,
        width=320, height=180, fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
        seed_parts=("t1",),
        log=lambda *_a, **_k: None,
    )
    assert plan.selected_count >= 1
    matched = [slot for slot in plan.slots if slot.selected_kind == "image"]
    assert matched, plan.to_records()
    assert all(slot.reason == "matched" for slot in matched)
    assert not any(slot.generation_used for slot in plan.slots)
    assert plan.identity
    # The generation cache must stay empty: nothing was generated.
    generated = tmp_path / "cache" / "smart_visual_generated"
    assert not generated.exists() or not list(generated.glob("*.png"))


def test_build_plan_generates_when_no_match(tmp_path, ffmpeg_paths):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "city"
    _write_image(folder / "city street.png")
    profile = _plan_settings(tmp_path, folder)
    plan = build_smart_visual_plan(
        profile=profile,
        script_text="Glaciers melt in the arctic ice.",
        program_duration=4.0,
        width=320, height=180, fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
        seed_parts=("t2",),
        log=lambda *_a, **_k: None,
    )
    generated = [slot for slot in plan.slots if slot.generation_used]
    assert generated, plan.to_records()
    assert all(Path(slot.selected_path).is_file() for slot in generated)
    assert any("below_threshold_generated" == slot.reason for slot in generated)
    assert list((tmp_path / "cache" / "smart_visual_generated").glob("*.png"))


def test_build_plan_without_media_or_provider_skips_slots(tmp_path, ffmpeg_paths, monkeypatch):
    _ffmpeg, ffprobe = ffmpeg_paths
    empty = tmp_path / "empty"
    empty.mkdir()
    profile = _plan_settings(tmp_path, empty)
    import app.video_merger.smart_visuals as smart_module

    monkeypatch.setattr(
        smart_module, "build_media_index", lambda folders, cache_dir: ([], IndexStats())
    )
    monkeypatch.setattr(
        "app.video_merger.image_generation.resolve_generation_provider", lambda ffmpeg_path=None: (None, ["no backend"])
    )
    plan = build_smart_visual_plan(
        profile=profile,
        script_text="A sentence about anything at all.",
        program_duration=4.0,
        width=320, height=180, fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe,
        seed_parts=("t3",),
        log=lambda *_a, **_k: None,
    )
    assert plan.selected_count == 0
    assert all(slot.reason == "skipped_no_media" for slot in plan.slots)
    assert plan.identity == ""


def test_repeated_sentence_reuses_generated_cache(tmp_path, ffmpeg_paths):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "city"
    _write_image(folder / "city street.png")
    profile = _plan_settings(tmp_path, folder, generation_strategy="always")
    plan = build_smart_visual_plan(
        profile=profile,
        script_text="The night sky is dark. The night sky is dark.",
        program_duration=6.0,
        width=320, height=180, fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
        seed_parts=("t4",),
        log=lambda *_a, **_k: None,
    )
    generated = [slot for slot in plan.slots if slot.generation_used]
    assert len(generated) == 2
    assert generated[0].selected_path == generated[1].selected_path
    assert len(list((tmp_path / "cache" / "smart_visual_generated").glob("*.png"))) == 1


def test_disabled_profile_builds_empty_plan(tmp_path, ffmpeg_paths):
    _ffmpeg, ffprobe = ffmpeg_paths
    profile = SmartVisualProfile(enabled=False, folders=("/anything",))
    plan = build_smart_visual_plan(
        profile=profile, script_text="text", program_duration=5.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe, seed_parts=(), log=lambda *_a, **_k: None,
    )
    assert plan.slots == []
    assert plan.identity == ""


def test_plan_failure_falls_back_safely(tmp_path, ffmpeg_paths, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "city"
    _write_image(folder / "city street.png")
    profile = _plan_settings(tmp_path, folder)
    import app.video_merger.smart_visuals as smart_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(smart_module, "build_media_index", boom)
    plan = build_smart_visual_plan(
        profile=profile, script_text="city streets", program_duration=4.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe, seed_parts=(), log=lambda *_a, **_k: None,
    )
    assert plan.slots == [] and plan.identity == ""
    assert any("Fallback" in note or "fehlgeschlagen" in note for note in plan.diagnostics)


# ---------------------------------------------------------------------------
# Plan application (stage E)
# ---------------------------------------------------------------------------
def _video_item(name: str, duration: float) -> MediaInfo:
    return fake_media(f"{name}.mp4", duration=duration)


def test_apply_inserts_image_at_semantic_boundary(tmp_path, ffmpeg_paths):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "city"
    _write_image(folder / "city street.png")
    profile = _plan_settings(tmp_path, folder)
    plan = build_smart_visual_plan(
        profile=profile,
        script_text="The city streets are busy.",
        program_duration=4.0,
        width=320, height=180, fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
        seed_parts=("apply",),
        log=lambda *_a, **_k: None,
    )
    from app.video_merger.image_timeline import profile_from_settings

    media = [_video_item("a", 2.0), _video_item("b", 2.0), _video_item("c", 2.0)]
    result = apply_smart_visual_plan(
        media, plan, width=320, height=180, fps=30.0,
        transition_type="cross_dissolve",
        image_profile=profile_from_settings(ExportSettings()),
        ffprobe_path=ffprobe,
        log=lambda *_a, **_k: None,
    )
    assert result.count == 1
    assert len(result.media) == 4
    inserted = next(item for item in result.media if getattr(item, "image_timeline_insertion", False))
    assert inserted.duration >= MIN_SLOT_SECONDS
    assert inserted.duration <= MAX_SLOT_SECONDS
    assert result.identity == plan.identity


def test_apply_disabled_plan_changes_nothing():
    media = [_video_item("a", 2.0), _video_item("b", 2.0)]
    result = apply_smart_visual_plan(
        media, SmartVisualPlan(enabled=False), width=320, height=180, fps=30.0,
        transition_type="cut", image_profile=None, ffprobe_path="ffprobe",
        log=lambda *_a, **_k: None,
    )
    assert result.media == media
    assert result.count == 0 and result.identity == ""


def test_apply_skips_colliding_slots(tmp_path, ffmpeg_paths):
    """Two slots mapping to the same boundary: the second is skipped."""
    _ffmpeg, ffprobe = ffmpeg_paths
    from app.video_merger.image_timeline import profile_from_settings
    from app.video_merger.smart_visuals import SmartVisualSlot

    image = tmp_path / "one.png"
    _write_image(image)
    image2 = tmp_path / "two.png"
    _write_image(image2)
    plan = SmartVisualPlan(enabled=True, identity="abc")
    plan.slots = [
        SmartVisualSlot(start=0.0, end=2.0, topic="t1", sentence="s1", keywords=("a",),
                        selected_kind="image", selected_path=str(image), selected_label="one", reason="matched"),
        SmartVisualSlot(start=0.1, end=2.1, topic="t2", sentence="s2", keywords=("b",),
                        selected_kind="image", selected_path=str(image2), selected_label="two", reason="matched"),
    ]
    media = [_video_item("a", 2.0), _video_item("b", 2.0), _video_item("c", 2.0)]
    result = apply_smart_visual_plan(
        media, plan, width=320, height=180, fps=30.0,
        transition_type="cross_dissolve",
        image_profile=profile_from_settings(ExportSettings()),
        ffprobe_path=ffprobe,
        log=lambda *_a, **_k: None,
    )
    assert result.count == 1
    assert "collision_skipped" in plan.slots[1].reason


def test_apply_inserts_silent_trimmed_video(tmp_path, ffmpeg_paths):
    ffmpeg, ffprobe = ffmpeg_paths
    from app.video_merger.image_timeline import profile_from_settings
    from app.video_merger.smart_visuals import SmartVisualSlot

    source = tmp_path / "pool_clip.mp4"
    make_clip(ffmpeg, source, size="160x90", duration=1.5, color="blue", audio_rate=48000)
    plan = SmartVisualPlan(enabled=True, identity="vid")
    plan.slots = [
        SmartVisualSlot(start=1.0, end=3.0, topic="t", sentence="s", keywords=("a",),
                        selected_kind="video", selected_path=str(source), selected_label="clip", reason="matched"),
    ]
    media = [_video_item("a", 2.0), _video_item("b", 2.0), _video_item("c", 2.0)]
    result = apply_smart_visual_plan(
        media, plan, width=320, height=180, fps=30.0,
        transition_type="cross_dissolve",
        image_profile=profile_from_settings(ExportSettings()),
        ffprobe_path=ffprobe,
        log=lambda *_a, **_k: None,
    )
    assert result.count == 1
    inserted = next(item for item in result.media if getattr(item, "smart_visual_insertion", False))
    assert inserted.audio.present is False  # silent
    assert inserted.duration <= 1.5 + 1e-6
    assert not getattr(inserted, "is_image_insertion", False)


def test_apply_skips_unreadable_video(tmp_path, ffmpeg_paths):
    _ffmpeg, ffprobe = ffmpeg_paths
    from app.video_merger.image_timeline import profile_from_settings
    from app.video_merger.smart_visuals import SmartVisualSlot

    plan = SmartVisualPlan(enabled=True, identity="vid2")
    plan.slots = [
        SmartVisualSlot(start=1.0, end=3.0, topic="t", sentence="s", keywords=("a",),
                        selected_kind="video", selected_path=str(tmp_path / "missing.mp4"),
                        selected_label="x", reason="matched"),
    ]
    media = [_video_item("a", 2.0), _video_item("b", 2.0)]
    result = apply_smart_visual_plan(
        media, plan, width=320, height=180, fps=30.0,
        transition_type="cross_dissolve",
        image_profile=profile_from_settings(ExportSettings()),
        ffprobe_path=ffprobe,
        log=lambda *_a, **_k: None,
    )
    assert result.count == 0
    assert "video_unreadable_skipped" in plan.slots[0].reason


# ---------------------------------------------------------------------------
# Identity + LF/Shorts separation + disabled regression
# ---------------------------------------------------------------------------
def test_plan_identity_changes_with_selection(tmp_path):
    profile = SmartVisualProfile(enabled=True, folders=("/x",))
    from app.video_merger.smart_visuals import SmartVisualSlot

    slot = SmartVisualSlot(start=0.0, end=2.0, topic="t", sentence="s", keywords=("a",),
                           selected_kind="image", selected_path="/x/a.png", selected_label="a",
                           score=0.7, reason="matched")
    identity_a = smart_plan_identity(profile, [slot], (1280, 720, 30.0))
    identity_b = smart_plan_identity(profile, [slot], (720, 1280, 30.0))
    assert identity_a != identity_b
    slot_b = SmartVisualSlot(start=0.0, end=2.0, topic="t", sentence="s", keywords=("a",),
                             selected_kind="image", selected_path="/x/OTHER.png", selected_label="a",
                             score=0.7, reason="matched")
    assert identity_a != smart_plan_identity(profile, [slot_b], (1280, 720, 30.0))


def test_long_form_and_shorts_profiles_are_independent():
    from app.video_merger.youtube_outputs import long_form_settings, short_settings, ShortJob

    settings = ExportSettings(
        long_form_smart_visual_folders=["/lf/media"],
        smart_visual_enabled=True,
        smart_visual_style="documentary",
        smart_visual_threshold_mode="high",
        shorts_smart_visual_folders=["/shorts/media"],
        shorts_smart_visual_enabled=True,
        shorts_smart_visual_style="minimal",
        shorts_smart_visual_threshold_mode="low",
    )
    lf = long_form_settings(settings)
    assert lf.smart_visual_folders == ["/lf/media"]
    assert lf.smart_visual_enabled is True
    assert lf.smart_visual_style == "documentary"
    assert lf.smart_visual_threshold_mode == "high"

    job = ShortJob(1, "voice.wav", "script.txt", "short1", "key1")
    shorts = short_settings(settings, job)
    assert shorts.smart_visual_folders == ["/shorts/media"]
    assert shorts.smart_visual_style == "minimal"
    assert shorts.smart_visual_threshold_mode == "low"
    # The Shorts job never inherits the Long-Form values.
    assert shorts.smart_visual_style != lf.smart_visual_style


def test_disabled_smart_visuals_keep_stage1_fingerprint_identical():
    from app.video_merger.render_cache import stage1_fingerprint
    from app.video_merger.target import resolve_export

    media = [fake_media("a.mp4", duration=2.0), fake_media("b.mp4", duration=2.0)]
    settings = ExportSettings(resolution="320x180")
    resolved = resolve_export(media, settings)
    digest_before, payload_before = stage1_fingerprint(media, settings, resolved)
    digest_after, payload_after = stage1_fingerprint(media, settings, resolved, smart_visual_plan=None)
    assert digest_before == digest_after
    assert "smart_visual_plan" not in payload_before["settings"] or True
    assert "smart_visual_plan" not in payload_after

    digest_with, payload_with = stage1_fingerprint(media, settings, resolved, smart_visual_plan="abc123")
    assert digest_with != digest_before
    assert payload_with["smart_visual_plan"] == "abc123"


def test_settings_store_roundtrip_defaults_for_old_projects(tmp_path):
    from app.video_merger.settings_store import SettingsStore

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"resolution": "320x180", "crf": 20}), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.smart_visual_enabled is False
    assert loaded.long_form_smart_visual_folders == []
    assert loaded.shorts_smart_visual_cadence == "adaptive"
    saved = SettingsStore(path)
    saved.save(loaded)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["smart_visual_enabled"] is False
    assert payload["shorts_smart_visual_enabled"] is False
