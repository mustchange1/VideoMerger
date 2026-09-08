"""Planning-level Short grouping and per-Short music resolution.

One Short is built from an ordered list of voiceover units. By default every
unit produces its own Short (the historical behaviour, and the state of every
project that has no groups configured). The user may group units so that ONE
Short renders them back to back on ONE timeline:

* one media plan / one video timeline for the whole group,
* one continuous voiceover timeline (unit A, configured pause, unit B, ...),
* ONE subtitle timeline whose timestamps keep accumulating across the units,
* one music track that covers the combined duration,
* one final MP4 plus one transcript text file.

Everything in this module is pure planning data. No rendered Short is ever
concatenated afterwards and no rendering, subtitle, alignment, music or
transition engine is modified: a grouped Short is handed to ``create_main``
exactly like the existing multi-voiceover Long-Form merge, which already
concatenates units with the configured pause on one timeline.

Ordering contract
-----------------
The voiceover/script list order shown in the GUI is authoritative. A group
always renders its members in that list order (Script 3 before Script 4, never
the reverse), independently of the order in which the user selected or clicked
them. Groups themselves are ordered by the position of their first member, so
Short numbering stays stable and reproducible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ExportSettings

#: Separator used inside a grouped Short output name, e.g. ``003-004``.
GROUP_NAME_SEPARATOR = "-"


@dataclass(frozen=True, slots=True)
class ShortPlan:
    """One Short exactly as the planning layer will render it.

    ``index`` is the 1-based position of the first member in the ordered
    voiceover list. For an ungrouped unit that is the historical Short number,
    which keeps output names and cache keys byte-identical for every project
    without groups.
    """

    index: int
    positions: tuple[int, ...]
    units: tuple[Path, ...]
    output_name: str

    @property
    def grouped(self) -> bool:
        return len(self.positions) > 1

    @property
    def anchor(self) -> Path:
        """First voiceover unit; also the key of per-Short overrides."""
        return self.units[0]

    def describe(self) -> str:
        """Human readable mapping for logs, diagnostics and the manifest."""
        names = " + ".join(unit.name for unit in self.units)
        if not self.grouped:
            return f"Short {self.output_name} -> {names}"
        positions = ", ".join(str(position) for position in self.positions)
        return f"Short {self.output_name} -> {names} (grouped voiceover units {positions})"


def _resolve_unit(value: Any) -> Path | None:
    """Resolve one voiceover reference the same way the unit list does."""
    try:
        text = str(value or "").strip()
    except (TypeError, ValueError):
        return None
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _same_unit(left: Any, right: Any) -> bool:
    left_path = _resolve_unit(left)
    right_path = _resolve_unit(right)
    if left_path is None or right_path is None:
        return str(left or "").strip() == str(right or "").strip() and bool(str(left or "").strip())
    return left_path == right_path


def _group_positions(
    raw_groups: Iterable[Any] | None,
    units: Sequence[Path | None],
) -> list[list[int]]:
    """Return validated groups as 0-based positions into ``units``."""
    position_of: dict[Path, int] = {}
    for position, unit in enumerate(units):
        if unit is not None and unit not in position_of:
            position_of[unit] = position

    claimed: set[int] = set()
    groups: list[list[int]] = []
    for raw_group in raw_groups or []:
        if isinstance(raw_group, (str, bytes, Path)):
            members = [raw_group]
        else:
            try:
                members = list(raw_group)
            except TypeError:
                continue
        positions: list[int] = []
        for member in members:
            unit = _resolve_unit(member)
            if unit is None:
                continue
            position = position_of.get(unit)
            if position is None or position in claimed or position in positions:
                continue
            positions.append(position)
            claimed.add(position)
        if len(positions) < 2:
            # A one-member group is indistinguishable from no grouping; release
            # the unit so it keeps producing its own Short.
            for position in positions:
                claimed.discard(position)
            continue
        positions.sort()
        groups.append(positions)

    groups.sort(key=lambda group: group[0])
    return groups


def normalize_short_groups(
    raw_groups: Iterable[Any] | None,
    ordered_units: Sequence[Any],
) -> list[list[Path]]:
    """Return validated groups over the ordered voiceover units.

    Rules (all defensive, so a stale or hand-edited project file can never
    produce a broken render):

    * members are resolved exactly like the unit list,
    * unknown members are dropped,
    * a unit belongs to at most one group - the first group that claims it wins,
    * members inside a group are sorted by their position in the unit list, so
      the list order is authoritative,
    * groups with fewer than two remaining members are dropped (that is simply
      "no grouping" for that unit),
    * groups are ordered by their first member's position.
    """
    units = [_resolve_unit(value) for value in ordered_units]
    resolved: list[list[Path]] = []
    for positions in _group_positions(raw_groups, units):
        members = [units[position] for position in positions]
        if all(member is not None for member in members):
            resolved.append(members)
    return resolved


def short_output_name(positions: Sequence[int]) -> str:
    """``"003"`` for one unit, ``"003-004"`` (all members) for a group."""
    if not positions:
        return "000"
    return GROUP_NAME_SEPARATOR.join(f"{int(position):03d}" for position in positions)


def short_cache_key(plan: ShortPlan) -> str:
    """Cache key for one planned Short.

    A single-unit Short keeps the historical key formula byte-identical
    (``youtube-short-<index>-<digest of "short:<index>:<unit>"``), so projects
    without groups reuse their existing cache entries. A grouped Short folds
    every member into the digest, which invalidates the entry as soon as the
    membership changes.
    """
    if not plan.grouped:
        payload = f"short:{plan.index}:{plan.units[0]}"
    else:
        payload = "short:{}:{}".format(plan.index, "|".join(str(unit) for unit in plan.units))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"youtube-short-{plan.index:03d}-{digest}"


def build_short_plan(
    ordered_units: Sequence[Any],
    raw_groups: Iterable[Any] | None = (),
) -> list[ShortPlan]:
    """Return one :class:`ShortPlan` per Short, in render order.

    Without groups this is exactly one plan per voiceover unit, numbered and
    named like before. With groups, every group becomes a single plan placed at
    the position of its first member.
    """
    units = [_resolve_unit(value) for value in ordered_units]
    plans: list[ShortPlan] = []
    consumed: set[int] = set()
    for positions in _group_positions(raw_groups, units):
        members = [units[position] for position in positions]
        if any(member is None for member in members):
            continue
        plan = ShortPlan(
            index=positions[0] + 1,
            positions=tuple(position + 1 for position in positions),
            units=tuple(member for member in members if member is not None),
            output_name=short_output_name([position + 1 for position in positions]),
        )
        plans.append(plan)
        consumed.update(positions)

    for position, unit in enumerate(units):
        if unit is None or position in consumed:
            continue
        plans.append(
            ShortPlan(
                index=position + 1,
                positions=(position + 1,),
                units=(unit,),
                output_name=short_output_name([position + 1]),
            )
        )

    plans.sort(key=lambda plan: plan.index)
    return plans


def _override_lookup(mapping: Any, anchor: Any) -> Any:
    """Return the per-Short override stored for ``anchor``, if any."""
    if not isinstance(mapping, dict):
        return None
    if anchor in mapping:
        return mapping[anchor]
    for key, value in mapping.items():
        if _same_unit(key, anchor):
            return value
    return None


def short_voiceover_pause(settings: ExportSettings, grouped: bool) -> float:
    """Inter-unit silence inside ONE Short.

    A single-unit Short keeps the historical ``0.0``: inter-unit silence belongs
    to the combined Long-Form timeline, never to an individual Short. A GROUPED
    Short really is one combined voiceover timeline (Script A, configured pause,
    Script B), so it keeps the project's own pause between its members - one
    continuous timeline, no overlap and no glued-together hard cut. The bound is
    the same 0-10 s window the rest of the product uses.
    """
    if not grouped:
        return 0.0
    try:
        return max(0.0, min(10.0, float(getattr(settings, "voiceover_pause", 0.7))))
    except (TypeError, ValueError):
        return 0.7


def short_music_for(settings: ExportSettings, anchor: Any) -> str:
    """Music track for one Short: its own override, else the shared Shorts track.

    A Short still stays silent when neither is configured - the strict
    Long-Form/Shorts music separation is never relaxed by grouping.
    """
    override = _override_lookup(getattr(settings, "short_music_overrides", None), anchor)
    text = str(override or "").strip()
    if text:
        return text
    return str(getattr(settings, "short_music_path", "") or "").strip()


def short_music_volume_for(settings: ExportSettings, anchor: Any) -> Any:
    """Music volume for one Short: its own override, else the shared Shorts value.

    The result is handed to :func:`~app.video_merger.youtube_outputs.output_music_volume`,
    which keeps the existing validation, the legacy ``music_volume`` migration
    fallback and the 44 % default completely untouched. ``None`` therefore still
    means "not configured", exactly as before.
    """
    override = _override_lookup(getattr(settings, "short_music_volume_overrides", None), anchor)
    if override is not None and str(override).strip() != "":
        return override
    return getattr(settings, "shorts_music_volume", None)


def describe_short_plans(plans: Sequence[ShortPlan]) -> str:
    """One compact line for the log/manifest when grouping is active."""
    grouped = [plan for plan in plans if plan.grouped]
    if not grouped:
        return ""
    parts = "; ".join(
        "{} = {}".format(plan.output_name, " + ".join(unit.name for unit in plan.units))
        for plan in grouped
    )
    return f"{len(plans)} Short(s) from {sum(len(plan.positions) for plan in plans)} voiceover unit(s); groups: {parts}"
