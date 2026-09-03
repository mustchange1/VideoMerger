"""Regression coverage for the subtitle timeline (SRT/VTT/ASS cue generation).

The real Windows project (20 voiceovers + one global script + 626 source clips,
YouTube Long-Form + Shorts) aborted with::

    SUBTITLE GENERATION FAILED [SRT/VTT/ASS timeline creation]:
    Overlapping subtitles at Cue 2.

Root cause: a script that only partially matches the voiceover leaves script
words unmatched inside a narrow acoustic gap. ``fallback_words`` floored every
such word at 20 ms and therefore marched them *past* the next real acoustic
anchor, which made the canonical word timeline run backwards;
``build_cues`` then applied its ``max(start + 0.02, end)`` minimum *after* the
"never reach the next cue" clamp, so the cue end landed past the next cue start
and ``validate_cues`` correctly rejected the timeline.

These tests pin the fixed contract:

* fallback timing never leaves its available window and never inverts order;
* the canonical word timeline is always strictly increasing with positive
  durations, and a valid timeline is returned untouched (identity preserved);
* cues never overlap, always satisfy ``start < end``, stay in chronological
  order, keep every single script word and never reach into the quiet pause;
* ``validate_cues`` stays exactly as strict as before — nothing is weakened and
  no word or cue is deleted to make validation pass.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.video_merger.alignment import (
    _CACHE_SCHEMA,
    MINIMUM_WORD_SPACING,
    LocalWordAligner,
    RecognizedWord,
    normalize_word_timeline,
    script_word_spans,
)
from app.video_merger.errors import VideoMergerError
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import (
    AlignmentResult,
    AudioInfo,
    ExportSettings,
    MainVideoResult,
    MediaInfo,
    ValidationReport,
    WordTiming,
)
from app.video_merger.subtitles import (
    build_cues,
    validate_cues,
    validate_subtitle_file,
    write_ass,
    write_canonical_timeline,
    write_srt,
    write_vtt,
)
from app.video_merger.youtube_outputs import EXPORT_MODE_SHORTS

LONG_SCRIPT_SENTENCE = (
    "Die Stille ist nicht leer, sie ist voller Antworten. "
    "Wer heute aufmerksam sein will, muss zuerst lernen, wegzuhören. "
    "Der Lärm verspricht uns Bedeutung, doch er liefert nur Ablenkung. "
    "Ein Gedanke braucht Raum, um überhaupt erst zu entstehen. "
)


def _word(text: str, start: float, end: float, confidence: float = 0.9) -> WordTiming:
    return WordTiming(
        text=text, start=start, end=end, confidence=confidence,
        script_start=0, script_end=len(text),
    )


def _alignment(words: list[WordTiming], hard_breaks: list[float] | None = None) -> AlignmentResult:
    return AlignmentResult(
        words=list(words), language="German", method="test word timestamps",
        compatibility=1.0, average_confidence=0.9, warnings=[],
        hard_breaks=list(hard_breaks or []),
    )


def _media(path: str, duration: float = 2.0) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=1920, height=1080,
        effective_width=1920, effective_height=1080, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="", audio=AudioInfo(),
    )


def _assert_complete_valid_cues(cues, words, program_end=None):
    """The full subtitle timeline contract, unchanged and not weakened."""
    validate_cues(cues, len(words))
    assert [cue.index for cue in cues] == list(range(1, len(cues) + 1))
    assert all(cue.start < cue.end for cue in cues), [(c.index, c.start, c.end) for c in cues]
    assert all(cue.start >= 0.0 for cue in cues)
    # Chronological order and the actual overlap guarantee: a cue end never
    # reaches the next cue start.
    assert all(left.end <= right.start for left, right in zip(cues, cues[1:]))
    assert all(left.start <= right.start for left, right in zip(cues, cues[1:]))
    # No word is dropped, re-ordered or re-spelled to make validation pass.
    flat = [word for cue in cues for word in cue.words]
    assert [word.text for word in flat] == [word.text for word in words]
    assert all(cue.line_count in (1, 2) for cue in cues)
    if program_end is not None:
        assert cues[-1].end <= program_end + 0.001


def _assert_valid_word_timeline(words):
    assert all(word.start >= 0.0 for word in words)
    assert all(word.start < word.end for word in words)
    assert all(
        right.start >= left.start + MINIMUM_WORD_SPACING - 1e-9
        for left, right in zip(words, words[1:])
    )


# --------------------------------------------------------------------------- #
# The reported failure: overlapping cues during SRT/VTT/ASS timeline creation
# --------------------------------------------------------------------------- #


def test_reported_overlapping_cue_2_failure_is_fixed(tmp_path):
    """The exact reported failure, reproduced verbatim.

    On the unfixed code this raises::

        SUBTITLE GENERATION FAILED [SRT/VTT/ASS timeline creation]:
        Überlappende Untertitel bei Cue 2.        (Overlapping subtitles at Cue 2.)

    Thirty unmatched script words sit before the first lexical anchor while two
    unmatched *spoken* words start later than that anchor. The old backfill
    computed ``end = right - (count - offset - 1) * 0.02``, went negative and
    collapsed 24 words onto the identical ``[0.0, 0.02]`` interval — duplicate
    cue starts are an overlap by construction, and it surfaced at Cue 2.
    """
    script = " ".join(f"Wort{index}" for index in range(30)) + " Anfang."
    spoken = [
        RecognizedWord("Neben", 0.50, 0.60, 0.4),
        RecognizedWord("Rauschen", 0.55, 0.65, 0.4),
        RecognizedWord("Anfang", 0.10, 0.30, 0.9),
    ]
    aligner = LocalWordAligner("tiny", lambda _path, _language: (spoken, "de"), use_cache=False)
    result = aligner.align(script, tmp_path / "voice.wav", "German", fallback_end=0.70)

    # Every script word survives and no two words share a start any more.
    assert [word.text for word in result.words] == [
        token for token, _start, _end in script_word_spans(script)
    ]
    assert len({word.start for word in result.words}) == len(result.words)
    _assert_valid_word_timeline(result.words)

    cues = build_cues(script, result, "long_1", program_end=result.words[-1].end + 0.2,
                      width=1920, height=1080, font_key="modern_sans_bold")
    _assert_complete_valid_cues(cues, result.words)

    # The complete sidecar + burn-in bundle is produced instead of aborting.
    write_srt(cues, tmp_path / "MainVideo_16x9.srt")
    write_vtt(cues, tmp_path / "MainVideo_16x9.vtt")
    validate_subtitle_file(tmp_path / "MainVideo_16x9.srt", "srt")
    validate_subtitle_file(tmp_path / "MainVideo_16x9.vtt", "vtt")
    write_canonical_timeline(script, result, cues, tmp_path / "MainVideo_16x9.subtitle_timeline.json")
    write_ass(
        script, cues, tmp_path / "MainVideo_16x9_burn.ass", "long_1", "Center", 1920, 1080,
        animation="static_phrase", font_key="modern_sans_bold",
    )
    assert "[Events]" in (tmp_path / "MainVideo_16x9_burn.ass").read_text(encoding="utf-8-sig")


def test_leading_fallback_overflow_no_longer_runs_backwards(tmp_path):
    """Unmatched script words before the first anchor must not overrun it.

    Ten script words precede the only acoustic anchor at 0.10 s. The old
    placement floored every fallback word at 20 ms, marched them past the
    anchor to 0.20 s and then jumped *backwards* to the anchor at 0.10 s.
    """
    script = "Eins Zwei Drei Vier Fünf Sechs Sieben Acht Neun Zehn Anfang."
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: ([RecognizedWord("Anfang", 0.10, 0.30, 0.9)], "de"),
        use_cache=False,
    )
    result = aligner.align(script, tmp_path / "voice.wav", "German", fallback_end=0.30)

    assert [word.text for word in result.words] == [
        token for token, _start, _end in script_word_spans(script)
    ]
    _assert_valid_word_timeline(result.words)
    assert all(word.confidence == 0.0 for word in result.words[:10])
    assert result.words[10].confidence == 0.9
    # The measured anchor keeps its real acoustic end; only its start moves, by
    # the minimum needed to clear the retained fallback run before it.
    assert result.words[10].end == pytest.approx(0.30)
    assert result.words[10].start >= result.words[9].end - 1e-9

    cues = build_cues(script, result, "long_1", program_end=result.words[-1].end + 0.2,
                      width=1920, height=1080)
    _assert_complete_valid_cues(cues, result.words)


def test_fallback_words_never_extend_past_the_next_acoustic_anchor(tmp_path):
    """Twelve unmatched script words inside a 100 ms gap must stay inside it."""
    script = "Alpha " + " ".join(f"Wort{index}" for index in range(12)) + " Omega"
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: (
            [RecognizedWord("Alpha", 0.00, 0.20, 0.9), RecognizedWord("Omega", 0.30, 0.50, 0.9)],
            "de",
        ),
        use_cache=False,
    )
    result = aligner.align(script, tmp_path / "voice.wav", "German", fallback_end=0.50)

    assert [word.text for word in result.words] == [
        token for token, _start, _end in script_word_spans(script)
    ]
    # The real anchors keep their measured acoustic timing exactly.
    assert result.words[0].start == pytest.approx(0.00)
    assert result.words[0].end == pytest.approx(0.20)
    _assert_valid_word_timeline(result.words)
    # Fallback words are bounded, positive and never invert the script order.
    fallback = result.words[1:-1]
    assert all(word.confidence == 0.0 for word in fallback)
    assert all(word.start >= 0.20 - 1e-9 for word in fallback)
    assert all(word.start < word.end for word in fallback)
    assert result.words[-1].start >= fallback[-1].end - 1e-9

    cues = build_cues(script, result, "long_1", program_end=0.50, width=1920, height=1080)
    _assert_complete_valid_cues(cues, result.words, program_end=0.50)


def test_dense_fallback_run_never_collapses_onto_duplicate_timestamps(tmp_path):
    """Eighty unmatched words before one anchor must not all pile up at 0.0 s.

    The old backfill computed ``end = right - (count - offset - 1) * 0.02``,
    which went negative for a large run and collapsed every word onto the same
    ``[0.0, 0.02]`` interval — duplicate starts guarantee an overlap.
    """
    script = " ".join(f"Wort{index}" for index in range(80)) + " Ende"
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: ([RecognizedWord("Ende", 0.05, 0.25, 0.9)], "de"),
        use_cache=False,
    )
    result = aligner.align(script, tmp_path / "voice.wav", "German", fallback_end=0.25)

    assert len(result.words) == 81
    _assert_valid_word_timeline(result.words)
    starts = [word.start for word in result.words]
    assert len(set(starts)) == len(starts), "duplicate word starts are an overlap by construction"
    assert all(word.start < word.end for word in result.words)

    cues = build_cues(script, result, "long_1", program_end=result.words[-1].end + 0.2)
    _assert_complete_valid_cues(cues, result.words)


# --------------------------------------------------------------------------- #
# The canonical timeline repair itself
# --------------------------------------------------------------------------- #


def test_normalize_word_timeline_is_a_no_op_for_a_valid_timeline():
    """Real acoustic timing is never touched: same list object, same values."""
    words = [
        _word("Heute", 0.00, 0.30), _word("sprechen", 0.30, 0.62),
        _word("wir", 0.62, 0.74), _word("über", 0.80, 1.10),
    ]
    assert normalize_word_timeline(words) is words
    assert normalize_word_timeline([]) == []


def test_normalize_word_timeline_keeps_overlapping_but_increasing_acoustic_ends():
    """faster-whisper word boundaries may overlap; that is *not* repaired.

    Only starts that are too close together (or run backwards) are moved, so
    measured speech timing survives and captions stay glued to the audio.
    """
    words = [
        _word("eins", 0.00, 0.30), _word("zwei", 0.22, 0.52), _word("drei", 0.44, 0.74),
    ]
    assert normalize_word_timeline(words) is words


def test_normalize_word_timeline_repairs_backwards_degenerate_and_dense_starts():
    words = [
        _word("a", 5.00, 5.30),
        _word("b", 4.80, 4.90),   # runs backwards
        _word("c", 4.85, 4.85),   # degenerate duration
        _word("d", 4.86, 5.10),   # closer than one displayable cue
        _word("e", -0.50, 0.40),  # negative start
    ]
    repaired = normalize_word_timeline(words)
    assert repaired is not words
    assert [word.text for word in repaired] == [word.text for word in words]
    _assert_valid_word_timeline(repaired)
    # The repaired run stays inside the original acoustic span.
    assert repaired[0].start == pytest.approx(5.00)
    assert max(word.end for word in repaired) <= max(word.end for word in words) + 1e-9
    # Repairing is idempotent and then returns the identical list.
    assert normalize_word_timeline(repaired) is repaired


def test_build_cues_repairs_a_foreign_non_monotone_timeline_without_dropping_words(tmp_path):
    """Defence in depth: an injected/legacy timeline must not abort a render."""
    script = "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett Kilo Lima Mike November Oscar Papa."
    words = [
        _word("Alpha", 0.00, 0.30), _word("Bravo", 0.30, 0.60), _word("Charlie", 0.60, 0.90),
        _word("Delta", 0.90, 1.20), _word("Echo", 1.20, 1.50), _word("Foxtrot", 1.50, 1.80),
        _word("Golf", 1.80, 2.10), _word("Hotel", 2.10, 2.40), _word("India", 2.40, 2.42),
        _word("Juliett", 2.42, 2.44), _word("Kilo", 2.20, 2.50), _word("Lima", 2.50, 2.80),
        _word("Mike", 2.80, 3.10), _word("November", 3.10, 3.40), _word("Oscar", 3.40, 3.70),
        _word("Papa.", 3.70, 4.00),
    ]
    alignment = _alignment(words, hard_breaks=[2.20])
    for preset, width, height in (("long_1", 1920, 1080), ("short_1", 1080, 1920)):
        cues = build_cues(script, alignment, preset, program_end=4.0, width=width, height=height)
        _assert_complete_valid_cues(cues, alignment.words, program_end=4.0)
    # The canonical timeline stays writable even though the cue words are
    # repaired copies of the words that were handed in.
    cues = build_cues(script, alignment, "long_1", program_end=4.0)
    timeline = tmp_path / "MainVideo_16x9.subtitle_timeline.json"
    write_canonical_timeline(script, alignment, cues, timeline)
    payload = json.loads(timeline.read_text(encoding="utf-8"))
    assert payload["schema"] == 2
    assert len(payload["words"]) == len(alignment.words)
    flat_indexes = [index for cue in payload["cues"] for index in cue["word_indexes"]]
    assert flat_indexes == list(range(len(alignment.words)))


def test_hard_break_inside_a_cue_never_produces_an_invalid_or_overlapping_end():
    """Near-duplicate hard boundaries must not push a cue end before its start."""
    script = LONG_SCRIPT_SENTENCE
    spans = script_word_spans(script)
    words = [
        _word(token, 0.20 + index * 0.34, 0.20 + index * 0.34 + 0.27)
        for index, (token, _start, _end) in enumerate(spans)
    ]
    # Boundaries 7 ms apart, both inside the span of a single group.
    alignment = _alignment(words, hard_breaks=[words[4].start + 0.005, words[4].start + 0.012])
    cues = build_cues(script, alignment, "long_1", program_end=words[-1].end + 0.2)
    _assert_complete_valid_cues(cues, words)


# --------------------------------------------------------------------------- #
# Alignment cache semantics
# --------------------------------------------------------------------------- #


def test_alignment_cache_schema_is_bumped_for_the_new_timing_semantics():
    """Schema 3 caches may hold non-monotone words and must never be replayed."""
    assert _CACHE_SCHEMA >= 4


def test_alignment_caches_from_the_previous_timing_semantics_are_rejected(tmp_path):
    """A schema-3 cache may hold non-monotone words and must never be replayed."""
    cache = tmp_path / "cache"
    aligner = LocalWordAligner("tiny", cache_dir=cache)
    assert aligner.use_cache is True

    stale = {
        "schema": _CACHE_SCHEMA - 1,
        "words": [{"text": "a", "start": 0.2, "end": 0.3, "confidence": 0.0,
                   "script_start": 0, "script_end": 1}],
        "language": "de", "method": "m", "compatibility": 1.0,
        "average_confidence": 0.9, "warnings": [], "hard_breaks": [],
        "detected_language": "de",
    }
    for category in ("alignments", "transcriptions"):
        path = cache / category / "key.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stale), encoding="utf-8")
        assert aligner._cache_read(category, "key") is None

    current = dict(stale, schema=_CACHE_SCHEMA)
    for category in ("alignments", "transcriptions"):
        (cache / category / "key.json").write_text(json.dumps(current), encoding="utf-8")
        assert aligner._cache_read(category, "key") is not None


def test_alignment_cache_write_and_read_round_trip_uses_the_current_schema(tmp_path):
    cache = tmp_path / "cache"
    aligner = LocalWordAligner("tiny", cache_dir=cache)
    aligner._cache_write("alignments", "k", {"words": []})
    payload = json.loads((cache / "alignments" / "k.json").read_text(encoding="utf-8"))
    assert payload["schema"] == _CACHE_SCHEMA
    assert aligner._cache_read("alignments", "k") == {"schema": _CACHE_SCHEMA, "words": []}


# --------------------------------------------------------------------------- #
# The real project shape: 20 voiceovers + one global script
# --------------------------------------------------------------------------- #


def _unit_words(rng: random.Random, count: int, duration: float) -> list[RecognizedWord]:
    """Realistic per-unit ASR: strictly inside the unit's own audio duration."""
    pool = LONG_SCRIPT_SENTENCE.split()
    budget = duration / max(1, count)
    words: list[RecognizedWord] = []
    for index in range(count):
        start = index * budget + rng.uniform(0.0, budget * 0.15)
        end = min(duration - 0.05, start + budget * rng.uniform(0.5, 0.8))
        if end <= start:
            continue
        words.append(RecognizedWord(
            rng.choice(pool).strip(".,"), round(start, 3), round(end, 3), rng.uniform(0.5, 1.0),
        ))
    return words


def test_global_script_over_twenty_voiceovers_yields_valid_long_form_and_shorts_cues(tmp_path):
    """20 voiceovers + one global script: Long-Form *and* every Short.

    A single Short aligns the complete global script against its own short
    voiceover, so compatibility is low and most script words receive fallback
    timing — exactly the run that failed on Windows.
    """
    rng = random.Random(20260903)
    unit_count = 20
    units = []
    for index in range(unit_count):
        duration = rng.uniform(4.0, 9.0)
        spoken = _unit_words(rng, rng.randint(9, 16), duration)
        units.append((tmp_path / f"voice_{index:03d}.wav", duration, spoken))
    global_script = (LONG_SCRIPT_SENTENCE * 3).strip()
    program_end = sum(duration for _path, duration, _words in units) + 0.7 * (unit_count - 1)

    def recognize(path, _language):
        for unit_path, _duration, spoken in units:
            if Path(path) == unit_path:
                return list(spoken), "de"
        raise AssertionError(f"unexpected voiceover {path}")

    aligner = LocalWordAligner("tiny", recognize, use_cache=False)

    # Long-Form: one global script over the complete concatenated timeline.
    combined = aligner.align_global(
        global_script,
        [(unit_path, duration) for unit_path, duration, _spoken in units],
        "German",
        0.7,
    )
    _assert_valid_word_timeline(combined.words)
    assert combined.words[-1].start < program_end
    long_cues = build_cues(
        global_script, combined, "long_1", program_end=program_end,
        width=1920, height=1080, font_key="modern_sans_bold",
    )
    _assert_complete_valid_cues(long_cues, combined.words, program_end=program_end)

    # Shorts: the same global script against each individual voiceover.
    for unit_path, duration, _spoken in units:
        per_unit = LocalWordAligner("tiny", recognize, use_cache=False).align(
            global_script, unit_path, "German", fallback_end=duration
        )
        assert [word.text for word in per_unit.words] == [
            token for token, _start, _end in script_word_spans(global_script)
        ]
        _assert_valid_word_timeline(per_unit.words)
        short_cues = build_cues(
            global_script, per_unit, "short_1", program_end=duration,
            width=1080, height=1920, font_key="inter",
        )
        _assert_complete_valid_cues(short_cues, per_unit.words, program_end=duration)


def test_seeded_fallback_heavy_alignments_never_produce_overlapping_cues():
    """Seeded property test over partially matching script/voiceover pairs."""
    pool = (LONG_SCRIPT_SENTENCE + "Aufmerksamkeit ist keine Technik, sondern eine Haltung. ").split()
    failures: list[str] = []
    for seed in range(90):
        rng = random.Random(seed)
        script_words = [rng.choice(pool).strip(".,") for _ in range(rng.randint(18, 70))]
        script = " ".join(script_words) + "."
        cursor = 0.0
        spoken = []
        for _ in range(rng.randint(10, 60)):
            duration = rng.uniform(0.05, 0.45)
            start = max(0.0, cursor + rng.uniform(-0.03, 0.25))
            spoken.append(RecognizedWord(
                rng.choice(pool).strip(".,") + (rng.choice(["", "", "."]) if rng.random() < 0.2 else ""),
                round(start, 3), round(start + duration, 3), rng.uniform(0.1, 1.0),
            ))
            cursor = start + duration * rng.uniform(0.5, 1.0)
        total = max(cursor, 0.5) + 1.0
        aligner = LocalWordAligner(
            "tiny", lambda _path, _language, _spoken=spoken: (_spoken, "de"), use_cache=False,
        )
        result = aligner.align(script, Path("voice.wav"), "German", fallback_end=total)
        try:
            _assert_valid_word_timeline(result.words)
            for preset, width, height in (("long_1", 1920, 1080), ("short_1", 1080, 1920)):
                cues = build_cues(script, result, preset, program_end=total, width=width, height=height)
                _assert_complete_valid_cues(cues, result.words, program_end=total)
        except (AssertionError, VideoMergerError) as exc:
            failures.append(f"seed {seed}: {exc}")
    assert failures == [], "\n".join(failures[:5])


# --------------------------------------------------------------------------- #
# Shorts without-replacement reservation hardening
# --------------------------------------------------------------------------- #


def test_unreserved_short_never_receives_the_complete_pool_without_a_shared_cursor(tmp_path):
    class ProbeEngine:
        ffprobe_path = tmp_path / "ffprobe"

    project = MainProjectEngine(ProbeEngine())
    media = [_media(f"/pool/clip_{index}.mp4") for index in range(3)]
    voices = []
    for index in range(3):
        path = tmp_path / f"voice_{index}.wav"
        path.write_bytes(b"audio")
        voices.append(path)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        subtitle_enabled=False,
        final_pause=0.0,
    )
    handed_out: list[tuple[list[Path], object]] = []

    def fake_create_main(job_media, job_settings, output_dir, **kwargs):
        handed_out.append(([item.path for item in job_media], kwargs.get("short_video_pool")))
        path = Path(output_dir) / f"{kwargs['output_stem']}.mp4"
        report = ValidationReport(
            True, [], path, duration=1.0, width=1080, height=1920, fps=30.0, has_video=True,
        )
        return MainVideoResult(path, None, None, report)

    project.create_main = fake_create_main  # type: ignore[method-assign]
    complete_pool = [item.path for item in media]

    # (a) successful preflight: every Short receives its own reserved prefix.
    with patch(
        "app.video_merger.main_project.probe_audio",
        side_effect=lambda _ffprobe, _path: SimpleNamespace(duration=1.0),
    ):
        project.create_youtube_exports(media, settings, tmp_path / "out_reserved")
    assert [paths for paths, _pool in handed_out] == [
        [Path("/pool/clip_0.mp4")], [Path("/pool/clip_1.mp4")], [Path("/pool/clip_2.mp4")],
    ]

    # (b) deferred preflight: every Short consumes ONE shared cursor, so the
    #     no-replacement rule still holds without the reservation step.
    handed_out.clear()
    with patch(
        "app.video_merger.main_project.probe_audio",
        side_effect=RuntimeError("ffprobe unavailable"),
    ):
        project.create_youtube_exports(media, settings, tmp_path / "out_deferred")
    pools = [pool for _paths, pool in handed_out]
    assert len(pools) == 3
    assert all(pool is not None for pool in pools)
    assert pools[0] is pools[1] is pools[2]

    # The invariant the hardening guarantees: a Short that is handed the
    # complete pool always carries a shared without-replacement cursor with it.
    for paths, pool in handed_out:
        assert pool is not None or paths != complete_pool
