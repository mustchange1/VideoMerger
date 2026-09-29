"""Phase 34 end-to-end (spec section 38): sentence-anchored, metadata-driven
Smart Visuals verified with REAL FFmpeg renders.

Script:
  "Meditation is important and has many benefits. Regular practice can
   improve concentration. Physical exercise is another useful habit."

Pool: three colored stills with real analyzer sidecars (meditation =
yellow, concentration = magenta, exercise = cyan) + one unrelated orange
asset. The render must begin each sentence with the matching topic visual,
keep voiceover and subtitles untouched, stay inside the safe duration
rules, never duplicate an asset and never generate anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video_merger.main_project import MainProjectEngine
from tests.test_phase30_e2e import (
    VideoMergerEngine,
    _base_settings,
    _color_ratio,
    _find_windows,
    _make_image,
    _probe_duration,
    _probe_size,
    _tone,
)
from tests.test_phase31_e2e import _create_confirmed, _smart_lf

pytestmark = pytest.mark.e2e

PHASE34_SCRIPT = (
    "Meditation is important and has many benefits. "
    "Regular practice can improve concentration. "
    "Physical exercise is another useful habit."
)

# Compact source timings, scaled by 3× in the real-render fixture so Phase 36
# can exercise its four-second hard minimum without extending past audio.
PHASE34_TIMINGS = [
    ("Meditation", 0.05, 0.28), ("is", 0.30, 0.34), ("important", 0.36, 0.58),
    ("and", 0.60, 0.62), ("has", 0.64, 0.70), ("many", 0.72, 0.82),
    ("benefits", 0.84, 1.00),
    ("Regular", 1.05, 1.20), ("practice", 1.22, 1.40), ("can", 1.42, 1.46),
    ("improve", 1.48, 1.65), ("concentration", 1.67, 1.90),
    ("Physical", 1.95, 2.05), ("exercise", 2.07, 2.20), ("is", 2.21, 2.23),
    ("another", 2.24, 2.28), ("useful", 2.29, 2.33), ("habit", 2.34, 2.38),
]


def _analysis(
    *,
    keywords: list[tuple[str, int]],
    concepts: list[tuple[str, int]],
    direct: list[str],
    search: list[str],
    negatives: list[str] | None = None,
) -> dict:
    payload = {
        "keywords": [
            {
                "keyword": term,
                "relevance_score": score,
                "category": "primary subject",
                "literal_or_semantic": "literal",
                "focus_level": "high",
                "visible_evidence": term,
            }
            for term, score in keywords
        ],
        "visual_concepts": [
            {"concept": term, "relevance_score": score} for term, score in concepts
        ],
        "direct_uses": direct,
        "search_phrases": search,
    }
    if negatives:
        payload["negative_matches"] = negatives
    return payload


def _phase34_factory(tmp_path: Path, ffmpeg: Path, ffprobe: Path):
    """Real project with FOUR base clips so the fitted timeline provides one
    insertion boundary per script sentence (3 sentences -> 3 boundaries)."""
    from app.video_merger.alignment import LocalWordAligner, RecognizedWord
    from tests.conftest import make_clip

    clips = []
    for index, color in enumerate(("red", "blue", "green", "purple")):
        clip = tmp_path / f"clip_{index}.mp4"
        make_clip(ffmpeg, clip, size="320x180", duration=0.6, color=color, audio_rate=None)
        clips.append(clip)

    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, 850, 7.2, 0.65)
    script = tmp_path / "script.txt"
    script.write_text(PHASE34_SCRIPT + "\n", encoding="utf-8")

    def recognize(path: Path, _language: str):
        return [
            RecognizedWord(word, start * 3.0, end * 3.0, 0.99)
            for word, start, end in PHASE34_TIMINGS
        ], "en"

    aligner = LocalWordAligner("phase34-fixture", recognize, cache_dir=tmp_path / "align-cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(clips)
    return engine, media, aligner, voice, script


def _phase34_pool(tmp_path: Path, ffmpeg: Path) -> Path:
    media = tmp_path / "pool"
    media.mkdir()
    for name, color in (
        ("meditation.jpg", "yellow"),
        ("concentration.jpg", "magenta"),
        ("exercise.jpg", "cyan"),
        ("unrelated.jpg", "orange"),
    ):
        _make_image(ffmpeg, media / name, color)
    analyses = {
        "meditation.jpg": _analysis(
            keywords=[("meditation", 100), ("mindfulness", 90), ("inner calm", 72)],
            concepts=[("calm", 60)],
            direct=["meditation session recording"],
            search=["person meditating"],
        ),
        "concentration.jpg": _analysis(
            keywords=[("concentration", 100), ("focus", 88)],
            concepts=[("attention", 55)],
            direct=["deep focus and concentration"],
            search=["focus concentration"],
        ),
        "exercise.jpg": _analysis(
            keywords=[("exercise", 100), ("running", 90)],
            concepts=[("movement", 55)],
            direct=["outdoor exercise running"],
            search=["physical exercise"],
        ),
        "unrelated.jpg": _analysis(
            keywords=[("automobile", 100), ("car", 92), ("vehicle", 80)],
            concepts=[("transport", 60)],
            direct=["automobile product shot"],
            search=["modern car"],
            negatives=["person", "human portrait"],
        ),
    }
    for name, payload in analyses.items():
        (media / f"{name}.analysis.json").write_text(json.dumps(payload), encoding="utf-8")
    return media


def _is_yellow(frame: bytes) -> bool:
    return _color_ratio(frame, (255, 255, 0)) > 0.55


def _is_magenta(frame: bytes) -> bool:
    return _color_ratio(frame, (255, 0, 255)) > 0.55


def _is_cyan(frame: bytes) -> bool:
    return _color_ratio(frame, (0, 255, 255)) > 0.55


def _is_orange(frame: bytes) -> bool:
    return _color_ratio(frame, (255, 165, 0)) > 0.55


def test_phase34_sentence_anchored_plan(tmp_path, ffmpeg_paths):
    """Plan level: analyzer-driven, sentence-anchored, unique selections."""
    from app.video_merger.smart_visuals import build_smart_visual_plan, smart_visual_profile_from_settings

    ffmpeg, ffprobe = ffmpeg_paths
    _engine, _media, _aligner, voice, script = _phase34_factory(tmp_path, ffmpeg, ffprobe)
    pool = _phase34_pool(tmp_path, ffmpeg)
    settings = _base_settings(voice, script, **_smart_lf(pool))
    profile = smart_visual_profile_from_settings(settings)

    logs: list[str] = []
    plan = build_smart_visual_plan(
        profile=profile,
        script_text=PHASE34_SCRIPT,
        program_duration=2.4,
        width=320,
        height=180,
        fps=25.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe,
        log=logs.append,
    )

    selected = [slot for slot in plan.slots if slot.selected_path]
    assert len(selected) == 3, [slot.reason for slot in plan.slots]

    # Sentence-anchored start order: one distinct, increasing start per sentence.
    starts = [slot.start for slot in selected]
    assert starts == sorted(starts) and len({round(s, 3) for s in starts}) == 3

    # Topic order follows the script: meditation -> concentration -> exercise.
    labels = [Path(slot.selected_path).name for slot in selected]
    assert labels == ["meditation.jpg", "concentration.jpg", "exercise.jpg"], labels
    assert [slot.sentence_index for slot in selected] == [0, 1, 2]

    # No duplication while alternatives remain; unrelated asset stays unused.
    assert len({slot.selected_path for slot in selected}) == 3
    assert "unrelated.jpg" not in labels

    # Metadata engine + Smart Match provenance + explainable evidence.
    from app.video_merger.smart_visuals import SLOT_MODE_MATCH, SLOT_SOURCE_SMART

    for slot in selected:
        assert slot.scoring_engine == "analyzer"
        assert slot.source_mode == SLOT_SOURCE_SMART
        assert slot.fallback_mode == SLOT_MODE_MATCH
        assert slot.sentence_anchored is True
        assert slot.matched_terms
        assert slot.reason.startswith("matched:")

    # Nothing was generated.
    generated = tmp_path / "cache" / "smart_visual_generated"
    assert not generated.exists() or not any(generated.iterdir())


def test_phase34_render_visuals_follow_sentences(tmp_path, ffmpeg_paths):
    """Real render: each sentence starts with its topic visual, voiceover and
    subtitles stay untouched, shortened boundary windows obey the safe rule,
    nothing is duplicated or generated."""
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _phase34_factory(tmp_path, ffmpeg, ffprobe)
    pool = _phase34_pool(tmp_path, ffmpeg)

    baseline = MainProjectEngine(engine).create_main(
        media,
        _base_settings(voice, script, subtitle_enabled=True),
        tmp_path / "base",
        aligner=aligner,
    )
    assert baseline.video.is_file() and baseline.report.ok

    result = _create_confirmed(
        engine, media,
        _base_settings(voice, script, subtitle_enabled=True, **_smart_lf(pool)),
        tmp_path / "smart",
        aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok

    # --- subtitles untouched -------------------------------------------------
    assert baseline.srt.is_file() and result.srt.is_file()
    assert result.srt.read_text(encoding="utf-8") == baseline.srt.read_text(encoding="utf-8"), \
        "Smart Visuals must never touch subtitle timing"

    # --- voiceover untouched --------------------------------------------------
    base_duration = _probe_duration(ffprobe, baseline.video)
    final_duration = _probe_duration(ffprobe, result.video)
    # Phase 36 locks the complete render to the 7.2-second voiceover; the
    # baseline's legacy end padding may be longer but Smart Visuals never are.
    assert final_duration == pytest.approx(7.2, abs=0.08)
    assert final_duration <= base_duration + 0.08

    width, height = _probe_size(ffprobe, result.video)
    yellow = _find_windows(ffmpeg, result.video, width, height, final_duration, _is_yellow)
    magenta = _find_windows(ffmpeg, result.video, width, height, final_duration, _is_magenta)
    cyan = _find_windows(ffmpeg, result.video, width, height, final_duration, _is_cyan)
    orange = _find_windows(ffmpeg, result.video, width, height, final_duration, _is_orange)

    # The three dense sentences form one coherent audio-locked section, so
    # exactly one relevant topic visual is retained and no flash is emitted.
    topic_windows = [windows for windows in (yellow, magenta, cyan) if windows]
    assert len(topic_windows) == 1, (yellow, magenta, cyan)
    assert all(end - start <= 0.15 for start, end in orange), \
        f"the unrelated asset must never appear: {orange}"
    assert topic_windows[0][0][1] - topic_windows[0][0][0] >= 5.0

    trace = json.loads((tmp_path / "smart" / "PHASE_35_DEBUG_TRACE.json").read_text(encoding="utf-8"))
    inserted = [item for item in trace["placements"] if item["status"] == "inserted"]
    assert len(inserted) == 1
    assert inserted[0]["visual_duration"] == pytest.approx(7.2, abs=0.1)

    # --- nothing was generated anywhere ---------------------------------------
    generated = tmp_path / "cache" / "smart_visual_generated"
    assert not generated.exists() or not any(generated.iterdir())
    assert not list(tmp_path.rglob("*.generated.*"))
