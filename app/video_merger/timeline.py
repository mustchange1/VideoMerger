from __future__ import annotations

from dataclasses import replace

from .models import MediaInfo
from .target import safe_transition_durations


DEFAULT_DURATION_BEFORE_MERGE = 0.70
DEFAULT_DURATION_AFTER_MERGE = 1.00


def duration_before_merge_value(settings) -> float:
    """Return the one canonical Before Merge multiplier.

    ``video_speed`` is accepted only as a migration/API compatibility value.
    A legacy caller that explicitly changed it away from 1.0 still receives
    the old behavior; new settings use the requested 0.70 default.
    """
    try:
        value = float(getattr(settings, "duration_before_merge", DEFAULT_DURATION_BEFORE_MERGE))
    except (TypeError, ValueError):
        value = DEFAULT_DURATION_BEFORE_MERGE
    try:
        legacy = float(getattr(settings, "video_speed", 1.0))
    except (TypeError, ValueError):
        legacy = 1.0
    if abs(value - DEFAULT_DURATION_BEFORE_MERGE) <= 1e-9 and abs(legacy - 1.0) > 1e-9:
        value = legacy
    return max(0.25, min(4.0, value))


def duration_after_merge_value(settings) -> float:
    """Return the independent post-merge multiplier."""
    try:
        value = float(getattr(settings, "duration_after_merge", DEFAULT_DURATION_AFTER_MERGE))
    except (TypeError, ValueError):
        value = DEFAULT_DURATION_AFTER_MERGE
    return max(0.25, min(4.0, value))


def after_merge_enabled(settings) -> bool:
    return bool(
        getattr(settings, "duration_after_merge_enabled", False)
        and abs(duration_after_merge_value(settings) - 1.0) > 1e-9
    )


def _source_copy(item: MediaInfo, minimum: float, playback_rate: float = 1.0) -> MediaInfo:
    source = item.source_duration or item.duration
    if abs(playback_rate - 1.0) <= 1e-6:
        return replace(item, source_duration=source, duration=max(source, minimum))
    # Global Main Video speed: the timeline duration is the scaled source;
    # the render graph applies the rate via setpts/atempo. The minimum clamp
    # protects extremely short clips independently of the rate.
    return replace(
        item,
        source_duration=source,
        duration=max(minimum, source / playback_rate),
        playback_rate=playback_rate,
    )


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


def _stretch_last_to_target(
    selected: list[MediaInfo], target: float, transition_duration: float, fps: float,
    minimum: float, max_stretch_percent: float, natural_last: float,
) -> float | None:
    """Extend ONLY the final occurrence by slowing it down (1.3.0).

    ``natural_last`` is the clip's natural timeline duration at the currently
    configured global speed (its speed-scaled source length). The last clip
    keeps its complete source content and plays slower so the logical chain
    ends exactly at target. Returns the final timeline duration on success;
    returns None when the required stretch would exceed ``max_stretch_percent``
    or would need a cut — the caller then falls back to the proven trimming
    behavior (never to Hold Last Frame).
    """
    limit = max(0.0, min(50.0, float(max_stretch_percent))) / 100.0
    for _ in range(16):
        current = _duration(selected, transition_duration, fps)
        delta = target - current
        if abs(delta) <= max(0.0005, 0.1 / fps):
            break
        candidate = selected[-1].duration + delta
        if candidate < natural_last - 1e-9 or candidate < minimum:
            return None
        if (candidate - natural_last) / max(natural_last, 1e-9) > limit + 1e-9:
            return None
        selected[-1] = replace(selected[-1], duration=candidate)
    final = selected[-1].duration
    stretch = (final - natural_last) / max(natural_last, 1e-9)
    if final < natural_last - 1e-6 or stretch > limit + 1e-9:
        return None
    return final


def fit_media_to_duration(
    media: list[MediaInfo],
    target_duration: float,
    transition_duration: float,
    fps: float,
    short_video_mode: str = "hold",
    duration_fit_mode: str = "cut",
    max_stretch_percent: float = 10.0,
    playback_rate: float = 1.0,
    folder_aware: bool = True,
    *,
    video_order_mode: str | None = None,
    video_order_rng=None,
    video_order_seed: int | None = None,
    legacy_root: object = None,
) -> tuple[list[MediaInfo], list[str]]:
    """Build an exact voiceover-driven visual sequence without changing order.

    ``hold`` plays the active timeline once and extends only its final rendered
    frame when material is short. ``loop`` repeats the *entire* active ordered
    timeline (for example C→A→D→B→C→A→D→B), including the selected transition
    at every loop boundary. The final repeated occurrence is trimmed and the
    render graph is constrained to the authoritative target duration.

    1.3.0 ``duration_fit_mode``:

    * ``cut`` (default) keeps the exact proven 1.2.4 behavior — the shortest
      ordered prefix covers the target and its last occurrence is trimmed.
    * ``stretch`` prefers the prefix ONE CLIP SHORTER and slows only its
      final occurrence as much as necessary so the complete source content
      fills the target exactly (avoids rendering a short sliver of an extra
      clip; bounded by ``max_stretch_percent`` relative to the configured
      playback speed). Transitions, order and visual continuity are
      preserved; no Hold Last Frame is ever introduced. When the required
      stretch exceeds the limit — or nothing needs stretching — the function
      falls back to the normal trimming behavior.

    1.3.0 ``playback_rate``: global Main Video speed (0.5–2.0). Every
    occurrence's timeline duration is the scaled source and carries the rate;
    the voiceover-driven target itself never changes.
    """
    if not media:
        raise ValueError("Keine Videoclips für die Haupt-Timeline vorhanden.")
    if short_video_mode not in {"hold", "loop"}:
        raise ValueError(f"Unbekannter Modus für kurzes Video: {short_video_mode}")
    if duration_fit_mode not in {"cut", "stretch"}:
        raise ValueError(f"Unbekannter Duration-Fit-Modus: {duration_fit_mode}")

    target = max(0.12, float(target_duration))
    minimum = max(0.12, 3.0 / max(fps, 1.0))
    if video_order_mode is not None:
        # Import lazily: video_pool delegates selection to this module.
        from .video_pool import order_media_for_video_order
        active_media = order_media_for_video_order(
            media, video_order_mode, rng=video_order_rng, seed=video_order_seed,
            legacy_root=legacy_root,
        )
    elif folder_aware:
        # Import lazily: video_pool delegates selection to this module.
        from .video_pool import folder_aware_order
        active_media = folder_aware_order(media)
    else:
        active_media = list(media)
    originals = [_source_copy(item, minimum, playback_rate) for item in active_media]
    one_pass_duration = _duration(originals, transition_duration, fps)
    warnings: list[str] = []
    limit_percent = max(0.0, min(50.0, float(max_stretch_percent)))

    selected: list[MediaInfo] = []
    if target <= one_pass_duration + 1e-6:
        # Preserve the shortest ordered prefix needed for the voiceover target.
        covered: list[MediaInfo] = []
        for item in originals:
            covered.append(item)
            if _duration(covered, transition_duration, fps) >= target - 1e-6:
                break
        if duration_fit_mode == "stretch" and len(covered) > 1:
            # Smart Stretch: use one clip LESS and extend (slow) its final
            # occurrence to absorb the remaining deficit. All math stays in
            # timeline space (already speed-scaled durations).
            shorter = list(covered[:-1])
            deficit = target - _duration(shorter, transition_duration, fps)
            last = shorter[-1]
            natural_last = last.duration
            if deficit > 1e-9:
                source_seconds = last.source_duration or last.duration
                candidate = list(shorter)
                candidate[-1] = replace(last, duration=natural_last + deficit)
                final = _stretch_last_to_target(
                    candidate, target, transition_duration, fps, minimum,
                    limit_percent, natural_last,
                )
                if final is not None:
                    candidate[-1] = replace(
                        candidate[-1], playback_rate=source_seconds / final,
                    )
                    stretch_pct = (final - natural_last) / max(natural_last, 1e-9) * 100.0
                    selected = candidate
                    warnings.append(
                        f"Smart Stretch: der letzte ausgewählte Clip läuft minimal "
                        f"{stretch_pct:.1f} % langsamer (Limit {limit_percent:g} %) und füllt die "
                        "Voiceover-Timeline exakt – voller Clip-Inhalt, Übergänge und "
                        "Reihenfolge bleiben erhalten."
                    )
                else:
                    warnings.append(
                        "Smart Stretch: die erforderliche Dehnung übersteigt das konfigurierte "
                        f"Limit ({limit_percent:g} %) – es gilt das normale Kürzen des letzten "
                        "Clips (kein Hold Last Frame)."
                    )
        if not selected:
            selected = list(covered)
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
