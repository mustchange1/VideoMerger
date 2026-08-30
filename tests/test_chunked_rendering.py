"""Automatic Windows-safe Chunked Rendering tests."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

import app.video_merger.engine as engine_module
from app.video_merger.chunked_render import (
    SAFE_COMMAND_TARGET,
    WINDOWS_COMMAND_LIMIT,
    ChunkingError,
    plan_segments,
)
from app.video_merger.command_builder import BuiltCommand, FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.errors import ExportCancelled, ExportError, ValidationError
from app.video_merger.models import ExportSettings, ResolvedExport, ValidationReport
from app.video_merger.platform_utils import format_command_for_log
from app.video_merger.target import resolve_export

from tests.conftest import fake_media, make_clip


def test_command_under_conservative_target_stays_on_normal_path(monkeypatch):
    engine = VideoMergerEngine("ffmpeg", "ffprobe")
    monkeypatch.setattr(engine, "_is_windows", lambda: True)
    command = ["ffmpeg.exe", "-i", "small.mp4", "-filter_complex", "x" * 100]
    assert len(format_command_for_log(command)) < SAFE_COMMAND_TARGET
    assert not engine._chunking_required(command)


def test_command_above_conservative_target_uses_chunking_before_30k_guard(monkeypatch):
    engine = VideoMergerEngine("ffmpeg", "ffprobe")
    monkeypatch.setattr(engine, "_is_windows", lambda: True)
    command = ["ffmpeg.exe", "-filter_complex", "x" * (SAFE_COMMAND_TARGET + 100)]
    assert SAFE_COMMAND_TARGET < len(format_command_for_log(command)) < WINDOWS_COMMAND_LIMIT
    assert engine._chunking_required(command)


def test_old_guard_remains_final_backstop(monkeypatch, tmp_path):
    engine = VideoMergerEngine("ffmpeg", "ffprobe")
    monkeypatch.setattr(engine, "_is_windows", lambda: True)
    with pytest.raises(ExportError, match="sichere Windows-Limit"):
        engine._execute(
            ["ffmpeg.exe", "x" * (WINDOWS_COMMAND_LIMIT + 1)],
            [],
            ResolvedExport(160, 90, 30, "30", [], [], 1),
            lambda _event: None,
            lambda _message: None,
            threading.Event(),
            "test",
            tmp_path,
        )


def test_export_dispatches_oversized_windows_command_to_chunking(monkeypatch, tmp_path):
    media = [fake_media(str(tmp_path / "clip.mp4"), duration=1.0, audio=False)]
    settings = ExportSettings(workflow_stage="basic", resolution="160x90")
    resolved = resolve_export(media, settings)
    engine = VideoMergerEngine("ffmpeg", "ffprobe")
    oversized = ["ffmpeg.exe", "-filter_complex", "x" * (SAFE_COMMAND_TARGET + 1)]
    dispatched: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(engine, "_is_windows", lambda: True)
    monkeypatch.setattr(engine, "preflight", lambda _log: None)
    monkeypatch.setattr(engine_module, "project_root", lambda: tmp_path)
    monkeypatch.setattr(engine.builder, "build", lambda *_args, **_kwargs: BuiltCommand(oversized, "graph"))

    def fake_chunk(*args):
        dispatched.append((args[4], args[3]))
        return ValidationReport(True, [], args[3])

    monkeypatch.setattr(engine, "_export_chunked", fake_chunk)
    result = engine.export(media, settings, resolved, tmp_path / "output.mp4")
    assert result.ok
    assert dispatched == [(oversized, tmp_path / "output.mp4")]


def test_planner_uses_only_post_transition_boundaries_and_keeps_overlap():
    plans = plan_segments(
        [2.0, 2.0, 2.0, 2.0], [0.5, 0.5, 0.5],
        lambda start, stop, logical_start, logical_end, video_start, duration: stop - start <= 3,
    )
    assert [(plan.media_start, plan.media_stop) for plan in plans] == [(0, 3), (2, 4)]
    assert plans[0].logical_end == pytest.approx(5.0)
    assert plans[1].logical_start == pytest.approx(5.0)
    assert plans[1].video_window_start == pytest.approx(0.5)
    assert plans[1].duration == pytest.approx(1.5)


def test_planner_keeps_one_small_project_as_one_segment():
    plans = plan_segments([2.0, 2.0], [0.5], lambda *args: True)
    assert len(plans) == 1
    assert plans[0].media_start == 0 and plans[0].media_stop == 2
    assert plans[0].duration == pytest.approx(3.5)


def test_planner_reports_indivisible_unsafe_boundary():
    with pytest.raises(ChunkingError):
        plan_segments([2.0, 2.0], [0.5], lambda *args: False)


def test_windowed_builder_trims_video_and_audio_to_continuous_window(tmp_path):
    media = [
        fake_media(str(tmp_path / "clip_a.mp4"), duration=2.0),
        fake_media(str(tmp_path / "clip_b.mp4"), duration=2.0),
    ]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", normalize_audio=False,
        voiceover_paths=[str(tmp_path / "voice.wav")], program_duration=2.0,
    )
    resolved = resolve_export(media, settings)
    builder = FFmpegCommandBuilder("ffmpeg")
    built = builder.build(
        media, settings, resolved, tmp_path / "segment.mp4",
        video_window_start=0.5, audio_window_start=3.5, window_duration=1.25,
    )
    assert "trim=start=0.5:duration=1.25" in built.filter_graph
    assert "[original_main]" in built.filter_graph
    assert "[a0]" in built.filter_graph
    assert "atrim=start=3.5:duration=1.25" in built.filter_graph
    assert "atrim=start=0.5:duration=1.25" in built.filter_graph
    assert "asetpts=PTS-STARTPTS" in built.filter_graph


def _large_fake_project(tmp_path, count=120):
    media = [
        fake_media(str(tmp_path / ("clip_" + "x" * 90 + f"_{i}.mp4")), duration=.5, audio=False)
        for i in range(count)
    ]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="basic", normalize_audio=False,
        transition_duration=0.0, quality_preset="custom", crf=28, preset="fast",
    )
    return media, settings, resolve_export(media, settings)


def test_fake_chunked_export_renders_segments_assembles_and_cleans(monkeypatch, tmp_path):
    # This exercises orchestration without disguising FFmpeg as a passing test:
    # the real-render case below uses the actual fixture and binaries.
    monkeypatch.setattr(engine_module, "project_root", lambda: tmp_path)
    media, settings, resolved = _large_fake_project(tmp_path)
    engine = VideoMergerEngine("ffmpeg", "ffprobe")
    calls: list[list[str]] = []

    def fake_execute(command, _media, _resolved, _progress, _log, _cancel, _transition, _workdir=None):
        calls.append(command)
        Path(command[-1]).write_bytes(b"fake mp4")

    monkeypatch.setattr(engine, "_execute", fake_execute)
    monkeypatch.setattr(
        engine_module,
        "validate_output",
        lambda path, _ffprobe, _resolved: ValidationReport(True, [], Path(path)),
    )
    output = tmp_path / "assembled.mp4"
    oversized = ["ffmpeg.exe", "x" * (SAFE_COMMAND_TARGET + 1)]
    report = engine._export_chunked(
        media, settings, resolved, output, oversized,
        lambda _event: None, lambda _message: None, threading.Event(), tmp_path,
    )
    assert report.ok
    assert output.is_file()
    assert len(calls) > 2
    assert any("-f" in call and "concat" in call for call in calls)
    assert not list((tmp_path / "temp").glob("chunked_*"))


@pytest.mark.e2e
def test_real_oversized_project_succeeds_automatically_with_ffmpeg(ffmpeg_paths, tmp_path, monkeypatch):
    """Real FFmpeg proof: 100+ selected clips are chunked and assembled.

    The Windows decision is simulated on non-Windows hosts only so the test
    validates the same production branch everywhere; FFmpeg itself remains
    real and the output is validated with the real FFprobe path.
    """
    ffmpeg, ffprobe = ffmpeg_paths
    source = tmp_path / "source.mp4"
    make_clip(ffmpeg, source, size="160x90", fps=30, duration=.35, color="blue", audio_rate=None)
    clip_paths: list[Path] = []
    for index in range(110):
        folder = tmp_path / ("pool_" + "nested_" * 8 + str(index))
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / ("selected_clip_" + "u" * 70 + f"_{index}.mp4")
        shutil.copy2(source, destination)
        clip_paths.append(destination)
    media = [fake_media(str(path), width=160, height=90, duration=.35, audio=False) for path in clip_paths]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="basic", normalize_audio=False,
        transition_duration=0.0, quality_preset="custom", crf=28, preset="ultrafast",
    )
    resolved = resolve_export(media, settings)
    command = FFmpegCommandBuilder(ffmpeg).build(media, settings, resolved, tmp_path / "full.mp4").command
    assert len(format_command_for_log(command)) > SAFE_COMMAND_TARGET
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    monkeypatch.setattr(engine, "_is_windows", lambda: True)
    logs: list[str] = []
    result = engine.export(
        media, settings, resolved, tmp_path / "chunked.mp4", log=logs.append,
    )
    assert result.ok
    assert (tmp_path / "chunked.mp4").is_file()
    assert any("automatic Chunked Rendering enabled" in line for line in logs)
    assert any("Chunk assembly" in line for line in logs)


def test_chunked_segment_failure_cleans_partial_segments(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_module, "project_root", lambda: tmp_path)
    media, settings, resolved = _large_fake_project(tmp_path)
    engine = VideoMergerEngine("ffmpeg", "ffprobe")

    def fail_segment(*_args, **_kwargs):
        raise ExportError("segment failed")

    monkeypatch.setattr(engine, "_execute", fail_segment)
    with pytest.raises(ExportError, match="segment failed"):
        engine._export_chunked(
            media, settings, resolved, tmp_path / "failed.mp4",
            ["ffmpeg.exe", "x" * (SAFE_COMMAND_TARGET + 1)],
            lambda _event: None, lambda _message: None, threading.Event(), tmp_path,
        )
    assert not list((tmp_path / "temp").glob("chunked_*"))


def test_chunked_cancellation_stops_before_first_segment_and_cleans(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_module, "project_root", lambda: tmp_path)
    media, settings, resolved = _large_fake_project(tmp_path)
    engine = VideoMergerEngine("ffmpeg", "ffprobe")
    cancel_event = threading.Event()
    cancel_event.set()
    executed = False

    def unexpected_execute(*_args, **_kwargs):
        nonlocal executed
        executed = True

    monkeypatch.setattr(engine, "_execute", unexpected_execute)
    with pytest.raises(ExportCancelled):
        engine._export_chunked(
            media, settings, resolved, tmp_path / "cancelled.mp4",
            ["ffmpeg.exe", "x" * (SAFE_COMMAND_TARGET + 1)],
            lambda _event: None, lambda _message: None, cancel_event, tmp_path,
        )
    assert not executed
    assert not list((tmp_path / "temp").glob("chunked_*"))


def test_chunked_assembly_failure_and_invalid_final_output_clean_user_master(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_module, "project_root", lambda: tmp_path)
    media, settings, resolved = _large_fake_project(tmp_path)
    engine = VideoMergerEngine("ffmpeg", "ffprobe")
    output = tmp_path / "assembly-failed.mp4"

    def fail_assembly(command, *_args, **_kwargs):
        if "concat" in command:
            raise ExportError("assembly failed")
        Path(command[-1]).write_bytes(b"fake segment")

    monkeypatch.setattr(engine, "_execute", fail_assembly)
    monkeypatch.setattr(
        engine_module, "validate_output",
        lambda path, _ffprobe, _resolved: ValidationReport(True, [], Path(path)),
    )
    with pytest.raises(ExportError, match="assembly failed"):
        engine._export_chunked(
            media, settings, resolved, output,
            ["ffmpeg.exe", "x" * (SAFE_COMMAND_TARGET + 1)],
            lambda _event: None, lambda _message: None, threading.Event(), tmp_path,
        )
    assert not output.exists()
    assert not list((tmp_path / "temp").glob("chunked_*"))

    def invalid_assembly(command, *_args, **_kwargs):
        Path(command[-1]).write_bytes(b"invalid final")

    monkeypatch.setattr(engine, "_execute", invalid_assembly)
    monkeypatch.setattr(
        engine_module, "validate_output",
        lambda path, _ffprobe, _resolved: ValidationReport(
            False, ["duration mismatch"], Path(path)
        ) if Path(path).name == output.name else ValidationReport(True, [], Path(path)),
    )
    with pytest.raises(ValidationError, match="Chunk assembly failed validation"):
        engine._export_chunked(
            media, settings, resolved, output,
            ["ffmpeg.exe", "x" * (SAFE_COMMAND_TARGET + 1)],
            lambda _event: None, lambda _message: None, threading.Event(), tmp_path,
        )
    assert not output.exists()
    assert not list((tmp_path / "temp").glob("chunked_*"))
