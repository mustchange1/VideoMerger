"""Phase 24 – soft timeline-area source ordering (source priority scheduling).

The user already organizes clip quality into folders. This module proves the one
new behavior built on top of that organization: which configured folder is used
at which approximate part of the timeline.

* Area ``1. Start & End`` is used for the beginning **and** the ending,
* Area ``2. Start to Middle`` for the earlier/main portion up to the midpoint
  target,
* Area ``3. Middle to End`` for the later/main portion up to the end reserve.

Everything else must stay untouched, and the tests below pin exactly that:

* no clip is ever cut, trimmed or re-timed to reach a zone target – a clip that
  completes after the target is accepted (soft boundaries),
* the result is a pure re-ordering of the incoming sequence, so the video order
  mode (Natural/Alphabetical/Random/Manual), the first-3 Legacy rule and any
  randomization inside a source remain byte-identical,
* folders without a role keep working as a general reserve and are never
  dropped, the legacy input root is never forced into the three roles,
* very short outputs shrink both reserves instead of failing or overlapping,
* YouTube Shorts draw from Area 1 + Area 2 and exclude Area 3 unless the user
  explicitly allows it, and the Shorts pipeline itself is unchanged,
* all new settings persist, reload and fall back to the documented defaults for
  projects saved before they existed, and they are part of the render identity.

The final test renders real media with three visually distinguishable source
folders and measures the produced frames, so the ordering is proven on the
actual output and not only on a list of paths.
"""

from __future__ import annotations

import json
import random
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.models import (
    MAX_TIMELINE_AREA_SECONDS,
    SHORTS_ALLOW_AREA_MIDDLE_END,
    TIMELINE_AREA_END_SECONDS,
    TIMELINE_AREA_MIDPOINT_PERCENT,
    TIMELINE_AREA_START_SECONDS,
    ExportSettings,
)
from app.video_merger.render_cache import (
    FINGERPRINT_SCHEMA,
    Stage1RenderCache,
    stage1_fingerprint,
)
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export
from app.video_merger.timeline_areas import (
    AREA_MIDDLE_END,
    AREA_START_END,
    AREA_START_MIDDLE,
    TIMELINE_AREA_LABELS,
    area_of,
    area_zone_bounds,
    folder_area_map,
    normalize_timeline_area,
    order_media_by_timeline_areas,
    shorts_area_pool,
    timeline_area_label,
    timeline_areas_configured,
)
from app.video_merger.video_pool import (
    VIDEO_ORDER_ALPHABETICAL,
    VIDEO_ORDER_NATURAL,
    VIDEO_ORDER_RANDOM,
    compute_pool_status,
    order_media_for_video_order,
)
from tests.conftest import fake_media, make_clip

# A real render of these 64x64 clips finishes in a few seconds; the bound keeps a
# pathological ordering regression from hanging the suite.
RENDER_BUDGET_SECONDS = 180.0
CLIP_SECONDS = 2.0
# The voiceover is the timing authority: the Main Video target is the spoken
# 12.0 s plus the 1.5 s visual outro, i.e. 13.5 s. With 2.0 s start/end reserves
# and a 50 % midpoint the soft zones are 0-2 s (Area 1), 2-6.75 s (Area 2),
# 6.75-11.5 s (Area 3) and 11.5-13.5 s (Area 1 again). Two clips per folder
# realize that as red / lime / lime / blue / blue / red, and because the pool is
# slightly shorter than the target every clip stays complete – the tail is the
# existing Hold-Last-Frame behavior, never a trimmed clip.
VOICE_SECONDS = 12.0
MAIN_TARGET = VOICE_SECONDS + 1.5
#: Measured colour runs of the rendered Long-Form: (source area, complete clips
#: in that run). Adjacent clips of one area merge into a single run, so the four
#: runs are exactly "Area 1 near the beginning → Area 2 through the earlier and
#: middle portion → Area 3 in the later portion → Area 1 near the end".
EXPECTED_RUNS = (
    (AREA_START_END, 1),
    (AREA_START_MIDDLE, 2),
    (AREA_MIDDLE_END, 2),
    (AREA_START_END, 1),
)
AREA_COLORS = {
    AREA_START_END: "red",
    AREA_START_MIDDLE: "lime",
    AREA_MIDDLE_END: "blue",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clips(folder: str, count: int, duration: float = 2.0) -> list:
    """``count`` clips of one configured folder, in project order."""
    return [
        replace(
            fake_media(f"/media/{folder}/clip_{index}.mp4", duration=duration, audio=False),
            source_folder=f"/media/{folder}",
        )
        for index in range(count)
    ]


def _settings(areas: dict[str, str], **overrides) -> ExportSettings:
    """Settings with the given folder → role mapping (keys are plain names)."""
    values = {
        "source_folder_areas": {f"/media/{name}": role for name, role in areas.items()},
        "source_folders": [f"/media/{name}" for name in areas],
        # Zone arithmetic below is written for 1.00x playback; the product
        # default of 0.70x (longer clips) is pinned by its own test.
        "duration_before_merge": 1.0,
    }
    values.update(overrides)
    return ExportSettings(**values)


def _names(media: list) -> list[str]:
    return [Path(item.path).parent.name + ":" + Path(item.path).stem for item in media]


def _roles_in_order(media: list, settings: ExportSettings) -> list[str]:
    area_map = folder_area_map(settings)
    return [area_of(item, area_map) or "-" for item in media]


# --------------------------------------------------------------------------- #
# configuration surface
# --------------------------------------------------------------------------- #
def test_documented_defaults_are_the_soft_targets_from_the_spec():
    """Defaults: 20 s / 20 s reserves, 50 % midpoint, Shorts exclude Area 3."""
    settings = ExportSettings()
    assert settings.source_folder_areas == {}
    assert settings.timeline_area_start_seconds == TIMELINE_AREA_START_SECONDS == 20.0
    assert settings.timeline_area_end_seconds == TIMELINE_AREA_END_SECONDS == 20.0
    assert settings.timeline_area_midpoint_percent == TIMELINE_AREA_MIDPOINT_PERCENT == 50.0
    assert settings.shorts_allow_area_middle_end is SHORTS_ALLOW_AREA_MIDDLE_END is False
    assert not timeline_areas_configured(settings)
    assert TIMELINE_AREA_LABELS == {
        AREA_START_END: "1. Start & End",
        AREA_START_MIDDLE: "2. Start to Middle",
        AREA_MIDDLE_END: "3. Middle to End",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (AREA_START_END, AREA_START_END),
        ("1", AREA_START_END),
        (" 1 ", AREA_START_END),
        ("Start & End", AREA_START_END),
        ("1. start and end", AREA_START_END),
        ("area 2", AREA_START_MIDDLE),
        ("Start to Middle", AREA_START_MIDDLE),
        (2, AREA_START_MIDDLE),
        ("3", AREA_MIDDLE_END),
        ("Middle to End", AREA_MIDDLE_END),
        ("", ""),
        (None, ""),
        ("4", ""),
        ("best quality", ""),
    ],
)
def test_role_aliases_normalize_to_one_canonical_value(raw, expected):
    """GUI combo, CLI flag and hand-edited project files all agree."""
    assert normalize_timeline_area(raw) == expected
    assert timeline_area_label(normalize_timeline_area(raw)) == (
        TIMELINE_AREA_LABELS[expected] if expected else "No area role"
    )


def test_role_mapping_resolves_folders_and_ignores_junk():
    """Only real, distinct folders carry a role; junk never raises."""
    settings = _settings({"a1": AREA_START_END, "a2": AREA_MIDDLE_END})
    resolved = folder_area_map(settings)
    assert len(resolved) == 2
    assert set(resolved.values()) == {AREA_START_END, AREA_MIDDLE_END}
    item = _clips("a1", 1)[0]
    assert area_of(item, resolved) == AREA_START_END
    # An item whose folder was never configured has no role.
    assert area_of(_clips("unknown", 1)[0], resolved) == ""
    # A legacy item without the canonical folder identity is resolved by path.
    legacy = replace(item, source_folder="")
    assert area_of(legacy, resolved) == AREA_START_END
    # Hand-edited or corrupt values degrade to "no roles", never to a crash.
    for junk in ("garbage", None, 42, {"": "1"}):
        assert folder_area_map(replace(ExportSettings(), source_folder_areas=junk)) == {}
    assert not timeline_areas_configured(replace(ExportSettings(), source_folder_areas="junk"))


@pytest.mark.parametrize("value", [-5.0, float("nan"), float("inf")])
def test_unusable_zone_targets_fall_back_to_the_documented_default(value):
    """A bad saved number never produces a broken timeline."""
    start_default, end_default = TIMELINE_AREA_START_SECONDS, TIMELINE_AREA_END_SECONDS
    settings = _settings(
        {"a1": AREA_START_END},
        timeline_area_start_seconds=value,
        timeline_area_end_seconds=value,
    )
    start, midpoint, end = area_zone_bounds(180.0, settings)
    assert start == pytest.approx(start_default)
    assert end == pytest.approx(180.0 - end_default)
    assert start <= midpoint <= end


def test_zero_zone_targets_legitimately_disable_both_reserves():
    """0 s is a valid user choice, not a corrupt value: no Area 1 zones."""
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=0.0,
        timeline_area_end_seconds=0.0,
        timeline_area_midpoint_percent=50.0,
    )
    start, midpoint, end = area_zone_bounds(120.0, settings)
    assert (start, midpoint, end) == (0.0, 60.0, 120.0)
    media = _clips("a1", 2) + _clips("a2", 2) + _clips("a3", 2)
    ordered = order_media_by_timeline_areas(media, 120.0, settings)
    assert sorted(_names(ordered)) == sorted(_names(media))
    assert _roles_in_order(ordered, settings) == [
        AREA_START_MIDDLE, AREA_START_MIDDLE, AREA_MIDDLE_END, AREA_MIDDLE_END,
        AREA_START_END, AREA_START_END,
    ]


def test_excessive_zone_targets_are_capped_and_never_overlap():
    """A 600 s reserve on a 60 s output shrinks instead of failing (§10)."""
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=MAX_TIMELINE_AREA_SECONDS,
        timeline_area_end_seconds=MAX_TIMELINE_AREA_SECONDS,
    )
    for target in (60.0, 30.0, 8.0):
        start, midpoint, end = area_zone_bounds(target, settings)
        assert 0.0 <= start <= midpoint <= end <= target


@pytest.mark.parametrize("percent", [0.0, 25.0, 40.0, 50.0, 60.0, 100.0, -10.0, 250.0])
def test_midpoint_percent_is_configurable_and_clamped(percent):
    """The midpoint is a setting, never a hard-coded 50 %."""
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_midpoint_percent=percent,
    )
    start, midpoint, end = area_zone_bounds(200.0, settings)
    assert start == pytest.approx(TIMELINE_AREA_START_SECONDS)
    assert end == pytest.approx(200.0 - TIMELINE_AREA_END_SECONDS)
    # Inside the reserves the configured percent decides exactly; outside them
    # the midpoint is clamped so the zones can never invert or overlap.
    wanted = min(max(percent, 0.0), 100.0) / 100.0 * 200.0
    assert midpoint == pytest.approx(min(max(wanted, start), end))
    assert start <= midpoint <= end


# --------------------------------------------------------------------------- #
# soft zone geometry
# --------------------------------------------------------------------------- #
def test_zone_bounds_follow_the_configured_reserve_seconds():
    settings = _settings(
        {"a1": AREA_START_END},
        timeline_area_start_seconds=10.0,
        timeline_area_end_seconds=30.0,
        timeline_area_midpoint_percent=50.0,
    )
    start, midpoint, end = area_zone_bounds(180.0, settings)
    assert (start, midpoint, end) == (10.0, 90.0, 150.0)


def test_short_outputs_shrink_both_reserves_instead_of_overlapping():
    """Very short videos: reserves shrink, the midpoint stays between them."""
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=20.0,
        timeline_area_end_seconds=20.0,
    )
    for target in (30.0, 24.0, 12.0, 6.0, 2.0):
        start, midpoint, end = area_zone_bounds(target, settings)
        assert 0.0 <= start <= midpoint <= end <= target, target
        assert start <= 0.4 * target + 1e-9
        assert target - end <= 0.4 * target + 1e-9


def test_zero_or_missing_target_disables_the_zone_model():
    settings = _settings({"a1": AREA_START_END})
    assert area_zone_bounds(0.0, settings) == (0.0, 0.0, 0.0)
    assert area_zone_bounds(-3.0, settings) == (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# the scheduler itself
# --------------------------------------------------------------------------- #
def test_area_roles_place_sources_at_the_expected_timeline_positions():
    """1. Start & End → 2. Start to Middle → 3. Middle to End → 1. Start & End."""
    media = _clips("a1", 4) + _clips("a2", 4) + _clips("a3", 4)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=5.0,
        timeline_area_end_seconds=5.0,
        timeline_area_midpoint_percent=50.0,
    )
    ordered = order_media_by_timeline_areas(media, 60.0, settings)
    roles = _roles_in_order(ordered, settings)
    # Zones: 0-5 s = Area 1, 5-30 s = Area 2, 30-55 s = Area 3, 55-60 s = Area 1
    # again. Area 1 owns only 8 s for two 5 s zones, so its four clips are split
    # evenly between the beginning and the ending instead of the leading zone
    # consuming all of them.
    assert roles == [AREA_START_END] * 2 + [AREA_START_MIDDLE] * 4 + [
        AREA_MIDDLE_END
    ] * 4 + [AREA_START_END] * 2, roles
    assert roles[0] == roles[-1] == AREA_START_END, "Area 1 belongs at both ends"
    assert roles.index(AREA_START_MIDDLE) < roles.index(AREA_MIDDLE_END)
    # The project order inside every role is untouched.
    assert _names(ordered)[:2] == ["a1:clip_0", "a1:clip_1"]
    assert _names(ordered)[2:6] == [f"a2:clip_{index}" for index in range(4)]
    assert _names(ordered)[6:10] == [f"a3:clip_{index}" for index in range(4)]
    assert _names(ordered)[10:] == ["a1:clip_2", "a1:clip_3"]


def test_plentiful_area_one_material_fills_the_configured_start_target():
    """With enough material the soft target rules and no cap applies."""
    media = _clips("a1", 12, duration=2.0) + _clips("a2", 4, duration=2.0)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE},
        timeline_area_start_seconds=10.0,
        timeline_area_end_seconds=10.0,
        timeline_area_midpoint_percent=50.0,
    )
    ordered = order_media_by_timeline_areas(media, 60.0, settings)
    roles = _roles_in_order(ordered, settings)
    # 24 s of Area 1 comfortably fills both 10 s reserves, so no share cap
    # applies: five clips lead (the 10 s soft target is reached), Area 2 follows
    # until it is spent, and every remaining Area 1 clip closes the timeline.
    assert roles[:5] == [AREA_START_END] * 5
    assert roles[5:9] == [AREA_START_MIDDLE] * 4
    assert roles[9:] == [AREA_START_END] * 7
    assert roles.count(AREA_START_END) == 12
    assert _names(ordered)[:5] == [f"a1:clip_{index}" for index in range(5)]


def test_ordering_is_a_pure_reordering_of_the_incoming_sequence():
    """No clip is added, dropped, duplicated, cut or re-timed."""
    rng = random.Random(7)
    for _attempt in range(12):
        media = _clips("a1", 3) + _clips("a2", 3) + _clips("a3", 2) + _clips("free", 2)
        rng.shuffle(media)
        settings = _settings(
            {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
            timeline_area_start_seconds=rng.uniform(0.0, 12.0),
            timeline_area_end_seconds=rng.uniform(0.0, 12.0),
            timeline_area_midpoint_percent=rng.uniform(0.0, 100.0),
        )
        target = rng.choice([0.0, 4.0, 12.0, 30.0, 90.0])
        ordered = order_media_by_timeline_areas(media, target, settings)
        assert sorted(_names(ordered)) == sorted(_names(media))
        assert all(
            original.duration == moved.duration
            for original, moved in zip(
                sorted(media, key=lambda item: str(item.path)),
                sorted(ordered, key=lambda item: str(item.path)),
            )
        )


def test_a_repeated_clip_object_is_still_a_pure_permutation():
    """Even a duplicated MediaInfo reference is kept exactly as often as given."""
    first = _clips("a1", 1)[0]
    second = _clips("a2", 1)[0]
    media = [first, second, first, second]
    settings = _settings({"a1": AREA_START_END, "a2": AREA_START_MIDDLE})
    ordered = order_media_by_timeline_areas(media, 40.0, settings)
    assert len(ordered) == 4
    assert ordered.count(first) == 2 and ordered.count(second) == 2


def test_a_clip_always_completes_and_zones_stay_soft():
    """A 23.7 s clip satisfies a 20 s start target; nothing is trimmed."""
    long_clip = replace(
        fake_media("/media/a1/clip_long.mp4", duration=23.7, audio=False),
        source_folder="/media/a1",
    )
    media = [long_clip] + _clips("a2", 6, duration=5.0)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE},
        timeline_area_start_seconds=20.0,
        timeline_area_end_seconds=20.0,
    )
    ordered = order_media_by_timeline_areas(media, 60.0, settings)
    assert _names(ordered)[0] == "a1:clip_long"
    assert ordered[0].duration == pytest.approx(23.7), "the clip must stay complete"
    # The zone boundary landed after the 20 s target, and that is accepted: the
    # next role simply starts from the real clock position.
    assert ordered[1].source_folder.endswith("a2")


def test_zone_targets_are_measured_on_the_rendered_timeline():
    """``Duration Before Merge`` lengthens clips, so the zones follow it.

    The zone targets are positions on the rendered video. At the product default
    of 0.70x a 2.0 s source clip occupies ~2.857 s, so a 5 s start reserve is
    reached after two clips instead of three. Clip durations are never modified.
    """
    media = _clips("a1", 6, duration=2.0) + _clips("a2", 2, duration=2.0)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE},
        timeline_area_start_seconds=5.0,
        timeline_area_end_seconds=5.0,
        duration_before_merge=0.70,
    )
    slow = order_media_by_timeline_areas(media, 30.0, settings)
    assert _roles_in_order(slow, settings)[:2] == [AREA_START_END] * 2
    assert _roles_in_order(slow, settings)[2] == AREA_START_MIDDLE
    fast = order_media_by_timeline_areas(media, 30.0, settings, playback_rate=1.0)
    assert _roles_in_order(fast, settings)[:3] == [AREA_START_END] * 3
    assert _roles_in_order(fast, settings)[3] == AREA_START_MIDDLE
    # Neither run changes a single clip duration.
    assert [item.duration for item in slow] == [2.0] * 8
    assert sorted(_names(slow)) == sorted(_names(media))


def test_several_folders_can_share_one_role_and_keep_their_project_order():
    """No arbitrary weighting: shared roles stay in the configured order."""
    media = _clips("best", 2) + _clips("good", 2) + _clips("a3", 2)
    settings = _settings(
        {"best": AREA_START_END, "good": AREA_START_END, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=3.0,
        timeline_area_end_seconds=3.0,
    )
    ordered = order_media_by_timeline_areas(media, 40.0, settings)
    leading = [name for name, role in zip(_names(ordered), _roles_in_order(ordered, settings))
               if role == AREA_START_END]
    assert leading[0] == "best:clip_0", "the shared role keeps the incoming order"
    assert set(leading) == {"best:clip_0", "best:clip_1", "good:clip_0", "good:clip_1"}


def test_folders_without_a_role_are_kept_and_used_as_reserve():
    """Unassigned material is never dropped and fills an exhausted role."""
    media = _clips("a1", 1) + _clips("a2", 1) + _clips("free", 4)
    settings = _settings({"a1": AREA_START_END, "a2": AREA_START_MIDDLE})
    ordered = order_media_by_timeline_areas(media, 60.0, settings)
    assert sorted(_names(ordered)) == sorted(_names(media))
    roles = _roles_in_order(ordered, settings)
    assert roles[0] == AREA_START_END
    assert roles[1] == AREA_START_MIDDLE
    # The four unassigned clips are still in the sequence (general reserve).
    assert roles.count("-") == 4


def test_an_exhausted_role_ends_its_zone_and_never_pads_with_another_role():
    """A zone is not filled with foreign material; every clip still survives."""
    media = _clips("a1", 2) + _clips("a2", 2) + _clips("a3", 2)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=20.0,
        timeline_area_end_seconds=20.0,
    )
    ordered = order_media_by_timeline_areas(media, 60.0, settings)
    assert sorted(_names(ordered)) == sorted(_names(media))
    # Area 1 owns two clips: one leads, the remaining one closes the timeline.
    assert _roles_in_order(ordered, settings) == [
        AREA_START_END, AREA_START_MIDDLE, AREA_START_MIDDLE,
        AREA_MIDDLE_END, AREA_MIDDLE_END, AREA_START_END,
    ]


def test_legacy_input_root_material_is_not_forced_into_a_role():
    """§9: the legacy root keeps working as an ordinary optional source."""
    legacy_clip = replace(
        fake_media("/legacy/root/old_clip.mp4", duration=3.0, audio=False),
        source_folder="/legacy/root",
    )
    media = [legacy_clip] + _clips("a1", 2)
    settings = _settings({"a1": AREA_START_END}, legacy_input_root="/legacy/root")
    ordered = order_media_by_timeline_areas(media, 30.0, settings)
    assert legacy_clip in ordered
    assert area_of(legacy_clip, folder_area_map(settings)) == ""
    # Without any configured role the legacy-priority order is not touched.
    untouched = order_media_by_timeline_areas(media, 30.0, ExportSettings(legacy_input_root="/legacy/root"))
    assert untouched == media


@pytest.mark.parametrize("mode", [VIDEO_ORDER_NATURAL, VIDEO_ORDER_ALPHABETICAL, VIDEO_ORDER_RANDOM])
def test_video_order_mode_decides_first_and_areas_only_regroup_the_result(mode):
    """The order inside a role is exactly what the video order mode produced."""
    media = _clips("a1", 3) + _clips("a2", 3) + _clips("a3", 3)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=4.0,
        timeline_area_end_seconds=4.0,
    )
    sequenced = order_media_for_video_order(media, mode, rng=random.Random(24), seed=24, legacy_root="")
    ordered = order_media_by_timeline_areas(sequenced, 40.0, settings)
    area_map = folder_area_map(settings)
    for area in TIMELINE_AREA_LABELS:
        expected = [item.path for item in sequenced if area_of(item, area_map) == area]
        actual = [item.path for item in ordered if area_of(item, area_map) == area]
        assert actual == expected, f"{mode}: the {area} order must be the mode's order"


def test_randomized_sequence_inside_a_role_stays_identical():
    """§12: randomization within a source is preserved, only the source changes."""
    media = _clips("a1", 4) + _clips("a2", 4) + _clips("a3", 4)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=5.0,
        timeline_area_end_seconds=5.0,
    )
    for seed in (1, 24, 99):
        sequenced = order_media_for_video_order(
            media, VIDEO_ORDER_RANDOM, rng=random.Random(seed), seed=seed, legacy_root=""
        )
        ordered = order_media_by_timeline_areas(sequenced, 50.0, settings)
        area_map = folder_area_map(settings)
        for area in TIMELINE_AREA_LABELS:
            assert [item.path for item in ordered if area_of(item, area_map) == area] == [
                item.path for item in sequenced if area_of(item, area_map) == area
            ]


def test_no_roles_or_no_target_keeps_the_historical_order_unchanged():
    """Byte-identical behavior for every project without a configured role."""
    media = _clips("a1", 3) + _clips("b", 2) + _clips("c", 1)
    assert order_media_by_timeline_areas(media, 60.0, ExportSettings()) == media
    assert order_media_by_timeline_areas(media, 0.0, _settings({"a1": AREA_START_END})) == media
    assert order_media_by_timeline_areas([], 60.0, _settings({"a1": AREA_START_END})) == []
    single = _clips("a1", 1)
    assert order_media_by_timeline_areas(single, 60.0, _settings({"a1": AREA_START_END})) == single


def test_short_material_cannot_stall_the_scheduler():
    """Fewer clips than zones: everything is still used exactly once."""
    media = _clips("a1", 1) + _clips("a2", 1) + _clips("a3", 1)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=20.0,
        timeline_area_end_seconds=20.0,
    )
    ordered = order_media_by_timeline_areas(media, 3.0, settings)
    assert sorted(_names(ordered)) == sorted(_names(media))
    assert _roles_in_order(ordered, settings)[0] == AREA_START_END


# --------------------------------------------------------------------------- #
# YouTube Shorts source pool
# --------------------------------------------------------------------------- #
def test_shorts_pool_uses_area_one_and_two_and_excludes_area_three():
    media = _clips("a1", 2) + _clips("a2", 2) + _clips("a3", 2) + _clips("free", 1)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END}
    )
    pool = shorts_area_pool(media, settings)
    assert sorted(_names(pool)) == sorted(
        ["a1:clip_0", "a1:clip_1", "a2:clip_0", "a2:clip_1", "free:clip_0"]
    )
    assert all(not Path(item.path).parent.name.endswith("a3") for item in pool)
    # The relative order of the remaining clips is untouched.
    assert _names(pool) == [name for name in _names(media) if not name.startswith("a3:")]


def test_shorts_pool_can_be_explicitly_expanded_to_area_three():
    media = _clips("a1", 1) + _clips("a3", 2)
    settings = _settings(
        {"a1": AREA_START_END, "a3": AREA_MIDDLE_END},
        shorts_allow_area_middle_end=True,
    )
    assert shorts_area_pool(media, settings) == media
    assert len(shorts_area_pool(media, _settings(
        {"a1": AREA_START_END, "a3": AREA_MIDDLE_END}))) == 1


def test_shorts_pool_falls_back_when_only_area_three_is_configured():
    """Excluding Area 3 must never produce an empty Shorts pool."""
    media = _clips("a3", 3)
    settings = _settings({"a3": AREA_MIDDLE_END})
    assert shorts_area_pool(media, settings) == media
    # Without any role the Shorts pool is the historical full pool.
    assert shorts_area_pool(media, ExportSettings()) == media
    assert shorts_area_pool([], settings) == []


# --------------------------------------------------------------------------- #
# GUI agreement
# --------------------------------------------------------------------------- #
def aware_selected(sequenced: list, target: float, settings: ExportSettings) -> list:
    """The prefix the render really uses, computed through the public helpers."""
    ordered = order_media_by_timeline_areas(sequenced, target, settings)
    status = compute_pool_status(
        sequenced, target, 0.0, 30.0, folder_aware=False, timeline_area_settings=settings
    )
    return ordered[: status.required]


def test_pool_status_uses_the_same_soft_ordering_as_the_render():
    """The status line and the Stage-1 sequence must never disagree."""
    media = _clips("a1", 3, duration=2.0) + _clips("a2", 3, duration=2.0) + _clips("a3", 3, duration=2.0)
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=3.0,
        timeline_area_end_seconds=3.0,
    )
    target = 9.0
    sequenced = order_media_for_video_order(media, VIDEO_ORDER_NATURAL, legacy_root="")
    ordered = order_media_by_timeline_areas(sequenced, target, settings)
    plain = compute_pool_status(sequenced, target, 0.0, 30.0, folder_aware=False)
    aware = compute_pool_status(
        sequenced, target, 0.0, 30.0, folder_aware=False, timeline_area_settings=settings
    )
    assert aware.total == plain.total, "the pool size never changes"
    assert aware.selected == plain.selected
    # The status describes the area-ordered sequence: its required prefix must
    # be exactly the prefix the render will use.
    assert [item.path for item in ordered[: aware.required]] == [
        item.path for item in aware_selected(sequenced, target, settings)
    ]


# --------------------------------------------------------------------------- #
# persistence, migration and render identity
# --------------------------------------------------------------------------- #
def test_settings_round_trip_through_the_project_file(tmp_path):
    store = SettingsStore(tmp_path / "project.json")
    settings = _settings(
        {"a1": AREA_START_END, "a2": AREA_START_MIDDLE, "a3": AREA_MIDDLE_END},
        timeline_area_start_seconds=12.5,
        timeline_area_end_seconds=27.5,
        timeline_area_midpoint_percent=42.0,
        shorts_allow_area_middle_end=True,
    )
    store.save(settings)
    loaded = store.load()
    assert loaded.source_folder_areas == settings.source_folder_areas
    assert loaded.timeline_area_start_seconds == 12.5
    assert loaded.timeline_area_end_seconds == 27.5
    assert loaded.timeline_area_midpoint_percent == 42.0
    assert loaded.shorts_allow_area_middle_end is True
    assert folder_area_map(loaded) == folder_area_map(settings)


def test_project_saved_before_the_feature_reloads_with_defaults(tmp_path):
    """Backward compatibility: an old project file keeps working unchanged."""
    path = tmp_path / "legacy-project.json"
    payload = {
        "source_folders": ["/media/a1", "/media/a2"],
        "video_order_mode": "natural",
        "legacy_input_root": "/media/input",
        "music_path": "",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.source_folder_areas == {}
    assert loaded.timeline_area_start_seconds == TIMELINE_AREA_START_SECONDS
    assert loaded.timeline_area_end_seconds == TIMELINE_AREA_END_SECONDS
    assert loaded.timeline_area_midpoint_percent == TIMELINE_AREA_MIDPOINT_PERCENT
    assert loaded.shorts_allow_area_middle_end is False
    assert loaded.legacy_input_root == "/media/input"
    assert loaded.source_folders == ["/media/a1", "/media/a2"]
    # And the historical order is untouched for such a project.
    media = _clips("a1", 2) + _clips("a2", 2)
    assert order_media_by_timeline_areas(media, 60.0, loaded) == media


def test_corrupt_saved_area_values_degrade_to_no_roles(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text(json.dumps({
        "source_folder_areas": {"a1": "best quality", "a2": 99},
        "timeline_area_start_seconds": "not a number",
        "timeline_area_midpoint_percent": None,
    }), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert folder_area_map(loaded) == {}
    assert area_zone_bounds(120.0, loaded) == (
        TIMELINE_AREA_START_SECONDS, 60.0, 120.0 - TIMELINE_AREA_END_SECONDS
    )


def _fingerprint(media: list, settings: ExportSettings) -> tuple[str, dict]:
    return stage1_fingerprint(media, settings, resolve_export(media, settings))


def test_area_settings_are_part_of_the_render_identity():
    """The cache must not reuse a render made with different source ordering."""
    media = _clips("a1", 2) + _clips("a2", 2)
    base = _settings({"a1": AREA_START_END, "a2": AREA_START_MIDDLE})
    fingerprint, payload = _fingerprint(media, base)
    variants = (
        ExportSettings(),
        _settings({"a1": AREA_START_MIDDLE, "a2": AREA_START_END}),
        _settings({"a1": AREA_START_END}),
        _settings({"a1": AREA_START_END, "a2": AREA_START_MIDDLE}, timeline_area_start_seconds=5.0),
        _settings({"a1": AREA_START_END, "a2": AREA_START_MIDDLE}, timeline_area_end_seconds=5.0),
        _settings(
            {"a1": AREA_START_END, "a2": AREA_START_MIDDLE}, timeline_area_midpoint_percent=30.0
        ),
        _settings(
            {"a1": AREA_START_END, "a2": AREA_START_MIDDLE}, shorts_allow_area_middle_end=True
        ),
    )
    for variant in variants:
        assert _fingerprint(media, variant)[0] != fingerprint, variant
    # Identical configuration → identical identity (no spurious invalidation),
    # including an equivalent mapping written in another key order.
    assert _fingerprint(media, _settings({"a1": AREA_START_END, "a2": AREA_START_MIDDLE}))[0] == fingerprint
    assert _fingerprint(media, _settings({"a2": AREA_START_MIDDLE, "a1": AREA_START_END}))[0] == fingerprint
    assert json.dumps(payload, sort_keys=True) == json.dumps(payload, sort_keys=True)


def test_fingerprint_schema_bump_invalidates_area_less_cache_entries():
    """A schema-4 fingerprint is unreachable, so old entries are never reused."""
    assert FINGERPRINT_SCHEMA == 5
    media = _clips("a1", 1)
    _digest, payload = _fingerprint(media, _settings({"a1": AREA_START_END}))
    assert payload["schema"] == FINGERPRINT_SCHEMA


# --------------------------------------------------------------------------- #
# real render: measured frames of the produced media
# --------------------------------------------------------------------------- #
def _run(command: list[str], timeout: int = 180) -> bytes:
    result = subprocess.run(
        command, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False
    )
    assert result.returncode == 0, (
        f"{' '.join(str(part) for part in command[:6])}…\n"
        f"{result.stderr.decode('utf-8', 'replace')[-800:]}"
    )
    return result.stdout


def _probe(ffprobe: Path, path: Path) -> dict:
    return json.loads(_run([
        str(ffprobe), "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", str(path),
    ]).decode("utf-8"))


def _frame_rgb(ffmpeg: Path, path: Path, at: float) -> tuple[float, float, float]:
    """Mean R/G/B of one decoded frame: which source folder is on screen?"""
    raw = _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-ss", f"{at:.3f}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    assert raw, f"no frame decoded at {at:.3f} s"
    pixels = len(raw) // 3
    red = sum(raw[index * 3] for index in range(pixels)) / pixels
    green = sum(raw[index * 3 + 1] for index in range(pixels)) / pixels
    blue = sum(raw[index * 3 + 2] for index in range(pixels)) / pixels
    return red, green, blue


#: A 9:16 Short letterboxes the 16:9 source, so a pure colour averages ~80/255
#: over the whole frame; the thresholds separate the three sources unambiguously
#: while still rejecting the dark intro/outro sections.
COLOUR_FLOOR = 55.0
COLOUR_MARGIN = 25.0


def _classify(ffmpeg: Path, path: Path, at: float) -> str:
    red, green, blue = _frame_rgb(ffmpeg, path, at)
    if red > COLOUR_FLOOR and red > green + COLOUR_MARGIN and red > blue + COLOUR_MARGIN:
        return AREA_START_END
    if green > COLOUR_FLOOR and green > red + COLOUR_MARGIN and green > blue + COLOUR_MARGIN:
        return AREA_START_MIDDLE
    if blue > COLOUR_FLOOR and blue > red + COLOUR_MARGIN and blue > green + COLOUR_MARGIN:
        return AREA_MIDDLE_END
    return "other"



def _colour_runs(
    ffmpeg: Path, path: Path, start: float, stop: float, step: float = 0.1
) -> list[tuple[str, float]]:
    """Segment the rendered video into runs of one source colour.

    Sampling in 0.1 s steps is far finer than any clip here, so a run shorter
    than a complete clip would be visible immediately: that is how "no clip was
    cut to satisfy a zone target" is measured on real media.
    """
    runs: list[tuple[str, float]] = []
    position = start
    while position < stop:
        colour = _classify(ffmpeg, path, position)
        if runs and runs[-1][0] == colour:
            runs[-1] = (colour, runs[-1][1] + step)
        else:
            runs.append((colour, step))
        position += step
    return runs



def _voice(ffmpeg: Path, path: Path, seconds: float) -> Path:
    _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(path),
    ])
    return path


@pytest.mark.e2e
def test_real_render_places_areas_on_the_timeline_and_shorts_exclude_area3(ffmpeg_paths, tmp_path):
    """§13: measured proof on real media rendered by the product pipeline.

    Three folders carry unambiguous solid colours (red = 1. Start & End,
    lime = 2. Start to Middle, blue = 3. Middle to End). The rendered Long-Form
    must show red near the beginning, lime through the earlier/middle portion,
    blue in the later portion and red again near the end – with every clip
    complete – while the rendered Short must not contain a single blue frame.
    """
    from app.video_merger.engine import VideoMergerEngine
    from app.video_merger.main_project import MainProjectEngine
    from app.video_merger.youtube_outputs import EXPORT_MODE_COMBINED

    ffmpeg, ffprobe = ffmpeg_paths
    work = tmp_path / "areas"
    work.mkdir(parents=True)
    folders = {
        AREA_START_END: work / "area1_start_end",
        AREA_START_MIDDLE: work / "area2_start_middle",
        AREA_MIDDLE_END: work / "area3_middle_end",
    }
    counts = {AREA_START_END: 2, AREA_START_MIDDLE: 2, AREA_MIDDLE_END: 2}
    clips: list[Path] = []
    for area, folder in folders.items():
        for index in range(counts[area]):
            path = folder / f"clip_{index}.mp4"
            make_clip(
                ffmpeg, path, size="64x36", fps=15, duration=CLIP_SECONDS,
                color=AREA_COLORS[area], audio_rate=None,
            )
            clips.append(path)
    # Natural order = area1, area2, area3 – the same order a user would see.
    clips.sort()
    voice = _voice(ffmpeg, work / "voice.wav", VOICE_SECONDS)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_COMBINED,
        source_folders=[str(folder) for folder in folders.values()],
        source_folder_areas={str(folder): area for area, folder in folders.items()},
        timeline_area_start_seconds=2.0,
        timeline_area_end_seconds=2.0,
        timeline_area_midpoint_percent=50.0,
        long_form_intro_seconds=0.0,
        short_intro_seconds=0.0,
        short_outro_seconds=0.0,
        shorts_allow_area_middle_end=False,
        voiceover_paths=[str(voice)],
        script_mode="single",
        # No burned captions and no cross-dissolve: both would mix the source
        # colours and make the measurement ambiguous.
        subtitle_enabled=False,
        transition_duration=0.0,
        long_form_transition_duration=0.0,
        shorts_transition_duration=0.0,
        original_audio_mode="mute",
        normalize_audio=False,
        ducking_enabled=False,
        music_path="",
        short_music_path="",
        video_order_mode="natural",
        # 1.00x keeps the measured clip lengths equal to the source lengths, so
        # the colour positions below are exact timeline arithmetic.
        duration_before_merge=1.0,
        workflow_stage="main",
        encoding="CPU",
        quality_preset="custom",
        crf=32,
        preset="ultrafast",
        resolution="640x360",
        aspect="16:9",
    )
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(clips, lambda _message: None)
    logs: list[str] = []
    project = MainProjectEngine(engine, render_cache=Stage1RenderCache(work / "stage1-cache"))
    started = time.perf_counter()
    result = project.create_youtube_exports(
        media, settings, work / "output", aligner=None, log=logs.append
    )
    elapsed = time.perf_counter() - started
    assert elapsed < RENDER_BUDGET_SECONDS, f"render took {elapsed:.1f} s"
    main_video = result.long_form.video
    assert main_video.is_file() and main_video.stat().st_size > 0
    assert result.shorts, "the combined run must also produce a Short"

    joined = "\n".join(logs)
    assert "Timeline areas (soft targets):" in joined, joined[-1500:]
    # The log states the Shorts policy explicitly: Area 3 is excluded.
    assert "Timeline areas (Shorts):" in joined, joined[-1500:]
    assert "3. Middle to End excluded" in joined, joined[-1500:]

    # ---- Long-Form: the measured colour at each part of the timeline -------
    info = _probe(ffprobe, main_video)
    stream = next(item for item in info["streams"] if item["codec_type"] == "video")
    audio = next(item for item in info["streams"] if item["codec_type"] == "audio")
    duration = float(info["format"]["duration"])
    assert (stream["width"], stream["height"]) == (640, 360)
    assert audio["codec_type"] == "audio"
    assert float(audio["duration"]) >= duration - 0.3, "audio must reach the video end"
    assert duration == pytest.approx(MAIN_TARGET, abs=0.35), (duration, joined[-1200:])

    # ---- the measured colour runs are the configured timeline areas --------
    runs = _colour_runs(ffmpeg, main_video, 0.05, duration - 0.05)
    measured = [(colour, length) for colour, length in runs if colour != "other"]
    assert [colour for colour, _length in measured] == [
        colour for colour, _clips in EXPECTED_RUNS
    ], (runs, measured)
    for (colour, clip_count), (_measured_colour, length) in zip(EXPECTED_RUNS, measured):
        assert length >= clip_count * CLIP_SECONDS - 0.4, (
            f"{TIMELINE_AREA_LABELS[colour]} appears for only {length:.2f} s "
            f"instead of {clip_count} complete clip(s): a zone target must never "
            "trim a clip"
        )
    # Area 1 really is at BOTH ends, Area 2 precedes Area 3, and every one of
    # the six clips is present in the render (2 + 4 + 4 + 2 s of source colour).
    assert measured[0][0] == AREA_START_END
    assert measured[-1][0] == AREA_START_END
    assert sum(length for _colour, length in measured) >= 6 * CLIP_SECONDS - 0.6

    # ---- Shorts: Area 1 + Area 2 only, Area 3 never on screen --------------
    short = result.shorts[0].video
    short_info = _probe(ffprobe, short)
    short_duration = float(short_info["format"]["duration"])
    assert short_duration > 1.0
    short_stream = next(item for item in short_info["streams"] if item["codec_type"] == "video")
    # Shorts keep their own vertical profile; only the source pool changed.
    assert short_stream["height"] > short_stream["width"], short_stream
    short_runs = _colour_runs(ffmpeg, short, 0.15, short_duration - 0.15, step=0.2)
    seen = {colour for colour, _length in short_runs}
    assert AREA_MIDDLE_END not in seen, (
        f"Area 3 material appeared in a YouTube Short: {sorted(seen)}"
    )
    assert AREA_START_END in seen and AREA_START_MIDDLE in seen, sorted(seen)
    for colour, length in short_runs:
        if colour != "other":
            assert length >= CLIP_SECONDS - 0.5, (colour, length, short_runs)

    
    assert not any("FAILED" in line for line in logs), joined[-1500:]
