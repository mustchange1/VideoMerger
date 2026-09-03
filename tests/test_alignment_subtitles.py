from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord, script_word_spans
from app.video_merger.models import AlignmentResult, WordTiming
from app.video_merger.subtitle_presets import SUBTITLE_PRESETS, default_preset, get_preset
from app.video_merger.subtitles import (
    SubtitleCue, build_cues, validate_cues, validate_subtitle_file,
    write_ass, write_srt, write_vtt,
)


def _recognizer(words, language="de"):
    def recognize(_path, _requested):
        return [RecognizedWord(text, start, end, confidence) for text, start, end, confidence in words], language
    return recognize


def test_exactly_five_long_and_five_short_presets_with_required_names():
    assert len(SUBTITLE_PRESETS) == 10
    assert [p.label for p in SUBTITLE_PRESETS[:5]] == [
        "LONG FORM 1 – Clean Editorial", "LONG FORM 2 – Documentary Box",
        "LONG FORM 3 – Minimal Cinematic", "LONG FORM 4 – Subtle Highlight",
        "LONG FORM 5 – Podcast / Interview",
    ]
    assert [p.label for p in SUBTITLE_PRESETS[5:]] == [
        "SHORT FORM 1 – Kinetic Chunk", "SHORT FORM 2 – Bold Highlight",
        "SHORT FORM 3 – Clean Pop", "SHORT FORM 4 – Karaoke Lite",
        "SHORT FORM 5 – Impact",
    ]
    assert default_preset("16:9") == "long_1"
    assert default_preset("9:16") == "short_1"


@pytest.mark.parametrize(
    "script,asr,language",
    [
        (
            "Übermäßig schöne Grüße, Köln!",
            [("Übermäßig", 0.10, 0.42, .95), ("schöne", .45, .72, .96),
             ("Grüße", .78, 1.08, .94), ("Köln", 1.12, 1.42, .93)],
            "de",
        ),
        (
            "It's John's well-made example, isn't it?",
            [("It's", .05, .25, .95), ("John's", .28, .55, .94),
             ("well", .58, .72, .91), ("made", .73, .90, .92),
             ("example", .92, 1.24, .96), ("isn't", 1.27, 1.50, .95),
             ("it", 1.52, 1.65, .93)],
            "en",
        ),
    ],
)
def test_script_wording_punctuation_umlauts_and_contractions_are_authoritative(tmp_path, script, asr, language):
    aligner = LocalWordAligner("tiny", _recognizer(asr, language))
    result = aligner.align(script, tmp_path / "unused.wav", "German" if language == "de" else "English")
    assert " ".join(word.text for word in result.words).replace(" well made", " well- made") != ""
    spans = script_word_spans(script)
    assert [word.text for word in result.words] == [token for token, _a, _b in spans]
    assert result.compatibility > 0.8
    assert all(a.start <= a.end <= b.end for a, b in zip(result.words, result.words[1:]))


def test_word_timestamps_follow_actual_recognized_boundaries_not_equal_division(tmp_path):
    script = "One two three four"
    measured = [
        ("One", .12, .20, .9), ("two", .72, .84, .9),
        ("three", .91, 1.50, .9), ("four", 2.30, 2.42, .9),
    ]
    result = LocalWordAligner("tiny", _recognizer(measured, "en")).align(
        script, tmp_path / "voice.wav", "English"
    )
    assert [round(w.start, 2) for w in result.words] == [.12, .72, .91, 2.30]
    assert result.words[2].end - result.words[2].start > result.words[0].end - result.words[0].start


def test_mismatch_retains_every_script_word_with_bounded_fallback_timestamps(tmp_path):
    result = LocalWordAligner(
        "tiny", _recognizer([("completely", .1, .4, .8), ("different", .5, .9, .8)], "en")
    ).align("Authoritative script words stay here.", tmp_path / "voice.wav", "English")
    assert result.compatibility < .72
    assert any("appear to differ" in warning for warning in result.warnings)
    assert [word.text for word in result.words] == [
        "Authoritative", "script", "words", "stay", "here.",
    ]
    assert all(0.1 <= word.start < word.end <= 0.9 for word in result.words)
    assert all(word.confidence == 0.0 for word in result.words)


def _perfect_alignment(script: str) -> AlignmentResult:
    spans = script_word_spans(script)
    words = [
        WordTiming(token, index * .31, index * .31 + .22, .98, start, end)
        for index, (token, start, end) in enumerate(spans)
    ]
    return AlignmentResult(words, "de", "test word timestamps", 1.0, .98)


@pytest.mark.parametrize("preset", [p.key for p in SUBTITLE_PRESETS])
def test_all_ten_presets_generate_resolution_relative_valid_ass(tmp_path, preset):
    script = "Dies ist ein ruhiger Satz. Danach folgt ein zweites gut lesbares Beispiel."
    alignment = _perfect_alignment(script)
    cues = build_cues(script, alignment, preset)
    ass_1080 = tmp_path / f"{preset}_1080.ass"
    ass_4k = tmp_path / f"{preset}_4k.ass"
    position = "Bottom" if preset.startswith("long") else "Medium-Low"
    write_ass(script, cues, ass_1080, preset, position, 1920, 1080)
    write_ass(script, cues, ass_4k, preset, position, 3840, 2160)
    text_1080 = ass_1080.read_text(encoding="utf-8-sig")
    text_4k = ass_4k.read_text(encoding="utf-8-sig")
    size_1080 = int(re.search(r"Style: Caption,Arial,(\d+)", text_1080).group(1))
    size_4k = int(re.search(r"Style: Caption,Arial,(\d+)", text_4k).group(1))
    assert size_4k == pytest.approx(size_1080 * 2, abs=1)
    assert text_1080.count("Dialogue:") >= len(alignment.words)
    assert "PlayResX: 1920" in text_1080 and "PlayResY: 1080" in text_1080


def test_srt_vtt_are_monotonic_valid_youtube_tracks_and_end_with_program(tmp_path):
    script = "Hallo Welt! Dies bleibt exakt geschrieben."
    alignment = _perfect_alignment(script)
    cues = build_cues(script, alignment, "long_1")
    srt, vtt = tmp_path / "MainVideo.srt", tmp_path / "MainVideo.vtt"
    write_srt(cues, srt)
    write_vtt(cues, vtt)
    validate_subtitle_file(srt, "srt")
    validate_subtitle_file(vtt, "vtt")
    assert "Hallo Welt!" in srt.read_text(encoding="utf-8")
    assert vtt.read_text(encoding="utf-8").startswith("WEBVTT")
    validate_cues(cues, len(alignment.words))
    assert cues[-1].end < 20


def test_empty_reliable_alignment_writes_valid_empty_sidecars(tmp_path):
    alignment = AlignmentResult([], "en", "no reliable matches", 0.0, 0.0)
    cues = build_cues("Nothing can be trusted here.", alignment, "long_1")
    assert cues == []
    srt, vtt = tmp_path / "empty.srt", tmp_path / "empty.vtt"
    write_srt(cues, srt)
    write_vtt(cues, vtt)
    validate_subtitle_file(srt, "srt")
    validate_subtitle_file(vtt, "vtt")
    assert srt.read_text(encoding="utf-8") == "\n"
    assert vtt.read_text(encoding="utf-8") == "WEBVTT\n\n"


def test_short_and_long_scripts_keep_every_word_without_orphans():
    for script in ("Kurz.", " ".join(f"Wort{index}" + ("." if index % 17 == 16 else "") for index in range(220))):
        alignment = _perfect_alignment(script)
        cues = build_cues(script, alignment, "long_1")
        validate_cues(cues, len(alignment.words))
        assert sum(len(cue.words) for cue in cues) == len(script_word_spans(script))
        assert all(len(cue.words) <= get_preset("long_1").max_words for cue in cues)


def test_subtitle_validator_rejects_overlap():
    word = WordTiming("test", 0, 1)
    with pytest.raises(Exception, match="Überlappende"):
        validate_cues([
            SubtitleCue(1, 0, 1, "A", [word]),
            SubtitleCue(2, .9, 1.5, "B", [word]),
        ])


@pytest.mark.e2e
def test_real_local_faster_whisper_german_word_timestamps(tmp_path):
    if os.environ.get("VIDEOMERGER_TEST_REAL_ALIGNMENT") != "1":
        pytest.skip("set VIDEOMERGER_TEST_REAL_ALIGNMENT=1 for downloaded local model test")
    espeak = shutil.which("espeak-ng")
    if not espeak:
        pytest.skip("espeak-ng not available")
    voice = tmp_path / "deutsch.wav"
    subprocess.run(
        [espeak, "-v", "de", "-s", "135", "-w", str(voice),
         "Das ist ein einfacher Test für deutsche Untertitel."],
        check=True, timeout=30,
    )
    model = os.environ.get("VIDEOMERGER_TEST_ALIGNMENT_MODEL", "tiny")
    result = LocalWordAligner(model).align(
        "Das ist ein einfacher Test für deutsche Untertitel.", voice, "German"
    )
    assert result.method.startswith(f"faster-whisper/{model}")
    assert len(result.words) == 8
    assert result.compatibility > .60
    assert all(word.end > word.start for word in result.words)
    assert result.words[-1].end > result.words[0].start + 1.5


@pytest.mark.e2e
def test_real_local_faster_whisper_english_word_timestamps(tmp_path):
    if os.environ.get("VIDEOMERGER_TEST_REAL_ALIGNMENT") != "1":
        pytest.skip("set VIDEOMERGER_TEST_REAL_ALIGNMENT=1 for downloaded local model test")
    espeak = shutil.which("espeak-ng")
    if not espeak:
        pytest.skip("espeak-ng not available")
    voice = tmp_path / "english.wav"
    subprocess.run(
        [espeak, "-v", "en", "-s", "135", "-w", str(voice),
         "This is a simple test for English subtitles."],
        check=True, timeout=30,
    )
    model = os.environ.get("VIDEOMERGER_TEST_ALIGNMENT_MODEL", "tiny")
    result = LocalWordAligner(model).align(
        "This is a simple test for English subtitles.", voice, "English"
    )
    assert result.language.startswith("en")
    assert len(result.words) == 8
    assert result.compatibility > .60
    assert all(result.words[index].start <= result.words[index + 1].start for index in range(7))
