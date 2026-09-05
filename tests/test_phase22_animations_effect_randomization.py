"""Phase 22: clean subtitle animations, the opening visual effect, the Legacy
Input Root randomization preference and the cache identity of every new setting.

Four contracts are pinned here.

1. **Clean subtitle animations** — Outline Highlight painted filled rectangular
   areas outside the glyphs and is gone from the selectable set *and* from the
   renderer: no ASS override can produce an outline colour, an outline/shadow
   width, a vector drawing or a clip rectangle any more. Word Highlight stays a
   Long-Form animation but is removed from Shorts, whose default is the
   conservative phrase-level ``phrase_focus``. Every deprecated or unknown stored
   value migrates deterministically to a valid animation of its own collection.

2. **Opening visual effect** — ``none`` by default, subtle (5 % peak), centred,
   covering the opening portion only, applied to the assembled visual timeline
   *before* the subtitle burn-in, continuous across chunked rendering and never
   changing the voiceover-driven target duration.

3. **Randomization preference** — while Random order is active, the first three
   clips come from the Legacy Input Root (randomized among themselves, distinct
   where possible) and are reserved *before* the rest of the sequence is built.
   Clip 4 onwards keeps the unchanged full-pool behavior, and Manual,
   Alphabetical and Natural order are not affected at all.

4. **Cache identity** — the Stage-1 fingerprint (schema 3) changes with every
   new render-affecting setting and with the effective media order.
"""

from __future__ import annotations

import random
import re
from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.alignment import script_word_spans
from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.main_project import _log_legacy_priority
from app.video_merger.models import (
    AlignmentResult,
    ExportSettings,
    WordTiming,
)
from app.video_merger.opening_effects import (
    MIN_OPENING_EFFECT_SECONDS,
    OPENING_EFFECT_KEYS,
    OPENING_EFFECT_LABELS,
    OPENING_EFFECT_NONE,
    OPENING_EFFECT_SECONDS,
    OPENING_EFFECT_ZOOM,
    OPENING_EFFECT_ZOOM_IN,
    OPENING_EFFECT_ZOOM_OUT,
    OPENING_EFFECTS,
    normalize_opening_effect,
    opening_effect_filter,
    opening_effect_window,
)
from app.video_merger.render_cache import (
    FINGERPRINT_SCHEMA,
    STAGE2_FINGERPRINT_SCHEMA,
    build_stage2_payload,
    stage1_fingerprint,
    stage2_fingerprint,
)
from app.video_merger.subtitle_preview import preview_cue, word_style_for
from app.video_merger.subtitles import (
    ANIMATION_OPTIONS,
    DEFAULT_LONG_ANIMATION,
    DEFAULT_SHORT_ANIMATION,
    LONG_ANIMATION_OPTIONS,
    SHORT_ANIMATION_OPTIONS,
    accepted_animation_values,
    animation_options,
    build_cues,
    normalize_subtitle_animation,
    write_ass,
)
from app.video_merger.target import resolve_export
from app.video_merger.video_pool import (
    LEGACY_PRIORITY_CLIPS,
    legacy_priority_prefix,
    order_media_for_video_order,
    reserve_legacy_priority,
)
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_SHORTS,
    build_short_jobs,
    short_settings,
)
from tests.conftest import fake_media

SCRIPT = "Dies ist ein ruhiger Satz. Danach folgt ein zweites gut lesbares Beispiel."

#: ASS override tags that would paint filled areas outside the glyphs, cover
#: large frame regions or draw vector boxes. None of them may ever be emitted.
FORBIDDEN_ASS_TAGS = {
    "3c", "4c", "bord", "shad", "be", "blur", "clip", "iclip", "p",
    "frx", "fry", "frz", "fax", "fay", "org",
}


def _alignment(script: str = SCRIPT) -> AlignmentResult:
    """A perfect word timeline, exactly like the other subtitle suites use."""
    spans = script_word_spans(script)
    words = [
        WordTiming(token, index * 0.31, index * 0.31 + 0.22, 0.98, start, end)
        for index, (token, start, end) in enumerate(spans)
    ]
    return AlignmentResult(words, "de", "test word timestamps", 1.0, 0.98)


def _ass_events(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").split("[Events]", 1)[1]


def _ass_tags(events: str) -> set[str]:
    return {tag.casefold() for tag in re.findall(r"\\([A-Za-z]+)", events)}


# --------------------------------------------------------------------------- #
# 1. Clean subtitle animations
# --------------------------------------------------------------------------- #


def test_word_highlight_is_long_form_only_and_shorts_default_to_phrase_focus():
    long_keys = dict(LONG_ANIMATION_OPTIONS)
    short_keys = dict(SHORT_ANIMATION_OPTIONS)

    # The new conservative Short default: phrase-level, no per-word switching,
    # no rectangular highlight, stable on a 9:16 mobile frame.
    assert DEFAULT_SHORT_ANIMATION == "phrase_focus"
    assert DEFAULT_SHORT_ANIMATION in short_keys
    assert DEFAULT_LONG_ANIMATION == "static_phrase"
    assert DEFAULT_LONG_ANIMATION in long_keys
    assert ExportSettings().short_subtitle_animation == DEFAULT_SHORT_ANIMATION
    assert ExportSettings().subtitle_animation == DEFAULT_LONG_ANIMATION

    # Word Highlight is not selectable for Shorts any more, but stays available
    # for the Long-Form where it renders cleanly (``\c`` only).
    assert "word_highlight" not in short_keys
    assert "word_highlight" in long_keys
    assert animation_options("short") == SHORT_ANIMATION_OPTIONS
    assert animation_options("long") == LONG_ANIMATION_OPTIONS

    # Old projects must never crash: the removed values stay *accepted* and are
    # migrated, while only the clean animations remain selectable.
    assert accepted_animation_values("short") == (
        "static_phrase", "phrase_focus", "type_reveal", "color_change",
        "word_highlight", "outline_highlight",
    )
    assert accepted_animation_values("long") == (
        "static_phrase", "phrase_focus", "type_reveal", "color_change",
        "word_highlight", "outline_highlight",
    )
    for collection in ("long", "short"):
        for accepted in accepted_animation_values(collection):
            migrated = normalize_subtitle_animation(accepted, collection)
            assert migrated in dict(animation_options(collection)), (collection, accepted)


def test_outline_highlight_is_not_selectable_in_either_collection():
    selectable = {key for key, _label in ANIMATION_OPTIONS}
    assert "outline_highlight" not in selectable
    assert "outline_highlight" not in dict(LONG_ANIMATION_OPTIONS)
    assert "outline_highlight" not in dict(SHORT_ANIMATION_OPTIONS)
    # Phrase Focus took its place, so the option count stays the same.
    assert "phrase_focus" in selectable
    assert len(selectable) == len(LONG_ANIMATION_OPTIONS) == 5
    assert len(SHORT_ANIMATION_OPTIONS) == 4


@pytest.mark.parametrize(
    ("stored", "collection", "expected"),
    [
        # The removed rectangular variant migrates to the clean colour variant.
        ("outline_highlight", "long", "color_change"),
        ("outline_highlight", "short", "color_change"),
        ("OUTLINE_HIGHLIGHT", "long", "color_change"),
        # Word Highlight stays valid for the Long-Form, migrates for Shorts.
        ("word_highlight", "long", "word_highlight"),
        ("word_highlight", "short", "phrase_focus"),
        # Every clean animation survives untouched in its own collection.
        ("static_phrase", "long", "static_phrase"),
        ("static_phrase", "short", "static_phrase"),
        ("phrase_focus", "long", "phrase_focus"),
        ("phrase_focus", "short", "phrase_focus"),
        ("type_reveal", "short", "type_reveal"),
        ("color_change", "short", "color_change"),
        # Unknown, empty or non-string values fall back to the collection
        # default instead of raising while an old project is loaded.
        ("", "long", DEFAULT_LONG_ANIMATION),
        ("", "short", DEFAULT_SHORT_ANIMATION),
        (None, "long", DEFAULT_LONG_ANIMATION),
        (None, "short", DEFAULT_SHORT_ANIMATION),
        ("does_not_exist", "short", DEFAULT_SHORT_ANIMATION),
        (17, "long", DEFAULT_LONG_ANIMATION),
    ],
)
def test_stored_animation_values_migrate_deterministically(stored, collection, expected):
    assert normalize_subtitle_animation(stored, collection) == expected
    # The migrated value is always a selectable one of that collection.
    assert normalize_subtitle_animation(stored, collection) in accepted_animation_values(
        collection
    )


def test_a_saved_short_word_highlight_migrates_at_the_job_boundary():
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=["voice_1.wav"],
        short_subtitle_animation="word_highlight",
    )
    job = build_short_jobs(settings)[0]
    assert short_settings(settings, job).subtitle_animation == "phrase_focus"

    # A deprecated Outline Highlight in either profile migrates as well.
    legacy = replace(settings, short_subtitle_animation="outline_highlight")
    assert short_settings(legacy, job).subtitle_animation == "color_change"


@pytest.mark.parametrize("collection,preset", [("long", "long_1"), ("short", "short_2")])
@pytest.mark.parametrize(
    "animation",
    [key for key, _label in LONG_ANIMATION_OPTIONS]
    + ["outline_highlight", "word_highlight", "does_not_exist"],
)
def test_no_animation_can_render_areas_outside_the_glyphs(tmp_path, collection, animation, preset):
    """The renderer itself is safe — not only the selectable list."""
    cues = build_cues(SCRIPT, _alignment(), preset)
    path = tmp_path / f"{collection}_{animation}.ass"
    write_ass(
        SCRIPT, cues, path, preset, "Bottom Center", 1920, 1080, animation=animation
    )
    events = _ass_events(path)
    assert events.count("Dialogue:") >= len(cues)
    forbidden = _ass_tags(events) & FORBIDDEN_ASS_TAGS
    assert forbidden == set(), f"{animation} emitted {sorted(forbidden)}"
    # The spoken text stays complete: every word of the script is captioned.
    spoken = re.sub(r"\{[^}]*\}", "", events)
    for token in SCRIPT.split():
        assert token in spoken


def test_the_preview_migrates_and_never_flags_the_removed_accent_outline():
    layout = preview_cue(
        "Alpha bravo charlie delta", "inter", "short_2", "Top Center", 1080, 1920,
        animation="outline_highlight", active_word=1,
    )
    assert layout.animation == "color_change"

    long_layout = preview_cue(
        "Alpha bravo charlie delta", "inter", "long_1", "Bottom", 1920, 1080,
        animation="word_highlight", active_word=1,
    )
    assert long_layout.animation == "word_highlight"
    short_layout = preview_cue(
        "Alpha bravo charlie delta", "inter", "short_2", "Top Center", 1080, 1920,
        animation="word_highlight", active_word=1,
    )
    assert short_layout.animation == "phrase_focus"

    # The third tuple element was the accent-outline flag of the removed variant;
    # it can never be raised again, whatever is stored in a project.
    for animation in ("outline_highlight", "word_highlight", "phrase_focus", "type_reveal"):
        for index in range(4):
            assert word_style_for(short_layout, animation, index, 4, 1)[2] is False


# --------------------------------------------------------------------------- #
# 2. The optional opening visual effect
# --------------------------------------------------------------------------- #


def test_opening_effect_registry_defaults_to_none():
    assert [key for key, _label in OPENING_EFFECTS] == ["none", "zoom_in", "zoom_out"]
    assert OPENING_EFFECT_KEYS == {"none", "zoom_in", "zoom_out"}
    assert OPENING_EFFECT_LABELS["none"] == "None"
    assert ExportSettings().opening_effect == OPENING_EFFECT_NONE
    # Subtle by construction: five percent peak magnification.
    assert OPENING_EFFECT_ZOOM == pytest.approx(0.05)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("none", OPENING_EFFECT_NONE),
        ("", OPENING_EFFECT_NONE),
        (None, OPENING_EFFECT_NONE),
        ("off", OPENING_EFFECT_NONE),
        ("disabled", OPENING_EFFECT_NONE),
        ("zoom_in", OPENING_EFFECT_ZOOM_IN),
        ("ZOOM-IN", OPENING_EFFECT_ZOOM_IN),
        (" Gentle Zoom In ", OPENING_EFFECT_ZOOM_IN),
        ("zoomin", OPENING_EFFECT_ZOOM_IN),
        ("zoom_out", OPENING_EFFECT_ZOOM_OUT),
        ("gentle_zoom_out", OPENING_EFFECT_ZOOM_OUT),
        ("zoomout", OPENING_EFFECT_ZOOM_OUT),
        ("does_not_exist", OPENING_EFFECT_NONE),
        (42, OPENING_EFFECT_NONE),
    ],
)
def test_stored_opening_effect_values_normalize_safely(stored, expected):
    assert normalize_opening_effect(stored) == expected


@pytest.mark.parametrize(
    ("intro", "program", "expected"),
    [
        (2.5, 20.0, 2.5),          # the visual intro IS the opening portion
        (0.0, 20.0, OPENING_EFFECT_SECONDS),   # no intro -> the default window
        (0.2, 20.0, OPENING_EFFECT_SECONDS),   # too short to be meaningful
        (20.0, 4.0, 4.0),          # never longer than the whole program
        (0.0, 0.2, 0.0),           # a tiny program gets no effect at all
        (12.0, 0.0, 12.0),         # unknown program length -> uncapped window
        ("bad", 20.0, OPENING_EFFECT_SECONDS),
    ],
)
def test_opening_effect_window_covers_the_opening_portion_only(intro, program, expected):
    assert opening_effect_window(intro, program) == pytest.approx(expected)


@pytest.mark.parametrize("effect", [OPENING_EFFECT_ZOOM_IN, OPENING_EFFECT_ZOOM_OUT])
def test_opening_effect_filter_is_subtle_centred_and_crash_safe(effect):
    chain = opening_effect_filter(effect, 1920, 1080, 2.5)

    # A per-frame scale followed by a fixed centred crop; ``setsar=1`` MUST come
    # after the crop — any filter between the varying scale and the fixed crop
    # reproducibly crashes FFmpeg on a zoom-out ramp.
    assert chain.startswith("scale=w='trunc(iw*")
    assert ":eval=frame:flags=lanczos," in chain
    assert chain.endswith(",crop=w=1920:h=1080:x=(iw-ow)/2:y=(ih-oh)/2,setsar=1")
    assert chain.index("scale=") < chain.index("crop=") < chain.index("setsar=1")
    # NOTHING sits between the per-frame scale and the fixed-size crop.
    assert chain[chain.index("flags=lanczos") + len("flags=lanczos"):chain.index("crop=")] == ","

    # Subtle peak magnification and even output sizes for yuv420p.
    assert f"(1+{_zoom_text()}*" in chain
    assert "/2)*2" in chain

    # The ramp is limited to the window and returns to a neutral 1.00x frame.
    assert "min(t/2.5,1)" in chain
    if effect == OPENING_EFFECT_ZOOM_OUT:
        assert "(1-min(t/2.5,1))" in chain      # peak first, pulls back
    else:
        assert "sin(PI*min(t/2.5,1))" in chain  # eases up and settles back


def _zoom_text() -> str:
    return f"{OPENING_EFFECT_ZOOM:.6f}".rstrip("0").rstrip(".")


@pytest.mark.parametrize(
    ("effect", "width", "height", "window"),
    [
        (OPENING_EFFECT_NONE, 1920, 1080, 2.5),
        ("does_not_exist", 1920, 1080, 2.5),
        (OPENING_EFFECT_ZOOM_IN, 1920, 1080, 0.0),
        (OPENING_EFFECT_ZOOM_IN, 1920, 1080, MIN_OPENING_EFFECT_SECONDS - 0.1),
        (OPENING_EFFECT_ZOOM_IN, 0, 1080, 2.5),
        (OPENING_EFFECT_ZOOM_IN, 1920, -1, 2.5),
    ],
)
def test_opening_effect_is_disabled_without_a_usable_window(effect, width, height, window):
    assert opening_effect_filter(effect, width, height, window) == ""


def test_chunked_rendering_keeps_one_continuous_ramp():
    chain = opening_effect_filter(OPENING_EFFECT_ZOOM_IN, 1920, 1080, 2.5, time_offset=8.0)
    assert "(t+8)" in chain
    assert "min((t+8)/2.5,1)" in chain
    # A segment behind the window evaluates to a neutral, lossless pass-through.
    assert opening_effect_filter(
        OPENING_EFFECT_ZOOM_IN, 1920, 1080, 2.5, time_offset=0.0
    ) != chain


def _stage1_graph(tmp_path, **changes):
    media = [
        fake_media(str(tmp_path / "A.mp4"), duration=4),
        fake_media(str(tmp_path / "B.mp4"), duration=4),
    ]
    values = {
        "resolution": "1920x1080", "workflow_stage": "main", "program_duration": 6,
        "voiceover_path": str(tmp_path / "voice.wav"),
        "original_audio_mode": "mute", "normalize_audio": False,
        "subtitle_enabled": True, "subtitle_ass_path": str(tmp_path / "captions.ass"),
        "visual_intro_seconds": 2.5,
    }
    values.update(changes)
    settings = ExportSettings(**values)
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "out.mp4")
    return built, settings, resolved


def test_the_opening_effect_is_applied_before_the_subtitle_burn_in(tmp_path):
    (tmp_path / "captions.ass").write_text("[Script Info]\n", encoding="utf-8")
    with_effect, _settings, _resolved = _stage1_graph(
        tmp_path, opening_effect=OPENING_EFFECT_ZOOM_IN
    )
    graph = with_effect.filter_graph
    assert "[vprogram]scale=w='trunc(iw*" in graph
    assert "[vopening]" in graph
    # Captions are burned into the already effect-processed timeline, so they are
    # never scaled or reframed themselves.
    assert "[vprogram]scale=w='trunc(iw*" in graph
    assert "[vopening]subtitles=filename=" in graph
    assert graph.index("[vprogram]") < graph.index("[vopening]") < graph.index("subtitles=")

    without_effect, _, _ = _stage1_graph(tmp_path, opening_effect=OPENING_EFFECT_NONE)
    assert "vopening" not in without_effect.filter_graph
    assert "eval=frame" not in without_effect.filter_graph

    # A deprecated/unknown stored value behaves exactly like None.
    unknown, _, _ = _stage1_graph(tmp_path, opening_effect="outline_zoom")
    assert "vopening" not in unknown.filter_graph


def test_the_opening_effect_never_changes_the_render_target(tmp_path):
    (tmp_path / "captions.ass").write_text("[Script Info]\n", encoding="utf-8")
    plain, _, resolved_plain = _stage1_graph(tmp_path, opening_effect=OPENING_EFFECT_NONE)
    zoomed, _, resolved_zoom = _stage1_graph(tmp_path, opening_effect=OPENING_EFFECT_ZOOM_OUT)

    assert resolved_plain.expected_duration == pytest.approx(resolved_zoom.expected_duration)
    assert plain.command[plain.command.index("-t") + 1] == zoomed.command[
        zoomed.command.index("-t") + 1
    ]
    # The effect adds no frame and drops none: the same timeline padding/trim.
    for token in ("tpad=stop_mode=clone", "trim=", "settb=AVTB"):
        assert token in zoomed.filter_graph


def test_the_opening_effect_is_a_main_video_feature_only(tmp_path):
    (tmp_path / "captions.ass").write_text("[Script Info]\n", encoding="utf-8")
    stage2, _, _ = _stage1_graph(
        tmp_path, workflow_stage="outro", opening_effect=OPENING_EFFECT_ZOOM_IN,
        main_video_path=str(tmp_path / "main.mp4"),
    )
    assert "vopening" not in stage2.filter_graph


# --------------------------------------------------------------------------- #
# 3. Random order reserves the Legacy Input Root first
# --------------------------------------------------------------------------- #


def _pool(tmp_path: Path, *, legacy: int = 3, other: int = 5):
    """A deterministic pool: ``legacy`` clips in the root, ``other`` elsewhere."""
    legacy_root = tmp_path / "input"
    extra_root = tmp_path / "extra"
    media = []
    for index in range(legacy):
        media.append(fake_media(str(legacy_root / f"legacy_{index}.mp4"), duration=4.0))
    for index in range(other):
        media.append(fake_media(str(extra_root / f"clip_{index}.mp4"), duration=4.0))
    return media, legacy_root, extra_root


def test_random_order_reserves_three_legacy_input_root_clips_first(tmp_path):
    media, legacy_root, extra_root = _pool(tmp_path)
    ordered = order_media_for_video_order(
        media, "random", seed=20260905, legacy_root=legacy_root
    )

    assert len(ordered) == len(media)
    assert sorted(item.path for item in ordered) == sorted(item.path for item in media)
    prefix = ordered[:LEGACY_PRIORITY_CLIPS]
    # Clips 1-3 all come from the Legacy Input Root, are distinct and the rest of
    # the sequence keeps the unchanged full-pool behavior.
    assert {item.path.parent for item in prefix} == {legacy_root}
    assert len({item.path for item in prefix}) == LEGACY_PRIORITY_CLIPS
    assert all(item.path.parent == extra_root for item in ordered[LEGACY_PRIORITY_CLIPS:])


def test_the_reserved_prefix_is_randomized_among_itself(tmp_path):
    media, legacy_root, _extra = _pool(tmp_path)
    sequences = {
        tuple(item.path.name for item in order_media_for_video_order(
            media, "random", seed=seed, legacy_root=legacy_root
        )[:LEGACY_PRIORITY_CLIPS])
        for seed in range(24)
    }
    # The three reserved clips are shuffled, not simply taken in folder order.
    assert len(sequences) > 1
    assert all(len(set(names)) == LEGACY_PRIORITY_CLIPS for names in sequences)


def test_random_order_is_deterministic_for_a_seed(tmp_path):
    media, legacy_root, _extra = _pool(tmp_path)
    first = order_media_for_video_order(media, "random", seed=7, legacy_root=legacy_root)
    second = order_media_for_video_order(media, "random", seed=7, legacy_root=legacy_root)
    other = order_media_for_video_order(media, "random", seed=8, legacy_root=legacy_root)
    assert [item.path for item in first] == [item.path for item in second]
    assert [item.path for item in first] != [item.path for item in other]
    # An explicitly passed rng is used instead of the seed.
    via_rng = order_media_for_video_order(
        media, "random", rng=random.Random(7), legacy_root=legacy_root
    )
    assert [item.path for item in via_rng] == [item.path for item in first]


def test_an_empty_or_missing_legacy_root_changes_nothing(tmp_path):
    media, _legacy_root, _extra = _pool(tmp_path)
    baseline = [
        item.path for item in order_media_for_video_order(media, "random", seed=11)
    ]
    for legacy_root in ("", None, tmp_path / "does_not_exist", tmp_path / "input" / "empty.txt"):
        ordered = order_media_for_video_order(
            media, "random", seed=11, legacy_root=legacy_root
        )
        # No reservation happens, so the historical shuffle stays bit-identical.
        assert [item.path for item in ordered] == baseline


def test_fewer_than_three_legacy_clips_reserve_all_of_them(tmp_path):
    for legacy_count in (0, 1, 2):
        media, legacy_root, extra_root = _pool(tmp_path, legacy=legacy_count, other=6)
        ordered = order_media_for_video_order(
            media, "random", seed=5, legacy_root=legacy_root
        )
        prefix = [item for item in ordered if item.path.parent == legacy_root]
        if legacy_count == 0:
            assert prefix == []
            assert all(item.path.parent == extra_root for item in ordered)
        else:
            # All available legacy clips come first, then the pool fills the rest.
            assert ordered[:legacy_count] == prefix
            assert len(prefix) == legacy_count


def test_manual_alphabetical_and_natural_order_ignore_the_preference(tmp_path):
    media, legacy_root, _extra = _pool(tmp_path)
    for mode in ("manual", "alphabetical", "natural", "folder_alternating"):
        plain = order_media_for_video_order(media, mode)
        preferred = order_media_for_video_order(media, mode, legacy_root=legacy_root)
        assert [item.path for item in plain] == [item.path for item in preferred]


def test_the_reservation_helper_keeps_the_remaining_pool_intact(tmp_path):
    media, legacy_root, _extra = _pool(tmp_path)
    prefix, rest = reserve_legacy_priority(media, legacy_root, random.Random(3))
    assert len(prefix) == LEGACY_PRIORITY_CLIPS
    assert len(rest) == len(media) - LEGACY_PRIORITY_CLIPS
    assert not ({item.path for item in prefix} & {item.path for item in rest})
    assert prefix + rest and sorted(item.path for item in prefix + rest) == sorted(
        item.path for item in media
    )
    # The log helper reads an already effective sequence: its leading Legacy
    # Input Root clips are exactly the reserved prefix of a Random order.
    ordered = order_media_for_video_order(media, "random", seed=3, legacy_root=legacy_root)
    assert legacy_priority_prefix(ordered, legacy_root) == ordered[:LEGACY_PRIORITY_CLIPS]
    assert {item.path for item in legacy_priority_prefix(ordered, legacy_root)} == {
        item.path for item in ordered[:LEGACY_PRIORITY_CLIPS]
    }
    assert legacy_priority_prefix(ordered, "") == []


def test_the_legacy_preference_is_logged_once_and_only_when_it_applies(tmp_path):
    media, legacy_root, _extra = _pool(tmp_path)
    logs: list[str] = []
    _log_legacy_priority(
        media,
        ExportSettings(video_order_mode="random", legacy_input_root=str(legacy_root)),
        logs.append,
    )
    assert len(logs) == 1
    assert logs[0].startswith("Legacy Input Root priority (Random): clips 1-3 = ")
    assert "remaining randomized pool starts at clip 4" in logs[0]
    assert all(f"legacy_{index}.mp4" in logs[0] for index in range(3))

    # Manual/Alphabetical/Natural projects and an empty root stay silent.
    for settings in (
        ExportSettings(video_order_mode="natural", legacy_input_root=str(legacy_root)),
        ExportSettings(video_order_mode="random", legacy_input_root=""),
    ):
        logs.clear()
        _log_legacy_priority(media, settings, logs.append)
        assert logs == []


# --------------------------------------------------------------------------- #
# 4. Cache identity of every new render-affecting setting
# --------------------------------------------------------------------------- #


def _stage1_identity(tmp_path: Path, *, subtitle_requested: bool = False, **changes):
    media = [fake_media(str(tmp_path / f"clip_{index}.mp4"), duration=4.0) for index in range(3)]
    values = {
        "resolution": "1920x1080", "workflow_stage": "main", "program_duration": 8,
        "voiceover_path": str(tmp_path / "voice.wav"),
    }
    values.update(changes)
    settings = ExportSettings(**values)
    resolved = resolve_export(media, settings)
    digest = stage1_fingerprint(
        media, settings, resolved, subtitle_requested=subtitle_requested
    )[0]
    return digest, media, settings, resolved


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("long_form_intro_seconds", 0.0),
        ("long_form_outro_seconds", 0.0),
        ("short_intro_seconds", 0.0),
        ("short_outro_seconds", 0.0),
        ("visual_intro_seconds", 2.5),
        ("final_pause", 4.0),
        ("opening_effect", OPENING_EFFECT_ZOOM_IN),
    ],
)
def test_every_new_timeline_setting_changes_the_stage1_fingerprint(tmp_path, field, value):
    baseline, media, settings, resolved = _stage1_identity(tmp_path)
    changed = replace(settings, **{field: value})
    assert stage1_fingerprint(media, changed, resolved)[0] != baseline, field
    assert FINGERPRINT_SCHEMA == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subtitle_animation", "type_reveal"),
        ("subtitle_style", "long_4"),
        ("subtitle_position", "Top"),
    ],
)
def test_subtitle_settings_change_the_fingerprint_of_a_subtitle_render(tmp_path, field, value):
    """Subtitle identity fields count whenever subtitles are requested."""
    baseline, media, settings, resolved = _stage1_identity(tmp_path, subtitle_requested=True)
    changed = replace(settings, **{field: value})
    assert stage1_fingerprint(
        media, changed, resolved, subtitle_requested=True
    )[0] != baseline, field
    # Without a subtitle render those fields stay out of the identity, so an
    # unrelated cache entry is not invalidated by a caption preference.
    plain, _media, plain_settings, plain_resolved = _stage1_identity(tmp_path)
    assert stage1_fingerprint(
        media, replace(plain_settings, **{field: value}), plain_resolved
    )[0] == plain


def test_a_short_job_identity_follows_the_short_subtitle_profile(tmp_path):
    """``short_subtitle_animation`` reaches the cache through the Short job."""
    media = [fake_media(str(tmp_path / "clip_0.mp4"), duration=4.0)]
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS, voiceover_paths=[str(tmp_path / "voice.wav")],
        short_subtitle_animation="phrase_focus",
    )
    job = build_short_jobs(settings)[0]
    baseline_settings = short_settings(settings, job)
    resolved = resolve_export(media, baseline_settings)
    baseline = stage1_fingerprint(
        media, baseline_settings, resolved, subtitle_requested=True
    )[0]

    changed = short_settings(replace(settings, short_subtitle_animation="type_reveal"), job)
    assert changed.subtitle_animation == "type_reveal"
    assert stage1_fingerprint(
        media, changed, resolved, subtitle_requested=True
    )[0] != baseline


def test_the_effective_media_order_stays_part_of_the_stage1_identity(tmp_path):
    baseline, media, settings, _resolved = _stage1_identity(tmp_path)
    reordered = list(reversed(media))
    assert stage1_fingerprint(
        reordered, settings, resolve_export(reordered, settings)
    )[0] != baseline


def test_the_legacy_preference_changes_the_identity_through_the_effective_order(tmp_path):
    """The root matters because it changes the effective media order."""
    media, legacy_root, _extra = _pool(tmp_path)
    settings = ExportSettings(
        resolution="1920x1080", workflow_stage="main", program_duration=8,
        video_order_mode="random",
    )
    plain_order = order_media_for_video_order(media, "random", seed=7)
    preferred_order = order_media_for_video_order(media, "random", seed=7, legacy_root=legacy_root)
    assert [item.path for item in plain_order] != [item.path for item in preferred_order]
    plain = stage1_fingerprint(
        plain_order, settings, resolve_export(plain_order, settings)
    )[0]
    preferred = stage1_fingerprint(
        preferred_order, settings, resolve_export(preferred_order, settings)
    )[0]
    assert plain != preferred


def test_stage2_identity_keeps_its_own_schema(tmp_path):
    main = tmp_path / "main.mp4"
    media = [fake_media(str(main), duration=4.0)]
    settings = ExportSettings(
        workflow_stage="outro", main_video_path=str(main), resolution="1920x1080",
    )
    resolved = resolve_export(media, settings)
    payload = build_stage2_payload(media, settings, resolved)
    assert payload["schema"] == STAGE2_FINGERPRINT_SCHEMA == 2
    assert stage2_fingerprint(media, settings, resolved)[0]
    # A Stage-1-only setting (the opening effect) is not a Stage-2 identity field.
    assert "opening_effect" not in payload["settings"]
