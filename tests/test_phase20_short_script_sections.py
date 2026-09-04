"""Global script sections for individual Shorts + Main Video output modes.

Two contracts are pinned here.

1. Multiple voiceovers + ONE large global script::

       5 voiceovers + 1 global script
       = 1 Long-Form with the COMPLETE script over the complete timeline
       + 5 Shorts, each with ONLY the script section its own voiceover speaks

   The sections are derived acoustically from ONE global script mapping (the
   same ``align_global`` call the Long-Form uses, so the alignment cache makes
   the second request a hit). The complete global script is never aligned
   against an individual Short, the user never splits the script by hand, no
   valid script word is lost, and every Short's captions stay glued to its own
   voiceover. The multiple individual scripts workflow is untouched.

2. The selected YouTube output mode alone drives the Create Main Video action:
   Long-Form only, Shorts only, or BOTH from the same single action — with the
   same semantics for One-Click, and exactly one render per job.
"""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.video_merger.alignment import (
    LocalWordAligner,
    RecognizedWord,
    script_word_spans,
)
from app.video_merger.main_project import MainProjectEngine, global_script_path
from app.video_merger.models import (
    AudioInfo,
    CompleteWorkflowResult,
    ExportSettings,
    MainVideoResult,
    MediaInfo,
    ValidationReport,
    WordTiming,
)
from app.video_merger.script_sections import (
    script_section_path,
    split_global_script,
    unit_start_times,
)
from app.video_merger.subtitle_modes import (
    normalize_subtitle_output_mode,
    subtitle_render_requested,
)
from app.video_merger.subtitles import build_cues, validate_cues
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
    NO_SCRIPT_SECTION,
    build_short_jobs,
    short_settings,
)

# One global script whose three paragraphs are spoken by three voiceovers.
SECTIONS = [
    "Ruhe ist die erste Antwort des Tages.",
    "Bewegung folgt dem Gedanken langsam.",
    "Stille trägt das Ende dieser Geschichte.",
]
GLOBAL_SCRIPT = "\n\n".join(SECTIONS)
DURATIONS = [4.0, 3.5, 5.0]
PAUSE = 0.7
UNIT_STARTS = [0.0, 4.7, 8.9]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _private_section_directory(tmp_path, monkeypatch):
    """Keep derived section files out of the repository's own temp folder."""
    monkeypatch.setattr(
        "app.video_merger.script_sections.project_root", lambda: tmp_path / "project"
    )
    return tmp_path / "project" / "temp" / "script_sections"


def _spoken(section: str, duration: float) -> list[RecognizedWord]:
    """Realistic per-unit ASR strictly inside that unit's own audio duration."""
    tokens = section.split()
    slot = duration / max(1, len(tokens))
    return [
        RecognizedWord(
            token.strip(".,"), round(index * slot + 0.1, 3),
            round(min(duration - 0.05, index * slot + 0.1 + slot * 0.8), 3), 0.95,
        )
        for index, token in enumerate(tokens)
    ]


def _project(tmp_path: Path, spoken: dict[str, list[RecognizedWord]]):
    """Create the voiceover files, the global script and a matching recognizer."""
    voices: list[Path] = []
    for index in range(len(spoken)):
        path = tmp_path / f"voice_{index + 1}.wav"
        path.write_bytes(b"audio")
        voices.append(path)
    recognized = {
        str(path.resolve()): words for path, words in zip(voices, spoken.values())
    }

    def recognize(path, _language):
        key = str(Path(path).expanduser().resolve())
        if key not in recognized:
            raise AssertionError(f"unexpected voiceover {path}")
        return list(recognized[key]), "de"

    script = tmp_path / "global_script.txt"
    script.write_text(GLOBAL_SCRIPT, encoding="utf-8")
    return voices, script, recognize


def _settings(
    tmp_path: Path,
    voices: list[Path],
    script: Path,
    mode: str,
    *,
    script_mode: str = "single",
    script_paths: list[Path] | None = None,
    subtitle_output_mode: str = "with_subtitles",
) -> ExportSettings:
    return ExportSettings(
        export_mode=mode,
        voiceover_paths=[str(path) for path in voices],
        voiceover_path=str(voices[0]) if voices else "",
        script_mode=script_mode,
        global_script_path=str(script) if script_mode == "single" else "",
        script_paths=[str(path) for path in (script_paths or ([script] if script_mode == "single" else []))],
        script_path=str(script) if script_mode == "single" else "",
        subtitle_enabled=True,
        subtitle_output_mode=subtitle_output_mode,
        subtitle_language="German",
        subtitle_style="long_3",
        subtitle_font="modern_sans_bold",
        subtitle_position="Bottom Center",
        short_subtitle_style="short_2",
        short_subtitle_font="inter",
        short_subtitle_animation="word_highlight",
        short_subtitle_position="Top Center",
        voiceover_pause=PAUSE,
        final_pause=0.0,
    )


def _media(path: str, duration: float = 3.0) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=1920, height=1080,
        effective_width=1920, effective_height=1080, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="", audio=AudioInfo(),
    )


def _audio(_ffprobe, path):
    """Stand-in for probe_audio: durations come from the unit's own name."""
    index = int(Path(path).stem.rsplit("_", 1)[-1]) - 1
    return SimpleNamespace(
        path=Path(path).expanduser().resolve(),
        duration=DURATIONS[index],
        sample_rate=48000, channels=2, codec="pcm_s16le",
    )


def _run_export(
    tmp_path: Path,
    settings: ExportSettings,
    *,
    recognize=None,
    aligner=None,
    engine=None,
    media=None,
    complete: bool = False,
    output: str = "output",
):
    """Run the real orchestrator with a fake Stage-1 and record every job."""
    if aligner is None:
        aligner = CountingAligner("tiny", recognize, use_cache=False)
    project = MainProjectEngine(engine or SimpleNamespace(ffprobe_path=tmp_path / "ffprobe"))
    captured: list[SimpleNamespace] = []
    logs: list[str] = []

    def fake_stage1(job_media, job_settings, job_dir, **kwargs):
        stem = kwargs["output_stem"]
        path = Path(job_dir) / f"{stem}.mp4"
        report = ValidationReport(
            True, [], path, duration=1.0,
            width=1920 if job_settings.aspect == "16:9" else 1080,
            height=1080 if job_settings.aspect == "16:9" else 1920,
            fps=30.0, has_video=True,
        )
        captured.append(SimpleNamespace(
            stem=stem, settings=job_settings, directory=Path(job_dir),
            media=[item.path for item in job_media], kwargs=kwargs,
        ))
        return MainVideoResult(path, None, None, report)

    if complete:
        def fake_complete(job_media, job_settings, job_dir, **kwargs):
            main = fake_stage1(job_media, job_settings, job_dir, **kwargs)
            return CompleteWorkflowResult(main, main.video, main.report)

        project._create_complete_single = fake_complete  # type: ignore[method-assign]
    else:
        project.create_main = fake_stage1  # type: ignore[method-assign]

    with patch("app.video_merger.main_project.probe_audio", side_effect=_audio):
        result = project.create_youtube_exports(
            media if media is not None else [], settings, tmp_path / output,
            aligner=aligner, log=logs.append, complete=complete,
        )
    return SimpleNamespace(result=result, jobs=captured, logs=logs, aligner=aligner)


class CountingAligner(LocalWordAligner):
    """Counts global mappings so 'derived once' stays provable."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.global_calls: list[tuple] = []

    def align_global(self, script, units, language="German", inter_unit_pause=0.7):
        self.global_calls.append((script, tuple(units), language, inter_unit_pause))
        return super().align_global(script, units, language, inter_unit_pause)


def _long_form(jobs) -> SimpleNamespace:
    return next(job for job in jobs if job.settings.aspect == "16:9")


def _shorts(jobs) -> list[SimpleNamespace]:
    return [job for job in jobs if job.settings.aspect == "9:16"]


def _words(text: str) -> list[str]:
    return [token for token, _start, _end in script_word_spans(text)]


def _assert_valid_cues(cues, words, program_end=None):
    """The unchanged subtitle timeline contract (see test_phase19)."""
    validate_cues(cues, len(words))
    assert all(cue.start < cue.end for cue in cues), [(c.index, c.start, c.end) for c in cues]
    assert all(left.end <= right.start for left, right in zip(cues, cues[1:]))
    assert all(left.start <= right.start for left, right in zip(cues, cues[1:]))
    assert [word.text for cue in cues for word in cue.words] == [word.text for word in words]
    if program_end is not None:
        assert cues[-1].end <= program_end + 0.001


# --------------------------------------------------------------------------- #
# 1a. The pure section split
# --------------------------------------------------------------------------- #


def _word(text: str, start: float, script_start: int, script_end: int) -> WordTiming:
    return WordTiming(
        text=text, start=start, end=start + 0.2, confidence=0.9,
        script_start=script_start, script_end=script_end,
    )


def test_unit_start_times_add_exactly_one_pause_between_units():
    assert unit_start_times([4.0, 3.5, 5.0], PAUSE) == pytest.approx(UNIT_STARTS)
    assert unit_start_times([4.0], PAUSE) == pytest.approx([0.0])
    assert unit_start_times([2.0, 2.0], 0.0) == pytest.approx([0.0, 2.0])
    assert unit_start_times([], PAUSE) == []


def test_split_global_script_gives_every_voiceover_its_own_contiguous_section():
    spans = script_word_spans(GLOBAL_SCRIPT)
    starts = [0.10, 0.67, 1.24, 1.81, 2.39, 2.96, 3.53,
              4.80, 5.50, 6.20, 6.90, 7.60,
              9.00, 9.83, 10.67, 11.50, 12.33, 13.17]
    words = [
        _word(token, moment, char_start, char_end)
        for (token, char_start, char_end), moment in zip(spans, starts)
    ]
    assert split_global_script(GLOBAL_SCRIPT, words, DURATIONS, PAUSE) == SECTIONS


def test_split_global_script_keeps_a_pause_word_with_the_preceding_speech():
    spans = script_word_spans("Eins Zwei Drei Vier")
    # "Drei" is spoken inside the pause after unit 0, "Vier" inside unit 1.
    moments = [0.20, 1.00, 4.20, 5.00]
    words = [
        _word(token, moment, char_start, char_end)
        for (token, char_start, char_end), moment in zip(spans, moments)
    ]
    assert split_global_script("Eins Zwei Drei Vier", words, [4.0, 4.0], PAUSE) == [
        "Eins Zwei Drei", "Vier",
    ]


def test_split_global_script_marks_a_voiceover_without_script_words_as_empty():
    text = "Alpha Bravo Charlie Delta Echo."
    spans = script_word_spans(text)
    # Everything is spoken by the third voiceover only.
    moments = [9.10, 9.60, 10.10, 10.60, 11.10]
    words = [
        _word(token, moment, char_start, char_end)
        for (token, char_start, char_end), moment in zip(spans, moments)
    ]
    assert split_global_script(text, words, DURATIONS, PAUSE) == ["", "", text]
    assert split_global_script(text, [], DURATIONS, PAUSE) == ["", "", ""]
    assert split_global_script("", words, DURATIONS, PAUSE) == ["", "", ""]
    assert split_global_script(text, words, [], PAUSE) == []


def test_split_global_script_never_loses_or_duplicates_a_script_word():
    """Seeded property run: the sections are a lossless partition of the script."""
    rng = random.Random(20260904)
    for _attempt in range(60):
        unit_count = rng.randint(1, 6)
        durations = [round(rng.uniform(1.0, 8.0), 3) for _ in range(unit_count)]
        pause = rng.choice([0.0, 0.3, 0.7, 1.5])
        text = " ".join(f"Wort{index}" for index in range(rng.randint(3, 60))) + "."
        spans = script_word_spans(text)
        starts = unit_start_times(durations, pause)
        total = starts[-1] + durations[-1]
        moments = sorted(round(rng.uniform(0.0, total), 3) for _ in spans)
        words = [
            _word(token, moment, char_start, char_end)
            for (token, char_start, char_end), moment in zip(spans, moments)
        ]
        sections = split_global_script(text, words, durations, pause)
        assert len(sections) == unit_count
        # Every script word appears exactly once, in the authoritative order.
        assert [token for section in sections for token in _words(section)] == _words(text)
        # Each section is one contiguous slice of the original script text.
        cursor = 0
        for section in sections:
            if not section:
                continue
            position = text.index(section, cursor)
            assert position >= cursor
            cursor = position + len(section)


def test_split_global_script_groups_units_that_share_one_voiceover_file():
    text = "Eins Zwei Drei Vier"
    spans = script_word_spans(text)
    # One file plays twice in a row, then a second file speaks the last word.
    moments = [0.20, 5.20, 6.00, 10.00]
    words = [
        _word(token, moment, char_start, char_end)
        for (token, char_start, char_end), moment in zip(spans, moments)
    ]
    durations = [4.0, 4.0, 4.0]
    # Without grouping every timeline slot keeps its own slice.
    assert split_global_script(text, words, durations, PAUSE) == ["Eins", "Zwei Drei", "Vier"]
    # Grouped by identity, both slots of the same file share one section that
    # stays in the authoritative script order.
    assert split_global_script(
        text, words, durations, PAUSE, unit_keys=["a", "a", "b"],
    ) == ["Eins Zwei Drei", "Eins Zwei Drei", "Vier"]
    # A mismatched key list is ignored instead of shifting any section.
    assert split_global_script(
        text, words, durations, PAUSE, unit_keys=["a", "a"],
    ) == ["Eins", "Zwei Drei", "Vier"]


def test_script_section_path_is_content_addressed_and_keeps_a_stable_mtime(tmp_path):
    first = script_section_path("Ruhe ist die erste Antwort.", "Short_001", tmp_path)
    stamp = first.stat().st_mtime_ns
    assert first.read_text(encoding="utf-8") == "Ruhe ist die erste Antwort."
    assert first.name.startswith("Short_001_") and first.suffix == ".txt"
    # A repeated run must not create a new path or a new mtime: both are part of
    # the Stage-1 render cache fingerprint and would repeat the whole alignment.
    again = script_section_path("Ruhe ist die erste Antwort.", "Short_001", tmp_path)
    assert again == first
    assert again.stat().st_mtime_ns == stamp
    other = script_section_path("Bewegung folgt dem Gedanken.", "Short_001", tmp_path)
    assert other != first
    assert other.read_text(encoding="utf-8") == "Bewegung folgt dem Gedanken."


# --------------------------------------------------------------------------- #
# 1b. One global script across the real orchestrator
# --------------------------------------------------------------------------- #


def test_long_form_keeps_the_complete_script_while_each_short_gets_its_own_section(tmp_path):
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_COMBINED)
    run = _run_export(tmp_path, settings, recognize=recognize,
                      media=[_media(f"/pool/clip_{index}.mp4") for index in range(3)])

    long_form = _long_form(run.jobs)
    assert long_form.stem == "YouTube_LongForm"
    # The Long-Form still uses the COMPLETE global script over all voiceovers.
    assert global_script_path(long_form.settings) == script.resolve()
    assert [Path(item).name for item in long_form.settings.voiceover_paths] == [
        "voice_1.wav", "voice_2.wav", "voice_3.wav",
    ]
    assert long_form.settings.voiceover_pause == pytest.approx(PAUSE)
    assert long_form.settings.subtitle_style == "long_3"

    shorts = _shorts(run.jobs)
    assert [job.stem for job in shorts] == ["001", "002", "003"]
    for index, job in enumerate(shorts):
        section_path = global_script_path(job.settings)
        assert section_path is not None
        # Not the complete global script any more …
        assert section_path != script.resolve()
        assert section_path.is_file()
        # … but exactly this voiceover's own section.
        assert section_path.read_text(encoding="utf-8") == SECTIONS[index]
        assert job.settings.voiceover_paths == [str(voices[index].resolve())]
        assert job.settings.voiceover_pause == pytest.approx(0.0)

    # The complete script is preserved across the Shorts: no word is dropped,
    # duplicated or re-ordered, and the Long-Form still carries all of them.
    per_short = [
        _words(global_script_path(job.settings).read_text(encoding="utf-8")) for job in shorts
    ]
    assert [token for section in per_short for token in section] == _words(GLOBAL_SCRIPT)
    assert all(section for section in per_short)


def test_shorts_only_mode_uses_the_same_sections_without_a_long_form_job(tmp_path):
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_SHORTS)
    run = _run_export(tmp_path, settings, recognize=recognize)

    assert run.result.long_form is None
    assert len(run.result.shorts) == 3
    shorts = _shorts(run.jobs)
    assert [job.stem for job in shorts] == ["001", "002", "003"]
    assert [
        global_script_path(job.settings).read_text(encoding="utf-8") for job in shorts
    ] == SECTIONS
    # The global mapping still happens exactly once for the whole run.
    assert len(run.aligner.global_calls) == 1


def test_global_script_is_mapped_once_and_never_against_an_individual_short(tmp_path):
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_COMBINED)
    run = _run_export(tmp_path, settings, recognize=recognize)

    # ONE global mapping for the complete run, with exactly the arguments the
    # Long-Form job uses — so its own align_global call is a cache hit and no
    # voiceover is transcribed twice.
    assert len(run.aligner.global_calls) == 1
    mapped_script, units, language, pause = run.aligner.global_calls[0]
    assert mapped_script == GLOBAL_SCRIPT
    assert [Path(path).name for path, _duration in units] == [
        "voice_1.wav", "voice_2.wav", "voice_3.wav",
    ]
    assert [duration for _path, duration in units] == pytest.approx(DURATIONS)
    assert language == "German"
    assert pause == pytest.approx(PAUSE)

    # No Short ever receives the complete global script.
    for job in _shorts(run.jobs):
        assert global_script_path(job.settings) != script.resolve()
        assert len(_words(global_script_path(job.settings).read_text(encoding="utf-8"))) < len(
            _words(GLOBAL_SCRIPT)
        )
    assert any("Global script sections for Shorts" in line for line in run.logs)


def test_each_short_section_is_captioned_from_its_own_voiceover_timing(tmp_path):
    """Subtitle timing stays synchronized to the corresponding voiceover."""
    spoken = {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    voices, script, recognize = _project(tmp_path, spoken)
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_COMBINED)
    run = _run_export(tmp_path, settings, recognize=recognize)

    for index, job in enumerate(_shorts(run.jobs)):
        text = global_script_path(job.settings).read_text(encoding="utf-8")
        duration = DURATIONS[index]
        aligner = LocalWordAligner("tiny", recognize, use_cache=False)
        alignment = aligner.align(
            text, Path(job.settings.voiceover_paths[0]), "German", fallback_end=duration,
        )
        # The section is exactly what this voiceover says: every word is a real
        # acoustic anchor, no bounded fallback timing is needed any more.
        assert alignment.compatibility == pytest.approx(1.0)
        assert all(word.confidence > 0.0 for word in alignment.words)
        assert [word.text for word in alignment.words] == _words(text)
        assert alignment.words[-1].start < duration
        cues = build_cues(
            text, alignment, job.settings.subtitle_style, program_end=duration,
            width=1080, height=1920, font_key=job.settings.subtitle_font,
        )
        _assert_valid_cues(cues, alignment.words, program_end=duration)
        assert cues, "a Short with a spoken section must be captioned"

    # The Long-Form captions the complete script over the complete timeline.
    long_form = _long_form(run.jobs)
    combined = run.aligner.align_global(
        GLOBAL_SCRIPT, list(zip(voices, DURATIONS)), "German", PAUSE,
    )
    program_end = sum(DURATIONS) + PAUSE * (len(DURATIONS) - 1)
    assert [word.text for word in combined.words] == _words(GLOBAL_SCRIPT)
    long_cues = build_cues(
        GLOBAL_SCRIPT, combined, long_form.settings.subtitle_style, program_end=program_end,
        width=1920, height=1080, font_key=long_form.settings.subtitle_font,
    )
    _assert_valid_cues(long_cues, combined.words, program_end=program_end)
    assert combined.words[-1].end <= program_end


def test_short_and_long_form_subtitle_profiles_stay_separate_with_sections(tmp_path):
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_COMBINED)
    run = _run_export(tmp_path, settings, recognize=recognize)

    long_form = _long_form(run.jobs)
    for job in _shorts(run.jobs):
        assert (long_form.settings.aspect, job.settings.aspect) == ("16:9", "9:16")
        assert long_form.settings.output_preset == "youtube_landscape"
        assert job.settings.output_preset == "youtube_vertical"
        assert long_form.settings.subtitle_style.startswith("long")
        assert job.settings.subtitle_style == "short_2"
        assert job.settings.subtitle_font == "inter"
        assert job.settings.subtitle_animation == "word_highlight"
        assert job.settings.subtitle_position == "Top Center"
        assert job.settings.render_variant_key != long_form.settings.render_variant_key


def test_multiple_individual_scripts_workflow_is_untouched(tmp_path):
    """Matched scripts keep their own per-voiceover assignment; no sections."""
    voices: list[Path] = []
    scripts: list[Path] = []
    for index, section in enumerate(SECTIONS, start=1):
        voice = tmp_path / f"voice_{index}.wav"
        voice.write_bytes(b"audio")
        voices.append(voice)
        own = tmp_path / f"voice_{index}.txt"
        own.write_text(section, encoding="utf-8")
        scripts.append(own)
    settings = _settings(
        tmp_path, voices, scripts[0], EXPORT_MODE_COMBINED,
        script_mode="matched", script_paths=scripts,
    )
    recognize = {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    run = _run_export(
        tmp_path, settings,
        recognize=lambda path, _language: (
            list(recognize[SECTIONS[int(Path(path).stem[-1]) - 1]]), "de"
        ),
    )

    assert run.aligner.global_calls == []
    assert not any("Global script sections" in line for line in run.logs)
    shorts = _shorts(run.jobs)
    assert [job.stem for job in shorts] == ["001", "002", "003"]
    for index, job in enumerate(shorts):
        # Matched mode never uses a global script field: each Short keeps the
        # script that was assigned to its own voiceover row.
        assert job.settings.global_script_path == ""
        assert job.settings.script_paths == [str(scripts[index].resolve())]
        assert job.settings.script_path == str(scripts[index].resolve())
        assert job.settings.script_mode == "matched"
        assert job.settings.subtitle_enabled is True
        assert global_script_path(job.settings) == scripts[index].resolve()
    assert [Path(item).name for item in _long_form(run.jobs).settings.script_paths] == [
        "voice_1.txt", "voice_2.txt", "voice_3.txt",
    ]


def test_voiceover_that_speaks_no_script_section_becomes_an_audio_only_short(tmp_path):
    """A Short never captions text its own voiceover does not say."""
    text = "Alpha Bravo Charlie Delta Echo."
    spoken = {
        "one": _spoken("One Two Three Four Five Six.", DURATIONS[0]),
        "two": _spoken("Rot Grün Blau Gelb Schwarz Weiß.", DURATIONS[1]),
        "three": _spoken(text, DURATIONS[2]),
    }
    voices: list[Path] = []
    for index in range(3):
        path = tmp_path / f"voice_{index + 1}.wav"
        path.write_bytes(b"audio")
        voices.append(path)
    recognized = {
        str(voices[0].resolve()): spoken["one"],
        str(voices[1].resolve()): spoken["two"],
        str(voices[2].resolve()): spoken["three"],
    }
    script = tmp_path / "global_script.txt"
    script.write_text(text, encoding="utf-8")
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_COMBINED)
    run = _run_export(
        tmp_path, settings,
        recognize=lambda path, _language: (
            list(recognized[str(Path(path).resolve())]), "de"
        ),
    )

    first, second, third = _shorts(run.jobs)
    for job in (first, second):
        # No invented captions: no script at all for this voiceover …
        assert global_script_path(job.settings) is None
        assert job.settings.script_paths == []
        assert job.settings.script_path == ""
        assert job.settings.subtitle_enabled is False
        # … which is exactly create_main's audio-only path, not an abort. The
        # Single Global Script mode only fails when subtitles are requested
        # without a script.
        source_requested = bool(job.settings.subtitle_enabled or global_script_path(job.settings))
        assert not subtitle_render_requested(
            normalize_subtitle_output_mode(job.settings.subtitle_output_mode), source_requested
        )
    assert global_script_path(third.settings).read_text(encoding="utf-8") == text
    # The Long-Form is unaffected and still captions the complete script.
    assert global_script_path(_long_form(run.jobs).settings) == script.resolve()
    assert any("speaks no part of the global script" in line for line in run.logs)


def test_single_voiceover_project_keeps_the_complete_global_script(tmp_path):
    voices, script, recognize = _project(tmp_path, {SECTIONS[0]: _spoken(SECTIONS[0], DURATIONS[0])})
    settings = _settings(tmp_path, voices[:1], script, EXPORT_MODE_COMBINED)
    run = _run_export(tmp_path, settings, recognize=recognize)

    # One voiceover speaks the complete script: nothing to derive, no extra
    # global mapping, and no derived section file.
    assert run.aligner.global_calls == []
    shorts = _shorts(run.jobs)
    assert len(shorts) == 1
    assert global_script_path(shorts[0].settings) == script.resolve()
    assert shorts[0].settings.subtitle_enabled is True


def test_sections_fall_back_to_the_previous_behaviour_when_not_derivable(tmp_path):
    """Any derivation problem degrades to today's behaviour, never to a crash."""
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_COMBINED)

    class NoGlobalMapping(LocalWordAligner):
        """A third-party aligner without any global script mapping."""

        @property
        def align_global(self):
            raise AttributeError("align_global")

    aligner = NoGlobalMapping("tiny", recognize, use_cache=False)
    assert not hasattr(aligner, "align_global")
    run = _run_export(tmp_path, settings, aligner=aligner, output="out_fallback")
    for job in _shorts(run.jobs):
        assert global_script_path(job.settings) == script.resolve()
    assert any("keeps the complete global script" in line for line in run.logs)

    # An engine that cannot probe audio degrades the same way.
    run = _run_export(
        tmp_path, settings, recognize=recognize, engine=object(), output="out_no_probe",
    )
    for job in _shorts(run.jobs):
        assert global_script_path(job.settings) == script.resolve()
    assert any("keeps the complete global script" in line for line in run.logs)


def test_without_subtitles_never_derives_sections(tmp_path):
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    settings = _settings(
        tmp_path, voices, script, EXPORT_MODE_SHORTS, subtitle_output_mode="without_subtitles",
    )
    run = _run_export(tmp_path, settings, recognize=recognize)
    assert run.aligner.global_calls == []
    for job in _shorts(run.jobs):
        assert global_script_path(job.settings) == script.resolve()


def test_missing_voiceover_file_keeps_its_previous_configuration(tmp_path):
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    (tmp_path / "voice_2.wav").unlink()
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_SHORTS)
    run = _run_export(tmp_path, settings, recognize=recognize)

    shorts = _shorts(run.jobs)
    assert [job.stem for job in shorts] == ["001", "002", "003"]
    # The two remaining units form the acoustic timeline for the sections, so
    # the middle paragraph (which no voiceover speaks) stays with the preceding
    # speech instead of being dropped.
    first_section = global_script_path(shorts[0].settings).read_text(encoding="utf-8")
    assert _words(first_section) == _words(SECTIONS[0] + " " + SECTIONS[1])
    assert global_script_path(shorts[2].settings).read_text(encoding="utf-8") == SECTIONS[2]
    assert [
        token for job in (shorts[0], shorts[2])
        for token in _words(global_script_path(job.settings).read_text(encoding="utf-8"))
    ] == _words(GLOBAL_SCRIPT)
    # The unavailable voiceover is not part of that timeline: it keeps the
    # project's own configuration instead of a silently shifted section.
    assert global_script_path(shorts[1].settings) == script.resolve()


def test_one_voiceover_used_by_several_rows_shares_one_section(tmp_path):
    """Duplicate audio rows are separate jobs but speak the same script part."""
    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    rows = [voices[0], voices[0], voices[1], voices[2]]
    settings = _settings(tmp_path, rows, script, EXPORT_MODE_SHORTS)
    run = _run_export(tmp_path, settings, recognize=recognize)

    shorts = _shorts(run.jobs)
    assert [job.stem for job in shorts] == ["001", "002", "003", "004"]
    assert [
        global_script_path(job.settings).read_text(encoding="utf-8") for job in shorts
    ] == [SECTIONS[0], SECTIONS[0], SECTIONS[1], SECTIONS[2]]
    # Every job still renders independently with its own cache identity.
    assert len({job.settings.render_variant_key for job in shorts}) == 4


def test_short_settings_section_argument_is_explicit_and_backwards_compatible(tmp_path):
    voices, script, _recognize = _project(tmp_path, {section: [] for section in SECTIONS})
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_SHORTS)
    job = build_short_jobs(settings)[0]

    default = short_settings(settings, job)
    assert global_script_path(default) == script.resolve()
    assert default.subtitle_enabled is True

    section = tmp_path / "section.txt"
    section.write_text(SECTIONS[1], encoding="utf-8")
    with_section = short_settings(settings, job, section)
    assert global_script_path(with_section) == section.resolve()
    assert with_section.script_paths == [str(section.resolve())]
    assert with_section.subtitle_enabled is True

    without = short_settings(settings, job, NO_SCRIPT_SECTION)
    assert without.global_script_path == ""
    assert without.script_paths == []
    assert without.subtitle_enabled is False
    # The vertical Shorts profile is identical in all three cases.
    for candidate in (default, with_section, without):
        assert candidate.aspect == "9:16"
        assert candidate.subtitle_style == "short_2"
        assert candidate.voiceover_pause == pytest.approx(0.0)


def test_repeated_runs_reuse_the_same_section_files_so_the_cache_stays_valid(tmp_path):
    """The derived section is a stable Stage-1 input, run after run.

    The section file is fingerprinted like any other script input (path, size,
    mtime and content digest), so a fresh path or a fresh mtime on every run
    would silently disable the Main Video render cache and repeat the voiceover
    alignment each time.
    """
    from app.video_merger.render_cache import file_signature

    voices, script, recognize = _project(tmp_path, {name: _spoken(name, duration)
                                                    for name, duration in zip(SECTIONS, DURATIONS)})
    settings = _settings(tmp_path, voices, script, EXPORT_MODE_COMBINED)
    runs = [
        _run_export(tmp_path, settings, recognize=recognize, output=f"run_{index}")
        for index in (1, 2)
    ]
    signatures = [
        [
            file_signature(global_script_path(job.settings), content_hash=True)
            for job in _shorts(run.jobs)
        ]
        for run in runs
    ]
    assert signatures[0] == signatures[1]
    assert len(signatures[0]) == 3
    # The Long-Form keeps pointing at the user's own global script file.
    for run in runs:
        assert global_script_path(_long_form(run.jobs).settings) == script.resolve()


def test_sections_survive_a_seeded_multi_voiceover_global_script_run(tmp_path):
    """Seeded runs: sections stay a lossless partition and caption cleanly."""
    rng = random.Random(4242)
    for case in range(12):
        unit_count = rng.randint(2, 5)
        durations = [round(rng.uniform(2.0, 7.0), 3) for _ in range(unit_count)]
        sections = [
            " ".join(f"{case}_{unit}_{index}" for index in range(rng.randint(4, 12))) + "."
            for unit in range(unit_count)
        ]
        text = "\n\n".join(sections)
        voices: list[Path] = []
        recognized: dict[str, list[RecognizedWord]] = {}
        for index, (section, duration) in enumerate(zip(sections, durations), start=1):
            path = tmp_path / f"case{case}_voice_{index}.wav"
            path.write_bytes(b"audio")
            voices.append(path)
            recognized[str(path.resolve())] = _spoken(section, duration)
        script = tmp_path / f"case{case}_global.txt"
        script.write_text(text, encoding="utf-8")
        settings = ExportSettings(
            export_mode=EXPORT_MODE_COMBINED,
            voiceover_paths=[str(path) for path in voices],
            script_mode="single",
            global_script_path=str(script),
            script_paths=[str(script)],
            subtitle_enabled=True,
            subtitle_language="German",
            voiceover_pause=round(rng.uniform(0.0, 1.2), 3),
            final_pause=0.0,
        )
        with patch(
            "app.video_merger.main_project.probe_audio",
            side_effect=lambda _ffprobe, path, _d=durations, _v=voices: SimpleNamespace(
                path=Path(path).expanduser().resolve(),
                duration=_d[_v.index(Path(path).expanduser().resolve())],
                sample_rate=48000, channels=2, codec="pcm_s16le",
            ),
        ):
            aligner = CountingAligner(
                "tiny",
                lambda path, _language, _r=recognized: (list(_r[str(Path(path).resolve())]), "de"),
                use_cache=False,
            )
            project = MainProjectEngine(SimpleNamespace(ffprobe_path=tmp_path / "ffprobe"))
            captured: list[ExportSettings] = []

            def fake_stage1(job_media, job_settings, job_dir, _captured=captured, **kwargs):
                _captured.append(job_settings)
                path = Path(job_dir) / f"{kwargs['output_stem']}.mp4"
                report = ValidationReport(
                    True, [], path, duration=1.0, width=1080, height=1920, fps=30.0, has_video=True,
                )
                return MainVideoResult(path, None, None, report)

            project.create_main = fake_stage1  # type: ignore[method-assign]
            project.create_youtube_exports(
                [], settings, tmp_path / f"out_case_{case}", aligner=aligner, log=lambda _m: None,
            )

        assert len(aligner.global_calls) == 1, case
        short_settings_captured = [item for item in captured if item.aspect == "9:16"]
        assert len(short_settings_captured) == unit_count, case
        derived = [
            global_script_path(item).read_text(encoding="utf-8") for item in short_settings_captured
        ]
        assert derived == sections, case
        assert [token for section in derived for token in _words(section)] == _words(text), case
        for item, section, duration in zip(short_settings_captured, derived, durations):
            alignment = LocalWordAligner(
                "tiny",
                lambda path, _language, _r=recognized: (list(_r[str(Path(path).resolve())]), "de"),
                use_cache=False,
            ).align(section, Path(item.voiceover_paths[0]), "German", fallback_end=duration)
            assert alignment.compatibility == pytest.approx(1.0), case
            cues = build_cues(
                section, alignment, item.subtitle_style, program_end=duration,
                width=1080, height=1920,
            )
            _assert_valid_cues(cues, alignment.words, program_end=duration)


# --------------------------------------------------------------------------- #
# 2. The Create Main Video action follows the selected output mode exactly
# --------------------------------------------------------------------------- #


def _mode_settings(tmp_path: Path, mode: str, count: int = 3) -> ExportSettings:
    voices: list[Path] = []
    for index in range(count):
        path = tmp_path / f"voice_{index + 1}.wav"
        path.write_bytes(b"audio")
        voices.append(path)
    script = tmp_path / "global_script.txt"
    script.write_text(GLOBAL_SCRIPT, encoding="utf-8")
    return _settings(tmp_path, voices, script, mode)


def test_main_video_long_form_only_renders_exactly_one_long_form_output(tmp_path):
    settings = _mode_settings(tmp_path, EXPORT_MODE_LONG_FORM)
    run = _run_export(tmp_path, settings, output="out_long")

    assert run.result.mode == EXPORT_MODE_LONG_FORM
    assert run.result.long_form is not None
    assert run.result.shorts == []
    assert len(run.jobs) == 1
    job = run.jobs[0]
    assert job.stem == "YouTube_LongForm"
    assert job.directory == tmp_path / "out_long" / "LongForm"
    assert job.settings.aspect == "16:9"
    assert job.settings.export_mode == EXPORT_MODE_LONG_FORM
    assert run.result.primary_output == tmp_path / "out_long" / "LongForm" / "YouTube_LongForm.mp4"
    # A Long-Form-only run needs no Shorts clip pool and no sections.
    assert job.kwargs.get("short_video_pool") is None
    assert run.aligner.global_calls == []


def test_main_video_shorts_only_renders_one_short_per_voiceover(tmp_path):
    settings = _mode_settings(tmp_path, EXPORT_MODE_SHORTS, count=5)
    run = _run_export(tmp_path, settings, output="out_shorts")

    assert run.result.mode == EXPORT_MODE_SHORTS
    assert run.result.long_form is None
    assert len(run.result.shorts) == 5
    assert len(run.jobs) == 5
    assert [job.stem for job in run.jobs] == [f"{index:03d}" for index in range(1, 6)]
    assert all(job.directory == tmp_path / "out_shorts" / "Shorts" for job in run.jobs)
    assert all(job.settings.aspect == "9:16" for job in run.jobs)
    assert all(job.settings.export_mode == EXPORT_MODE_SHORTS for job in run.jobs)
    assert len({job.settings.render_variant_key for job in run.jobs}) == 5
    assert run.result.primary_output == tmp_path / "out_shorts" / "Shorts" / "001.mp4"


def test_main_video_combined_renders_both_outputs_in_one_single_action(tmp_path):
    settings = _mode_settings(tmp_path, EXPORT_MODE_COMBINED, count=3)
    run = _run_export(tmp_path, settings, output="out_both")

    assert run.result.mode == EXPORT_MODE_COMBINED
    # BOTH outputs come from the same action: 1 Long-Form + 1 Short per voiceover.
    assert run.result.long_form is not None
    assert len(run.result.shorts) == 3
    assert len(run.jobs) == 4
    assert [job.stem for job in run.jobs] == ["YouTube_LongForm", "001", "002", "003"]
    assert run.jobs[0].directory == tmp_path / "out_both" / "LongForm"
    assert all(job.directory == tmp_path / "out_both" / "Shorts" for job in run.jobs[1:])
    assert run.jobs[0].settings.aspect == "16:9"
    assert all(job.settings.aspect == "9:16" for job in run.jobs[1:])
    outputs = [job.directory / f"{job.stem}.mp4" for job in run.jobs]
    assert len(set(outputs)) == 4
    assert run.result.primary_output == outputs[0]


def test_one_click_and_main_video_share_the_same_output_mode_semantics(tmp_path):
    recognize = {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    for mode, expected in (
        (EXPORT_MODE_LONG_FORM, ["YouTube_LongForm"]),
        (EXPORT_MODE_SHORTS, ["001", "002", "003"]),
        (EXPORT_MODE_COMBINED, ["YouTube_LongForm", "001", "002", "003"]),
    ):
        settings = _mode_settings(tmp_path, mode)
        main = _run_export(
            tmp_path, settings,
            recognize=lambda path, _language, _r=recognize: (
                list(_r[SECTIONS[int(Path(path).stem[-1]) - 1]]), "de"
            ),
            output=f"main_{mode}",
        )
        one_click = _run_export(
            tmp_path, settings,
            recognize=lambda path, _language, _r=recognize: (
                list(_r[SECTIONS[int(Path(path).stem[-1]) - 1]]), "de"
            ),
            complete=True, output=f"oneclick_{mode}",
        )
        assert [job.stem for job in main.jobs] == expected, mode
        assert [job.stem for job in one_click.jobs] == expected, mode
        assert [job.settings.aspect for job in main.jobs] == [
            job.settings.aspect for job in one_click.jobs
        ], mode
        assert (one_click.result.long_form is not None) == (main.result.long_form is not None)
        assert len(one_click.result.shorts) == len(main.result.shorts)
        # The same output mode also selects the same script for every job:
        # complete global script for the Long-Form, own section per Short.
        assert [str(global_script_path(job.settings)) for job in main.jobs] == [
            str(global_script_path(job.settings)) for job in one_click.jobs
        ], mode
        # One-Click composes the final video per job; Main Video renders Stage 1.
        assert all(isinstance(item, CompleteWorkflowResult) for item in one_click.result.shorts)


def test_gui_and_cli_main_video_actions_use_the_output_mode_orchestrator():
    """Create Main Video, One-Click and the CLI share one output-mode contract.

    Dependency-light on purpose: the GUI itself needs PySide6/Qt, which is not
    available in every test environment, so the wiring that makes the selected
    YouTube output mode control the Main Video action is pinned at the source.
    """
    root = Path(__file__).resolve().parents[1]
    workers = (root / "app" / "video_merger" / "gui" / "workers.py").read_text(encoding="utf-8")
    # Both Main Video and One-Click run the batch orchestrator, which is the
    # only place that turns the selected export mode into concrete jobs.
    assert 'if self.mode in {"main", "complete"}:' in workers
    assert "project.create_youtube_exports(" in workers
    assert 'complete=self.mode == "complete"' in workers

    window = (root / "app" / "video_merger" / "gui" / "main_window.py").read_text(encoding="utf-8")
    assert 'self.main_button = QPushButton("CREATE MAIN VIDEO")' in window
    assert 'self.main_button.clicked.connect(lambda: self._start("main"))' in window
    assert 'self.complete_button.clicked.connect(lambda: self._start("complete"))' in window
    # The project's YouTube Export Mode selection is what reaches the settings.
    assert "export_mode = normalize_export_mode(self.export_mode_combo.currentData())" in window
    assert "export_mode=export_mode," in window

    cli = (root / "app" / "cli.py").read_text(encoding="utf-8")
    assert 'if args.stage == "main":' in cli
    assert "create_youtube_exports(" in cli


def test_no_duplicate_or_unnecessary_renders_for_any_output_mode(tmp_path):
    media = [_media(f"/pool/clip_{index}.mp4") for index in range(6)]
    for mode, expected_jobs in (
        (EXPORT_MODE_LONG_FORM, 1),
        (EXPORT_MODE_SHORTS, 3),
        (EXPORT_MODE_COMBINED, 4),
    ):
        settings = _mode_settings(tmp_path, mode)
        run = _run_export(tmp_path, settings, media=media, output=f"dedupe_{mode}")
        # Exactly one Stage-1 render per planned job — never a second pass for
        # the other output type and never a repeated Long-Form.
        assert len(run.jobs) == expected_jobs, mode
        assert len(run.result.shorts) + (1 if run.result.long_form is not None else 0) == expected_jobs
        outputs = [str(job.directory / f"{job.stem}.mp4") for job in run.jobs]
        assert len(set(outputs)) == expected_jobs, mode
        assert len({job.settings.render_variant_key for job in run.jobs}) == expected_jobs, mode
        # The global script is mapped at most once for the whole run.
        assert len(run.aligner.global_calls) <= 1, mode
        if mode == EXPORT_MODE_COMBINED:
            # The Long-Form keeps the complete pool; the Shorts consume it
            # without replacement, so no clip is rendered twice across Shorts.
            assert len(run.jobs[0].media) == 6
            handed_out = [tuple(job.media) for job in run.jobs[1:]]
            assert sum(len(item) for item in handed_out) == len(
                {clip for item in handed_out for clip in item}
            )
