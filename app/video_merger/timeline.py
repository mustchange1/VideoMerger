from __future__ import annotations

from dataclasses import replace

from .models import MediaInfo
from .target import safe_transition_durations


def _source_copy(item: MediaInfo, minimum: float) -> MediaInfo:
    source = item.source_duration or item.duration
    return replace(item, source_duration=source, duration=max(source, minimum))


def _duration(media: list[MediaInfo], transition_duration: float, fps: float) -> float:
    effective, transitions = safe_transition_durations(
        [clip.duration for clip in media], transition_duration, fps
    )
    return sum(effective) - sum(transitions)


def _trim_last_to_target(
    selected: list[MediaInfo], target: float, transition_duration: float, fps: float,
    minimum: float,
) -> None:
    """Reduce the last occurrence so the logical chain ends at target.

    Transition clamping depends on both neighboring durations, therefore this
    is solved iteratively. The final FFmpeg graph additionally trims to the
    authoritative target at frame precision; this helper avoids decoding an
    unnecessarily long final occurrence.
    """
    for _ in range(16):
        current = _duration(selected, transition_duration, fps)
        delta = target - current
        if abs(delta) <= max(0.0005, 0.1 / fps):
            return
        last = selected[-1]
        source = last.source_duration or last.duration
        candidate = max(minimum, min(source, last.duration + delta))
        if abs(candidate - last.duration) < 0.0001:
            return
        selected[-1] = replace(last, duration=candidate)


def fit_media_to_duration(
    media: list[MediaInfo],
    target_duration: float,
    transition_duration: float,
    fps: float,
    short_video_mode: str = "hold",
) -> tuple[list[MediaInfo], list[str]]:
    """Build an exact voiceover-driven visual sequence without changing order.

    ``hold`` plays the active timeline once and extends only its final rendered
    frame when material is short. ``loop`` repeats the *entire* active ordered
    timeline (for example C→A→D→B→C→A→D→B), including the selected transition
    at every loop boundary. The final repeated occurrence is trimmed and the
    render graph is constrained to the authoritative target duration.
    """
    if not media:
        raise ValueError("Keine Videoclips für die Haupt-Timeline vorhanden.")
    if short_video_mode not in {"hold", "loop"}:
        raise ValueError(f"Unbekannter Modus für kurzes Video: {short_video_mode}")

    target = max(0.12, float(target_duration))
    minimum = max(0.12, 3.0 / max(fps, 1.0))
    originals = [_source_copy(item, minimum) for item in media]
    one_pass_duration = _duration(originals, transition_duration, fps)
    warnings: list[str] = []

    selected: list[MediaInfo] = []
    if target <= one_pass_duration + 1e-6:
        # Preserve the shortest ordered prefix needed for the voiceover target.
        for item in originals:
            selected.append(item)
            if _duration(selected, transition_duration, fps) >= target - 1e-6:
                break
        _trim_last_to_target(selected, target, transition_duration, fps, minimum)
        if len(selected) < len(originals) or selected[-1].duration < (selected[-1].source_duration or selected[-1].duration) - 0.01:
            warnings.append(
                "Videomaterial ist länger als die Voiceover-Timeline; die aktive Reihenfolge bleibt erhalten "
                "und der letzte benötigte Clip wird passend gekürzt."
            )
        return selected, warnings

    if short_video_mode == "hold":
        selected = list(originals)
        shortage = target - one_pass_duration
        final = selected[-1]
        selected[-1] = replace(final, duration=final.duration + shortage)
        # Transition clamping can alter the exact shortage for extremely short
        # clips; converge without limiting the held duration to source length.
        for _ in range(16):
            delta = target - _duration(selected, transition_duration, fps)
            if abs(delta) <= max(0.0005, 0.1 / fps):
                break
            selected[-1] = replace(selected[-1], duration=max(minimum, selected[-1].duration + delta))
        warnings.append(
            f"Videomaterial ist {shortage:.3f} s kürzer als die Ziel-Timeline; "
            "Hold Last Frame verlängert ausschließlich den finalen gerenderten Frame."
        )
        return selected, warnings

    # Full-timeline loop: append occurrences in the exact active manual order.
    # A high safety limit prevents pathological projects made entirely from
    # sub-frame files from creating an unbounded direct filter graph.
    occurrence = 0
    while _duration(selected, transition_duration, fps) < target - 1e-6:
        selected.append(_source_copy(originals[occurrence % len(originals)], minimum))
        occurrence += 1
        if occurrence > 10_000:
            raise ValueError("Die Loop-Timeline würde mehr als 10.000 Clip-Vorkommen benötigen.")
    _trim_last_to_target(selected, target, transition_duration, fps, minimum)
    rounds = occurrence / len(originals)
    warnings.append(
        "Full-Timeline Loop aktiv: die vollständige manuelle Reihenfolge wird ab dem ersten Clip "
        f"wiederholt ({occurrence} Vorkommen, {rounds:.2f} Durchläufe); auch die Loop-Grenze "
        "verwendet den ausgewählten Übergang."
    )
    return selected, warnings
