"""Phase 34 unit tests: metadata-driven matching + sentence-anchored timeline.

Smart Visuals consumes the PRE-GENERATED analyzer JSON of every asset as the
authoritative visual knowledge base (spec section 2): the per-keyword
``relevance_score`` values are first-class ranking signals, evidence follows
the DIRECT > STRONG ASSOCIATION > CONTEXTUAL > METAPHORICAL hierarchy,
negative matches suppress false positives, and visual quality only breaks
near-ties. Matching is sentence-anchored: the current sentence is the query
(strongest weight), the previous topic joins only as a secondary component,
visuals begin at sentence boundaries and the 5.0 s image duration is a
TARGET that safely shortens at topic-changing boundaries. No generation, no
vision calls, no pixel analysis - metadata only.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from random import Random

import pytest

import app.video_merger.image_generation as image_generation
from app.video_merger.smart_metadata import (
    AssetMetadata,
    CONTEXT_INHERIT_WEIGHT,
    MATCH_EXACT,
    MATCH_MORPHOLOGICAL,
    MATCH_SYNONYM,
    NEGATIVE_CONFLICT_CAP,
    SCORE_SCALE,
    SentenceQuery,
    asset_metadata_from_dict,
    build_sentence_queries,
    find_analysis_sidecar,
    prepare_asset,
    score_asset,
)
from app.video_merger.smart_visuals import (
    DEFAULT_SMART_IMAGE_DURATION,
    SMART_INSERT_STRONG_THRESHOLD,
    SMART_MATCH_THRESHOLD,
    SMART_MODE_RANDOM_ONLY,
    SMART_MODE_SMART_INSERTS,
    SMART_MODE_SMART_MATCH,
    SLOT_MODE_MATCH,
    SLOT_SOURCE_RANDOM,
    SLOT_SOURCE_SMART,
    SmartVisualProfile,
    build_media_index,
    build_smart_visual_plan,
    split_script_sentences,
)

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG)


def _write_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-video-bytes")


def _analysis_next_to(folder: Path, asset_name: str, payload: dict) -> Path:
    """Adjacent sidecar layout: image.jpg + image.analysis.json."""
    stem = Path(asset_name).with_suffix("").name
    sidecar = folder / f"{stem}.analysis.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    return sidecar


def _analysis_deep(folder: Path, asset_name: str, payload: dict, nested: str = "") -> Path:
    """Analyzer layout: <root>/_DeepImageAnalysis/json/<name>.analysis.json."""
    deep = folder / "_DeepImageAnalysis" / "json"
    deep.mkdir(parents=True, exist_ok=True)
    stem = Path(asset_name).with_suffix("").stem + Path(asset_name).suffix
    name = Path(asset_name).name
    target = deep / (f"{nested}{name}.analysis.json" if nested else f"{Path(asset_name).stem}.analysis.json")
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


MEDITATION_ASSET = {
    "identity": "meditation_person_04",
    "detailed_description": "A person meditating quietly in a bright room.",
    "keywords": [
        {"keyword": "meditation", "relevance_score": 100, "category": "concept",
         "literal_or_semantic": "literal", "focus_level": "primary"},
        {"keyword": "meditating", "relevance_score": 97, "category": "action",
         "literal_or_semantic": "literal", "focus_level": "primary"},
        {"keyword": "mindfulness", "relevance_score": 92, "category": "concept",
         "literal_or_semantic": "semantic", "focus_level": "primary"},
        {"keyword": "mental clarity", "relevance_score": 82, "category": "concept",
         "literal_or_semantic": "semantic", "focus_level": "supporting"},
        {"keyword": "relaxation", "relevance_score": 61, "category": "concept",
         "literal_or_semantic": "semantic", "focus_level": "supporting"},
        {"keyword": "person", "relevance_score": 50, "category": "subject",
         "literal_or_semantic": "literal", "focus_level": "supporting"},
        {"keyword": "interior", "relevance_score": 14, "category": "environment",
         "literal_or_semantic": "literal", "focus_level": "secondary"},
    ],
    "visual_concepts": [{"concept": "inner peace", "relevance_score": 84}],
    "direct_uses": ["meditation guide illustration"],
    "strong_associations": ["wellness article header"],
    "contextual_uses": ["stress reduction blog"],
    "metaphorical_uses": ["journey to inner calm"],
    "search_phrases": ["person meditating at home"],
    "negative_matches": ["medical treatment"],
    "visual_quality": "excellent",
    "source_file": "meditation_person_04.jpg",
    "analyzer_version": "2.1",
    "vision_model": "test-analyzer",
}

SPA_ASSET = {
    "identity": "spa_relaxation_01",
    "detailed_description": "A calm spa room with towels and candles.",
    "keywords": [
        {"keyword": "relaxation", "relevance_score": 80, "category": "concept",
         "literal_or_semantic": "semantic", "focus_level": "primary"},
        {"keyword": "spa", "relevance_score": 70, "category": "place",
         "literal_or_semantic": "literal", "focus_level": "primary"},
        {"keyword": "person", "relevance_score": 55, "category": "subject",
         "literal_or_semantic": "literal", "focus_level": "supporting"},
    ],
    "metaphorical_uses": ["meditation mood plate"],
    "negative_matches": [],
    "visual_quality": "good",
}

CAR_ASSET = {
    "identity": "oldtimer_09",
    "detailed_description": "A vintage automobile parked on a street.",
    "keywords": [
        {"keyword": "automobile", "relevance_score": 100, "category": "object",
         "literal_or_semantic": "literal", "focus_level": "primary"},
        {"keyword": "vintage", "relevance_score": 75, "category": "style",
         "literal_or_semantic": "literal", "focus_level": "supporting"},
    ],
    "negative_matches": [],
    "visual_quality": "excellent",
}


def _meditation_query(text: str = "Meditating regularly can improve mental clarity and reduce stress.") -> SentenceQuery:
    return build_sentence_queries([text])[0]


# ---------------------------------------------------------------------------
# Sidecar discovery (spec section 3) - AG / AF / AH
# ---------------------------------------------------------------------------
def test_adjacent_sidecar_is_discovered(tmp_path):
    folder = tmp_path / "pool"
    _write_image(folder / "meditation_person_04.jpg")
    sidecar = _analysis_next_to(folder, "meditation_person_04.jpg", MEDITATION_ASSET)
    found = find_analysis_sidecar(folder / "meditation_person_04.jpg", folder)
    assert found == sidecar


def test_nested_deep_analysis_layout_is_discovered(tmp_path):
    folder = tmp_path / "pool"
    _write_image(folder / "meditation_person_04.jpg")
    _analysis_deep(folder, "meditation_person_04.jpg", MEDITATION_ASSET)
    found = find_analysis_sidecar(folder / "meditation_person_04.jpg", folder)
    assert found is not None and found.parent.name == "json"
    assert "_DeepImageAnalysis" in str(found)


def test_same_filenames_in_different_folders_remain_distinct(tmp_path):
    folder_a = tmp_path / "pool_a"
    folder_b = tmp_path / "pool_b"
    _write_image(folder_a / "photo.jpg")
    _write_image(folder_b / "photo.jpg")
    _analysis_next_to(folder_a, "photo.jpg", {"keywords": [
        {"keyword": "meditation", "relevance_score": 100}]})
    _analysis_next_to(folder_b, "photo.jpg", {"keywords": [
        {"keyword": "automobile", "relevance_score": 100}]})
    entries, _ = build_media_index((str(folder_a), str(folder_b)), tmp_path / "cache")
    assert len(entries) == 2, "identity is the real file path, not the filename"
    by_folder = {Path(entry.path).parent.name: entry for entry in entries}
    assert "meditation" in str(by_folder["pool_a"].analysis)
    assert "automobile" in str(by_folder["pool_b"].analysis)


# ---------------------------------------------------------------------------
# Weighted matching (spec sections 10-15) - A..H
# ---------------------------------------------------------------------------
def test_A_exact_high_relevance_beats_low_generic_keyword():
    asset = asset_metadata_from_dict("a.jpg", "image", MEDITATION_ASSET)
    query = _meditation_query()
    result = score_asset(query, prepare_asset(asset))
    # meditation(100) dominates: the asset scores very high...
    assert result.score >= 80
    # ...and a generic-weak keyword alone can never reach that.
    weak = asset_metadata_from_dict("b.jpg", "image", {
        "keywords": [{"keyword": "interior", "relevance_score": 12,
                      "focus_level": "secondary"}],
    })
    weak_result = score_asset(query, prepare_asset(weak))
    assert result.score > weak_result.score * 3


def test_B_morphological_variants_match():
    queries = build_sentence_queries([
        "She likes to meditate every morning.",
        "The philosopher wrote about philosophy.",
    ])
    asset = asset_metadata_from_dict("a.jpg", "image", {
        "keywords": [
            {"keyword": "meditation", "relevance_score": 95},
            {"keyword": "philosophy", "relevance_score": 90},
        ],
    })
    prepared = prepare_asset(asset)
    for query in queries:
        result = score_asset(query, prepared)
        assert result.score >= 60, f"word-form family must match: {query.text}"
        # The match must NOT require the exact surface form.
        assert result.evidence[0].match in ("morphological", "exact", "synonym")


def test_C_synonym_variants_match_where_supported():
    asset = asset_metadata_from_dict("a.jpg", "image", {
        "keywords": [{"keyword": "mountains", "relevance_score": 90}],
    })
    # "Alps" resolves to the same concept family as "mountain(s)".
    query = build_sentence_queries(["We crossed the Alps at dawn."])[0]
    result = score_asset(query, prepare_asset(asset))
    assert result.score > 0
    assert result.evidence[0].match == "synonym"


def test_D_phrase_match_works():
    asset = asset_metadata_from_dict("a.jpg", "image", {
        "search_phrases": ["person meditating at home"],
    })
    query = build_sentence_queries(["A person meditating at home changes habits."])[0]
    result = score_asset(query, prepare_asset(asset))
    phrase_hits = [e for e in result.evidence if e.match == "phrase"]
    assert phrase_hits, "multi-word phrases must match as phrases"
    assert result.score > 0


def test_E_direct_use_beats_metaphorical_use():
    direct = asset_metadata_from_dict("a.jpg", "image", {
        "direct_uses": ["meditation session recording"],
    })
    metaphorical = asset_metadata_from_dict("b.jpg", "image", {
        "metaphorical_uses": ["meditation inner peace journey"],
    })
    query = _meditation_query("She is meditating.")
    direct_score = score_asset(query, prepare_asset(direct)).score
    metaphorical_score = score_asset(query, prepare_asset(metaphorical)).score
    assert direct_score > metaphorical_score > 0


def test_F_strong_association_can_beat_weak_direct_generic_match():
    strong_assoc = asset_metadata_from_dict("a.jpg", "image", {
        "strong_associations": ["mental clarity and mindfulness retreat"],
    })
    weak_direct = asset_metadata_from_dict("b.jpg", "image", {
        "direct_uses": ["generic clarity"],  # only one generic token overlaps
    })
    query = _meditation_query("Mental clarity improves with mindfulness.")
    strong_score = score_asset(query, prepare_asset(strong_assoc)).score
    weak_score = score_asset(query, prepare_asset(weak_direct)).score
    assert strong_score > weak_score


def test_G_negative_match_reduces_score():
    with_negative = asset_metadata_from_dict("a.jpg", "image", {
        "keywords": [
            {"keyword": "health", "relevance_score": 90},
            {"keyword": "wellness", "relevance_score": 85},
            {"keyword": "body", "relevance_score": 80},
        ],
        "negative_matches": ["medical treatment"],
    })
    without_negative = asset_metadata_from_dict("b.jpg", "image", {
        "keywords": [
            {"keyword": "health", "relevance_score": 90},
            {"keyword": "wellness", "relevance_score": 85},
            {"keyword": "body", "relevance_score": 80},
        ],
    })
    query = build_sentence_queries(["Health, body and wellness matter."])[0]
    score_neg = score_asset(query, prepare_asset(with_negative))
    score_pos = score_asset(query, prepare_asset(without_negative))
    assert score_neg.negative_conflicts, "the conflict must be detected"
    assert score_neg.score < score_pos.score, "negative evidence reduces the score"
    # A strong explicit conflict caps the score hard (section 15).
    assert score_neg.score <= score_pos.score * NEGATIVE_CONFLICT_CAP + 1.0


def test_H_high_focus_keywords_receive_appropriate_weight():
    primary = asset_metadata_from_dict("a.jpg", "image", {
        "keywords": [{"keyword": "meditation", "relevance_score": 90,
                      "focus_level": "primary"}],
    })
    secondary = asset_metadata_from_dict("b.jpg", "image", {
        "keywords": [{"keyword": "meditation", "relevance_score": 90,
                      "focus_level": "secondary"}],
    })
    query = _meditation_query()
    primary_score = score_asset(query, prepare_asset(primary)).score
    secondary_score = score_asset(query, prepare_asset(secondary)).score
    assert primary_score > secondary_score


def test_I_sentence_context_is_stronger_than_inherited_context():
    # Sentence 2's own token match must outweigh an inherited-only match.
    queries = build_sentence_queries([
        "Meditation has many benefits.",
        "Breathing exercises help the body relax.",
    ])
    asset = asset_metadata_from_dict("a.jpg", "image", {
        "keywords": [
            {"keyword": "meditation", "relevance_score": 95},
            {"keyword": "breathing", "relevance_score": 95},
        ],
    })
    prepared = prepare_asset(asset)
    second = score_asset(queries[1], prepared)
    breathing = next(e for e in second.evidence if "breath" in e.term)
    # The sentence's own keyword carries the full weight; the inherited
    # "meditation" context is discounted (never lost entirely - section 42).
    assert breathing.value >= CONTEXT_INHERIT_WEIGHT * 100


def test_pronoun_sentence_inherits_topic_context():
    queries = build_sentence_queries([
        "Meditation has many benefits.",
        "It can reduce stress and improve focus.",
    ])
    asset = asset_metadata_from_dict("a.jpg", "image", {
        "keywords": [{"keyword": "meditation", "relevance_score": 100}],
    })
    result = score_asset(queries[1], prepare_asset(asset))
    assert result.score > 0, "continuation sentences keep their topic"


# ---------------------------------------------------------------------------
# Sentence segmentation + anchoring (spec sections 6-9) - J..N
# ---------------------------------------------------------------------------
def test_J_sentence_boundaries_are_detected():
    sentences = split_script_sentences(
        "Meditation is very important. It has many benefits."
    )
    assert len(sentences) == 2
    assert sentences[0].startswith("Meditation")
    assert sentences[1].startswith("It")


def _analysis_pool(tmp_path: Path, script_sentences: int = 3) -> Path:
    """Pool with one strong, distinct asset per meditation/concentration/
    exercise topic plus an unrelated asset - all with analyzer sidecars."""
    folder = tmp_path / "pool"
    payloads = {
        "meditation_person.jpg": {
            "keywords": [{"keyword": "meditation", "relevance_score": 100},
                         {"keyword": "mindfulness", "relevance_score": 90}],
            "direct_uses": ["meditation guide"],
        },
        "concentration_desk.jpg": {
            "keywords": [{"keyword": "concentration", "relevance_score": 100},
                         {"keyword": "focus", "relevance_score": 88}],
            "direct_uses": ["concentration training"],
        },
        "exercise_running.jpg": {
            "keywords": [{"keyword": "exercise", "relevance_score": 100},
                         {"keyword": "running", "relevance_score": 90}],
            "direct_uses": ["exercise routine"],
        },
        "oldtimer_car.jpg": CAR_ASSET,
    }
    for name, payload in payloads.items():
        _write_image(folder / name)
        _analysis_next_to(folder, name, payload)
    return folder


HABITS_SCRIPT = (
    "Meditation is important and has many benefits. "
    "Regular practice can improve concentration. "
    "Physical exercise is another useful habit. "
)


def _plan34(tmp_path: Path, folder: Path, script: str, *, mode: str = SMART_MODE_SMART_MATCH,
            nonce: int = 0, image_duration: float = DEFAULT_SMART_IMAGE_DURATION,
            program_duration: float = 24.0, insert_percent: int = 25):
    profile = SmartVisualProfile(
        enabled=True, folders=(str(folder),), mode=mode,
        image_duration=image_duration, insert_percent=insert_percent,
        randomize_nonce=nonce, cadence="every_1",
    )
    return build_smart_visual_plan(
        profile=profile, script_text=script, program_duration=program_duration,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path="ffprobe-not-used", seed_parts=("p34", f"nonce={nonce}"),
        log=lambda *_a, **_k: None,
    )


def test_K_relevant_visual_starts_at_the_sentence_boundary(tmp_path):
    plan = _plan34(tmp_path, _analysis_pool(tmp_path), HABITS_SCRIPT)
    slots = [slot for slot in plan.slots if slot.selected_kind]
    assert len(slots) == 3
    # Every slot is anchored at its own sentence (sentence index + start
    # follow the sentence order - the visual begins WITH the sentence).
    assert [slot.sentence_index for slot in slots] == [0, 1, 2]
    starts = [slot.start for slot in slots]
    assert starts == sorted(starts)
    assert slots[0].start == pytest.approx(0.0, abs=1e-6), \
        "the first visual begins at the first sentence boundary"
    # The first sentence's meditation asset wins over the unrelated car.
    assert "meditation" in Path(slots[0].selected_path).name


def test_L_new_topic_starts_a_new_visual_at_the_boundary(tmp_path):
    plan = _plan34(tmp_path, _analysis_pool(tmp_path), HABITS_SCRIPT)
    slots = [slot for slot in plan.slots if slot.selected_kind]
    assert "concentration" in Path(slots[1].selected_path).name
    assert "exercise" in Path(slots[2].selected_path).name
    assert len({slot.selected_path for slot in slots}) == 3


def test_M_five_second_target_is_preserved_when_safe(tmp_path):
    # Sentences far enough apart: the full 5.0 s target stays untouched.
    plan = _plan34(tmp_path, _analysis_pool(tmp_path), HABITS_SCRIPT,
                   program_duration=30.0)
    filled = [slot for slot in plan.slots if slot.selected_kind]
    assert filled
    assert all(slot.insert_duration == pytest.approx(5.0) for slot in filled)


def test_N_duration_shortens_safely_at_topic_boundaries(tmp_path):
    # Three different topics within 24 s => sentences ~8 s apart: the target
    # survives. Pack the same script into 9 s => ~3 s apart: each visual
    # shortens to the next sentence boundary instead of running 5 s over it.
    long = _plan34(tmp_path, _analysis_pool(tmp_path), HABITS_SCRIPT,
                   program_duration=24.0)
    short = _plan34(tmp_path, _analysis_pool(tmp_path), HABITS_SCRIPT,
                    program_duration=9.0)
    long_slots = [s for s in long.slots if s.selected_kind]
    short_slots = [s for s in short.slots if s.selected_kind]
    assert all(s.insert_duration == pytest.approx(5.0) for s in long_slots)
    for slot in short_slots[:-1]:
        assert slot.insert_duration < 5.0, \
            "the target must yield to the next topic boundary"
        assert slot.insert_duration >= 0.5
    # The voiceover clock is untouched: slot starts still span the program.
    assert short_slots[-1].end <= 9.0 + 1e-6


def test_O_compatible_context_does_not_churn_visuals(tmp_path):
    # Two sentences about the SAME topic: no reason to shorten early.
    folder = tmp_path / "pool"
    payload = {"keywords": [{"keyword": "meditation", "relevance_score": 100}]}
    _write_image(folder / "meditation_a.jpg")
    _write_image(folder / "meditation_b.jpg")
    _analysis_next_to(folder, "meditation_a.jpg", payload)
    _analysis_next_to(folder, "meditation_b.jpg", payload)
    script = ("Meditation has many benefits. "
              "Meditation also improves focus and calm. ")
    plan = _plan34(tmp_path, folder, script, program_duration=8.0)
    filled = [s for s in plan.slots if s.selected_kind]
    assert len(filled) == 2
    # Both slots stay on topic and the first keeps its full target because
    # the next sentence still fits the same visual context.
    assert filled[0].insert_duration == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Modes (spec sections 16-18) - Q..T
# ---------------------------------------------------------------------------
def test_Q_best_match_mode_chooses_the_strongest_asset(tmp_path):
    folder = tmp_path / "pool"
    strong = {"keywords": [{"keyword": "meditation", "relevance_score": 100}]}
    weaker = {"keywords": [{"keyword": "relaxation", "relevance_score": 60}]}
    _write_image(folder / "strong.jpg")
    _write_image(folder / "weaker.jpg")
    _write_image(folder / "unrelated.jpg")
    _analysis_next_to(folder, "strong.jpg", strong)
    _analysis_next_to(folder, "weaker.jpg", weaker)
    _analysis_next_to(folder, "unrelated.jpg", CAR_ASSET)
    plan = _plan34(tmp_path, folder, "Meditation helps the mind. ",
                   program_duration=8.0)
    filled = [s for s in plan.slots if s.selected_kind]
    assert filled
    assert Path(filled[0].selected_path).name == "strong.jpg"
    assert filled[0].source_mode == SLOT_SOURCE_SMART
    assert filled[0].fallback_mode == SLOT_MODE_MATCH
    assert filled[0].score >= SMART_MATCH_THRESHOLD


def test_R_weak_match_falls_back_to_random_existing(tmp_path):
    folder = _analysis_pool(tmp_path)
    plan = _plan34(tmp_path, folder,
                   "Quantum satellites orbit distant galaxies. ",
                   program_duration=8.0)
    filled = [s for s in plan.slots if s.selected_kind]
    assert filled
    assert all(slot.source_mode == SLOT_SOURCE_RANDOM for slot in filled)
    assert all(not slot.generation_used for slot in filled)
    assert all(Path(slot.selected_path).exists() for slot in filled)


def test_S_mostly_random_retains_mostly_random_behavior(tmp_path):
    folder = _analysis_pool(tmp_path)
    script = (
        "Meditation is important and has many benefits. "
        "A sentence about kitchen and cooking. "
        "Regular practice can improve concentration. "
        "Another thought about stock markets. "
        "Physical exercise is another useful habit. "
        "Banana exports rise sharply this quarter. "
    )
    plan = _plan34(tmp_path, folder, script, mode=SMART_MODE_SMART_INSERTS,
                   insert_percent=50, program_duration=36.0)
    filled = [s for s in plan.slots if s.selected_kind]
    assert filled
    sources = {slot.source_mode for slot in filled}
    assert SLOT_SOURCE_RANDOM in sources, "Mostly Random keeps random draws"
    smart = [slot for slot in filled if slot.source_mode == SLOT_SOURCE_SMART]
    for slot in smart:
        assert slot.score >= SMART_INSERT_STRONG_THRESHOLD
        name = Path(slot.selected_path).name
        assert any(topic in name for topic in ("meditation", "concentration", "exercise")), \
            f"smart opportunities must pick strong metadata matches, got {name}"


def test_T_random_only_does_not_invoke_semantic_scoring(tmp_path, monkeypatch):
    import app.video_merger.smart_metadata as smart_metadata

    def _boom(*_args, **_kwargs):
        raise AssertionError("random_only must not score semantically")

    monkeypatch.setattr(smart_metadata, "score_asset", _boom)
    folder = _analysis_pool(tmp_path)
    plan = _plan34(tmp_path, folder, HABITS_SCRIPT, mode=SMART_MODE_RANDOM_ONLY)
    filled = [s for s in plan.slots if s.selected_kind]
    assert filled
    assert all(slot.source_mode == SLOT_SOURCE_RANDOM for slot in filled)
    assert all(slot.score == 0.0 for slot in filled)


# ---------------------------------------------------------------------------
# Global uniqueness (spec section 19) - U..W
# ---------------------------------------------------------------------------
def test_U_images_remain_unique_while_unused_images_exist(tmp_path):
    folder = _analysis_pool(tmp_path)
    plan = _plan34(tmp_path, folder, HABITS_SCRIPT)
    paths = [s.selected_path for s in plan.slots if s.selected_kind and s.selected_kind == "image"]
    assert len(paths) >= 3
    assert len(set(paths)) == len(paths)


def test_V_videos_remain_unique_while_unused_videos_exist(tmp_path):
    folder = tmp_path / "pool"
    payloads = {
        "meditation_clip.mp4": {"keywords": [{"keyword": "meditation", "relevance_score": 100}]},
        "focus_clip.mp4": {"keywords": [{"keyword": "concentration", "relevance_score": 100}]},
        "running_clip.mp4": {"keywords": [{"keyword": "exercise", "relevance_score": 100}]},
    }
    for name, payload in payloads.items():
        _write_video(folder / name)
        _analysis_next_to(folder, name, payload)
    plan = _plan34(tmp_path, folder, HABITS_SCRIPT)
    videos = [s for s in plan.slots if s.selected_kind == "video"]
    assert len(videos) == 3
    assert len({s.selected_path for s in videos}) == 3


def test_W_reuse_only_after_pool_exhaustion(tmp_path):
    folder = tmp_path / "pool"
    payload = {"keywords": [{"keyword": "meditation", "relevance_score": 100}]}
    _write_image(folder / "only_a.jpg")
    _write_image(folder / "only_b.jpg")
    _analysis_next_to(folder, "only_a.jpg", payload)
    _analysis_next_to(folder, "only_b.jpg", payload)
    script = ("Meditation helps. Meditation calms. Meditation focuses. "
              "Meditation breathes. Meditation rests. ")
    plan = _plan34(tmp_path, folder, script, program_duration=40.0)
    paths = [s.selected_path for s in plan.slots if s.selected_kind]
    assert len(paths) == 5
    assert set(paths) == {str(folder / "only_a.jpg"), str(folder / "only_b.jpg")}
    # Reuse only begins AFTER both assets were used once.
    assert set(paths[:2]) == set(paths) or len(set(paths[:2])) == 2
    for previous, current in zip(paths, paths[1:]):
        assert previous != current, "never the same asset twice in a row"


# ---------------------------------------------------------------------------
# Randomize (spec section 24) - X..Y
# ---------------------------------------------------------------------------
def test_X_randomize_produces_a_different_valid_result(tmp_path):
    folder = tmp_path / "pool"
    for index in range(5):
        _write_image(folder / f"meditation_{index}.jpg")
        _analysis_next_to(folder, f"meditation_{index}.jpg", {
            "keywords": [{"keyword": "meditation", "relevance_score": 100 - index}],
        })
    script = ("Meditation session one. Meditation session two. "
              "Meditation session three. Meditation session four. ")
    base = _plan34(tmp_path, folder, script)
    alt = _plan34(tmp_path, folder, script, nonce=4)
    base_paths = [s.selected_path for s in base.slots if s.selected_kind]
    alt_paths = [s.selected_path for s in alt.slots if s.selected_kind]
    assert base_paths != alt_paths, "Randomize must change the assignment"
    assert len(set(alt_paths)) == len(alt_paths), "uniqueness survives Randomize"
    # Deterministic with a fixed seed.
    again = _plan34(tmp_path, folder, script, nonce=4)
    assert [s.selected_path for s in again.slots if s.selected_kind] == alt_paths


def test_Y_randomize_keeps_semantic_relevance_in_smart_match(tmp_path):
    folder = tmp_path / "pool"
    for index in range(4):
        _write_image(folder / f"meditation_{index}.jpg")
        _analysis_next_to(folder, f"meditation_{index}.jpg", {
            "keywords": [{"keyword": "meditation", "relevance_score": 95 - index}],
        })
    _write_image(folder / "oldtimer.jpg")
    _analysis_next_to(folder, "oldtimer.jpg", CAR_ASSET)
    plan = _plan34(tmp_path, folder, "Meditation brings calm. ", nonce=7,
                   program_duration=8.0)
    smart = [s for s in plan.slots if s.source_mode == SLOT_SOURCE_SMART]
    assert smart
    for slot in smart:
        assert "meditation" in Path(slot.selected_path).name, \
            "Randomize explores ALTERNATE strong candidates, never junk"
        assert slot.score >= SMART_MATCH_THRESHOLD


def test_Z_rare_high_value_assets_are_reserved(tmp_path):
    # Spec section 21 verbatim: sentence 2 depends on asset A much more
    # than sentence 1 does, so A must be reserved for sentence 2 even
    # though sentence 1 comes first.
    folder = tmp_path / "pool"
    asset_a = {"keywords": [{"keyword": "meditation", "relevance_score": 100}]}
    asset_b = {"keywords": [{"keyword": "meditation", "relevance_score": 88}]}
    _write_image(folder / "asset_a.jpg")
    _write_image(folder / "asset_b.jpg")
    _analysis_next_to(folder, "asset_a.jpg", asset_a)
    _analysis_next_to(folder, "asset_b.jpg", asset_b)

    from app.video_merger.smart_visuals import (
        assign_smart_visual_selections,
        assign_slot_times,
        build_semantic_slots,
    )

    entries, _ = build_media_index((str(folder),), tmp_path / "cache")
    # Craft scores directly through the engine: sentence 1 sees A=95, B=91;
    # sentence 2 sees A=100, B=70. We emulate this with tailored metadata.
    entries_by_name = {Path(entry.path).name: entry for entry in entries}
    import dataclasses
    entries_by_name["asset_a.jpg"] = dataclasses.replace(
        entries_by_name["asset_a.jpg"],
        analysis={"keywords": [
            {"keyword": "morning meditation", "relevance_score": 95},
            {"keyword": "evening meditation", "relevance_score": 100},
        ]},
    )
    entries_by_name["asset_b.jpg"] = dataclasses.replace(
        entries_by_name["asset_b.jpg"],
        analysis={"keywords": [
            {"keyword": "morning meditation", "relevance_score": 91},
            {"keyword": "evening meditation", "relevance_score": 70},
        ]},
    )
    entries = [entries_by_name["asset_a.jpg"], entries_by_name["asset_b.jpg"]]

    sentences = ["A morning meditation session.", "An evening meditation session."]
    timed = assign_slot_times(build_semantic_slots(sentences, "every_1"), 16.0)
    profile = SmartVisualProfile(enabled=True, folders=(str(folder),),
                                 mode=SMART_MODE_SMART_MATCH, cadence="every_1")
    slots = assign_smart_visual_selections(timed, entries, profile=profile, rng=Random(11))
    first = Path(slots[0].selected_path).name
    second = Path(slots[1].selected_path).name
    # Sentence 2's decisive asset (A dominates evening 100 vs B 70) must go
    # to sentence 2; sentence 1 keeps the nearly-equivalent B.
    assert second == "asset_a.jpg", "the rare high-value asset is reserved"
    assert first == "asset_b.jpg"


# ---------------------------------------------------------------------------
# Analyze Timeline transparency (spec sections 22-23) - AA..AB
# ---------------------------------------------------------------------------
def test_AA_analyze_returns_explainable_evidence(tmp_path):
    folder = _analysis_pool(tmp_path)
    plan = _plan34(tmp_path, folder, HABITS_SCRIPT)
    records = plan.to_records()
    smart_records = [r for r in records if r["source_label"] == "Smart Match"]
    assert smart_records
    for record in smart_records:
        assert record["matched_terms"], "top evidence terms are shown"
        assert record["evidence"], "explainable evidence lines exist"
        assert record["sentence_index"] >= 0
        assert float(record["match"]) > 0
    # Only the FEW strongest terms are exposed (never 50 keywords).
    assert all(len(r["matched_terms"].split(", ")) <= 3 for r in smart_records)


def test_AB_analyze_timeline_does_not_render(tmp_path, monkeypatch):
    """Planning must never invoke FFmpeg/ffprobe or touch image generation."""
    import subprocess

    def _no_subprocess(*_args, **_kwargs):
        raise AssertionError("Analyze Timeline must not spawn processes")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    folder = _analysis_pool(tmp_path)
    plan = _plan34(tmp_path, folder, HABITS_SCRIPT)
    assert plan.selected_count >= 1, "a real plan is produced without rendering"


def test_AC_smart_visuals_never_calls_image_generation(tmp_path, monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError("image generation invoked by Smart Visuals")

    for name in dir(image_generation):
        obj = getattr(image_generation, name)
        if callable(obj) and name.startswith(("generate", "resolve", "local")):
            try:
                monkeypatch.setattr(image_generation, name, _explode)
            except AttributeError:
                pass
    folder = _analysis_pool(tmp_path)
    for mode in (SMART_MODE_SMART_MATCH, SMART_MODE_SMART_INSERTS, SMART_MODE_RANDOM_ONLY):
        plan = _plan34(tmp_path, folder, HABITS_SCRIPT, mode=mode)
        assert all(not slot.generation_used for slot in plan.slots)


# ---------------------------------------------------------------------------
# Metadata cache (spec sections 27-28) - AD..AE
# ---------------------------------------------------------------------------
def test_AD_metadata_cache_works(tmp_path):
    folder = _analysis_pool(tmp_path)
    cache = tmp_path / "cache"
    entries_first, stats_first = build_media_index((str(folder),), cache)
    entries_second, stats_second = build_media_index((str(folder),), cache)
    assert stats_first.indexed == 4
    assert stats_second.reused == 4 and stats_second.indexed == 0, \
        "unchanged assets are restored from the cache, never re-parsed"
    assert all(entry.has_analysis for entry in entries_second)
    assert {e.path for e in entries_first} == {e.path for e in entries_second}


def test_AE_cache_invalidates_after_sidecar_modification(tmp_path):
    folder = tmp_path / "pool"
    _write_image(folder / "photo.jpg")
    sidecar = _analysis_next_to(folder, "photo.jpg", {
        "keywords": [{"keyword": "meditation", "relevance_score": 100}],
    })
    cache = tmp_path / "cache"
    entries, _ = build_media_index((str(folder),), cache)
    assert "meditation" in str(entries[0].analysis)

    # Modify ONLY this one sidecar (bump mtime+size).
    import os
    sidecar.write_text(json.dumps({
        "keywords": [{"keyword": "automobile", "relevance_score": 100}],
    }), encoding="utf-8")
    os.utime(sidecar, (time.time() + 5, time.time() + 5))

    entries, stats = build_media_index((str(folder),), cache)
    assert "automobile" in str(entries[0].analysis), \
        "the changed sidecar is re-parsed"
    assert stats.reused == 1, \
        "the ASSET file is untouched, so the entry shell is reused"


# ---------------------------------------------------------------------------
# Large pool performance (spec section 39)
# ---------------------------------------------------------------------------
def test_large_pool_index_once_unique_and_deterministic(tmp_path):
    folder_images = tmp_path / "images"
    folder_videos = tmp_path / "videos"
    topics = ["meditation", "concentration", "exercise", "nature", "city"]
    for index in range(250):
        topic = topics[index % len(topics)]
        _write_image(folder_images / f"img_{index:03d}.jpg")
        _analysis_next_to(folder_images, f"img_{index:03d}.jpg", {
            "keywords": [
                {"keyword": topic, "relevance_score": 100 - (index % 7)},
                {"keyword": f"generic_{index % 13}", "relevance_score": 20},
            ],
        })
    for index in range(250):
        topic = topics[(index + 2) % len(topics)]
        _write_video(folder_videos / f"vid_{index:03d}.mp4")
        _analysis_next_to(folder_videos, f"vid_{index:03d}.mp4", {
            "keywords": [{"keyword": topic, "relevance_score": 95 - (index % 9)}],
        })

    cache = tmp_path / "cache"
    started = time.perf_counter()
    entries, stats = build_media_index((str(folder_images), str(folder_videos)), cache)
    first_build = time.perf_counter() - started
    assert len(entries) == 500

    # Second build: metadata indexed ONCE - everything comes from the cache.
    started = time.perf_counter()
    entries_cached, stats_cached = build_media_index(
        (str(folder_images), str(folder_videos)), cache)
    second_build = time.perf_counter() - started
    assert stats_cached.reused == 500 and stats_cached.indexed == 0
    assert second_build < max(5.0, first_build), "cache keeps rebuilds cheap"

    script = ("Meditation is important and has many benefits. "
              "Regular practice can improve concentration. "
              "Physical exercise is another useful habit. "
              "A walk in the nature forest relaxes the mind. "
              "The city streets are full of life tonight. ")
    plan_a = _plan34(tmp_path, folder_images, script, program_duration=50.0)
    plan_b = _plan34(tmp_path, folder_images, script, program_duration=50.0)
    paths_a = [s.selected_path for s in plan_a.slots if s.selected_kind]
    paths_b = [s.selected_path for s in plan_b.slots if s.selected_kind]
    assert paths_a == paths_b, "fixed seed => deterministic selection"
    assert paths_a and len(set(paths_a)) == len(paths_a), \
        "uniqueness holds across a 250-image pool"
    for slot in plan_a.slots:
        if slot.source_mode == SLOT_SOURCE_SMART:
            assert slot.score >= SMART_MATCH_THRESHOLD

    # Full 500-asset selection completes without catastrophic scaling.
    started = time.perf_counter()
    profile = SmartVisualProfile(enabled=True,
                                 folders=(str(folder_images), str(folder_videos)),
                                 mode=SMART_MODE_SMART_MATCH, cadence="every_1")
    mixed = build_smart_visual_plan(
        profile=profile, script_text=script, program_duration=50.0,
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache2",
        ffprobe_path="ffprobe-not-used", seed_parts=("p34-mixed",),
        log=lambda *_a, **_k: None,
    )
    elapsed = time.perf_counter() - started
    assert mixed.selected_count >= 1
    assert elapsed < 30.0, f"selection over 500 assets too slow: {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Video + image normalization into one representation (spec section 4)
# ---------------------------------------------------------------------------
def test_video_and_image_metadata_normalize_into_one_representation(tmp_path):
    folder = tmp_path / "pool"
    _write_image(folder / "still.jpg")
    _write_video(folder / "clip.mp4")
    _analysis_next_to(folder, "still.jpg", MEDITATION_ASSET)
    _analysis_next_to(folder, "clip.mp4", {
        "keywords": [{"keyword": "meditation", "relevance_score": 96}],
        "source_file": "clip.mp4",
    })
    entries, _ = build_media_index((str(folder),), tmp_path / "cache")
    kinds = {entry.kind for entry in entries}
    assert kinds == {"image", "video"}
    assert all(entry.has_analysis for entry in entries), \
        "videos consume their own analyzer JSON - no forced image schema"
    plan = _plan34(tmp_path, folder, "Meditation practice begins here. ",
                   program_duration=8.0)
    filled = [s for s in plan.slots if s.selected_kind]
    assert filled
    assert filled[0].selected_kind in {"image", "video"}


def test_metadata_from_dict_preserves_individual_scores():
    asset = asset_metadata_from_dict("a.jpg", "image", MEDITATION_ASSET)
    scores = {keyword.term.casefold(): keyword.relevance for keyword in asset.keywords}
    assert scores["meditation"] == 100
    assert scores["relaxation"] == 61
    assert scores["interior"] == 14, \
        "numeric relevance is preserved per keyword - never flattened"
    assert asset.quality > 0.9
    assert asset.negative_matches == ("medical treatment",)
    assert asset.uses["direct_uses"] == ("meditation guide illustration",)
