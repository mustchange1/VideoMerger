"""Phase 35 opt-in Smart Visual timeline geometry.

This module is deliberately isolated from the ordinary VideoMerger timeline.
Nothing imports or executes it unless a Smart Visual profile is active.  It
models the same chained-item geometry used by FFmpeg: an item's rendered start
is the current chain duration minus its incoming transition overlap.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence

from .models import ExportSettings, MediaInfo, WordTiming
from .target import resolve_export

_SENTENCE_END = re.compile(r"(?<=[.!?])(?:[\"'»”’)]*)\s+|\n+")


@dataclass(slots=True, frozen=True)
class SpeechUnit:
    index: int
    text: str
    char_start: int
    char_end: int
    start: float
    end: float
    first_word_index: int
    last_word_index: int
    boundary_reason: str = "sentence"


@dataclass(slots=True, frozen=True)
class RenderChainItem:
    index: int
    item_id: str
    path: str
    kind: str
    duration: float
    final_start: float
    final_end: float
    fully_visible_start: float
    incoming_overlap: float
    outgoing_overlap: float
    source_start: float
    source_end: float


@dataclass(slots=True)
class SmartVisualPlacement:
    placement_id: str
    visual_path: str
    visual_kind: str
    slot_type: str
    requested_audio_boundary: float
    selected_speech_boundary: float
    speech_unit_index: int = -1
    speech_text: str = ""
    semantic_reason: str = ""
    semantic_evidence: tuple[str, ...] = ()
    source_item_before_split: str = ""
    source_local_position: float = 0.0
    split_position: float = 0.0
    transition_type: str = ""
    transition_duration: float = 0.0
    xfade_overlap: float = 0.0
    visual_duration: float = 0.0
    calculated_visual_start: float = 0.0
    calculated_visual_end: float = 0.0
    final_expected_start: float = 0.0
    final_expected_end: float = 0.0
    final_rendered_start: float | None = None
    final_rendered_end: float | None = None
    placement_drift: float = 0.0
    item_count_before: int = 0
    item_count_after: int = 0
    previous_insertions_present: bool = False
    status: str = "pending"  # pending | inserted | skipped | invalid
    exact: bool = False
    reason: str = ""
    automatic: bool = True
    manual_override: dict = field(default_factory=dict)


@dataclass(slots=True)
class SmartVisualTimeline:
    enabled: bool
    source_items: list[RenderChainItem] = field(default_factory=list)
    speech_units: list[SpeechUnit] = field(default_factory=list)
    placements: list[SmartVisualPlacement] = field(default_factory=list)
    resolved_items: list[RenderChainItem] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    identity: str = ""
    confirmed: bool = False

    @property
    def valid(self) -> bool:
        return not self.validation_errors

    def finalize_identity(self) -> str:
        payload = {
            "enabled": self.enabled,
            "speech_units": [asdict(item) for item in self.speech_units],
            "placements": [asdict(item) for item in self.placements],
            "resolved_items": [asdict(item) for item in self.resolved_items],
        }
        self.identity = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return self.identity

    def to_dict(self) -> dict:
        return {
            "schema": "videomerger-smart-visual-timeline-v1",
            "enabled": self.enabled,
            "valid": self.valid,
            "confirmed": self.confirmed,
            "identity": self.identity,
            "source_items": [asdict(item) for item in self.source_items],
            "speech_units": [asdict(item) for item in self.speech_units],
            "placements": [asdict(item) for item in self.placements],
            "resolved_items": [asdict(item) for item in self.resolved_items],
            "diagnostics": list(self.diagnostics),
            "validation_errors": list(self.validation_errors),
        }

    def write_reports(self, json_path: Path, text_path: Path) -> None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "PHASE 35 SMART VISUAL TIMELINE TRACE",
            f"identity: {self.identity}",
            f"valid: {self.valid}",
            f"placements: {len(self.placements)}",
            "",
        ]
        for item in self.placements:
            lines.extend((
                f"[{item.placement_id}] {Path(item.visual_path).name or item.visual_path}",
                f"  status={item.status} automatic={item.automatic} exact={item.exact}",
                f"  requested={item.requested_audio_boundary:.6f}s selected={item.selected_speech_boundary:.6f}s",
                f"  expected={item.final_expected_start:.6f}-{item.final_expected_end:.6f}s drift={item.placement_drift:+.6f}s",
                f"  source={item.source_item_before_split} local={item.source_local_position:.6f}s split={item.split_position:.6f}s",
                f"  transition={item.transition_type} duration={item.transition_duration:.6f}s overlap={item.xfade_overlap:.6f}s",
                f"  duration={item.visual_duration:.6f}s items={item.item_count_before}->{item.item_count_after}",
                f"  previous_insertions_present={item.previous_insertions_present}",
                f"  reason={item.reason or item.semantic_reason}",
                "",
            ))
        if self.validation_errors:
            lines.append("VALIDATION ERRORS")
            lines.extend(f"- {message}" for message in self.validation_errors)
        text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sentence_spans(script: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(script):
        end = match.start()
        raw = script[cursor:end]
        stripped = raw.strip()
        if stripped:
            left = cursor + len(raw) - len(raw.lstrip())
            right = end - (len(raw) - len(raw.rstrip()))
            spans.append((left, right, script[left:right]))
        cursor = match.end()
    raw = script[cursor:]
    if raw.strip():
        left = cursor + len(raw) - len(raw.lstrip())
        right = len(script) - (len(raw) - len(raw.rstrip()))
        spans.append((left, right, script[left:right]))
    return spans


def build_speech_units(script: str, words: Sequence[WordTiming]) -> list[SpeechUnit]:
    """Map punctuation-delimited script sentences onto canonical word timing.

    Character spans from forced alignment are authoritative.  A tolerant token
    order fallback is used only for foreign/test alignments without spans.
    Sentence starts are clamped to the first word start and therefore can never
    land inside the preceding spoken sentence.
    """
    spans = _sentence_spans(script)
    if not spans or not words:
        return []
    result: list[SpeechUnit] = []
    next_word = 0
    for sentence_index, (char_start, char_end, text) in enumerate(spans):
        indexes = [
            index for index, word in enumerate(words)
            if word.script_end > char_start and word.script_start < char_end
            and (word.script_end > word.script_start)
        ]
        if not indexes:
            count = len(re.findall(r"\b[\w’'-]+\b", text, flags=re.UNICODE))
            if count and next_word < len(words):
                indexes = list(range(next_word, min(len(words), next_word + count)))
        if not indexes:
            continue
        first, last = indexes[0], indexes[-1]
        start = max(0.0, float(words[first].start))
        end = max(start, float(words[last].end))
        if result:
            start = max(start, result[-1].end)
            if end < start:
                end = start
        result.append(SpeechUnit(
            index=sentence_index,
            text=text,
            char_start=char_start,
            char_end=char_end,
            start=start,
            end=end,
            first_word_index=first,
            last_word_index=last,
        ))
        next_word = last + 1
    return result


def render_chain(media: Sequence[MediaInfo], settings: ExportSettings) -> list[RenderChainItem]:
    """Return pre-render coordinates from the exact production resolver."""
    if not media:
        return []
    probe = replace(settings, workflow_stage="", timeline_target_duration=0.0)
    resolved = resolve_export(list(media), probe)
    result: list[RenderChainItem] = []
    cursor = 0.0
    for index, (item, duration) in enumerate(zip(media, resolved.effective_durations)):
        incoming = resolved.transitions[index - 1] if index else 0.0
        outgoing = resolved.transitions[index] if index < len(resolved.transitions) else 0.0
        start = max(0.0, cursor - incoming) if index else 0.0
        end = start + duration
        source_start = max(0.0, float(getattr(item, "source_start", 0.0) or 0.0))
        rate = max(0.25, min(4.0, float(getattr(item, "playback_rate", 1.0) or 1.0)))
        kind = "smart_visual" if getattr(item, "smart_visual_insertion", False) else (
            "image" if getattr(item, "is_image_insertion", False) else "source"
        )
        result.append(RenderChainItem(
            index=index,
            item_id=f"{kind}:{index}:{Path(item.path).name}",
            path=str(item.path),
            kind=kind,
            duration=duration,
            final_start=start,
            final_end=end,
            fully_visible_start=start + incoming,
            incoming_overlap=incoming,
            outgoing_overlap=outgoing,
            source_start=source_start,
            source_end=source_start + duration * rate,
        ))
        cursor = end
    return result


def minimum_fragment_seconds(fps: float) -> float:
    """Smallest xfade-safe speech split: six complete output frames."""
    # Three frames are decodable in isolation but can end between source-clock
    # frames after playback-rate scaling, starving a chained xfade. Six frames
    # (never below 0.2 s) keeps both overlap sides stable in FFmpeg 6.
    return max(0.20, 6.0 / max(1.0, float(fps)))


def _split_item(item: MediaInfo, left_duration: float) -> tuple[MediaInfo, MediaInfo]:
    duration = float(item.duration)
    rate = max(0.25, min(4.0, float(getattr(item, "playback_rate", 1.0) or 1.0)))
    source_start = max(0.0, float(getattr(item, "source_start", 0.0) or 0.0))
    left = replace(item, duration=left_duration, source_start=source_start)
    right = replace(
        item,
        duration=duration - left_duration,
        source_start=source_start + left_duration * rate,
    )
    return left, right


def _visual_geometry(chain: Sequence[RenderChainItem], visual_index: int) -> RenderChainItem:
    return chain[visual_index]


def insert_at_render_time(
    items: Sequence[MediaInfo],
    visual: MediaInfo,
    requested: float,
    settings: ExportSettings,
    fps: float,
) -> tuple[list[MediaInfo], SmartVisualPlacement]:
    """Sequentially insert one visual at a final render-chain coordinate.

    Candidate existing boundaries and source-item splits are evaluated through
    ``resolve_export`` itself.  A bounded frame-grid search chooses the lowest
    final-timeline drift; no plan-level/source-duration approximation is used.
    """
    working = list(items)
    placement = SmartVisualPlacement(
        placement_id="",
        visual_path=str(visual.path),
        visual_kind="video" if not visual.is_image_insertion else "image",
        slot_type="speech_boundary",
        requested_audio_boundary=float(requested),
        selected_speech_boundary=float(requested),
        visual_duration=float(visual.duration),
        transition_type=str(settings.transition_type),
        item_count_before=len(working),
        previous_insertions_present=any(getattr(item, "smart_visual_insertion", False) for item in working),
    )
    if not working:
        placement.status, placement.reason = "skipped", "source_clip_unavailable"
        return working, placement
    frame = 1.0 / max(1.0, fps)
    minimum = minimum_fragment_seconds(fps)
    best: tuple[float, list[MediaInfo], int, str, float, MediaInfo | None] | None = None

    def consider(candidate: list[MediaInfo], visual_index: int, reason: str, split: float, source: MediaInfo | None):
        nonlocal best
        geometry = _visual_geometry(render_chain(candidate, settings), visual_index)
        drift = geometry.final_start - requested
        score = abs(drift)
        if best is None or score < best[0] - 1e-9:
            best = (score, candidate, visual_index, reason, split, source)

    # Existing boundaries are valid and avoid unnecessary source splits.
    for boundary in range(0, len(working) + 1):
        candidate = working[:boundary] + [visual] + working[boundary:]
        adjacent = working[boundary - 1] if boundary > 0 else working[0]
        consider(candidate, boundary, "existing_boundary", 0.0, adjacent)

    # Search only normal source clips. Splitting images or previous Smart
    # Visuals would make manual/automatic placement semantics ambiguous.
    for index, item in enumerate(working):
        if item.is_image_insertion or item.smart_visual_insertion:
            continue
        duration = float(item.duration)
        if duration < 2.0 * minimum - 1e-9:
            continue
        first = int(math.ceil(minimum / frame))
        last = int(math.floor((duration - minimum) / frame))
        # Exhaustive frame search is deterministic and exact. Bound very long
        # clips by searching around the render-coordinate estimate, then refine.
        frame_indexes: Iterable[int]
        if last - first <= 1800:
            frame_indexes = range(first, last + 1)
        else:
            base_chain = render_chain(working, settings)[index]
            estimate = int(round((requested - base_chain.final_start) / frame))
            lo, hi = max(first, estimate - 120), min(last, estimate + 120)
            frame_indexes = range(lo, hi + 1)
        for frame_index in frame_indexes:
            split = round(frame_index * frame, 9)
            left, right = _split_item(item, split)
            candidate = working[:index] + [left, visual, right] + working[index + 1:]
            consider(candidate, index + 1, "source_split", split, item)

    if best is None:
        placement.status, placement.reason = "skipped", "split_impossible"
        return working, placement
    _score, candidate, visual_index, reason, split, source = best
    chain = render_chain(candidate, settings)
    geometry = chain[visual_index]
    placement.source_item_before_split = str(source.path) if source is not None else ""
    placement.source_local_position = split
    placement.split_position = split
    placement.transition_duration = geometry.incoming_overlap
    placement.xfade_overlap = geometry.incoming_overlap
    placement.calculated_visual_start = geometry.final_start
    placement.calculated_visual_end = geometry.final_end
    placement.final_expected_start = geometry.final_start
    placement.final_expected_end = geometry.final_end
    placement.placement_drift = geometry.final_start - requested
    placement.item_count_after = len(candidate)
    placement.status = "inserted"
    placement.exact = abs(placement.placement_drift) <= frame + 1e-9
    placement.reason = reason if placement.exact else f"{reason}:frame_or_transition_constraint"
    return candidate, placement


def validate_timeline(timeline: SmartVisualTimeline, fps: float) -> list[str]:
    errors: list[str] = []
    minimum = minimum_fragment_seconds(fps)
    inserted = [item for item in timeline.placements if item.status == "inserted"]
    audio_end = max((unit.end for unit in timeline.speech_units), default=None)
    for item in inserted:
        if item.visual_duration <= 0:
            errors.append(f"{item.placement_id}: impossible visual duration")
        if not Path(item.visual_path).is_file():
            errors.append(f"{item.placement_id}: missing visual asset {item.visual_path}")
        if item.requested_audio_boundary < 0 or (
            audio_end is not None and item.requested_audio_boundary > audio_end + 1e-6
        ):
            errors.append(f"{item.placement_id}: timing outside canonical audio range")
    ordered = sorted(inserted, key=lambda value: value.final_expected_start)
    for left, right in zip(ordered, ordered[1:]):
        if right.final_expected_start < left.final_expected_start - 1e-9:
            errors.append(f"{right.placement_id}: placement order conflict")
        # Adjacent items may overlap only by the production xfade amount.
        # Anything beyond that is a conflicting placement, not a transition.
        if right.final_expected_start < (
            left.final_expected_end - right.xfade_overlap - (1.0 / max(1.0, fps))
        ):
            errors.append(
                f"{right.placement_id}: overlaps {left.placement_id} beyond transition geometry"
            )
    for item in timeline.resolved_items:
        if item.kind == "source" and item.duration < minimum - 1e-6:
            errors.append(f"{item.item_id}: source fragment {item.duration:.6f}s below {minimum:.6f}s")
    timeline.validation_errors = errors
    return errors
