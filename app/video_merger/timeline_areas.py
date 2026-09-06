"""Soft timeline-based source ordering ("timeline areas").

The user already organizes clips into folders by quality, so this module
deliberately contains **no** analysis of any kind: no scoring, no motion or
quality measurement, no semantic classification and no ranking of individual
clips. Its only job is to decide *which configured folder is used at which
approximate part of the timeline*.

Three roles can be assigned per configured video folder:

``1. Start & End``
    Intended for the beginning **and** the ending of the Long-Form video.
``2. Start to Middle``
    Intended for the earlier/main portion, up to the configurable midpoint.
``3. Middle to End``
    Intended for the later/main portion, up to the configurable end reserve.

Every boundary is a **soft target**. A clip is never cut, trimmed or split to
satisfy a zone: the current clip always completes first and the switch to the
next role happens at that natural clip boundary, which may be earlier or later
than the configured target. When the timeline is too short for the configured
reserves, the zones shrink proportionally. When a role has no material left, its
zone simply ends at that natural clip boundary – a zone is never padded with
another role's clips, because that would push the following roles out of the
timeline position the user assigned them. Whatever the zones did not consume
keeps its incoming order at the end of the sequence, so folders without a role
stay the general reserve they always were, nothing is ever dropped and no render
can fail because of this feature.

Inside a role the incoming sequence is preserved exactly, which means the
existing project order (Natural / Alphabetical / Random / Manual, including the
Legacy Input Root priority and the folder alternation rule) keeps its proven
randomization: this module only re-groups whole clips, it never re-shuffles,
re-sorts, weights or picks "better" clips.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence

from .models import (
    MAX_TIMELINE_AREA_SECONDS,
    SHORTS_ALLOW_AREA_MIDDLE_END,
    TIMELINE_AREA_END_SECONDS,
    TIMELINE_AREA_MIDPOINT_PERCENT,
    TIMELINE_AREA_START_SECONDS,
    MediaInfo,
)
from .timeline import duration_before_merge_value
from .video_pool import media_source_folder, normalize_legacy_root

AREA_START_END = "area_1_start_end"
AREA_START_MIDDLE = "area_2_start_middle"
AREA_MIDDLE_END = "area_3_middle_end"

TIMELINE_AREAS = (AREA_START_END, AREA_START_MIDDLE, AREA_MIDDLE_END)

TIMELINE_AREA_LABELS = {
    AREA_START_END: "1. Start & End",
    AREA_START_MIDDLE: "2. Start to Middle",
    AREA_MIDDLE_END: "3. Middle to End",
}

#: GUI/CLI/config values a user may type; all map onto the three roles above.
#: An empty (or unknown) value means "no role", which keeps the folder in the
#: general pool exactly like before this feature existed.
TIMELINE_AREA_ALIASES = {
    "": "",
    "none": "",
    "no area": "",
    "off": "",
    "1": AREA_START_END,
    "1.": AREA_START_END,
    "area 1": AREA_START_END,
    "area1": AREA_START_END,
    "area_1": AREA_START_END,
    "start & end": AREA_START_END,
    "start and end": AREA_START_END,
    "1. start & end": AREA_START_END,
    "1. start and end": AREA_START_END,
    "start_end": AREA_START_END,
    "start-end": AREA_START_END,
    AREA_START_END: AREA_START_END,
    "2": AREA_START_MIDDLE,
    "2.": AREA_START_MIDDLE,
    "area 2": AREA_START_MIDDLE,
    "area2": AREA_START_MIDDLE,
    "area_2": AREA_START_MIDDLE,
    "start to middle": AREA_START_MIDDLE,
    "2. start to middle": AREA_START_MIDDLE,
    "start_to_middle": AREA_START_MIDDLE,
    "start-middle": AREA_START_MIDDLE,
    AREA_START_MIDDLE: AREA_START_MIDDLE,
    "3": AREA_MIDDLE_END,
    "3.": AREA_MIDDLE_END,
    "area 3": AREA_MIDDLE_END,
    "area3": AREA_MIDDLE_END,
    "area_3": AREA_MIDDLE_END,
    "middle to end": AREA_MIDDLE_END,
    "3. middle to end": AREA_MIDDLE_END,
    "middle_to_end": AREA_MIDDLE_END,
    "middle-end": AREA_MIDDLE_END,
    AREA_MIDDLE_END: AREA_MIDDLE_END,
}

#: Areas a YouTube Short may draw from. ``3. Middle to End`` is excluded unless
#: the user explicitly allows it, so Shorts never receive the later/main pool.
SHORTS_TIMELINE_AREAS = (AREA_START_END, AREA_START_MIDDLE)

#: Queue key of a clip whose folder carries no role: the general reserve.
UNASSIGNED = ""

LogCallback = Callable[[str], None]


#: A hand-written role may carry its number ("2) Start to Middle"). The number
#: must agree with the resolved role, otherwise the value is rejected instead of
#: silently scheduling the wrong folders.
_NUMBERED_PREFIX = re.compile(r"^([123])\s*[.)\-:]\s*(.+)$")


def normalize_timeline_area(value: object) -> str:
    """Return one canonical role key, or ``""`` for no/unknown role."""
    text = str(value if value is not None else "").strip().casefold()
    if text in TIMELINE_AREA_ALIASES:
        return TIMELINE_AREA_ALIASES[text]
    match = _NUMBERED_PREFIX.match(text)
    if match:
        area = TIMELINE_AREA_ALIASES.get(match.group(2).strip(), "")
        if area and area.startswith(f"area_{match.group(1)}_"):
            return area
    return ""


def timeline_area_label(value: object) -> str:
    """Human-readable role name used by the GUI, the CLI help and the log."""
    return TIMELINE_AREA_LABELS.get(normalize_timeline_area(value), "No area role")


def folder_key(value: object) -> str:
    """Normalized comparison key of a configured folder path (``""`` when empty)."""
    return normalize_legacy_root(value)


def folder_area_map(settings: object) -> dict[str, str]:
    """Configured folder → role, normalized; empty when no role is assigned.

    An empty result is the switch that keeps every historical project
    byte-identical: no role means no scheduling at all.
    """
    raw = getattr(settings, "source_folder_areas", None)
    result: dict[str, str] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        folder = folder_key(key)
        area = normalize_timeline_area(value)
        if folder and area:
            result[folder] = area
    return result


def timeline_areas_configured(settings: object) -> bool:
    return bool(folder_area_map(settings))


def area_of(item: MediaInfo, area_map: dict[str, str]) -> str:
    """Role of one clip's source folder (``""`` when the folder has no role)."""
    if not area_map:
        return UNASSIGNED
    return area_map.get(media_source_folder(item), UNASSIGNED)


def _seconds(value: object, default: float) -> float:
    try:
        number = float(value if value is not None else default)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < 0.0:
        return default
    return min(number, MAX_TIMELINE_AREA_SECONDS)


def _percent(value: object, default: float) -> float:
    try:
        number = float(value if value is not None else default)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(100.0, max(0.0, number))


def area_zone_bounds(target_duration: float, settings: object) -> tuple[float, float, float]:
    """Soft zone boundaries ``(start_zone_end, midpoint, end_zone_start)``.

    All three are guidance in seconds on the output timeline, never hard cuts:

    * ``[0, start_zone_end)``        → ``1. Start & End``
    * ``[start_zone_end, midpoint)`` → ``2. Start to Middle``
    * ``[midpoint, end_zone_start)`` → ``3. Middle to End``
    * ``[end_zone_start, target)``   → ``1. Start & End`` again

    When both reserves together would need more than 80 % of the timeline they
    cannot physically fit, so they shrink to 30 % each instead of forcing
    impossible boundaries; the midpoint is then clamped between them, which may
    leave a middle zone empty. A non-positive target yields no zones at all.
    """
    try:
        target = float(target_duration)
    except (TypeError, ValueError):
        target = 0.0
    if not math.isfinite(target) or target <= 0.0:
        return 0.0, 0.0, 0.0
    start_zone = _seconds(
        getattr(settings, "timeline_area_start_seconds", TIMELINE_AREA_START_SECONDS),
        TIMELINE_AREA_START_SECONDS,
    )
    end_zone = _seconds(
        getattr(settings, "timeline_area_end_seconds", TIMELINE_AREA_END_SECONDS),
        TIMELINE_AREA_END_SECONDS,
    )
    if start_zone + end_zone > 0.8 * target:
        # Graceful degradation for short timelines: keep both reserves, only
        # smaller, so the middle zones still exist and no clip is ever forced
        # into an impossible boundary.
        start_zone = end_zone = 0.3 * target
    midpoint = target * _percent(
        getattr(settings, "timeline_area_midpoint_percent", TIMELINE_AREA_MIDPOINT_PERCENT),
        TIMELINE_AREA_MIDPOINT_PERCENT,
    ) / 100.0
    end_start = max(0.0, target - end_zone)
    midpoint = min(max(midpoint, start_zone), max(start_zone, end_start))
    return start_zone, midpoint, end_start


def _zone_plan(target: float, settings: object) -> tuple[tuple[float, str], ...]:
    start_zone, midpoint, end_start = area_zone_bounds(target, settings)
    return (
        (start_zone, AREA_START_END),
        (midpoint, AREA_START_MIDDLE),
        (end_start, AREA_MIDDLE_END),
        (max(target, end_start), AREA_START_END),
    )


def _playback_rate(settings: object, playback_rate: object = None) -> float:
    """The rate one whole clip really plays at, so zones match the render.

    ``Duration Before Merge`` slows clips down (0.70x default = longer clips),
    and the zone targets are timeline positions of the *rendered* video. Using
    the same canonical multiplier as the duration selector keeps the soft zones
    and the real timeline in agreement without touching any timing behavior.
    """
    try:
        rate = float(
            playback_rate if playback_rate is not None else duration_before_merge_value(settings)
        )
    except (TypeError, ValueError):
        return 1.0
    return rate if math.isfinite(rate) and rate > 0.0 else 1.0


def _clip_seconds(item: MediaInfo, rate: float) -> float:
    """Rendered length of one complete clip; the clip itself is never cut."""
    try:
        duration = float(getattr(item, "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    duration = max(0.0, duration)
    return duration / rate if rate > 0.0 else duration


def _area_one_start_cap(
    queue: Sequence[MediaInfo], start_zone: float, end_zone: float, rate: float = 1.0
) -> int | None:
    """How many ``1. Start & End`` clips the leading zone may consume.

    ``None`` means "as many as the soft target asks for". A cap only exists when
    the role's material cannot fill both of its zones: Area 1 serves the
    beginning *and* the ending, so scarce material is shared between the two ends
    instead of the leading zone consuming everything and the ending losing its
    role completely. The share follows the configured zone targets (20 s / 20 s
    splits evenly), always keeps at least one clip for each end when two or more
    exist, and never cuts a clip to achieve it.
    """
    total = len(queue)
    if total < 2:
        return None
    available = sum(_clip_seconds(item, rate) for item in queue)
    if available >= start_zone + end_zone:
        return None
    combined = start_zone + end_zone
    share = start_zone / combined if combined > 0.0 else 0.5
    return max(1, min(total - 1, round(total * share)))


def _next_item(
    queues: dict[str, list[tuple[int, MediaInfo]]], area: str
) -> tuple[int, MediaInfo] | None:
    """Next whole clip of the zone's own role, or ``None`` when it is spent.

    Deliberately no cross-role padding: an exhausted role ends its zone at the
    natural clip boundary (soft target) and the following role starts there.
    """
    bucket = queues.get(area)
    if bucket:
        return bucket.pop(0)
    return None


def order_media_by_timeline_areas(
    media: Sequence[MediaInfo],
    target_duration: float,
    settings: object,
    *,
    log: LogCallback | None = None,
    playback_rate: float | None = None,
) -> list[MediaInfo]:
    """Re-group an already ordered sequence into the configured timeline areas.

    The result is always a permutation of ``media``: every clip stays present
    exactly once, nothing is added, dropped, trimmed or split. The incoming
    order inside each role is preserved, so the existing randomization and the
    Legacy Input Root priority keep working untouched.

    Zone targets are positions on the rendered timeline, so every clip is
    measured with the project's canonical ``Duration Before Merge`` multiplier
    (or an explicit ``playback_rate``); clip durations are never modified.

    It is a no-op — the historical sequence is returned unchanged — when no
    folder has a role, when there is no positive target duration (the basic
    merge without a voiceover timeline), or when fewer than two clips exist.
    """
    items = list(media)
    if len(items) < 2:
        return items
    area_map = folder_area_map(settings)
    if not area_map:
        return items
    try:
        target = float(target_duration)
    except (TypeError, ValueError):
        target = 0.0
    if not math.isfinite(target) or target <= 0.0:
        return items

    # Queues carry the incoming index, not the object identity: the permutation
    # invariant (every clip present exactly once) must hold even if a caller ever
    # passes the same MediaInfo object twice.
    queues: dict[str, list[tuple[int, MediaInfo]]] = {
        AREA_START_END: [], AREA_START_MIDDLE: [], AREA_MIDDLE_END: [], UNASSIGNED: [],
    }
    for index, item in enumerate(items):
        queues[area_of(item, area_map)].append((index, item))

    ordered: list[MediaInfo] = []
    used: set[int] = set()
    clock = 0.0
    rate = _playback_rate(settings, playback_rate)
    start_zone, _midpoint, end_start = area_zone_bounds(target, settings)
    start_cap = _area_one_start_cap(
        [item for _index, item in queues[AREA_START_END]],
        start_zone, max(0.0, target - end_start), rate,
    )
    for index, (zone_end, area) in enumerate(_zone_plan(target, settings)):
        # Only the leading zone can be capped: it shares its role with the end
        # reserve, and scarce Area 1 material belongs at both ends.
        limit = start_cap if index == 0 else None
        taken = 0
        # Soft boundary: the loop finishes the current clip before it looks at
        # the target again, so a clip may extend past the zone end (23.7 s for a
        # 20 s target is intended) and is never cut to fit.
        while clock < zone_end - 1e-6 and (limit is None or taken < limit):
            entry = _next_item(queues, area)
            if entry is None:
                break
            index, item = entry
            ordered.append(item)
            used.add(index)
            taken += 1
            clock += _clip_seconds(item, rate)
    # Clips no zone consumed keep their incoming relative order: they remain the
    # unused pool tail exactly like before, and Required-Only selection still
    # decides how many of them are ever rendered.
    ordered.extend(item for index, item in enumerate(items) if index not in used)
    if log is not None and ordered[:len(items)] != items:
        log(timeline_area_log_line(items, ordered, target, settings, area_map))
    return ordered


def shorts_area_pool(
    media: Sequence[MediaInfo],
    settings: object,
    *,
    log: LogCallback | None = None,
) -> list[MediaInfo]:
    """Shorts source pool: ``1. Start & End`` + ``2. Start to Middle``.

    ``3. Middle to End`` stays out of every Short unless the user explicitly
    allows it, which keeps the higher-quality sources for the vertical outputs
    without touching any other Shorts behavior. Order and randomization inside
    the pool are untouched, and the pool degrades gracefully: if excluding
    Area 3 would leave nothing, the complete pool is used instead of failing.
    """
    items = list(media)
    area_map = folder_area_map(settings)
    if not area_map:
        return items
    allowed = bool(getattr(settings, "shorts_allow_area_middle_end", SHORTS_ALLOW_AREA_MIDDLE_END))
    if allowed:
        if log is not None and any(area_of(item, area_map) == AREA_MIDDLE_END for item in items):
            log(
                "Timeline areas (Shorts): 3. Middle to End explicitly allowed → "
                f"all {len(items)} clip(s) eligible."
            )
        return items
    # Folders without a role stay eligible: they are the general reserve.
    allowed_areas = (*SHORTS_TIMELINE_AREAS, UNASSIGNED)
    eligible = [item for item in items if area_of(item, area_map) in allowed_areas]
    if not eligible:
        if log is not None:
            log(
                "Timeline areas (Shorts): only 3. Middle to End material configured → "
                f"using all {len(items)} clip(s) instead of an empty Shorts pool."
            )
        return items
    if log is not None and len(eligible) != len(items):
        log(
            "Timeline areas (Shorts): 3. Middle to End excluded → "
            f"{len(eligible)} of {len(items)} clip(s) eligible "
            "(1. Start & End + 2. Start to Middle)."
        )
    return eligible


def timeline_area_log_line(
    source: Iterable[MediaInfo],
    ordered: Sequence[MediaInfo],
    target_duration: float,
    settings: object,
    area_map: dict[str, str] | None = None,
) -> str:
    """One concise line describing the soft zones and what really happened."""
    area_map = folder_area_map(settings) if area_map is None else area_map
    start_zone, midpoint, end_start = area_zone_bounds(target_duration, settings)
    zones = (
        f"0.0-{start_zone:.1f} s = {TIMELINE_AREA_LABELS[AREA_START_END]}",
        f"{start_zone:.1f}-{midpoint:.1f} s = {TIMELINE_AREA_LABELS[AREA_START_MIDDLE]}",
        f"{midpoint:.1f}-{end_start:.1f} s = {TIMELINE_AREA_LABELS[AREA_MIDDLE_END]}",
        (
            f"{end_start:.1f}-{max(float(target_duration), end_start):.1f} s = "
            f"{TIMELINE_AREA_LABELS[AREA_START_END]}"
        ),
    )
    moved = sum(
        1
        for index, item in enumerate(list(source)[: len(ordered)])
        if index < len(ordered) and ordered[index] is not item
    )
    counts = {area: 0 for area in (*TIMELINE_AREAS, UNASSIGNED)}
    for item in ordered:
        counts[area_of(item, area_map)] += 1
    area_summary = (
        f" · clips per area: {counts[AREA_START_END]}/{counts[AREA_START_MIDDLE]}"
        f"/{counts[AREA_MIDDLE_END]} + {counts[UNASSIGNED]} unassigned"
    )
    return (
        "Timeline areas (soft targets): " + " · ".join(zones) + area_summary
        + f" · {moved} clip(s) re-grouped, none cut"
    )
