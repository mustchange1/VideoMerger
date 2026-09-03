"""Phase 4: multi-voiceover timeline, global script, ordering and pauses.

These tests are deliberately dependency-light. They validate the canonical
math/filter/alignment layers without turning unavailable FFmpeg or GUI
libraries into false passes; the existing E2E suites retain their real guards.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import (
    MainProjectEngine,
    _concatenate_alignment,
    global_script_path,
    ordered_voiceover_units,
    voiceover_paths,
    voiceover_timeline_duration,
)
from app.video_merger.models import AudioAssetInfo, AlignmentResult, ExportSettings, ValidationReport, WordTiming
from app.video_merger.render_cache import Stage1RenderCache, build_stage1_payload
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export
from app.video_merger.video_pool import compute_pool_status
from app.video_merger.subtitles import build_cues
from app.video_merger.voiceover_order import (
    VOICEOVER_ORDER_MODES,
    order_voiceover_paths,
    voiceover_order_indices,
)

from tests.conftest import fake_media


def _paths(tmp_path: Path, count: int) -> list[Path]:
    result = []
    for index in range(count):
        path = tmp_path / f"voice_{index + 1}.wav"
        path.write_bytes(b"audio")
        result.append(path)
    return result


def test_phase4_defaults_keep_end_padding_and_add_separate_pause():
    settings = ExportSettings()
    assert settings.voiceover_pause == pytest.approx(0.7)
    assert settings.final_pause == pytest.approx(1.0)
    assert settings.voiceover_order_mode == "natural"
    assert settings.script_mode == "single"


def test_voiceover_timeline_has_one_pause_only_between_units():
    assert voiceover_timeline_duration([1.0], 0.7) == pytest.approx(1.0)
    assert voiceover_timeline_duration([1.0, 2.0], 0.7) == pytest.approx(3.7)
    assert voiceover_timeline_duration([1.0] * 5, 1.5) == pytest.approx(11.0)
    assert voiceover_timeline_duration([], 0.7) == pytest.approx(0.0)


def test_natural_order_is_numeric_and_alphabetical(tmp_path):
    paths = [tmp_path / name for name in ("voice10.wav", "voice2.wav", "Voice1.wav")]
    assert [p.name for p in order_voiceover_paths(paths, "natural")] == [
        "Voice1.wav", "voice2.wav", "voice10.wav"
    ]


def test_order_modes_include_all_requested_deterministic_choices():
    assert VOICEOVER_ORDER_MODES == ("natural", "mtime_oldest", "mtime_newest", "manual")
    assert voiceover_order_indices([], "mtime_oldest") == []
    assert voiceover_order_indices(["b.wav", "a.wav"], "manual") == [0, 1]


def test_modification_date_oldest_and_newest_are_stable(tmp_path):
    paths = _paths(tmp_path, 3)
    for index, path in enumerate(paths):
        os.utime(path, ns=(1_000 + index * 10, 2_000 + index * 10))
    assert [p.name for p in order_voiceover_paths(paths, "mtime_oldest")] == [
        "voice_1.wav", "voice_2.wav", "voice_3.wav"
    ]
    assert [p.name for p in order_voiceover_paths(paths, "mtime_newest")] == [
        "voice_3.wav", "voice_2.wav", "voice_1.wav"
    ]


def test_manual_order_has_highest_priority(tmp_path):
    paths = _paths(tmp_path, 3)
    manual = [paths[2], paths[0], paths[1]]
    assert order_voiceover_paths(manual, "manual") == manual


def test_matched_scripts_follow_audio_and_preserve_basename_matching(tmp_path):
    voices = [tmp_path / "b.wav", tmp_path / "a.wav"]
    scripts = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for path in voices + scripts:
        path.touch()
    settings = ExportSettings(
        voiceover_paths=[str(path) for path in voices],
        script_paths=[str(path) for path in scripts],
        script_mode="matched",
        voiceover_order_mode="natural",
    )
    ordered_voices, ordered_scripts = ordered_voiceover_units(settings)
    assert [path.stem for path in ordered_voices] == ["a", "b"]
    assert [path.stem for path in ordered_scripts] == ["a", "b"]


def test_global_script_is_stored_once_and_is_authoritative(tmp_path):
    voices = _paths(tmp_path, 5)
    global_script = tmp_path / "complete.txt"
    global_script.write_text("one two three", encoding="utf-8")
    settings = ExportSettings(
        voiceover_paths=[str(path) for path in voices],
        script_paths=[str(global_script)],
        global_script_path=str(global_script),
        script_mode="single",
    )
    assert global_script_path(settings) == global_script.resolve()
    assert len(settings.script_paths) == 1
    assert ordered_voiceover_units(settings)[1] == [global_script.resolve()]


def test_legacy_global_script_path_migrates_from_first_script(tmp_path):
    script = tmp_path / "legacy.txt"
    script.touch()
    settings = ExportSettings(script_paths=[str(script)], script_mode="single")
    assert global_script_path(settings) == script.resolve()


def test_global_alignment_maps_once_and_includes_cumulative_pause(tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    calls: list[Path] = []

    aligner = LocalWordAligner(use_cache=False)
    def recognize(path: Path, _language: str | None):
        calls.append(path)
        token = "alpha" if path == first.resolve() else "bravo"
        return [RecognizedWord(token, 0.1, 0.4)], "en"
    aligner._recognizer = recognize
    result = aligner.align_global(
        "alpha bravo", [(first, 1.0), (second, 1.0)], "English", 0.7
    )
    assert calls == [first.resolve(), second.resolve()]
    assert [(word.text, word.start, word.end) for word in result.words] == [
        ("alpha", 0.1, 0.4), ("bravo", 1.8, 2.1)
    ]
    assert result.hard_breaks == [pytest.approx(1.7)]


def test_global_alignment_cache_reuses_complete_mapping(tmp_path):
    first, second = _paths(tmp_path, 2)
    second.write_bytes(b"different audio")
    calls: list[Path] = []
    aligner = LocalWordAligner(cache_dir=tmp_path / "cache")
    def recognize(path: Path, _language: str | None):
        calls.append(path)
        return [RecognizedWord(path.stem, 0.1, 0.2)], "en"
    aligner._recognize = recognize
    first_result = aligner.align_global(
        "voice_1 voice_2", [(first, 1.0), (second, 1.0)], "English", 0.7
    )
    assert len(calls) == 2
    second_result = aligner.align_global(
        "voice_1 voice_2", [(first, 1.0), (second, 1.0)], "English", 0.7
    )
    assert len(calls) == 2
    assert second_result.words == first_result.words
    assert aligner.last_timings["cache_level"] == "alignment"


@pytest.mark.parametrize("count", [2, 5, 10, 11])
def test_filter_graph_has_one_real_pause_for_each_adjacent_unit(tmp_path, count):
    voices = _paths(tmp_path, count)
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", normalize_audio=False,
        voiceover_paths=[str(path) for path in voices], voiceover_pause=0.7,
        program_duration=count + 0.7 * (count - 1),
        timeline_target_duration=count + 0.7 * (count - 1) + 1,
    )
    media = [fake_media(str(tmp_path / "clip.mp4"), duration=30.0)]
    resolved = resolve_export(media, settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolved)
    assert graph.count("anullsrc=r=48000:cl=stereo:d=0.7") == count - 1
    assert graph.count("concat=n=") >= 1


def test_zero_pause_keeps_voiceovers_contiguous_without_silence_filter(tmp_path):
    voices = _paths(tmp_path, 2)
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", normalize_audio=False,
        voiceover_paths=[str(path) for path in voices], voiceover_pause=0.0,
        program_duration=2.0, timeline_target_duration=3.0,
    )
    resolved = resolve_export([fake_media(str(tmp_path / "clip.mp4"), duration=4)], settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph([fake_media(str(tmp_path / "clip.mp4"), duration=4)], settings, resolved)
    assert "anullsrc=r=48000:cl=" not in graph
    assert "concat=n=2:v=0:a=1" in graph


def test_pause_does_not_change_single_voiceover_graph(tmp_path):
    voice = _paths(tmp_path, 1)[0]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", normalize_audio=False,
        voiceover_paths=[str(voice)], voiceover_pause=2.0,
        program_duration=1.0, timeline_target_duration=2.0,
    )
    resolved = resolve_export([fake_media(str(tmp_path / "clip.mp4"), duration=3)], settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph([fake_media(str(tmp_path / "clip.mp4"), duration=3)], settings, resolved)
    assert "anullsrc=r=48000:cl=" not in graph


def test_matched_alignment_offsets_include_pause_and_hard_boundary():
    first = AlignmentResult(
        words=[WordTiming("Alpha", .1, .3, script_start=0, script_end=5)],
        language="en", method="forced", compatibility=1, average_confidence=1,
    )
    second = AlignmentResult(
        words=[WordTiming("Bravo", .1, .3, script_start=0, script_end=5)],
        language="en", method="forced", compatibility=1, average_confidence=1,
    )
    merged = _concatenate_alignment(
        [(first, 0.0, 0), (second, 1.7, 7)], "Alpha\n\nBravo", ["en", "en"]
    )
    assert merged.words[1].start == pytest.approx(1.8)
    assert merged.hard_breaks == [1.7]


def test_subtitle_cues_never_cross_global_silence(tmp_path):
    alignment = AlignmentResult(
        words=[
            WordTiming("Alpha", .1, .3, script_start=0, script_end=5),
            WordTiming("Bravo", 1.8, 2.1, script_start=6, script_end=11),
        ], language="en", method="forced", compatibility=1, average_confidence=1,
        hard_breaks=[1.7],
    )
    cues = build_cues("Alpha Bravo", alignment, "long_1", program_end=3, width=1920, height=1080)
    assert len(cues) == 2
    assert cues[0].end <= 1.7
    assert cues[1].start >= 1.7


def test_cache_payload_contains_phase4_render_identity(tmp_path):
    voices = _paths(tmp_path, 2)
    settings = ExportSettings(
        voiceover_paths=[str(path) for path in voices], voiceover_pause=1.5,
        voiceover_order_mode="mtime_newest", global_script_path=str(tmp_path / "global.txt"),
        script_mode="single",
    )
    (tmp_path / "global.txt").write_text("a b", encoding="utf-8")
    media = [fake_media(str(tmp_path / "clip.mp4"), duration=3)]
    resolved = resolve_export(media, settings)
    payload = build_stage1_payload(media, settings, resolved, script_files=[tmp_path / "global.txt"])
    values = payload["settings"]
    assert values["voiceover_pause"] == pytest.approx(1.5)
    assert values["voiceover_order_mode"] == "mtime_newest"
    assert values["global_script_path"] == str(tmp_path / "global.txt")


def test_phase4_settings_persist_order_pause_and_global_script(tmp_path):
    path = tmp_path / "settings.json"
    selected = ExportSettings(
        voiceover_pause=2.0, voiceover_order_mode="manual", script_mode="single",
        global_script_path="C:/Users/Name/Global.txt",
        voiceover_paths=["C:/Users/Name/B.wav", "C:/Users/Name/A.wav"],
        script_paths=["C:/Users/Name/Global.txt"],
    )
    store = SettingsStore(path)
    store.save(selected)
    loaded = store.load()
    assert loaded.voiceover_pause == pytest.approx(2.0)
    assert loaded.voiceover_order_mode == "manual"
    assert loaded.global_script_path.endswith("Global.txt")
    assert loaded.voiceover_paths == selected.voiceover_paths


@pytest.mark.parametrize("mode", ["natural", "mtime_oldest", "mtime_newest", "manual"])
def test_all_voiceover_order_modes_roundtrip(tmp_path, mode):
    path = tmp_path / f"settings-{mode}.json"
    selected = ExportSettings(
        voiceover_order_mode=mode,
        voiceover_paths=["C:/Users/Name/B.wav", "C:/Users/Name/A.wav"],
    )
    store = SettingsStore(path)
    store.save(selected)
    assert store.load().voiceover_order_mode == mode


def test_phase4_ordered_voiceover_paths_use_manual_list_headlessly(tmp_path):
    voices = _paths(tmp_path, 3)
    settings = ExportSettings(
        voiceover_paths=[str(voices[2]), str(voices[0]), str(voices[1])],
        voiceover_order_mode="manual",
    )
    assert voiceover_paths(settings) == [path.resolve() for path in [voices[2], voices[0], voices[1]]]


def test_video_pool_target_includes_pause_but_keeps_end_padding_separate():
    settings = ExportSettings(voiceover_pause=0.7, final_pause=1.0)
    target = voiceover_timeline_duration([2.0, 3.0], settings.voiceover_pause) + settings.final_pause
    media = [fake_media("clip.mp4", duration=20.0)]
    status = compute_pool_status(media, target, 0.0, 30.0)
    assert status.target_duration == pytest.approx(6.7)


def test_pause_and_order_change_stage1_fingerprint(tmp_path):
    voice = _paths(tmp_path, 2)
    script = tmp_path / "global.txt"
    script.write_text("one two", encoding="utf-8")
    media = [fake_media(str(tmp_path / "clip.mp4"), duration=4)]
    base = ExportSettings(
        voiceover_paths=[str(path) for path in voice], script_paths=[str(script)],
        global_script_path=str(script), subtitle_enabled=True,
    )
    resolved = resolve_export(media, base)
    from app.video_merger.render_cache import stage1_fingerprint
    first, _ = stage1_fingerprint(media, base, resolved, script_files=[script])
    # Construct the changed variant explicitly to make the cache contract clear
    # and platform independent.
    changed = ExportSettings(
        voiceover_paths=base.voiceover_paths, script_paths=base.script_paths,
        global_script_path=base.global_script_path, subtitle_enabled=True,
        voiceover_pause=1.5, voiceover_order_mode="manual",
    )
    changed_resolved = resolve_export(media, changed)
    second, _ = stage1_fingerprint(media, changed, changed_resolved, script_files=[script])
    assert first != second


def test_oneclick_global_script_probe_is_not_per_voiceover_duplication(tmp_path):
    # The complete workflow's preflight decision is based on one global path,
    # even when the legacy per-row script list is empty.
    script = tmp_path / "complete.txt"
    script.touch()
    settings = ExportSettings(
        voiceover_paths=[str(tmp_path / "one.wav")],
        global_script_path=str(script), script_mode="single",
        intro_path=str(tmp_path / "intro.mp4"),
    )
    from app.video_merger.main_project import global_script_path as resolve_global
    assert resolve_global(settings) == script.resolve()
    assert settings.script_paths == []


def test_individual_script_regression_stays_positional_when_no_basename_match(tmp_path):
    voices = _paths(tmp_path, 2)
    scripts = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for index, script in enumerate(scripts):
        script.write_text(f"word {index}", encoding="utf-8")
    settings = ExportSettings(
        voiceover_paths=[str(path) for path in voices],
        script_paths=[str(path) for path in scripts], script_mode="matched",
        voiceover_order_mode="manual",
    )
    ordered_voices, ordered_scripts = ordered_voiceover_units(settings)
    assert ordered_voices[0].name == "voice_1.wav"
    assert ordered_scripts[0] == scripts[0].resolve()


def test_partial_individual_script_matching_does_not_shift_middle_assignment(tmp_path):
    voices = [tmp_path / name for name in ("a.wav", "b.wav", "c.wav")]
    scripts = [tmp_path / name for name in ("a.txt", "c.txt")]
    for path in voices + scripts:
        path.touch()
    settings = ExportSettings(
        voiceover_paths=[str(path) for path in voices],
        script_paths=[str(path) for path in scripts],
        script_mode="matched",
        voiceover_order_mode="manual",
    )
    ordered_voices, ordered_scripts = ordered_voiceover_units(settings)
    assert [path.stem for path in ordered_voices] == ["a", "b", "c"]
    assert [path.stem if path else None for path in ordered_scripts] == ["a", None, "c"]


def test_alignment_omits_extra_spoken_words_and_marks_an_acoustic_gap(tmp_path):
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: (
            [
                RecognizedWord("Today", 0.0, 0.2, 0.9),
                RecognizedWord("we", 0.2, 0.4, 0.9),
                RecognizedWord("discuss", 0.4, 0.6, 0.9),
                RecognizedWord("discipline", 0.6, 0.8, 0.9),
                RecognizedWord("consistency", 0.8, 1.0, 0.9),
                RecognizedWord("and", 1.0, 1.2, 0.9),
                RecognizedWord("focus", 1.2, 1.4, 0.9),
            ],
            "en",
        ),
    )
    result = aligner.align(
        "Today we discuss discipline and focus.", tmp_path / "voice.wav", "English"
    )
    assert [word.text for word in result.words] == [
        "Today", "we", "discuss", "discipline", "and", "focus.",
    ]
    assert "consistency" not in " ".join(word.text for word in result.words)
    assert result.hard_breaks == [pytest.approx(0.8), pytest.approx(1.0)]
    cues = build_cues("ignored enclosing text", result, "long_1", program_end=2.0)
    assert [cue.text for cue in cues] == ["Today we discuss discipline", "and focus."]
    assert cues[0].end <= 0.8
    assert cues[1].start >= 1.0


def test_alignment_retains_script_only_words_with_local_fallback_timing(tmp_path):
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: (
            [
                RecognizedWord("Today", 0.0, 0.2, 0.9),
                RecognizedWord("we", 0.2, 0.4, 0.9),
                RecognizedWord("discuss", 0.4, 0.6, 0.9),
                RecognizedWord("discipline", 0.6, 0.8, 0.9),
            ],
            "en",
        ),
    )
    result = aligner.align(
        "Today we discuss discipline and consistency.", tmp_path / "voice.wav", "English"
    )
    assert [word.text for word in result.words] == [
        "Today", "we", "discuss", "discipline", "consistency.",
    ]
    assert [round(word.start, 2) for word in result.words[:4]] == [0.0, 0.2, 0.4, 0.6]
    assert result.words[-1].start == pytest.approx(0.8)
    assert result.words[-1].end - result.words[-1].start <= 0.24
    assert result.words[-1].confidence == 0.0


def test_script_only_middle_word_is_retained_with_a_local_fallback(tmp_path):
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: (
            [
                RecognizedWord("Alpha", 0.0, 0.2, 0.9),
                RecognizedWord("Gamma", 0.9, 1.1, 0.9),
            ],
            "en",
        ),
    )
    result = aligner.align("Alpha Bravo Gamma", tmp_path / "voice.wav", "English")
    assert [word.text for word in result.words] == ["Alpha", "Bravo", "Gamma"]
    assert result.words[0].start == pytest.approx(0.0)
    assert result.words[0].end == pytest.approx(0.2)
    assert result.words[1].start == pytest.approx(0.2)
    assert result.words[1].end == pytest.approx(0.44)
    assert result.words[2].start == pytest.approx(0.9)
    assert result.hard_breaks == [pytest.approx(0.9)]
    cues = build_cues("Alpha Bravo Gamma", result, "long_1", program_end=2.0)
    assert [cue.text for cue in cues] == ["Alpha Bravo", "Gamma"]
    assert cues[0].end <= 0.9
    assert cues[1].start >= 0.9


def test_alignment_can_resume_after_a_missing_middle_audio_phrase(tmp_path):
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: (
            [
                RecognizedWord("A", 0.0, 0.2, 0.9),
                RecognizedWord("B", 0.2, 0.4, 0.9),
                RecognizedWord("C", 0.9, 1.1, 0.9),
            ],
            "en",
        ),
    )
    result = aligner.align("A C", tmp_path / "voice.wav", "English")
    assert [word.text for word in result.words] == ["A", "C"]
    assert result.words[1].start == pytest.approx(0.9)
    cues = build_cues("A C", result, "long_1", program_end=2.0)
    assert [cue.text for cue in cues] == ["A", "C"]
    assert cues[0].end <= 0.2
    assert cues[1].start >= 0.9


def test_zero_confidence_match_keeps_real_acoustic_boundaries(tmp_path):
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: (
            [RecognizedWord("spoken", 0.2, 0.5, 0.0)],
            "en",
        ),
    )
    result = aligner.align("spoken", tmp_path / "voice.wav", "English")
    assert [word.text for word in result.words] == ["spoken"]
    assert result.words[0].start == pytest.approx(0.2)
    assert result.words[0].end == pytest.approx(0.5)


@pytest.mark.parametrize("count", [2, 5, 10, 20])
def test_global_script_alignment_scales_without_script_duplication(tmp_path, count):
    voices = _paths(tmp_path, count)
    calls = []

    def recognize(path, _language):
        calls.append(path)
        index = int(path.stem.split("_")[-1])
        return [RecognizedWord(f"word{index}", 0.1, 0.3, 0.9)], "en"

    script = " ".join(f"word{index}" for index in range(1, count + 1))
    result = LocalWordAligner("tiny", recognize).align_global(
        script, [(path, 1.0) for path in voices], "English", 0.7
    )
    assert len(calls) == count
    assert [word.text for word in result.words] == [
        f"word{index}" for index in range(1, count + 1)
    ]
    assert len(result.hard_breaks) == count - 1


def test_missing_voiceover_is_not_a_timeline_unit_or_pause(tmp_path, monkeypatch):
    present = tmp_path / "present.wav"
    missing = tmp_path / "missing.wav"
    present.touch()
    media = [fake_media(str(tmp_path / "clip.mp4"), duration=5.0)]
    settings = ExportSettings(
        voiceover_paths=[str(present), str(missing)],
        voiceover_pause=0.7,
        subtitle_enabled=False,
        resolution="320x180",
        encoding="CPU",
        preset="fast",
        crf=28,
        normalize_audio=False,
    )
    engine = VideoMergerEngine("/nonexistent/ffmpeg", "/nonexistent/ffprobe")
    captured = {}

    def fake_probe(_ffprobe, path):
        return AudioAssetInfo(path, 1.0, 48000, 2, "wav")

    def fake_export(media_value, settings_value, _resolved, output, **_kwargs):
        captured["settings"] = settings_value
        Path(output).write_bytes(b"video")
        return ValidationReport(True, [], Path(output), duration=2.0, width=320, height=180, fps=30.0, has_video=True, has_audio=True)

    monkeypatch.setattr("app.video_merger.main_project.probe_audio", fake_probe)
    monkeypatch.setattr(engine, "export", fake_export)
    result = MainProjectEngine(
        engine, render_cache=Stage1RenderCache(tmp_path / "stage1-cache")
    ).create_main(media, settings, tmp_path / "out")
    assert result.report.ok
    assert captured["settings"].voiceover_paths == [str(present.resolve())]
    assert captured["settings"].program_duration == pytest.approx(1.0)
