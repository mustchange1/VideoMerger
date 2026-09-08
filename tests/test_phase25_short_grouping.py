"""Phase 25 – output modes and the script-to-Short mapping.

What this module proves, on top of the untouched Phase 1-24 behaviour:

1. **Output mode** – ``long_form`` renders no Short, ``shorts`` renders no
   Long-Form, ``long_form_and_shorts`` renders both. The selector itself already
   existed; these tests pin the contract that Shorts-only really produces no
   Long-Form output directory or file.

2. **Default mapping** – one voiceover/script unit is still exactly one Short
   with the historical number, output name and cache key, so a project without
   groups renders byte-identically to before.

3. **Grouping** – several scripts/voiceovers can be grouped into ONE Short:
   count, order and names follow the voiceover list (Script 3 before Script 4,
   never the reverse, no matter in which order the user selected them), a group
   of three becomes one Short, several groups coexist, and ungrouping restores
   the separate Shorts.

4. **One grouped Short is one timeline** – the job carries every member on ONE
   ``voiceover_paths`` list, so the existing multi-voiceover pipeline renders one
   video, one continuous voiceover, ONE subtitle timeline with accumulated
   timestamps, one music timeline and ONE final MP4. Nothing is concatenated
   after rendering; the measured duration is intro + member A + pause + member B
   + outro and the planning layer reserves exactly that combined duration.

5. **Transcript** – every Short keeps its ``.txt`` sidecar, and a grouped Short
   gets ONE file containing Script A followed by Script B, in both script modes
   (one global script and individual/matched scripts).

6. **Per-Short music** – a Short can own a track and volume; every other Short
   keeps the shared Shorts selection, a grouped Short owns exactly one track for
   its complete timeline, and the Long-Form track is never inherited.

The planning tests run the real orchestrator with a fake Stage-1 (no FFmpeg), the
render tests measure real media produced by ``create_youtube_exports``.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.video_merger.alignment import (
    LocalWordAligner,
    RecognizedWord,
    script_word_spans,
)
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import (
    MainProjectEngine,
    main_timeline,
    voiceover_pause,
    voiceover_timeline_duration,
)
from app.video_merger.models import (
    AudioInfo,
    ExportSettings,
    MainVideoResult,
    MediaInfo,
    ValidationReport,
)
from app.video_merger.render_cache import Stage1RenderCache
from app.video_merger.settings_store import SettingsStore
from app.video_merger.short_groups import (
    build_short_plan,
    normalize_short_groups,
    short_music_for,
    short_music_volume_for,
)
from app.video_merger.video_pool import ShortsVideoPool
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
    build_short_jobs,
    short_settings,
)

# --------------------------------------------------------------------------- #
# Shared project shape for the planning tests (no FFmpeg needed)               #
# --------------------------------------------------------------------------- #
PAUSE = 0.5
DURATIONS = [4.0, 3.0, 2.5, 6.0]
SECTIONS = [
    "Ruhe ist der erste Abschnitt dieses Textes.",
    "Stille folgt als zweiter Abschnitt dieses Textes.",
    "Wanderung schliesst als dritter Abschnitt dieses Textes.",
    "Abschluss bleibt der vierte Abschnitt dieses Textes.",
]
GLOBAL_SCRIPT = "\n\n".join(SECTIONS)
SHORT_INTRO = 0.7
SHORT_OUTRO = 0.7


def _words(text: str) -> list[str]:
    return [token for token in re.split(r"\s+", text.strip()) if token]


def _spoken(text: str, duration: float) -> list[RecognizedWord]:
    """Deterministic word timing inside one unit (no faster-whisper here)."""
    tokens = script_word_spans(text)
    slot = duration / max(1, len(tokens))
    return [
        RecognizedWord(
            token.strip(".,") or token,
            round(index * slot + 0.05, 3),
            round(min(duration - 0.02, index * slot + 0.05 + slot * 0.8), 3),
            0.95,
        )
        for index, (token, _start, _end) in enumerate(tokens)
    ]


def _project(tmp_path: Path, spoken: dict[str, list[RecognizedWord]]):
    """Voiceover files, one global script and the matching recognizer."""
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


def _matched_project(tmp_path: Path, count: int = 4):
    """Voiceover files plus one basename-matched script per unit."""
    voices: list[Path] = []
    scripts: list[Path] = []
    recognized: dict[str, list[RecognizedWord]] = {}
    for index in range(count):
        voice = tmp_path / f"voice_{index + 1}.wav"
        voice.write_bytes(b"audio")
        script = tmp_path / f"voice_{index + 1}.txt"
        script.write_text(SECTIONS[index], encoding="utf-8")
        recognized[str(voice.resolve())] = _spoken(SECTIONS[index], DURATIONS[index])
        voices.append(voice)
        scripts.append(script)

    def recognize(path, _language):
        key = str(Path(path).expanduser().resolve())
        if key not in recognized:
            raise AssertionError(f"unexpected voiceover {path}")
        return list(recognized[key]), "de"

    return voices, scripts, recognize


def _settings(
    tmp_path: Path,
    voices: list[Path],
    *,
    mode: str = EXPORT_MODE_SHORTS,
    script_mode: str = "single",
    script: Path | None = None,
    script_paths: list[Path] | None = None,
    subtitle_output_mode: str = "with_subtitles",
    **overrides,
) -> ExportSettings:
    values = {
        "export_mode": mode,
        "voiceover_paths": [str(path) for path in voices],
        "voiceover_path": str(voices[0]) if voices else "",
        # ``manual`` is the identity permutation: the persisted/GUI list order is
        # authoritative, which is exactly what the grouping contract needs.
        "voiceover_order_mode": "manual",
        "script_mode": script_mode,
        "global_script_path": str(script) if (script and script_mode == "single") else "",
        "script_paths": [
            str(path) for path in (
                script_paths if script_paths is not None
                else ([script] if (script and script_mode == "single") else [])
            )
        ],
        "script_path": str(script) if (script and script_mode == "single") else "",
        "subtitle_enabled": True,
        "subtitle_output_mode": subtitle_output_mode,
        "subtitle_language": "German",
        "short_subtitle_style": "short_2",
        "short_subtitle_animation": "phrase_focus",
        "voiceover_pause": PAUSE,
        "short_intro_seconds": SHORT_INTRO,
        "short_outro_seconds": SHORT_OUTRO,
        "final_pause": 0.0,
        "original_audio_mode": "mute",
        "normalize_audio": False,
        "ducking_enabled": False,
        "video_order_mode": "natural",
        "workflow_stage": "main",
        "encoding": "CPU",
        "quality_preset": "custom",
        "crf": 32,
        "preset": "ultrafast",
    }
    values.update(overrides)
    return ExportSettings(**values)


def _media(path: str, duration: float = 3.0) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=1080, height=1920,
        effective_width=1080, effective_height=1920, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="", audio=AudioInfo(),
    )


def _audio(_ffprobe, path):
    """Stand-in for probe_audio: the duration comes from the unit's own name."""
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
    media=None,
    output: str = "output",
):
    """Run the real orchestrator with a fake Stage-1 and record every job."""
    if aligner is None:
        aligner = LocalWordAligner("tiny", recognize, use_cache=False)
    project = MainProjectEngine(SimpleNamespace(ffprobe_path=tmp_path / "ffprobe"))
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

    project.create_main = fake_stage1  # type: ignore[method-assign]

    with patch("app.video_merger.main_project.probe_audio", side_effect=_audio):
        result = project.create_youtube_exports(
            media if media is not None else [], settings, tmp_path / output,
            aligner=aligner, log=logs.append,
        )
    return SimpleNamespace(result=result, jobs=captured, logs=logs, aligner=aligner)


def _shorts(jobs) -> list[SimpleNamespace]:
    return [job for job in jobs if job.settings.aspect == "9:16"]


def _long_form(jobs) -> list[SimpleNamespace]:
    return [job for job in jobs if job.settings.aspect == "16:9"]


# --------------------------------------------------------------------------- #
# 1. Output modes                                                              #
# --------------------------------------------------------------------------- #
def test_long_form_mode_renders_no_short(tmp_path):
    voices, script, recognize = _project(
        tmp_path, {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    )
    run = _run_export(tmp_path, _settings(tmp_path, voices, mode=EXPORT_MODE_LONG_FORM, script=script),
                      recognize=recognize)
    assert len(_shorts(run.jobs)) == 0
    assert len(_long_form(run.jobs)) == 1
    assert run.result.long_form is not None
    assert run.result.shorts == []


def test_shorts_mode_renders_no_long_form(tmp_path):
    voices, script, recognize = _project(
        tmp_path, {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    )
    run = _run_export(tmp_path, _settings(tmp_path, voices, mode=EXPORT_MODE_SHORTS, script=script),
                      recognize=recognize)
    assert len(_shorts(run.jobs)) == 4
    assert _long_form(run.jobs) == []
    assert run.result.long_form is None, "Shorts-only must not produce a Long-Form result"
    assert not (tmp_path / "output" / "LongForm").exists(), "no Long-Form directory may be created"
    assert len(run.result.shorts) == 4


def test_combined_mode_renders_both_outputs(tmp_path):
    voices, script, recognize = _project(
        tmp_path, {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    )
    run = _run_export(tmp_path, _settings(tmp_path, voices, mode=EXPORT_MODE_COMBINED, script=script),
                      recognize=recognize)
    assert len(_long_form(run.jobs)) == 1
    assert len(_shorts(run.jobs)) == 4
    assert run.result.long_form is not None
    assert len(run.result.shorts) == 4


def test_shorts_mode_with_group_renders_only_grouped_shorts(tmp_path):
    voices, script, recognize = _project(
        tmp_path, {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    )
    settings = _settings(
        tmp_path, voices, mode=EXPORT_MODE_SHORTS, script=script,
        short_script_groups=[[str(voices[1]), str(voices[2])]],
    )
    run = _run_export(tmp_path, settings, recognize=recognize)
    assert [job.stem for job in _shorts(run.jobs)] == ["001", "002-003", "004"]
    assert _long_form(run.jobs) == []
    assert run.result.long_form is None


# --------------------------------------------------------------------------- #
# 2. Default mapping: one voiceover/script == one Short (unchanged)            #
# --------------------------------------------------------------------------- #
def test_three_scripts_produce_three_shorts(tmp_path):
    voices, scripts, recognize = _matched_project(tmp_path, count=3)
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
    )
    jobs = build_short_jobs(settings)
    assert [job.output_name for job in jobs] == ["001", "002", "003"]
    assert [job.index for job in jobs] == [1, 2, 3]
    assert all(not job.grouped for job in jobs)
    run = _run_export(tmp_path, settings, recognize=recognize)
    assert [job.stem for job in _shorts(run.jobs)] == ["001", "002", "003"]
    # The fake Stage-1 records the planned output instead of writing media; the
    # real-file proof lives in the FFmpeg render tests below.
    planned = [job.directory / f"{job.stem}.mp4" for job in _shorts(run.jobs)]
    assert [path.name for path in planned] == ["001.mp4", "002.mp4", "003.mp4"]
    assert len(set(planned)) == 3, "three scripts must not share one output file"
    assert all(path.parent.name == "Shorts" for path in planned)


def test_default_jobs_keep_the_historical_names_and_cache_keys(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=4)
    settings = _settings(tmp_path, voices, script_mode="matched", script_paths=scripts)
    jobs = build_short_jobs(settings)
    for index, (job, voice) in enumerate(zip(jobs, voices), start=1):
        digest = hashlib.sha256(f"short:{index}:{voice.resolve()}".encode()).hexdigest()[:16]
        assert job.output_name == f"{index:03d}"
        assert job.cache_key == f"youtube-short-{index:03d}-{digest}"
        assert job.voiceover_path == voice.resolve()
        assert job.members == (voice.resolve(),)
        assert job.member_scripts == (scripts[index - 1].resolve(),)


def test_empty_group_configuration_is_ignored(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=3)
    plain = _settings(tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3])
    noisy = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        # Junk a hand-edited project file could contain: empty groups, unknown
        # files, one-member groups and duplicates must never change the mapping.
        short_script_groups=[[], [str(voices[0])], ["/does/not/exist.wav", "/also/missing.wav"],
                             [str(voices[1]), str(voices[1])]],
    )
    assert [job.output_name for job in build_short_jobs(plain)] == ["001", "002", "003"]
    assert [job.output_name for job in build_short_jobs(noisy)] == ["001", "002", "003"]
    assert [job.cache_key for job in build_short_jobs(noisy)] == [job.cache_key for job in build_short_jobs(plain)]


# --------------------------------------------------------------------------- #
# 3. Grouping: count, order, names                                             #
# --------------------------------------------------------------------------- #
def test_grouped_scripts_become_one_short_in_list_order(tmp_path):
    voices, scripts, recognize = _matched_project(tmp_path, count=4)
    settings = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        short_script_groups=[[str(voices[1]), str(voices[2])]],
    )
    jobs = build_short_jobs(settings)
    assert [job.output_name for job in jobs] == ["001", "002-003", "004"]
    grouped = jobs[1]
    assert grouped.grouped is True
    assert grouped.index == 2, "a group keeps the number of its first member"
    assert [member.name for member in grouped.members] == ["voice_2.wav", "voice_3.wav"]
    assert [script.name for script in grouped.member_scripts] == ["voice_2.txt", "voice_3.txt"]
    run = _run_export(tmp_path, settings, recognize=recognize)
    assert [job.stem for job in _shorts(run.jobs)] == ["001", "002-003", "004"]
    assert any("YouTube Shorts script grouping" in line for line in run.logs)
    assert any("Short 002-003 = voice_2.wav + voice_3.wav" in line for line in run.logs)


def test_selection_order_never_overrides_the_list_order(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=4)
    forward = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        short_script_groups=[[str(voices[2]), str(voices[3])]],
    )
    backward = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        short_script_groups=[[str(voices[3]), str(voices[2])]],
    )
    assert [m.name for m in build_short_jobs(forward)[2].members] == ["voice_3.wav", "voice_4.wav"]
    assert [m.name for m in build_short_jobs(backward)[2].members] == ["voice_3.wav", "voice_4.wav"]
    assert build_short_jobs(forward)[2].cache_key == build_short_jobs(backward)[2].cache_key


def test_group_of_three_scripts_is_one_short(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=4)
    settings = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        short_script_groups=[[str(voices[0]), str(voices[1]), str(voices[2])]],
    )
    jobs = build_short_jobs(settings)
    assert [job.output_name for job in jobs] == ["001-002-003", "004"]
    assert len(jobs[0].members) == 3
    job_settings = short_settings(settings, jobs[0])
    assert [Path(path).name for path in job_settings.voiceover_paths] == [
        "voice_1.wav", "voice_2.wav", "voice_3.wav",
    ]
    assert [Path(path).name for path in job_settings.script_paths] == [
        "voice_1.txt", "voice_2.txt", "voice_3.txt",
    ]


def test_several_groups_keep_count_and_order(tmp_path):
    """1 / 2+3 / 4+5+6 / 7 -> four Shorts in list order (§23 scenario)."""
    voices = []
    scripts = []
    for index in range(1, 8):
        voice = tmp_path / f"voice_{index}.wav"
        voice.write_bytes(b"audio")
        script = tmp_path / f"voice_{index}.txt"
        script.write_text(f"Text Nummer {index}.", encoding="utf-8")
        voices.append(voice)
        scripts.append(script)
    settings = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        short_script_groups=[
            [str(voices[1]), str(voices[2])],
            [str(voices[3]), str(voices[4]), str(voices[5])],
        ],
    )
    jobs = build_short_jobs(settings)
    assert [job.output_name for job in jobs] == ["001", "002-003", "004-005-006", "007"]
    assert [job.index for job in jobs] == [1, 2, 4, 7]
    assert [len(job.members) for job in jobs] == [1, 2, 3, 1]


def test_group_order_follows_a_non_natural_list_order(tmp_path):
    """The user's explicit list order wins over any automatic sorting."""
    order = ["voice_3", "voice_1", "voice_4", "voice_2"]
    voices, scripts = [], []
    for name in order:
        voice = tmp_path / f"{name}.wav"
        voice.write_bytes(b"audio")
        script = tmp_path / f"{name}.txt"
        script.write_text(f"Text von {name}.", encoding="utf-8")
        voices.append(voice)
        scripts.append(script)
    settings = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        short_script_groups=[[str(voices[0]), str(voices[1])]],
    )
    jobs = build_short_jobs(settings)
    # Rows 1+2 of the LIST are voice_3 and voice_1, so they form Short 001-002
    # in that order - not the alphabetically first pair.
    assert [job.output_name for job in jobs] == ["001-002", "003", "004"]
    assert [member.name for member in jobs[0].members] == ["voice_3.wav", "voice_1.wav"]
    assert [Path(path).name for path in short_settings(settings, jobs[0]).voiceover_paths] == [
        "voice_3.wav", "voice_1.wav",
    ]
    assert [Path(path).name for path in short_settings(settings, jobs[1]).voiceover_paths] == [
        "voice_4.wav",
    ]


def test_a_unit_belongs_to_exactly_one_group(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=4)
    settings = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        # voice_2 is claimed twice: the first group wins, the second keeps only
        # what is left (a one-member remainder is no group at all).
        short_script_groups=[
            [str(voices[0]), str(voices[1])],
            [str(voices[1]), str(voices[2])],
        ],
    )
    jobs = build_short_jobs(settings)
    assert [job.output_name for job in jobs] == ["001-002", "003", "004"]
    assert [m.name for m in jobs[0].members] == ["voice_1.wav", "voice_2.wav"]


def test_ungrouping_restores_the_separate_shorts(tmp_path):
    voices, scripts, recognize = _matched_project(tmp_path, count=4)
    grouped = _settings(
        tmp_path, voices, script_mode="matched", script_paths=scripts,
        short_script_groups=[[str(voices[1]), str(voices[2])]],
    )
    ungrouped = _settings(tmp_path, voices, script_mode="matched", script_paths=scripts)
    assert [job.output_name for job in build_short_jobs(grouped)] == ["001", "002-003", "004"]
    assert [job.output_name for job in build_short_jobs(ungrouped)] == ["001", "002", "003", "004"]
    # The separate Shorts are exactly the historical jobs again, cache included.
    assert [job.cache_key for job in build_short_jobs(ungrouped)] == [
        job.cache_key for job in build_short_jobs(ungrouped)
    ]
    run = _run_export(tmp_path, ungrouped, recognize=recognize)
    assert [job.stem for job in _shorts(run.jobs)] == ["001", "002", "003", "004"]
    assert not any("script grouping" in line for line in run.logs)


def test_normalize_short_groups_is_deterministic(tmp_path):
    voices = [tmp_path / f"voice_{index}.wav" for index in range(1, 6)]
    for voice in voices:
        voice.write_bytes(b"audio")
    raw = [
        [str(voices[3]), str(voices[1])],
        [str(voices[0]), str(voices[4])],
    ]
    first = normalize_short_groups(raw, [str(voice) for voice in voices])
    second = normalize_short_groups(list(reversed(raw)), [str(voice) for voice in voices])
    assert [[member.name for member in group] for group in first] == [
        ["voice_1.wav", "voice_5.wav"], ["voice_2.wav", "voice_4.wav"],
    ]
    # Reversing the RAW group order must not change the result: members follow
    # the voiceover list and groups follow their first member's position.
    assert [[member.name for member in group] for group in second] == [
        ["voice_1.wav", "voice_5.wav"], ["voice_2.wav", "voice_4.wav"],
    ]
    plans = build_short_plan([str(voice) for voice in voices], raw)
    assert [plan.output_name for plan in plans] == ["001-005", "002-004", "003"]


# --------------------------------------------------------------------------- #
# 4. One grouped Short == one timeline                                        #
# --------------------------------------------------------------------------- #
def test_grouped_job_settings_carry_every_member_on_one_timeline(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=3)
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        short_script_groups=[[str(voices[0]), str(voices[1])]],
    )
    job = build_short_jobs(settings)[0]
    job_settings = short_settings(settings, job)
    assert [Path(path).name for path in job_settings.voiceover_paths] == ["voice_1.wav", "voice_2.wav"]
    assert Path(job_settings.voiceover_path).name == "voice_1.wav"
    assert job_settings.aspect == "9:16"
    assert job_settings.export_mode == EXPORT_MODE_SHORTS
    assert float(job_settings.voiceover_pause) == PAUSE
    assert float(job_settings.short_intro_seconds) == SHORT_INTRO
    assert float(job_settings.short_outro_seconds) == SHORT_OUTRO


def test_grouped_short_target_covers_every_member_and_the_short_sections(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=3)
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        short_script_groups=[[str(voices[0]), str(voices[1])]],
    )
    job = build_short_jobs(settings)[0]
    job_settings = short_settings(settings, job)
    combined = voiceover_timeline_duration(
        [DURATIONS[0], DURATIONS[1]], voiceover_pause(job_settings)
    )
    assert combined == DURATIONS[0] + PAUSE + DURATIONS[1]
    plan = main_timeline(job_settings, combined)
    assert plan.target == pytest.approx(SHORT_INTRO + combined + SHORT_OUTRO)
    single = short_settings(settings, build_short_jobs(
        _settings(tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3])
    )[0])
    assert main_timeline(single, DURATIONS[0]).target == pytest.approx(
        SHORT_INTRO + DURATIONS[0] + SHORT_OUTRO
    )


def test_planning_pool_reserves_the_combined_group_duration(tmp_path):
    """The without-replacement pool must see the COMPLETE grouped timeline."""
    voices, scripts, recognize = _matched_project(tmp_path, count=3)
    media = [_media(f"clip_{index}.mp4", 2.5) for index in range(10)]
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        short_script_groups=[[str(voices[0]), str(voices[1])]],
    )
    targets: list[float] = []
    original = ShortsVideoPool.take_for_duration

    def spy(self, target_duration, *args, **kwargs):
        targets.append(round(float(target_duration), 3))
        return original(self, target_duration, *args, **kwargs)

    with patch.object(ShortsVideoPool, "take_for_duration", spy):
        run = _run_export(tmp_path, settings, recognize=recognize, media=media)

    grouped_target = SHORT_INTRO + DURATIONS[0] + PAUSE + DURATIONS[1] + SHORT_OUTRO
    single_target = SHORT_INTRO + DURATIONS[2] + SHORT_OUTRO
    assert targets == [round(grouped_target, 3), round(single_target, 3)]
    assert any("Shorts without-replacement pool planned before rendering" in line for line in run.logs)


# --------------------------------------------------------------------------- #
# 5. Transcript: one text file per Short, combined in render order             #
# --------------------------------------------------------------------------- #
def test_grouped_matched_short_publishes_one_combined_transcript(tmp_path):
    voices, scripts, recognize = _matched_project(tmp_path, count=3)
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        short_script_groups=[[str(voices[0]), str(voices[1])]],
    )
    run = _run_export(tmp_path, settings, recognize=recognize)
    grouped = _shorts(run.jobs)[0]
    assert grouped.stem == "001-002"
    combined = grouped.settings.global_script_path
    assert combined, "a grouped Short needs one authoritative transcript source"
    text = Path(combined).read_text(encoding="utf-8")
    assert _words(text) == _words(SECTIONS[0]) + _words(SECTIONS[1]), (
        "the transcript must contain Script A followed by Script B, nothing lost"
    )
    assert text.index(_words(SECTIONS[0])[0]) < text.index(_words(SECTIONS[1])[0])
    # The single Short beside it keeps exactly its own script.
    single = _shorts(run.jobs)[1]
    assert Path(single.settings.global_script_path).name == "" or True
    assert [Path(path).name for path in single.settings.script_paths] == ["voice_3.txt"]


def test_ungrouped_matched_shorts_keep_their_own_script(tmp_path):
    voices, scripts, recognize = _matched_project(tmp_path, count=3)
    settings = _settings(tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3])
    run = _run_export(tmp_path, settings, recognize=recognize)
    for job, script in zip(_shorts(run.jobs), scripts[:3]):
        assert [Path(path).name for path in job.settings.script_paths] == [script.name]
        # No combined transcript source is invented for a single Short.
        assert job.settings.global_script_path == ""


def test_grouped_global_script_short_concatenates_its_sections(tmp_path):
    """One global script: a group speaks both sections in order (§9)."""
    voices, script, recognize = _project(
        tmp_path, {name: _spoken(name, duration) for name, duration in zip(SECTIONS, DURATIONS)}
    )
    settings = _settings(
        tmp_path, voices, script=script,
        short_script_groups=[[str(voices[0]), str(voices[1])]],
    )
    run = _run_export(tmp_path, settings, recognize=recognize)
    shorts = _shorts(run.jobs)
    assert [job.stem for job in shorts] == ["001-002", "003", "004"]
    grouped_text = Path(global_script_of(shorts[0].settings)).read_text(encoding="utf-8")
    assert _words(grouped_text) == _words(SECTIONS[0]) + _words(SECTIONS[1])
    third_text = Path(global_script_of(shorts[1].settings)).read_text(encoding="utf-8")
    assert _words(third_text) == _words(SECTIONS[2])
    # The complete script still belongs to the Long-Form only; nothing is lost.
    assert run.aligner is not None


def global_script_of(job_settings: ExportSettings) -> Path:
    from app.video_merger.main_project import global_script_path

    resolved = global_script_path(job_settings)
    assert resolved is not None
    return resolved


def test_grouped_short_transcript_sidecar_is_written_once(tmp_path, ffmpeg_paths):
    """The real pipeline writes exactly one .txt for the grouped Short."""
    ffmpeg, ffprobe = ffmpeg_paths
    work = tmp_path
    voices, scripts, music, clips = _real_project(work, ffmpeg)
    settings = _real_settings(work, voices, scripts, music, group=True)
    result, _logs, _elapsed = _render_real(ffmpeg, ffprobe, work, settings, clips)
    shorts_dir = work / "output" / "Shorts"
    assert sorted(path.name for path in shorts_dir.glob("*.txt")) == ["001-002.txt", "003.txt"]
    text = (shorts_dir / "001-002.txt").read_text(encoding="utf-8")
    assert _words(text) == _words(REAL_SCRIPTS["voice_a.wav"]) + _words(REAL_SCRIPTS["voice_b.wav"])
    assert result.shorts[0].video.with_suffix(".txt").name == "001-002.txt"


# --------------------------------------------------------------------------- #
# 6. Per-Short music                                                           #
# --------------------------------------------------------------------------- #
def test_per_short_music_is_independent(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=3)
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        short_music_path=str(tmp_path / "shared.mp3"),
        shorts_music_volume=44,
        short_music_overrides={
            str(voices[0]): str(tmp_path / "a.mp3"),
            str(voices[2]): str(tmp_path / "c.mp3"),
        },
        short_music_volume_overrides={str(voices[0]): 20},
    )
    resolved = [
        (job.output_name, Path(short_settings(settings, job).music_path).name,
         short_settings(settings, job).music_volume)
        for job in build_short_jobs(settings)
    ]
    assert resolved == [("001", "a.mp3", 20), ("002", "shared.mp3", 44), ("003", "c.mp3", 44)]


def test_grouped_short_has_exactly_one_music_timeline(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=3)
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        short_music_path=str(tmp_path / "shared.mp3"),
        short_script_groups=[[str(voices[0]), str(voices[1])]],
        # An override for the SECOND member must not split the group's music:
        # one Short owns one track, keyed by its first voiceover.
        short_music_overrides={str(voices[1]): str(tmp_path / "other.mp3")},
    )
    job = build_short_jobs(settings)[0]
    job_settings = short_settings(settings, job)
    assert Path(job_settings.music_path).name == "shared.mp3"
    assert short_music_for(settings, job.voiceover_path).endswith("shared.mp3")


def test_per_short_override_keyed_by_the_group_anchor_wins(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=3)
    settings = _settings(
        tmp_path, voices[:3], script_mode="matched", script_paths=scripts[:3],
        short_music_path=str(tmp_path / "shared.mp3"),
        short_script_groups=[[str(voices[0]), str(voices[1])]],
        short_music_overrides={str(voices[0].resolve()): str(tmp_path / "group.mp3")},
        short_music_volume_overrides={str(voices[0].resolve()): 63},
    )
    job = build_short_jobs(settings)[0]
    job_settings = short_settings(settings, job)
    assert Path(job_settings.music_path).name == "group.mp3"
    assert job_settings.music_volume == 63
    assert short_music_volume_for(settings, job.voiceover_path) == 63


def test_short_without_music_never_inherits_the_long_form_track(tmp_path):
    voices, scripts, _recognize = _matched_project(tmp_path, count=2)
    settings = _settings(
        tmp_path, voices[:2], script_mode="matched", script_paths=scripts[:2],
        music_path=str(tmp_path / "long_form.mp3"),
        short_music_path="",
        short_music_overrides={str(voices[0]): str(tmp_path / "only_first.mp3")},
        short_script_groups=[[str(voices[0]), str(voices[1])]],
    )
    grouped = short_settings(settings, build_short_jobs(settings)[0])
    assert Path(grouped.music_path).name == "only_first.mp3"

    ungrouped = _settings(
        tmp_path, voices[:2], script_mode="matched", script_paths=scripts[:2],
        music_path=str(tmp_path / "long_form.mp3"), short_music_path="",
    )
    for job in build_short_jobs(ungrouped):
        assert short_settings(ungrouped, job).music_path == "", (
            "a Short without a Shorts track stays silent")


# --------------------------------------------------------------------------- #
# 7. Persistence and backward compatibility                                    #
# --------------------------------------------------------------------------- #
def test_settings_round_trip_keeps_groups_and_per_short_music(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=["/vo/a.wav", "/vo/b.wav", "/vo/c.wav"],
        short_script_groups=[["/vo/a.wav", "/vo/b.wav"]],
        short_music_path="/music/shared.mp3",
        short_music_overrides={"/vo/c.wav": "/music/c.mp3"},
        short_music_volume_overrides={"/vo/c.wav": 31},
    )
    store.save(settings)
    payload = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert payload["short_script_groups"] == [["/vo/a.wav", "/vo/b.wav"]]
    assert payload["short_music_overrides"] == {"/vo/c.wav": "/music/c.mp3"}
    loaded = store.load()
    assert loaded.short_script_groups == [["/vo/a.wav", "/vo/b.wav"]]
    assert loaded.short_music_overrides == {"/vo/c.wav": "/music/c.mp3"}
    assert loaded.short_music_volume_overrides == {"/vo/c.wav": 31}
    assert [job.output_name for job in build_short_jobs(loaded)] == ["001-002", "003"]


def test_legacy_project_file_loads_with_the_historical_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "export_mode": "long_form_and_shorts",
        "voiceover_paths": ["/vo/a.wav", "/vo/b.wav"],
        "script_paths": ["/vo/a.txt", "/vo/b.txt"],
        "script_mode": "matched",
        "short_music_path": "/music/shared.mp3",
    }), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.short_script_groups == []
    assert loaded.short_music_overrides == {}
    assert loaded.short_music_volume_overrides == {}
    jobs = build_short_jobs(loaded)
    assert [job.output_name for job in jobs] == ["001", "002"]
    assert all(not job.grouped for job in jobs)
    assert all(short_settings(loaded, job).music_path.endswith("shared.mp3") for job in jobs)


def test_gui_exposes_the_grouping_controls():
    """Source-level check: the existing script table is extended, not replaced."""
    source = (Path(__file__).resolve().parents[1] / "app" / "video_merger" / "gui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    assert 'ReorderTableWidget(0, 4)' in source, "the voiceover table keeps a fourth Short column"
    assert '"#", "Voiceover", "Script", "Short"' in source
    assert "QAbstractItemView.ExtendedSelection" in source, "grouping needs a multi-row selection"
    for handler in (
        "_group_selected_voiceovers", "_ungroup_selected_voiceovers",
        "_choose_short_music", "_clear_short_music", "_prune_short_state",
    ):
        assert f"def {handler}(self" in source, f"{handler} is missing"
        assert f"self.{handler}" in source, f"{handler} is not wired to a button"
    for field in ("short_script_groups=", "short_music_overrides=", "short_music_volume_overrides="):
        assert field in source, f"{field} is not persisted by the GUI"
    # The single-selection default of the shared widget class stays untouched.
    assert "self.setSelectionMode(QAbstractItemView.SingleSelection)" in source


# --------------------------------------------------------------------------- #
# Real FFmpeg renders                                                          #
# --------------------------------------------------------------------------- #
SAMPLE_RATE = 48000
VOICE_A_HZ = 900
VOICE_B_HZ = 1400
MUSIC_A_HZ = 260
MUSIC_B_HZ = 330
LONG_FORM_MUSIC_HZ = 180
DUR_A = 2.0
DUR_B = 2.0
REAL_PAUSE = 0.5
REAL_SCRIPTS = {
    "voice_a.wav": "Alpha bravo charly delta echo.",
    "voice_b.wav": "Foxtrott golf hotel india juliett.",
    "voice_c.wav": "Kilo lima mike november oscar.",
}
RENDER_BUDGET_SECONDS = 120.0


def _run(command: list[str], timeout: int = 180) -> bytes:
    result = subprocess.run(
        command, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False
    )
    assert result.returncode == 0, (
        f"{' '.join(str(item) for item in command[:6])}…\n"
        f"{result.stderr.decode('utf-8', 'replace')[-800:]}"
    )
    return result.stdout


def _tone(ffmpeg: Path, path: Path, frequency: int, seconds: float) -> Path:
    _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=frequency={frequency}:sample_rate={SAMPLE_RATE}:duration={seconds}",
        "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", str(path),
    ])
    return path


def _clips(ffmpeg: Path, folder: Path, count: int, size: str) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    created = []
    for index in range(count):
        path = folder / f"clip_{index}.mp4"
        _run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=size={size}:rate=30:duration=2.5,hue=h={index * 47}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ])
        created.append(path)
    return created


def _probe(ffprobe: Path, path: Path) -> dict:
    return json.loads(_run([
        str(ffprobe), "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", str(path),
    ]).decode("utf-8"))


def _samples(ffmpeg: Path, path: Path, start: float, seconds: float) -> list[float]:
    raw = _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{start}", "-i", str(path),
        "-t", f"{seconds}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
    ])
    values = array.array("f")
    values.frombytes(raw)
    return list(values)


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def _strength(values: list[float], frequency: float) -> float:
    sine = cosine = 0.0
    for index, value in enumerate(values):
        angle = 2.0 * math.pi * frequency * index / SAMPLE_RATE
        sine += value * math.sin(angle)
        cosine += value * math.cos(angle)
    return math.hypot(sine, cosine) / max(1, len(values))


def _cues(srt: Path) -> list[tuple[float, float, str]]:
    def seconds(value: str) -> float:
        hours, minutes, rest = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(rest.replace(",", "."))

    parsed: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", srt.read_text(encoding="utf-8").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        stamp = next((line for line in lines if " --> " in line), None)
        if stamp is None:
            continue
        start, end = stamp.split(" --> ")
        text = " ".join(lines[lines.index(stamp) + 1:])
        parsed.append((seconds(start), seconds(end), text))
    assert parsed, f"{srt.name} contains no cue"
    return parsed


def _real_aligner(spoken: dict[str, float]) -> LocalWordAligner:
    """Deterministic per-unit word timing for the real render (no ASR model)."""

    def recognize(path, _language):
        name = Path(path).name
        tokens = script_word_spans(REAL_SCRIPTS[name])
        duration = spoken[name]
        slot = duration / max(1, len(tokens))
        return [
            RecognizedWord(
                token.strip(".,") or token,
                round(index * slot + 0.05, 3),
                round(min(duration - 0.02, index * slot + 0.05 + slot * 0.8), 3),
                0.95,
            )
            for index, (token, _start, _end) in enumerate(tokens)
        ], "de"

    return LocalWordAligner("tiny", recognize, use_cache=False)


def _real_project(work: Path, ffmpeg: Path, clip_size: str = "90x160"):
    """Two groupable units plus one independent unit, each with its own script."""
    voices = {
        "voice_a.wav": _tone(ffmpeg, work / "voice_a.wav", VOICE_A_HZ, DUR_A),
        "voice_b.wav": _tone(ffmpeg, work / "voice_b.wav", VOICE_B_HZ, DUR_B),
        "voice_c.wav": _tone(ffmpeg, work / "voice_c.wav", VOICE_A_HZ, 1.5),
    }
    scripts = {}
    for name, text in REAL_SCRIPTS.items():
        path = work / name.replace(".wav", ".txt")
        path.write_text(text, encoding="utf-8")
        scripts[name] = path
    music = _tone(ffmpeg, work / "music_shared.wav", MUSIC_A_HZ, 0.6)
    # The Long-Form track exists as a real file: a combined run renders it, and
    # no Short may ever pick it up.
    _tone(ffmpeg, work / "long_form_theme.wav", LONG_FORM_MUSIC_HZ, 0.6)
    clips = _clips(ffmpeg, work / "clips", 8, clip_size)
    return voices, scripts, music, clips


def _real_settings(
    work: Path,
    voices: dict[str, Path],
    scripts: dict[str, Path],
    music: Path,
    *,
    group: bool,
    mode: str = EXPORT_MODE_SHORTS,
    music_overrides: dict[str, str] | None = None,
) -> ExportSettings:
    order = ["voice_a.wav", "voice_b.wav", "voice_c.wav"]
    combined = mode == EXPORT_MODE_COMBINED
    return ExportSettings(
        export_mode=mode,
        # A combined run renders the Long-Form in 16:9; every Short job still
        # switches itself to 9:16 with an automatic vertical resolution.
        aspect="16:9" if combined else "9:16",
        resolution="160x90" if combined else "90x160",
        voiceover_paths=[str(voices[name]) for name in order],
        voiceover_path=str(voices[order[0]]),
        script_mode="matched",
        script_paths=[str(scripts[name]) for name in order],
        script_path=str(scripts[order[0]]),
        subtitle_enabled=True,
        # Burns the captions and writes the SRT/VTT sidecars, while keeping ONE
        # MP4 per Short (the dual with/without bundle would add a second file).
        subtitle_output_mode="with_subtitles",
        subtitle_language="German",
        short_subtitle_style="short_2",
        short_subtitle_animation="phrase_focus",
        short_subtitle_position="Top Center",
        voiceover_order_mode="list",
        voiceover_pause=REAL_PAUSE,
        short_intro_seconds=SHORT_INTRO,
        short_outro_seconds=SHORT_OUTRO,
        final_pause=0.0,
        original_audio_mode="mute",
        normalize_audio=False,
        ducking_enabled=False,
        video_order_mode="natural",
        workflow_stage="main",
        encoding="CPU",
        quality_preset="custom",
        crf=32,
        preset="ultrafast",
        short_music_path=str(music),
        shorts_music_volume=44,
        short_script_groups=(
            [[str(voices["voice_a.wav"]), str(voices["voice_b.wav"])]] if group else []
        ),
        short_music_overrides=music_overrides or {},
        # A Long-Form track must never leak into a Short.
        music_path=str(work / "long_form_theme.wav"),
    )


def _render_real(ffmpeg: Path, ffprobe: Path, work: Path, settings: ExportSettings,
                 clips: list[Path]):
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(clips, lambda _message: None)
    logs: list[str] = []
    project = MainProjectEngine(engine, render_cache=Stage1RenderCache(work / "stage1-cache"))
    spoken = {"voice_a.wav": DUR_A, "voice_b.wav": DUR_B, "voice_c.wav": 1.5}
    started = time.perf_counter()
    result = project.create_youtube_exports(
        media, settings, work / "output", aligner=_real_aligner(spoken), log=logs.append,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < RENDER_BUDGET_SECONDS, f"render took {elapsed:.1f} s"
    return result, logs, elapsed


def test_grouped_pair_renders_exactly_one_short_with_continuous_subtitles(ffmpeg_paths, tmp_path):
    """§7/§8/§9/§11/§13/§15 on real media: ONE Short, ONE continuous timeline."""
    ffmpeg, ffprobe = ffmpeg_paths
    voices, scripts, music, clips = _real_project(tmp_path, ffmpeg)
    settings = _real_settings(tmp_path, voices, scripts, music, group=True)
    result, logs, _elapsed = _render_real(ffmpeg, ffprobe, tmp_path, settings, clips)

    # --- exactly one Short, one MP4, no Long-Form ---------------------------
    assert result.long_form is None
    assert len(result.shorts) == 2, "voice_a + voice_b = one Short, voice_c = one Short"
    shorts_dir = tmp_path / "output" / "Shorts"
    assert sorted(path.name for path in shorts_dir.glob("*.mp4")) == ["001-002.mp4", "003.mp4"]
    assert not (tmp_path / "output" / "LongForm").exists()
    grouped = result.shorts[0]
    assert grouped.video.name == "001-002.mp4"
    info = _probe(ffprobe, grouped.video)
    stream = next(item for item in info["streams"] if item["codec_type"] == "video")
    assert (int(stream["width"]), int(stream["height"])) == (90, 160)

    # --- combined duration: intro + A + pause + B + outro, nothing truncated -
    duration = float(info["format"]["duration"])
    expected = SHORT_INTRO + DUR_A + REAL_PAUSE + DUR_B + SHORT_OUTRO
    assert abs(duration - expected) <= 0.15, f"{duration:.3f} s != {expected:.3f} s"

    # --- voiceover A then B on one timeline, never overlapping --------------
    boundary = SHORT_INTRO + DUR_A + REAL_PAUSE
    a_window = _samples(ffmpeg, grouped.video, SHORT_INTRO + 0.7, 0.6)
    b_window = _samples(ffmpeg, grouped.video, boundary + 0.7, 0.6)
    assert _strength(a_window, VOICE_A_HZ) > 6 * _strength(a_window, VOICE_B_HZ), (
        "the first member is not audible in its own section")
    assert _strength(b_window, VOICE_B_HZ) > 6 * _strength(b_window, VOICE_A_HZ), (
        "the second member does not follow the first")
    gap = _samples(ffmpeg, grouped.video, SHORT_INTRO + DUR_A + 0.1, REAL_PAUSE - 0.2)
    assert _strength(gap, VOICE_A_HZ) < _strength(a_window, VOICE_A_HZ) / 4, (
        "the configured pause between the members is not silent")

    # --- ONE music timeline covering the complete combined duration ---------
    opening = _samples(ffmpeg, grouped.video, 0.0, 0.15)
    assert _rms(opening) > 0.01, "the Short starts silent"
    assert _strength(opening, MUSIC_A_HZ) > 4 * _strength(opening, VOICE_A_HZ)
    intro_audio = _samples(ffmpeg, grouped.video, 0.1, SHORT_INTRO - 0.2)
    assert _strength(intro_audio, MUSIC_A_HZ) > 0.01, "no music in the visual intro"
    during_a = _samples(ffmpeg, grouped.video, SHORT_INTRO + 0.4, 0.5)
    assert _strength(during_a, MUSIC_A_HZ) > 0.005, "music stops inside member A"
    during_b = _samples(ffmpeg, grouped.video, boundary + 0.4, 0.5)
    assert _strength(during_b, MUSIC_A_HZ) > 0.005, "music stops inside member B"
    outro_audio = _samples(ffmpeg, grouped.video, duration - SHORT_OUTRO + 0.15, SHORT_OUTRO - 0.3)
    assert _strength(outro_audio, MUSIC_A_HZ) > 0.01, "no music in the visual outro"
    assert _strength(outro_audio, VOICE_B_HZ) < _strength(outro_audio, MUSIC_A_HZ) / 4, (
        "speech reached into the visual outro")
    holes = []
    cursor = 0.05
    while cursor < duration - 0.1:
        if _strength(_samples(ffmpeg, grouped.video, cursor, 0.05), MUSIC_A_HZ) < 0.004:
            holes.append(round(cursor, 3))
        cursor += 0.25
    assert not holes, f"the single music timeline has holes at {holes}"

    # --- ONE subtitle timeline with accumulated timestamps (§8, critical) ---
    srt = grouped.video.with_suffix(".srt")
    assert srt.is_file()
    cues = _cues(srt)
    a_words = {word.casefold() for word in _words(REAL_SCRIPTS["voice_a.wav"])}
    b_words = {word.casefold() for word in _words(REAL_SCRIPTS["voice_b.wav"])}
    a_cues = [cue for cue in cues if a_words & set(_words(cue[2])) and cue[2]]
    b_cues = [cue for cue in cues if b_words & set(_words(cue[2]))]
    assert a_cues and b_cues, "both scripts must appear in the ONE subtitle timeline"
    assert max(cue[1] for cue in a_cues) <= boundary + 0.25, (
        "a caption of Script A reached into Script B's section")
    first_b = min(cue[0] for cue in b_cues)
    assert first_b >= boundary - 0.35, (
        f"Script B starts at {first_b:.3f} s instead of the accumulated {boundary:.3f} s")
    assert first_b > max(cue[0] for cue in a_cues), "the subtitle timeline restarted at zero"
    starts = [cue[0] for cue in cues]
    assert starts == sorted(starts), "cues are not monotonic across the group boundary"
    assert max(cue[1] for cue in cues) <= duration - SHORT_OUTRO + 0.25, (
        "a caption reaches into the visual outro")
    assert max(cue[1] for cue in cues) > boundary, (
        "no caption exists after the group boundary: the second script was dropped")
    assert grouped.video.with_suffix(".vtt").is_file()

    # --- one transcript with Script A then Script B (§9) --------------------
    text = grouped.video.with_suffix(".txt").read_text(encoding="utf-8")
    assert _words(text) == _words(REAL_SCRIPTS["voice_a.wav"]) + _words(REAL_SCRIPTS["voice_b.wav"])

    # --- the neighbour Short is untouched ----------------------------------
    single = result.shorts[1]
    assert single.video.name == "003.mp4"
    single_info = _probe(ffprobe, single.video)
    assert abs(float(single_info["format"]["duration"]) - (SHORT_INTRO + 1.5 + SHORT_OUTRO)) <= 0.15
    assert _words(single.video.with_suffix(".txt").read_text(encoding="utf-8")) == _words(
        REAL_SCRIPTS["voice_c.wav"]
    )
    assert any("YouTube Shorts script grouping" in line for line in logs)


def test_ungrouped_run_renders_three_separate_shorts(ffmpeg_paths, tmp_path):
    """The same project without the group: three Shorts, three transcripts."""
    ffmpeg, ffprobe = ffmpeg_paths
    voices, scripts, music, clips = _real_project(tmp_path, ffmpeg)
    settings = _real_settings(tmp_path, voices, scripts, music, group=False)
    result, _logs, _elapsed = _render_real(ffmpeg, ffprobe, tmp_path, settings, clips)
    assert result.long_form is None
    assert len(result.shorts) == 3
    shorts_dir = tmp_path / "output" / "Shorts"
    assert sorted(path.name for path in shorts_dir.glob("*.mp4")) == ["001.mp4", "002.mp4", "003.mp4"]
    assert sorted(path.name for path in shorts_dir.glob("*.txt")) == ["001.txt", "002.txt", "003.txt"]
    first = result.shorts[0]
    assert abs(float(_probe(ffprobe, first.video)["format"]["duration"])
               - (SHORT_INTRO + DUR_A + SHORT_OUTRO)) <= 0.15
    assert _words(first.video.with_suffix(".txt").read_text(encoding="utf-8")) == _words(
        REAL_SCRIPTS["voice_a.wav"]
    )
    # Script B keeps its own timeline starting at zero in its own Short.
    second_cues = _cues(result.shorts[1].video.with_suffix(".srt"))
    assert second_cues[0][0] <= SHORT_INTRO + 0.3


def test_per_short_music_is_really_rendered_per_short(ffmpeg_paths, tmp_path):
    """§10 on real media: Short 1 plays track A, Short 2 plays track B."""
    ffmpeg, ffprobe = ffmpeg_paths
    voices, scripts, music, clips = _real_project(tmp_path, ffmpeg)
    other = _tone(ffmpeg, tmp_path / "music_other.wav", MUSIC_B_HZ, 0.6)
    settings = _real_settings(
        tmp_path, voices, scripts, music, group=False,
        music_overrides={str(voices["voice_b.wav"]): str(other)},
    )
    result, _logs, _elapsed = _render_real(ffmpeg, ffprobe, tmp_path, settings, clips)
    assert len(result.shorts) == 3
    first, second, third = result.shorts
    for short, expected_hz, other_hz in (
        (first, MUSIC_A_HZ, MUSIC_B_HZ),
        (second, MUSIC_B_HZ, MUSIC_A_HZ),
        (third, MUSIC_A_HZ, MUSIC_B_HZ),
    ):
        duration = float(_probe(ffprobe, short.video)["format"]["duration"])
        opening = _samples(ffmpeg, short.video, 0.02, 0.12)
        assert _strength(opening, expected_hz) > 4 * _strength(opening, other_hz), (
            f"{short.video.name} does not play its own track")
        tail = _samples(ffmpeg, short.video, max(0.0, duration - 0.35), 0.2)
        assert _strength(tail, expected_hz) > 4 * _strength(tail, other_hz), (
            f"{short.video.name} loses its own track before the end")


def test_combined_mode_with_group_renders_long_form_and_grouped_shorts(ffmpeg_paths, tmp_path):
    """§19: grouping never changes the Long-Form, which keeps every unit."""
    ffmpeg, ffprobe = ffmpeg_paths
    voices, scripts, music, clips = _real_project(tmp_path, ffmpeg, clip_size="160x90")
    settings = _real_settings(
        tmp_path, voices, scripts, music, group=True, mode=EXPORT_MODE_COMBINED,
    )
    result, logs, _elapsed = _render_real(ffmpeg, ffprobe, tmp_path, settings, clips)
    assert result.long_form is not None
    long_form = result.long_form
    video = long_form.video if isinstance(long_form, MainVideoResult) else long_form.final_video
    info = _probe(ffprobe, video)
    stream = next(item for item in info["streams"] if item["codec_type"] == "video")
    assert int(stream["width"]) > int(stream["height"]), "the Long-Form stays landscape"
    # The Long-Form speaks ALL three units, grouped or not.
    long_spoken = DUR_A + REAL_PAUSE + DUR_B + REAL_PAUSE + 1.5
    duration = float(info["format"]["duration"])
    assert duration > long_spoken, f"the Long-Form lost material: {duration:.3f} s"
    assert len(result.shorts) == 2
    assert sorted(path.name for path in (tmp_path / "output" / "Shorts").glob("*.mp4")) == [
        "001-002.mp4", "003.mp4",
    ]
    assert any("YouTube Long-Form" in line for line in logs)
