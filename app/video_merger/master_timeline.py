"""Single-pass, audio-locked Master Timeline planning.

The builder assigns one asset from a unified image/video pool to every visual
slot. It never appends media to a pre-fitted timeline: slots partition the
voiceover interval exactly once.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Sequence

from .asset_cooldown import AssetCooldownHistory
from .media_pool import MediaPool, MediaPoolAsset
from .smart_timeline import SemanticSection

MASTER_SLOT_MIN_SECONDS = 4.0
MASTER_SLOT_MAX_SECONDS = 12.0
MASTER_MODE_RANDOM = "pure_random"
MASTER_MODE_SMART = "smart_visuals"


def normalize_master_mode(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text in {"pure_random", "random", "random_only"}:
        return MASTER_MODE_RANDOM
    return MASTER_MODE_SMART


def clamp_ratio(value: object, default: int) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


@dataclass(slots=True, frozen=True)
class MasterTimelineSlot:
    index: int
    start: float
    end: float
    topic: str
    text: str
    asset: MediaPoolAsset
    score: float
    selection_mode: str
    boundary_reason: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(slots=True)
class MasterTimeline:
    target_duration: float
    slots: list[MasterTimelineSlot] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def selected_asset_ids(self) -> tuple[str, ...]:
        return tuple(slot.asset.asset_id for slot in self.slots)

    @property
    def image_count(self) -> int:
        return sum(slot.asset.kind == "image" for slot in self.slots)

    @property
    def video_count(self) -> int:
        return sum(slot.asset.kind == "video" for slot in self.slots)

    @property
    def image_percent(self) -> float:
        return 100.0 * self.image_count / len(self.slots) if self.slots else 0.0

    def validate(self) -> None:
        if not self.slots:
            raise ValueError("Master Timeline has no selectable media slots")
        tolerance = 1e-6
        if abs(self.slots[0].start) > tolerance:
            raise ValueError("Master Timeline does not start at zero")
        for previous, current in zip(self.slots, self.slots[1:]):
            if abs(previous.end - current.start) > tolerance:
                raise ValueError("Master Timeline contains a gap or overlap")
        if abs(self.slots[-1].end - self.target_duration) > tolerance:
            raise ValueError("Master Timeline exceeds or undershoots the voiceover endpoint")
        if self.target_duration >= MASTER_SLOT_MIN_SECONDS:
            for slot in self.slots:
                if not (MASTER_SLOT_MIN_SECONDS - tolerance <= slot.duration <= MASTER_SLOT_MAX_SECONDS + tolerance):
                    raise ValueError(f"Master Timeline slot {slot.index} is outside 4-12 seconds")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]{2,}", text.casefold(), flags=re.UNICODE))


def _score(section: SemanticSection, asset: MediaPoolAsset) -> float:
    query = _tokens(section.text)
    evidence = _tokens(" ".join((asset.category, " ".join(asset.keywords), asset.metadata_text)))
    if not query or not evidence:
        return 0.0
    return len(query & evidence) / math.sqrt(len(query) * len(evidence))


def _partition(
    target: float,
    sections: Sequence[SemanticSection],
    image_ratio_min: int = 0,
    image_ratio_max: int = 100,
) -> list[tuple[float, float, SemanticSection, str]]:
    """Balanced strict slots; long topics receive 3-4 inferred thought cuts."""
    if target <= 0:
        return []
    if target < MASTER_SLOT_MIN_SECONDS:
        count = 1
    else:
        count = max(1, int(math.ceil(target / MASTER_SLOT_MAX_SECONDS)))
        if 20.0 <= target <= 30.0:
            count = max(3, min(4, int(round(target / 8.0))))
        # Avoid a sub-four-second remainder by reducing the count when legal.
        while count > 1 and target / count < MASTER_SLOT_MIN_SECONDS:
            count -= 1
        # A percentage interval can be impossible for a small integer slot
        # count (for example 30–40% of seven slots). Add balanced slots, while
        # respecting the four-second floor, until an exact integer allocation
        # exists.
        maximum_count = max(count, int(math.floor(target / MASTER_SLOT_MIN_SECONDS)))
        for candidate in range(count, maximum_count + 1):
            low = int(math.ceil(candidate * image_ratio_min / 100.0 - 1e-12))
            high = int(math.floor(candidate * image_ratio_max / 100.0 + 1e-12))
            if low <= high:
                count = candidate
                break
    boundaries = [target * index / count for index in range(count + 1)]
    ordered = sorted(sections, key=lambda item: (item.start, item.index))
    fallback = ordered[0] if ordered else SemanticSection(
        index=0, text="", start=0.0, end=target, speech_unit_indices=(),
        boundary_reason="inferred_thought", unsplittable=False,
    )
    result = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        midpoint = (start + end) / 2.0
        section = next((item for item in ordered if item.start <= midpoint <= item.end), None)
        if section is None and ordered:
            section = min(ordered, key=lambda item: abs(((item.start + item.end) / 2.0) - midpoint))
        section = section or fallback
        exact_boundary = any(abs(end - item.end) < 1e-6 for item in ordered)
        reason = section.boundary_reason if exact_boundary or index == count - 1 else "inferred_thought_boundary"
        result.append((round(start, 9), round(end, 9), section, reason))
    return result


class MasterTimelineBuilder:
    def __init__(
        self,
        *,
        media_pool: MediaPool,
        sections: Sequence[SemanticSection],
        voiceover_duration: float,
        mode: str = MASTER_MODE_SMART,
        image_ratio_min: object = 30,
        image_ratio_max: object = 40,
        cooldown_videos: object = 3,
        cooldown_history: AssetCooldownHistory | None = None,
        seed_parts: Sequence[object] = (),
    ):
        self.media_pool = media_pool
        self.sections = list(sections)
        self.voiceover_duration = max(0.0, float(voiceover_duration))
        self.mode = normalize_master_mode(mode)
        low = clamp_ratio(image_ratio_min, 30)
        high = clamp_ratio(image_ratio_max, 40)
        self.image_ratio_min, self.image_ratio_max = sorted((low, high))
        self.cooldown_videos = cooldown_videos
        self.cooldown_history = cooldown_history
        digest = hashlib.sha256("|".join(map(str, seed_parts)).encode("utf-8")).hexdigest()
        self.rng = random.Random(int(digest[:16], 16))

    def build(self) -> MasterTimeline:
        blocked = self.cooldown_history.blocked_assets(self.cooldown_videos) if self.cooldown_history else set()
        pool = self.media_pool.without(blocked)
        timeline = MasterTimeline(target_duration=self.voiceover_duration)
        if blocked:
            timeline.diagnostics.append(f"Cooldown filtered {len(blocked)} asset(s).")
        intervals = _partition(
            self.voiceover_duration, self.sections,
            self.image_ratio_min, self.image_ratio_max,
        )
        if not intervals:
            return timeline
        if not pool.assets:
            raise ValueError("No media remains after applying asset cooldown")

        slot_count = len(intervals)
        minimum_images = int(math.ceil(slot_count * self.image_ratio_min / 100.0 - 1e-12))
        maximum_images = int(math.floor(slot_count * self.image_ratio_max / 100.0 + 1e-12))
        preferred = int(round(slot_count * ((self.image_ratio_min + self.image_ratio_max) / 200.0)))
        if maximum_images < minimum_images:
            # No exact integer percentage exists at this short duration. Pick
            # the closest feasible count instead of biasing upward to images.
            def distance(count: int) -> tuple[float, int]:
                percent = 100.0 * count / slot_count
                outside = (
                    self.image_ratio_min - percent if percent < self.image_ratio_min
                    else percent - self.image_ratio_max if percent > self.image_ratio_max
                    else 0.0
                )
                return outside, abs(count - preferred)
            image_target = min(range(slot_count + 1), key=distance)
            timeline.diagnostics.append(
                "Image ratio approximated: duration permits no exact integer slot allocation."
            )
        else:
            image_target = max(minimum_images, min(maximum_images, preferred))
        if not pool.images:
            image_target = 0
            timeline.diagnostics.append("Image ratio relaxed: no eligible images.")
        elif not pool.videos:
            image_target = slot_count
            timeline.diagnostics.append("Image ratio relaxed: no eligible videos.")

        scored_by_slot: list[dict[str, list[tuple[float, MediaPoolAsset]]]] = []
        for _start, _end, section, _reason in intervals:
            kinds = {"image": [], "video": []}
            for asset in pool.assets:
                score = _score(section, asset) if self.mode == MASTER_MODE_SMART else 0.0
                kinds[asset.kind].append((score, asset))
            for values in kinds.values():
                values.sort(key=lambda pair: (-pair[0], pair[1].asset_id))
            scored_by_slot.append(kinds)

        if image_target <= 0:
            image_slots: set[int] = set()
        elif image_target >= slot_count:
            image_slots = set(range(slot_count))
        elif self.mode == MASTER_MODE_RANDOM:
            image_slots = set(self.rng.sample(range(slot_count), image_target))
        else:
            advantages = []
            for index, kinds in enumerate(scored_by_slot):
                image_score = kinds["image"][0][0] if kinds["image"] else -1.0
                video_score = kinds["video"][0][0] if kinds["video"] else -1.0
                advantages.append((image_score - video_score, index))
            advantages.sort(key=lambda item: (-item[0], item[1]))
            image_slots = {index for _advantage, index in advantages[:image_target]}

        used: set[str] = set()
        last = ""
        for index, (start, end, section, reason) in enumerate(intervals):
            kind = "image" if index in image_slots else "video"
            candidates = list(scored_by_slot[index][kind])
            if not candidates:
                other = "video" if kind == "image" else "image"
                candidates = list(scored_by_slot[index][other])
                timeline.diagnostics.append(f"Slot {index}: {kind} quota relaxed; no eligible asset.")
            fresh = [pair for pair in candidates if pair[1].asset_id not in used]
            choices = fresh or [pair for pair in candidates if pair[1].asset_id != last] or candidates
            if self.mode == MASTER_MODE_RANDOM:
                score, asset = self.rng.choice(choices)
            else:
                score, asset = choices[0]
            used.add(asset.asset_id)
            last = asset.asset_id
            timeline.slots.append(MasterTimelineSlot(
                index=index, start=start, end=end, topic=section.text[:160], text=section.text,
                asset=asset, score=score, selection_mode=self.mode, boundary_reason=reason,
            ))
        timeline.validate()
        return timeline


def materialize_master_timeline(
    timeline: MasterTimeline,
    *,
    width: int,
    height: int,
    fps: float,
    transition_type: str,
    image_profile,
    ffprobe_path,
    render_settings,
) -> list:
    """Create the final MediaInfo chain directly from Master Timeline slots."""
    from .image_timeline import make_image_media, probe_image_size
    from .smart_timeline import lock_timeline_duration
    from .smart_visuals import _make_smart_video_media, _probe_video_entry
    from .target import resolve_export

    media = []
    video_offsets: dict[str, float] = {}
    for slot in timeline.slots:
        duration = slot.duration
        asset = slot.asset
        if asset.kind == "image":
            path = Path(asset.path)
            if not path.is_file():
                raise ValueError(f"Master Timeline image is missing: {asset.path}")
            item = make_image_media(
                path=path, duration=duration, width=width, height=height, fps=fps,
                size=probe_image_size(path, ffprobe_path), transition_type=transition_type,
                profile=image_profile,
            )
            fit_mode = str(getattr(render_settings, "smart_visual_image_fit_mode", "fill") or "fill")
            item = replace(
                item, smart_visual_insertion=True, smart_visual_audio_anchored=True,
                image_fit_mode=fit_mode if fit_mode in {"fit", "fill", "crop"} else "fill",
            )
        elif asset.source_media is not None:
            source = asset.source_media
            source_duration = float(source.source_duration or source.duration)
            offset = video_offsets.get(asset.asset_id, 0.0)
            if source_duration > 0 and offset >= source_duration:
                offset = 0.0
            item = replace(
                source, duration=duration, source_duration=source_duration,
                source_start=offset, smart_visual_insertion=True,
                smart_visual_audio_anchored=True,
            )
            video_offsets[asset.asset_id] = offset + duration
        else:
            probed = _probe_video_entry(asset.path, ffprobe_path)
            if not probed:
                raise ValueError(f"Master Timeline video is unreadable: {asset.path}")
            item = replace(
                _make_smart_video_media(asset.path, duration, probed, transition_type),
                smart_visual_audio_anchored=True,
            )
        media.append(item)

    # Each slot duration describes final timeline coverage. Compensate source
    # durations for incoming xfade overlap so resolve_export preserves those
    # exact boundaries rather than shortening the voiceover program.
    probe_settings = replace(render_settings, workflow_stage="", timeline_target_duration=0.0)
    for _iteration in range(3):
        resolved = resolve_export(media, probe_settings)
        adjusted = []
        changed = False
        for index, (item, slot) in enumerate(zip(media, timeline.slots)):
            incoming = resolved.transitions[index - 1] if index else 0.0
            wanted = slot.duration + incoming
            if abs(float(item.duration) - wanted) > 1e-6:
                item = replace(item, duration=wanted)
                changed = True
            adjusted.append(item)
        media = adjusted
        if not changed:
            break
    media = lock_timeline_duration(
        media, timeline.target_duration, probe_settings, fps
    )
    return [replace(item, smart_visual_audio_anchored=True) for item in media]
