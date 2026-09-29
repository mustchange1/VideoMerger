"""Phase 35 compatibility coverage under the Phase-36 semantic duration contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video_merger.smart_timeline import SpeechUnit
from app.video_merger.smart_visuals import (
    PHASE36_HARD_MIN_DURATION,
    PHASE36_TARGET_MAX_DURATION,
    SmartVisualProfile,
    build_smart_visual_plan,
)


def _pool(tmp_path: Path, terms: list[str]) -> Path:
    folder = tmp_path / "pool"
    folder.mkdir()
    for index, term in enumerate(terms):
        asset = folder / f"{index:02d}_{term}.jpg"
        asset.write_bytes(b"fixture")
        (folder / f"{asset.name}.analysis.json").write_text(json.dumps({
            "keywords": [{"keyword": term, "relevance_score": 100}],
            "direct_uses": [term],
        }), encoding="utf-8")
    return folder


def _units(sentences: list[str], starts: list[float], ends: list[float]) -> list[SpeechUnit]:
    cursor = 0
    result = []
    for index, (text, start, end) in enumerate(zip(sentences, starts, ends)):
        result.append(SpeechUnit(
            index=index, text=text, char_start=cursor, char_end=cursor + len(text),
            start=start, end=end, first_word_index=index, last_word_index=index,
        ))
        cursor += len(text) + 1
    return result


def _plan(tmp_path: Path, starts: list[float], ends: list[float]):
    terms = ["forest", "ocean", "city", "music", "science"][:len(starts)]
    verbs = ["grows", "moves", "shines", "plays", "advances"]
    sentences = [f"{term.title()} {verbs[index]}." for index, term in enumerate(terms)]
    folder = _pool(tmp_path, terms)
    return build_smart_visual_plan(
        profile=SmartVisualProfile(enabled=True, folders=(str(folder),), mode="smart_match"),
        script_text=" ".join(sentences), program_duration=max(ends),
        width=320, height=180, fps=30.0, cache_dir=tmp_path / "cache",
        ffprobe_path="ffprobe", speech_units=_units(sentences, starts, ends),
        seed_parts=("phase36-duration",), log=lambda *_a, **_k: None,
    )


def _automatic(plan):
    return [slot for slot in plan.slots if slot.selected_kind and slot.audio_anchored]


def test_dense_short_sentences_merge_instead_of_flashing(tmp_path):
    plan = _plan(tmp_path, [0.0, 4.0, 8.0], [3.5, 7.5, 9.0])
    automatic = _automatic(plan)
    assert len(automatic) == 1
    assert automatic[0].effective_insert_duration == pytest.approx(9.0)


def test_automatic_visual_duration_maximum_is_twelve_seconds(tmp_path):
    plan = _plan(tmp_path, [0.0, 15.0], [12.0, 28.0])
    durations = [slot.effective_insert_duration for slot in _automatic(plan)]
    assert durations == pytest.approx([12.0, 6.5, 6.5])
    assert max(durations) <= PHASE36_TARGET_MAX_DURATION


def test_topic_boundaries_use_acoustic_section_durations(tmp_path):
    plan = _plan(tmp_path, [0.0, 6.5, 14.5], [5.0, 13.0, 19.5])
    assert [slot.effective_insert_duration for slot in _automatic(plan)] == pytest.approx([5.0, 6.5, 5.0])


def test_dense_sentence_boundaries_become_one_semantic_section(tmp_path):
    plan = _plan(tmp_path, [0.0, 2.0, 4.0, 8.0], [1.5, 3.5, 7.5, 11.0])
    automatic = _automatic(plan)
    assert [slot.requested_audio_boundary for slot in automatic] == pytest.approx([0.0])
    assert automatic[0].effective_insert_duration == pytest.approx(11.0)


def test_short_final_edge_merges_backward_and_never_extends_past_speech(tmp_path):
    plan = _plan(tmp_path, [0.0, 5.0], [4.5, 6.0])
    automatic = _automatic(plan)
    assert len(automatic) == 1
    assert automatic[0].effective_insert_duration == pytest.approx(6.0)
    assert automatic[0].requested_audio_boundary + automatic[0].effective_insert_duration == pytest.approx(6.0)


def test_every_automatic_duration_is_within_phase36_contract(tmp_path):
    plan = _plan(tmp_path, [0.2, 4.4, 11.7, 23.9], [3.9, 10.8, 22.0, 25.0])
    durations = [slot.effective_insert_duration for slot in _automatic(plan)]
    assert durations
    assert min(durations) >= PHASE36_HARD_MIN_DURATION
    assert max(durations) <= PHASE36_TARGET_MAX_DURATION
