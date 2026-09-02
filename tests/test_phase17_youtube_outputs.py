"""Focused, dependency-light coverage for YouTube Long-Form and Shorts jobs."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import CompleteWorkflowResult, ExportSettings, MainVideoResult, ValidationReport
from app.video_merger.subtitle_modes import (
    SUBTITLE_OUTPUT_BOTH,
    SUBTITLE_OUTPUT_WITH,
    SUBTITLE_OUTPUT_WITHOUT,
    normalize_subtitle_output_mode,
    subtitle_clean_variant_requested,
    subtitle_sidecars_requested,
)
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
    build_short_jobs,
    short_settings,
)


def _voiceovers(tmp_path: Path, count: int) -> list[Path]:
    values = []
    for index in range(count):
        path = tmp_path / f"voice_{index + 1}.wav"
        path.write_bytes(b"audio")
        values.append(path)
    return values


def test_subtitle_contract_default_and_clean_variant_are_distinct() -> None:
    settings = ExportSettings()
    assert settings.subtitle_output_mode == SUBTITLE_OUTPUT_WITH
    assert normalize_subtitle_output_mode("burned_and_sidecars") == SUBTITLE_OUTPUT_BOTH
    assert subtitle_sidecars_requested(SUBTITLE_OUTPUT_WITH)
    assert not subtitle_clean_variant_requested(SUBTITLE_OUTPUT_WITH)
    assert subtitle_clean_variant_requested(SUBTITLE_OUTPUT_BOTH)
    assert not subtitle_sidecars_requested(SUBTITLE_OUTPUT_WITHOUT)


def test_global_script_is_independent_of_short_count(tmp_path: Path) -> None:
    voices = _voiceovers(tmp_path, 10)
    script = tmp_path / "global.txt"
    script.write_text("one large authoritative script", encoding="utf-8")
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        global_script_path=str(script),
        script_paths=[str(script)],
        script_mode="single",
    )
    jobs = build_short_jobs(settings)
    assert len(jobs) == 10
    assert [job.output_name for job in jobs] == [f"{index:03d}" for index in range(1, 11)]
    assert all(job.script_path == script.resolve() for job in jobs)
    assert len({job.cache_key for job in jobs}) == 10


def test_matched_scripts_are_one_per_voiceover_but_not_required_for_count(tmp_path: Path) -> None:
    voices = _voiceovers(tmp_path, 3)
    scripts = []
    for voice in voices[:2]:
        script = tmp_path / f"{voice.stem}.txt"
        script.write_text(voice.stem, encoding="utf-8")
        scripts.append(script)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        script_paths=[str(path) for path in scripts],
        script_mode="matched",
    )
    jobs = build_short_jobs(settings)
    assert len(jobs) == 3
    assert [job.script_path.name if job.script_path else None for job in jobs] == [
        "voice_1.txt", "voice_2.txt", None,
    ]


def test_short_settings_force_vertical_profile_and_isolate_audio_unit(tmp_path: Path) -> None:
    voice = _voiceovers(tmp_path, 1)[0]
    script = tmp_path / "global.txt"
    script.write_text("hello", encoding="utf-8")
    settings = ExportSettings(
        export_mode=EXPORT_MODE_COMBINED,
        voiceover_paths=[str(voice)],
        script_mode="single",
        global_script_path=str(script),
        script_paths=[str(script)],
        short_subtitle_style="short_3",
        short_subtitle_animation="word_highlight",
        short_subtitle_position="Bottom Center",
    )
    job = build_short_jobs(settings)[0]
    short = short_settings(settings, job)
    assert short.aspect == "9:16"
    assert short.output_preset == "youtube_vertical"
    assert short.voiceover_paths == [str(voice.resolve())]
    assert short.voiceover_pause == pytest.approx(0.0)
    assert short.subtitle_style == "short_3"
    assert short.render_variant_key == job.cache_key


def test_combined_mode_has_one_landscape_job_plus_one_short_per_voiceover(tmp_path: Path) -> None:
    project = MainProjectEngine(object())
    voices = _voiceovers(tmp_path, 2)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_COMBINED,
        voiceover_paths=[str(path) for path in voices],
        subtitle_enabled=False,
    )
    calls = []

    def fake_create_main(media, job_settings, output_dir, **kwargs):
        calls.append((job_settings, Path(output_dir), kwargs.get("output_stem")))
        path = Path(output_dir) / f"{kwargs['output_stem']}.mp4"
        report = ValidationReport(True, [], path, duration=1.0, width=1920 if job_settings.aspect == "16:9" else 1080, height=1080 if job_settings.aspect == "16:9" else 1920, fps=30.0, has_video=True)
        return MainVideoResult(path, None, None, report)

    project.create_main = fake_create_main  # type: ignore[method-assign]
    result = project.create_youtube_exports([], settings, tmp_path / "output")
    assert result.mode == EXPORT_MODE_COMBINED
    assert result.long_form is not None
    assert len(result.shorts) == 2
    assert calls[0][0].aspect == "16:9"
    assert calls[0][1] == tmp_path / "output" / "LongForm"
    assert [call[2] for call in calls[1:]] == ["001", "002"]
    assert all(call[0].aspect == "9:16" for call in calls[1:])


def test_one_click_dispatches_shorts_as_independent_complete_jobs(tmp_path: Path) -> None:
    project = MainProjectEngine(object())
    voices = _voiceovers(tmp_path, 3)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        subtitle_enabled=False,
    )
    calls = []

    def fake_complete(media, job_settings, output_dir, **kwargs):
        calls.append((job_settings, Path(output_dir), kwargs.get("output_stem")))
        path = Path(output_dir) / f"{kwargs['output_stem']}.mp4"
        report = ValidationReport(True, [], path, duration=1.0, width=1080, height=1920, fps=30.0, has_video=True)
        main = MainVideoResult(path, None, None, report)
        return CompleteWorkflowResult(main, path, report)

    project._create_complete_single = fake_complete  # type: ignore[method-assign]
    result = project.create_complete([], settings, tmp_path / "output")
    assert result.primary_output == tmp_path / "output" / "Shorts" / "001.mp4"
    assert len(result.shorts) == 3
    assert [stem for _settings, _directory, stem in calls] == ["001", "002", "003"]
    assert all(item.aspect == "9:16" for item, _directory, _stem in calls)


def test_create_youtube_exports_runs_one_independent_stage_one_job_per_voiceover(tmp_path: Path) -> None:
    class DummyEngine:
        pass

    project = MainProjectEngine(DummyEngine())
    voices = _voiceovers(tmp_path, 4)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        script_mode="single",
        # no script: this keeps the fake test independent of ASR/FFmpeg while
        # still exercising the one-job-per-voiceover orchestration.
        subtitle_enabled=False,
    )
    calls = []

    def fake_create_main(media, job_settings, output_dir, **kwargs):
        calls.append((job_settings, Path(output_dir), kwargs.get("output_stem")))
        path = Path(output_dir) / f"{kwargs['output_stem']}.mp4"
        report = ValidationReport(True, [], path, duration=1.0, width=1080, height=1920, fps=30.0, has_video=True)
        return MainVideoResult(path, None, None, report)

    project.create_main = fake_create_main  # type: ignore[method-assign]
    result = project.create_youtube_exports([], settings, tmp_path / "output")
    assert result.mode == EXPORT_MODE_SHORTS
    assert result.long_form is None
    assert len(result.shorts) == 4
    assert [stem for _settings, _directory, stem in calls] == ["001", "002", "003", "004"]
    assert [item.aspect for item, _directory, _stem in calls] == ["9:16"] * 4
    assert len({item.render_variant_key for item, _directory, _stem in calls}) == 4
    assert all(directory == (tmp_path / "output" / "Shorts") for _item, directory, _stem in calls)
