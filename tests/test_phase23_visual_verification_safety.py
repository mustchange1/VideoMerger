"""Phase 23 / Part A: robust visual verification that never false-fails a render.

A real Windows run produced a complete, FFprobe-validated 80.792 s MP4 with
passing burned-in subtitles, SRT and VTT — and still reported::

    SUBTITLE GENERATION FAILED [first/middle/final visual verification]:
    Verifikationsbild für final/... ist fehlgeschlagen: keine gültige PNG-Ausgabe

Root cause: the optional verification frames were sampled from the *spoken word*
timeline only. The final word of a real alignment ends at (or a few milliseconds
past) the encoded video duration, so FFmpeg was asked to seek at/after EOF,
decoded zero frames, wrote no PNG and the helper raised. That exception was
classified as a subtitle failure and the cleanup handler deleted the valid output
video, the clean master, the SRT and the VTT.

Contracts pinned here:

1. Every verification timestamp is clamped strictly inside the real video
   duration using a margin derived from the actual frame rate, with a sane
   minimum and a rule that keeps very short files valid.
2. A failed extraction is retried at strictly earlier bounded timestamps
   (bounded count, never endless, never beyond EOF).
3. A PNG counts as valid only when it exists, is non-empty and is structurally
   decodable (signature, IHDR with non-zero dimensions, IEND present).
4. Optional verification problems are reported as their own status
   (PASS/DEGRADED/FAIL/SKIPPED) and never invalidate a completed render.
5. Genuine failures still fail: missing/empty subtitle artifacts, subtitle
   creation errors and FFprobe validation errors keep the strict
   ``SUBTITLE GENERATION FAILED`` behaviour.
"""

from __future__ import annotations

import contextlib
import itertools
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.video_merger.errors import VideoMergerError
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import AlignmentResult, ExportSettings, WordTiming
from app.video_merger.subtitle_verification import (
    MAXIMUM_VERIFICATION_ATTEMPTS,
    PNG_SIGNATURE,
    VERIFICATION_LABELS,
    bounded_verification_times,
    create_visual_verification_frames,
    frame_safe_margin,
    png_frame_status,
    verify_subtitle_frames,
)
from tests.test_phase22_visual_sections import (
    FakeEngine,
    Project,
    _fake_probe,
    _real_fit_capture,
    _write,
)

# The real Windows failure: an 80.792 s container whose audio stream is 80.780 s.
CONTAINER_DURATION = 80.792
VIDEO_STREAM_DURATION = 80.780
FRAME_RATE = 30.0


def png_payload(width: int = 8, height: int = 8, *, ihdr: bool = True, iend: bool = True) -> bytes:
    """A structurally valid (or deliberately broken) PNG frame."""
    data = bytearray(PNG_SIGNATURE)
    if ihdr:
        data += struct.pack(">I", 13) + b"IHDR"
        data += struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0) + b"\x00" * 4
    if iend:
        data += struct.pack(">I", 0) + b"IEND" + b"\x00" * 4
    return bytes(data)


VALID_PNG = png_payload()


def alignment_ending_at(duration: float, *, last_word_start: float | None = None) -> AlignmentResult:
    """An alignment whose final word ends at the encoded video duration."""
    words = [
        WordTiming("Erster", duration * 0.02, min(duration * 0.12, duration * 0.02 + 0.7), 0.98),
        WordTiming("Mitte", duration * 0.5, min(duration * 0.6, duration * 0.5 + 0.6), 0.97),
        WordTiming(
            "Schlusswort",
            duration - min(0.042, duration * 0.1) if last_word_start is None else last_word_start,
            duration,
            0.96,
        ),
    ]
    return AlignmentResult(
        words=words, language="de", method="script-match",
        compatibility=1.0, average_confidence=0.97,
    )


def frame_paths(tmp_path: Path) -> dict[str, Path]:
    return {label: tmp_path / f"output.subtitle_{label}.png" for label in VERIFICATION_LABELS}


class RecordingExtractor:
    """Stands in for FFmpeg: records every seek and decides what it produces."""

    def __init__(self, *, write_file: bool = True, payload: bytes = VALID_PNG,
                 raise_error: Exception | None = None, returncode: int = 0, stderr: str = ""):
        self.seeks: list[float] = []
        self.write_file = write_file
        self.payload = payload
        self.raise_error = raise_error
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command, **_kwargs):
        timestamp = float(command[command.index("-ss") + 1])
        output = Path(command[-1])
        self.seeks.append(timestamp)
        if self.raise_error is not None:
            raise self.raise_error
        output.unlink(missing_ok=True)
        if self.write_file:
            output.write_bytes(self.payload)
        return SimpleNamespace(returncode=self.returncode, stderr=self.stderr, stdout="")


class FlakyFinalExtractor(RecordingExtractor):
    """Succeeds for first/middle, needs ``failures`` retries for the final frame."""

    def __init__(self, failures: int = 2):
        super().__init__()
        self.failures = failures
        self.final_attempts = 0

    def __call__(self, command, **kwargs):
        output = Path(command[-1])
        if "final" not in output.name:
            return super().__call__(command, **kwargs)
        self.final_attempts += 1
        self.seeks.append(float(command[command.index("-ss") + 1]))
        output.unlink(missing_ok=True)
        if self.final_attempts > self.failures:
            output.write_bytes(self.payload)
        return SimpleNamespace(returncode=0, stderr="", stdout="")


def verify(tmp_path: Path, extractor, *, alignment=None, duration=CONTAINER_DURATION,
           fps=FRAME_RATE, paths=None):
    logs: list[str] = []
    used = frame_paths(tmp_path) if paths is None else paths
    with patch("app.video_merger.subtitle_verification.subprocess.run", side_effect=extractor):
        verification = verify_subtitle_frames(
            Path("ffmpeg"), Path("output.mp4"),
            alignment_ending_at(CONTAINER_DURATION) if alignment is None else alignment,
            used, duration=duration, fps=fps, log=logs.append,
        )
    return verification, used, logs


# --------------------------------------------------------------------------- #
# 1./2. Bounded timestamps: never at or beyond EOF, always retryable
# --------------------------------------------------------------------------- #


def test_the_margin_comes_from_the_real_frame_rate_with_a_sane_minimum():
    assert frame_safe_margin(80.792, 30.0) == pytest.approx(2.0 / 30.0)
    # 60 fps: two frame periods (0.033 s) are below the floor, so the floor wins.
    assert frame_safe_margin(80.792, 60.0) == pytest.approx(0.04)
    # A 200 fps source would ask for a sub-millisecond margin: the floor keeps
    # the requested frame decodable.
    assert frame_safe_margin(80.792, 200.0) >= 0.04
    # No usable frame rate falls back to a documented default, never to zero.
    assert frame_safe_margin(80.792, None) > 0
    assert frame_safe_margin(80.792, 0.0) > 0
    assert frame_safe_margin(80.792, float("nan")) > 0


def test_a_final_word_at_the_video_end_is_never_seeked_at_or_beyond_eof(tmp_path):
    alignment = alignment_ending_at(CONTAINER_DURATION, last_word_start=CONTAINER_DURATION)
    extractor = RecordingExtractor()
    verification, _paths, _logs = verify(tmp_path, extractor, alignment=alignment)

    assert verification.status == "PASS"
    assert extractor.seeks, "no frame was requested"
    for timestamp in extractor.seeks:
        assert timestamp < CONTAINER_DURATION, "seeking at/after EOF decodes no frame"
        assert timestamp >= 0.0
    # The requested time (word start + up to 0.18 s) was clamped into the file.
    final = verification.frames[-1]
    assert final.requested > CONTAINER_DURATION - 0.05
    assert final.used is not None
    assert final.used <= CONTAINER_DURATION - frame_safe_margin(CONTAINER_DURATION, FRAME_RATE) + 1e-9


def test_a_timestamp_safely_inside_the_video_is_used_unchanged():
    times = bounded_verification_times(40.0, CONTAINER_DURATION, FRAME_RATE)
    assert times[0] == pytest.approx(40.0), "an unnecessary shift would leave the caption"
    assert len(times) == MAXIMUM_VERIFICATION_ATTEMPTS


def test_the_retry_ladder_walks_strictly_earlier_and_stays_bounded():
    times = bounded_verification_times(CONTAINER_DURATION + 5.0, CONTAINER_DURATION, FRAME_RATE)
    margin = frame_safe_margin(CONTAINER_DURATION, FRAME_RATE)
    assert len(times) == MAXIMUM_VERIFICATION_ATTEMPTS
    assert times == sorted(set(times), reverse=True)
    assert times[0] == pytest.approx(CONTAINER_DURATION - margin)
    for earlier, later in itertools.pairwise(times):
        assert later == pytest.approx(earlier - margin)
    assert all(0.0 <= value < CONTAINER_DURATION for value in times)


@pytest.mark.parametrize("duration", [0.05, 0.12, 0.4])
def test_very_short_videos_still_receive_a_valid_timestamp(tmp_path, duration):
    times = bounded_verification_times(duration, duration, FRAME_RATE)
    assert times, "a short video must still be verifiable"
    assert all(0.0 <= value < duration for value in times)
    # Degenerate candidates collapse instead of producing an endless ladder.
    assert len(times) <= MAXIMUM_VERIFICATION_ATTEMPTS
    extractor = RecordingExtractor()
    verification, _paths, _logs = verify(
        tmp_path, extractor, alignment=alignment_ending_at(duration), duration=duration,
    )
    assert verification.status == "PASS"
    assert all(timestamp < duration for timestamp in extractor.seeks)


def test_a_container_longer_than_the_video_stream_by_milliseconds_stays_decodable():
    # The bound comes from the container duration; the margin must still keep the
    # request inside the shorter video stream of the real Windows output.
    times = bounded_verification_times(VIDEO_STREAM_DURATION, CONTAINER_DURATION, FRAME_RATE)
    assert times[0] < VIDEO_STREAM_DURATION
    assert CONTAINER_DURATION - VIDEO_STREAM_DURATION < frame_safe_margin(
        CONTAINER_DURATION, FRAME_RATE
    )


def test_without_a_validated_duration_the_spoken_end_bounds_the_seek(tmp_path):
    extractor = RecordingExtractor()
    verification, _paths, _logs = verify(
        tmp_path, extractor, alignment=alignment_ending_at(12.5), duration=None, fps=None
    )
    assert verification.status == "PASS"
    assert max(extractor.seeks) < 12.5


# --------------------------------------------------------------------------- #
# 3. Retry logic
# --------------------------------------------------------------------------- #


def test_a_failed_final_frame_is_retried_at_an_earlier_bounded_timestamp(tmp_path):
    extractor = FlakyFinalExtractor(failures=2)
    logs: list[str] = []
    paths = frame_paths(tmp_path)
    with patch("app.video_merger.subtitle_verification.subprocess.run", side_effect=extractor):
        verification = verify_subtitle_frames(
            Path("ffmpeg"), Path("output.mp4"), alignment_ending_at(CONTAINER_DURATION),
            paths, duration=CONTAINER_DURATION, fps=FRAME_RATE, log=logs.append,
        )
    final = verification.frames[-1]
    assert final.ok is True
    assert final.attempts == 3, "bounded retries stop as soon as a frame decodes"
    assert final.used is not None and final.used < final.requested
    assert verification.status == "PASS"
    assert len(verification.paths) == 3
    fallback_line = next(line for line in logs if line.startswith("Visual verification final:"))
    assert "fallback=" in fallback_line and "PNG=PASS" in fallback_line
    assert len(logs) == len(VERIFICATION_LABELS), "one concise line per frame"


def test_the_retry_count_is_bounded_and_never_endless(tmp_path):
    extractor = FlakyFinalExtractor(failures=MAXIMUM_VERIFICATION_ATTEMPTS + 5)
    verification, _paths, logs = verify(tmp_path, extractor)
    assert extractor.final_attempts == MAXIMUM_VERIFICATION_ATTEMPTS
    assert verification.frames[-1].ok is False
    assert verification.status == "DEGRADED"
    assert any("PNG=FAIL" in line for line in logs)


def test_all_bounded_attempts_failing_is_reported_and_never_raised(tmp_path):
    extractor = RecordingExtractor(write_file=False)
    verification, paths, logs = verify(tmp_path, extractor)
    assert verification.status == "FAIL"
    assert verification.paths == []
    assert all(frame.ok is False for frame in verification.frames)
    assert len(extractor.seeks) == len(VERIFICATION_LABELS) * MAXIMUM_VERIFICATION_ATTEMPTS
    assert all(timestamp < CONTAINER_DURATION for timestamp in extractor.seeks)
    assert sum("PNG=FAIL" in line for line in logs) == len(VERIFICATION_LABELS)
    assert all(path.is_file() is False for path in paths.values()), "invalid output must be removed"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "zero bytes"),
        (png_payload(iend=False), "truncated (no IEND)"),
        (png_payload(width=0, height=0), "empty frame (0x0)"),
        (b"\x89PNG\r\n\x1a\n" + b"0" * 32, "no IHDR chunk"),
    ],
)
def test_an_invalid_frame_is_retried_and_reported_with_its_reason(tmp_path, payload, reason):
    extractor = RecordingExtractor(payload=payload)
    verification, _paths, logs = verify(tmp_path, extractor)
    assert verification.status == "FAIL"
    assert all(frame.detail == reason for frame in verification.frames)
    assert all("PNG=FAIL" in line for line in logs)


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60), OSError("ffmpeg not found")],
)
def test_an_extractor_crash_is_a_warning_not_an_exception(tmp_path, error):
    extractor = RecordingExtractor(raise_error=error)
    verification, _paths, logs = verify(tmp_path, extractor)
    assert verification.status == "FAIL"
    assert verification.paths == []
    assert all("extraction error" in frame.detail for frame in verification.frames)
    assert all("PNG=FAIL" in line for line in logs)


def test_a_non_zero_ffmpeg_exit_code_is_reported_with_its_stderr(tmp_path):
    extractor = RecordingExtractor(
        write_file=False, returncode=1, stderr="Invalid data found when processing input"
    )
    verification, _paths, _logs = verify(tmp_path, extractor)
    assert verification.status == "FAIL"
    assert "Invalid data found" in verification.frames[0].detail


# --------------------------------------------------------------------------- #
# 4. PNG validity
# --------------------------------------------------------------------------- #


def test_a_valid_png_is_accepted(tmp_path):
    ok, detail = png_frame_status(_write(tmp_path / "frame.png", VALID_PNG))
    assert ok is True
    assert detail == "8x8"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "zero bytes"),
        (b"not a png at all" * 4, "not a PNG"),
        (png_payload(iend=False), "truncated (no IEND)"),
        (png_payload(ihdr=False), "no IHDR chunk"),
        (png_payload(width=0), "empty frame (0x8)"),
        (VALID_PNG[:20], "no IHDR chunk"),
    ],
)
def test_a_zero_byte_or_corrupt_png_is_never_valid(tmp_path, payload, reason):
    path = tmp_path / "frame.png"
    path.write_bytes(payload)
    assert png_frame_status(path) == (False, reason)


def test_a_missing_png_is_not_valid(tmp_path):
    assert png_frame_status(tmp_path / "absent.png") == (False, "no file written")


def test_the_compatibility_helper_returns_only_decodable_frames(tmp_path):
    extractor = RecordingExtractor(write_file=False)
    logs: list[str] = []
    with patch("app.video_merger.subtitle_verification.subprocess.run", side_effect=extractor):
        frames = create_visual_verification_frames(
            Path("ffmpeg"), Path("output.mp4"), alignment_ending_at(CONTAINER_DURATION),
            frame_paths(tmp_path), duration=CONTAINER_DURATION, fps=FRAME_RATE, log=logs.append,
        )
    assert frames == []
    assert len(logs) == len(VERIFICATION_LABELS)


def test_an_alignment_without_words_is_still_rejected(tmp_path):
    empty = AlignmentResult(words=[], language="de", method="script-match",
                            compatibility=0.0, average_confidence=0.0)
    with pytest.raises(VideoMergerError, match="Keine Wörter"):
        verify_subtitle_frames(
            Path("ffmpeg"), Path("output.mp4"), empty, frame_paths(tmp_path),
            duration=CONTAINER_DURATION, fps=FRAME_RATE,
        )


# --------------------------------------------------------------------------- #
# 5. Pipeline classification: a valid render survives a failed verification
# --------------------------------------------------------------------------- #


def _frames_fake(count: int):
    def fake(_ffmpeg, _video, _alignment, paths, **_kwargs):
        return [_write(paths[label], VALID_PNG) for label in list(paths)[:count]]

    return fake


# Module-level singleton: a call in a default argument is evaluated once at
# import time and is rejected by the project's lint rules.
THREE_FRAMES = _frames_fake(3)


def _run_pipeline(tmp_path: Path, settings: ExportSettings, *, frames=THREE_FRAMES,
                  engine_hook=None, timeline_writer=None):
    """Run the real pipeline with a fake FFmpeg engine and controllable evidence."""
    project = Project(tmp_path)
    record: dict = {}
    engine = FakeEngine(tmp_path, record)
    if engine_hook is not None:
        engine_hook(engine)
    workflow = MainProjectEngine(engine)
    logs: list[str] = []
    patches = [
        patch("app.video_merger.main_project.probe_audio", side_effect=_fake_probe(project)),
        patch("app.video_merger.main_project.fit_media_to_duration",
              side_effect=_real_fit_capture(record)),
        patch("app.video_merger.main_project.create_visual_verification_frames",
              side_effect=frames),
    ]
    if timeline_writer is not None:
        patches.append(patch("app.video_merger.main_project.write_canonical_timeline",
                             side_effect=timeline_writer))
    with contextlib.ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        result = workflow.create_youtube_exports(
            project.media, settings, tmp_path / "output",
            aligner=project.aligner(), log=logs.append,
        )
    return SimpleNamespace(result=result, record=record, logs=logs, output_dir=tmp_path / "output")


def test_a_successful_render_is_retained_when_every_verification_frame_fails(tmp_path):
    project = Project(tmp_path)
    run = _run_pipeline(tmp_path, project.settings(), frames=_frames_fake(0))
    main = run.result.long_form

    assert main.video.is_file() and main.video.stat().st_size > 0
    assert main.srt is not None and main.srt.is_file()
    assert main.vtt is not None and main.vtt.is_file()
    assert main.canonical_timeline is not None and main.canonical_timeline.is_file()
    assert main.report.ok is True
    assert main.verification_status == "FAIL"
    assert main.verification_frames == []
    joined = "\n".join(run.logs)
    assert "SUBTITLE GENERATION FAILED" not in joined
    assert "Subtitle Generation: PASS" in joined
    assert "Burned-In Subtitles: PASS" in joined
    assert "rendered output retained · overall render status=SUCCESS" in joined
    assert "PNG=FAIL" in joined
    # One summary line: the pipeline does not add per-frame spam of its own.
    assert joined.count("overall render status=SUCCESS") == 1


def test_a_crash_inside_verification_is_also_survivable(tmp_path):
    def exploding(*_args, **_kwargs):
        raise RuntimeError("ffmpeg crashed while decoding the final frame")

    project = Project(tmp_path)
    run = _run_pipeline(tmp_path, project.settings(), frames=exploding)
    main = run.result.long_form
    assert main.video.is_file()
    assert main.srt is not None and main.srt.is_file()
    assert main.verification_status == "FAIL"
    assert any(line.startswith("WARNUNG: Visuelle Verifikation nicht möglich") for line in run.logs)
    assert "SUBTITLE GENERATION FAILED" not in "\n".join(run.logs)


def test_a_partial_verification_keeps_the_render_successful(tmp_path):
    project = Project(tmp_path)
    run = _run_pipeline(tmp_path, project.settings(), frames=_frames_fake(2))
    main = run.result.long_form
    assert main.video.is_file()
    assert main.verification_status == "DEGRADED"
    assert len(main.verification_frames) == 2
    assert "PNG=DEGRADED · 2/3 frames decoded" in "\n".join(run.logs)


def test_a_complete_verification_reports_three_frames_as_before(tmp_path):
    project = Project(tmp_path)
    run = _run_pipeline(tmp_path, project.settings())
    main = run.result.long_form
    assert main.verification_status == "PASS"
    assert len(main.verification_frames) == 3
    assert all(path.is_file() for path in main.verification_frames)
    assert "Visual verification frames (decoded from final MP4, internal evidence)" in "\n".join(run.logs)


def test_a_render_without_subtitles_skips_verification(tmp_path):
    project = Project(tmp_path)
    run = _run_pipeline(
        tmp_path, project.settings(subtitle_output_mode="without_subtitles", subtitle_enabled=False)
    )
    assert run.result.long_form.verification_status == "SKIPPED"
    assert run.result.long_form.verification_frames == []


def test_a_genuine_subtitle_artifact_failure_still_fails_the_job(tmp_path):
    def empty_timeline(_script, _alignment, _cues, path):
        Path(path).write_bytes(b"")

    project = Project(tmp_path)
    with pytest.raises(VideoMergerError) as error:
        _run_pipeline(tmp_path, project.settings(), timeline_writer=empty_timeline)
    message = str(error.value)
    assert message.startswith("SUBTITLE GENERATION FAILED")
    assert "subtitle output artifacts" in message
    # A genuinely incomplete subtitle bundle is still cleaned up.
    assert not list((tmp_path / "output").glob("*.mp4"))


def test_a_genuine_subtitle_creation_failure_still_fails_the_job(tmp_path):
    def broken_timeline(*_args, **_kwargs):
        raise VideoMergerError("Untertitel-Zeitachse ist ungültig.")

    project = Project(tmp_path)
    with pytest.raises(VideoMergerError) as error:
        _run_pipeline(tmp_path, project.settings(), timeline_writer=broken_timeline)
    assert str(error.value).startswith("SUBTITLE GENERATION FAILED")
    assert "SRT/VTT/ASS timeline creation" in str(error.value)


def test_an_ffprobe_validation_failure_still_fails_the_job(tmp_path):
    def invalid_output(engine):
        def export(*_args, **_kwargs):
            raise VideoMergerError("FFprobe-Validierung fehlgeschlagen: Kein Audiostream in der Ausgabe.")

        engine.export = export

    project = Project(tmp_path)
    with pytest.raises(VideoMergerError) as error:
        _run_pipeline(tmp_path, project.settings(), engine_hook=invalid_output)
    assert str(error.value).startswith("SUBTITLE GENERATION FAILED")
    assert "FFprobe-Validierung fehlgeschlagen" in str(error.value)
    assert not list((tmp_path / "output").glob("*.mp4"))


def test_a_burned_subtitle_failure_is_still_critical(tmp_path):
    def broken_burn(engine):
        def burn_subtitles(*_args, **_kwargs):
            raise VideoMergerError("subtitles filter failed")

        engine.burn_subtitles = burn_subtitles

    project = Project(tmp_path)
    with pytest.raises(VideoMergerError) as error:
        _run_pipeline(
            tmp_path,
            project.settings(subtitle_output_mode="with_and_without_subtitles"),
            engine_hook=broken_burn,
        )
    assert str(error.value).startswith("SUBTITLE GENERATION FAILED")
    assert "subtitle burn-in pass" in str(error.value)
