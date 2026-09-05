from __future__ import annotations

import math
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import VideoMergerError
from .models import AlignmentResult
from .platform_utils import hidden_process_flags, safe_subprocess_env

LogCallback = Callable[[str], None]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
VERIFICATION_LABELS = ("first", "middle", "final")
# Two frame periods keep a timestamp safely inside the encoded stream even when
# the container duration is a few milliseconds longer than the video stream
# (real Windows run: container 80.792 s, audio 80.780 s). 40 ms is the floor for
# sources without a usable frame rate.
MINIMUM_FRAME_MARGIN_SECONDS = 0.04
DEFAULT_VERIFICATION_FPS = 25.0
# The requested timestamp plus three strictly earlier fallbacks: bounded, never
# an endless retry loop.
MAXIMUM_VERIFICATION_ATTEMPTS = 4
FRAME_EXTRACTION_TIMEOUT_SECONDS = 60


def _verification_time(start: float, end: float) -> float:
    if end <= start:
        return max(0.0, start + 0.02)
    return max(0.0, start + min((end - start) * 0.5, 0.18))


def frame_safe_margin(duration: float, fps: float | None = None) -> float:
    """Distance before the end of a file that still decodes a real frame.

    Derived from the actual frame rate when one is known, with a sane minimum,
    and shrunk for very short videos so the resulting timestamp stays valid.
    """
    try:
        rate = float(fps) if fps else 0.0
    except (TypeError, ValueError):
        rate = 0.0
    if not math.isfinite(rate) or rate <= 0:
        rate = DEFAULT_VERIFICATION_FPS
    margin = max(2.0 / rate, MINIMUM_FRAME_MARGIN_SECONDS)
    try:
        length = float(duration) if duration else 0.0
    except (TypeError, ValueError):
        length = 0.0
    if not math.isfinite(length) or length <= 0:
        return margin
    # A 0.05 s clip must not be asked for a negative or empty timestamp.
    return min(margin, max(length * 0.25, 0.001))


def bounded_verification_times(
    requested: float,
    duration: float,
    fps: float | None = None,
    attempts: int = MAXIMUM_VERIFICATION_ATTEMPTS,
) -> list[float]:
    """Requested timestamp plus strictly earlier, always-decodable fallbacks.

    No candidate is ever at or beyond the real video duration and none is
    negative, so FFmpeg is never asked to seek past EOF.
    """
    margin = frame_safe_margin(duration, fps)
    try:
        wanted = float(requested)
    except (TypeError, ValueError):
        wanted = 0.0
    if not math.isfinite(wanted):
        wanted = 0.0
    try:
        length = float(duration) if duration else 0.0
    except (TypeError, ValueError):
        length = 0.0
    latest = max(0.0, length - margin) if math.isfinite(length) and length > 0 else max(0.0, wanted)
    start = min(max(0.0, wanted), latest)
    times: list[float] = []
    for step in range(max(1, int(attempts))):
        candidate = max(0.0, start - step * margin)
        if not any(abs(candidate - known) < 1e-9 for known in times):
            times.append(candidate)
    return times


def png_frame_status(path: Path) -> tuple[bool, str]:
    """Validate a verification PNG structurally: present, non-empty, decodable.

    A zero-byte or truncated file is never accepted as a valid frame.
    """
    if not path.is_file():
        return False, "no file written"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return False, f"unreadable ({exc})"
    if not data:
        return False, "zero bytes"
    if not data.startswith(PNG_SIGNATURE):
        return False, "not a PNG"
    if len(data) < 33 or data[12:16] != b"IHDR":
        return False, "no IHDR chunk"
    width, height = struct.unpack(">II", data[16:24])
    if width == 0 or height == 0:
        return False, f"empty frame ({width}x{height})"
    if b"IEND" not in data[-16:]:
        return False, "truncated (no IEND)"
    return True, f"{width}x{height}"


@dataclass(slots=True)
class FrameVerification:
    """One first/middle/final extraction attempt series and its outcome."""

    label: str
    word: str
    requested: float
    used: float | None = None
    attempts: int = 0
    path: Path | None = None
    ok: bool = False
    detail: str = ""

    def log_line(self) -> str:
        if self.ok:
            fallback = (
                "" if self.used is None or abs(self.used - self.requested) < 1e-6
                else f" · fallback={self.used:.6f}"
            )
            return (
                f"Visual verification {self.label}: requested={self.requested:.6f}"
                f"{fallback} · PNG=PASS ({self.detail})"
            )
        return (
            f"Visual verification {self.label}: requested={self.requested:.6f} · "
            f"{self.attempts} bounded attempts · PNG=FAIL ({self.detail or 'no decodable frame'})"
        )


@dataclass(slots=True)
class VisualVerification:
    """Optional internal quality evidence for an already valid render."""

    frames: list[FrameVerification] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        return [frame.path for frame in self.frames if frame.ok and frame.path is not None]

    @property
    def status(self) -> str:
        if not self.frames:
            return "SKIPPED"
        succeeded = sum(1 for frame in self.frames if frame.ok)
        if succeeded == len(self.frames):
            return "PASS"
        return "DEGRADED" if succeeded else "FAIL"

    def log_lines(self) -> list[str]:
        return [frame.log_line() for frame in self.frames]


def _extract_frame(ffmpeg: Path, video: Path, timestamp: float, output: Path) -> tuple[bool, str]:
    """Decode one frame at a bounded timestamp; problems are returned, not raised."""
    output.unlink(missing_ok=True)
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{timestamp:.6f}",
        "-i", str(video), "-map", "0:v:0", "-frames:v", "1", "-update", "1",
        str(output),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=FRAME_EXTRACTION_TIMEOUT_SECONDS, creationflags=hidden_process_flags(),
            env=safe_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.unlink(missing_ok=True)
        return False, f"extraction error: {exc}"
    if result.returncode != 0:
        output.unlink(missing_ok=True)
        return False, f"ffmpeg rc={result.returncode}: {result.stderr.strip()[:180]}"
    valid, detail = png_frame_status(output)
    if not valid:
        output.unlink(missing_ok=True)
    return valid, detail


def _reference_duration(duration: float | None, alignment: AlignmentResult) -> float:
    """The bound used for clamping: the validated output duration when known."""
    try:
        value = float(duration) if duration else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if math.isfinite(value) and value > 0:
        return value
    # Without a validated duration the spoken end is the only reliable bound.
    return max((float(word.end) for word in alignment.words), default=0.0)


def verify_subtitle_frames(
    ffmpeg: Path,
    video: Path,
    alignment: AlignmentResult,
    paths: dict[str, Path],
    *,
    duration: float | None = None,
    fps: float | None = None,
    log: LogCallback = lambda _message: None,
) -> VisualVerification:
    """Extract real output frames during the first/middle/final spoken words.

    The frames come from the already encoded and validated final MP4, not from a
    preview or a source clip. Every timestamp is clamped strictly inside the real
    video duration and retried at earlier bounded timestamps, so a frame that
    cannot be decoded is reported as a verification warning instead of turning a
    successful render into a subtitle failure.
    """
    if not alignment.words:
        raise VideoMergerError("Keine Wörter für visuelle Subtitle-Verifikation vorhanden.")
    indexes = [0, len(alignment.words) // 2, len(alignment.words) - 1]
    reference = _reference_duration(duration, alignment)
    verification = VisualVerification()
    for label, index in zip(VERIFICATION_LABELS, indexes):
        word = alignment.words[index]
        frame = FrameVerification(
            label=label, word=str(word.text),
            requested=_verification_time(word.start, word.end),
        )
        output = paths[label]
        output.parent.mkdir(parents=True, exist_ok=True)
        for timestamp in bounded_verification_times(frame.requested, reference, fps):
            frame.attempts += 1
            frame.used = timestamp
            frame.ok, frame.detail = _extract_frame(ffmpeg, video, timestamp, output)
            if frame.ok:
                frame.path = output
                break
        verification.frames.append(frame)
        log(frame.log_line())
    return verification


def create_visual_verification_frames(
    ffmpeg: Path,
    video: Path,
    alignment: AlignmentResult,
    paths: dict[str, Path],
    *,
    duration: float | None = None,
    fps: float | None = None,
    log: LogCallback = lambda _message: None,
) -> list[Path]:
    """Decoded verification frames of one rendered output (compatibility helper).

    Frames that could not be extracted are simply absent from the result; the
    caller decides how to classify that, and a completed valid render is never
    discarded because of it.
    """
    return verify_subtitle_frames(
        ffmpeg, video, alignment, paths, duration=duration, fps=fps, log=log,
    ).paths
