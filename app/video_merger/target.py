from __future__ import annotations

import math
import re
from collections import Counter

from .errors import VideoMergerError
from .models import ExportSettings, MediaSequence, ResolvedExport

_RESOLUTION_RE = re.compile(r"^\s*(\d+)\s*[x×]\s*(\d+)\s*$", re.IGNORECASE)
_COMMON_FPS = [(23.976, "24000/1001"), (24.0, "24"), (25.0, "25"), (29.97, "30000/1001"), (30.0, "30"), (50.0, "50"), (59.94, "60000/1001"), (60.0, "60")]


def parse_resolution(value: str) -> tuple[int, int]:
    match = _RESOLUTION_RE.match(value)
    if not match:
        raise VideoMergerError(f"Ungültige Zielauflösung: {value}")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 16 or height < 16 or width > 7680 or height > 7680:
        raise VideoMergerError(f"Zielauflösung außerhalb des erlaubten Bereichs: {value}")
    # H.264/yuv420p requires even dimensions.
    return width - width % 2, height - height % 2


def _auto_resolution(media: MediaSequence, aspect: str) -> tuple[int, int]:
    target_ratio = 16 / 9 if aspect == "16:9" else 9 / 16
    first_dims = {(m.effective_width, m.effective_height) for m in media}
    if len(first_dims) == 1:
        width, height = next(iter(first_dims))
        if abs(width / height - target_ratio) < 0.015:
            return width - width % 2, height - height % 2
    max_pixels = max(m.effective_width * m.effective_height for m in media)
    max_long = max(max(m.effective_width, m.effective_height) for m in media)
    if aspect == "16:9":
        if max_pixels >= 7_000_000 or max_long >= 3400:
            return 3840, 2160
        if max_pixels >= 3_000_000 or max_long >= 2300:
            return 2560, 1440
        if max_pixels >= 1_300_000 or max_long >= 1500:
            return 1920, 1080
        return 1280, 720
    if max_pixels >= 7_000_000 or max_long >= 3400:
        return 2160, 3840
    if max_pixels < 800_000 and max_long < 1500:
        return 720, 1280
    return 1080, 1920


def _canonical_fps(value: float) -> tuple[float, str]:
    best = min(_COMMON_FPS, key=lambda item: abs(item[0] - value))
    if abs(best[0] - value) <= 0.08:
        return best
    rounded = max(1.0, min(120.0, round(value, 3)))
    return rounded, f"{rounded:g}"


def choose_fps(media: MediaSequence, choice: str) -> tuple[float, str]:
    if choice and choice.casefold() not in {"auto", "source", "source / auto"}:
        try:
            value = float(choice.replace(",", "."))
        except ValueError as exc:
            raise VideoMergerError(f"Ungültige Framerate: {choice}") from exc
        return _canonical_fps(value)
    values = [m.fps for m in media]
    if max(values) - min(values) <= 0.08:
        return _canonical_fps(sum(values) / len(values))
    # 25/50 and 30/60 families have an obvious common cadence.
    canonical_values = [_canonical_fps(v)[0] for v in values]
    if all(abs(v / 25 - round(v / 25)) < 0.01 for v in canonical_values):
        return 25.0, "25"
    if all(abs(v / 30 - round(v / 30)) < 0.01 for v in canonical_values):
        return 30.0, "30"
    return 30.0, "30"


def safe_transition_durations(durations: list[float], requested: float, fps: float) -> tuple[list[float], list[float]]:
    if not durations:
        return [], []
    minimum_visual = max(0.12, 3.0 / max(fps, 1.0))
    effective = [max(float(duration), minimum_visual) for duration in durations]
    transitions: list[float] = []
    requested = max(0.0, min(float(requested), 5.0))
    if requested == 0:
        return effective, [0.0 for _ in range(max(0, len(effective) - 1))]
    for left, right in zip(effective, effective[1:]):
        # Keeping each adjacent transition below 45% ensures that the in/out
        # ramps of very short middle clips never overlap completely.
        value = min(requested, left * 0.45, right * 0.45)
        transitions.append(max(0.01, round(value, 6)))
    return effective, transitions


def resolve_export(media: MediaSequence, settings: ExportSettings) -> ResolvedExport:
    if not media:
        raise VideoMergerError("Es wurden keine analysierten Clips übergeben.")
    if settings.aspect not in {"16:9", "9:16"}:
        raise VideoMergerError(f"Unbekanntes Seitenverhältnis: {settings.aspect}")
    if settings.resolution.casefold() == "auto":
        width, height = _auto_resolution(media, settings.aspect)
    else:
        width, height = parse_resolution(settings.resolution)
    if settings.aspect == "16:9" and width <= height:
        raise VideoMergerError("Für 16:9 muss die Breite größer als die Höhe sein.")
    if settings.aspect == "9:16" and width >= height:
        raise VideoMergerError("Für 9:16 muss die Höhe größer als die Breite sein.")
    fps, fps_expr = choose_fps(media, settings.fps_choice)
    effective, transitions = safe_transition_durations([m.duration for m in media], settings.transition_duration, fps)
    chain_duration = max(0.0, sum(effective) - sum(transitions))
    # Stage 1 has an authoritative voiceover-derived endpoint. The selected
    # sequence may intentionally extend a few frames beyond it (for example
    # when the target lands inside a loop-boundary transition); the final graph
    # trims at this exact target instead of lengthening the user's program.
    if settings.workflow_stage == "main" and settings.timeline_target_duration > 0:
        expected_duration = float(settings.timeline_target_duration)
    else:
        expected_duration = chain_duration
    warnings: list[str] = []
    if len({round(m.fps, 2) for m in media}) > 1 and settings.fps_choice.casefold() in {"auto", "source", "source / auto"}:
        warnings.append(f"Gemischte Frameraten erkannt; Ziel-Framerate: {fps:g} fps.")
    for index, (actual, adjusted) in enumerate(zip((m.duration for m in media), effective), start=1):
        if adjusted > actual + 0.001:
            warnings.append(f"Clip {index} ist extrem kurz und wird auf {adjusted:.3f} s verlängert.")
    return ResolvedExport(
        width=width,
        height=height,
        fps=fps,
        fps_expr=fps_expr,
        effective_durations=effective,
        transitions=transitions,
        expected_duration=expected_duration,
        warnings=warnings,
    )
