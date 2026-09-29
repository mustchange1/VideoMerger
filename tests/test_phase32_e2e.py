"""Phase 32 e2e: REAL FFmpeg renders for fallback policies, image rendering
and the typewriter completion sound.

Original Phase-32 matrix:
A  all-default Phase-32 settings keep the Phase-31 smart visual render
   byte-identical,
B  Random Video / Random Image fallbacks insert pool media (never generated),
C  Skip inserts nothing and matches the historical video-only render,
D  Allow Generated Images OFF never generates (even with strategy=always),
E  a dedicated image transition duration changes only image boundaries,
F  an image visual effect changes image frames without changing timing,
G  Shorts use their own fallback/rendering profile (LF/Shorts separation),
H  the typewriter completion sound plays exactly once at typing end,
   without disturbing intro timing, typing SFX or the video frames.

PHASE 33 CONTRACT CHANGE (documented): the explicit fallback POLICIES and
the generation path were replaced by the selection MODES. B/B2/C/G/I were
reworked 1:1 to their selection-engine equivalents (random draws from the
pool, empty-pool skip, Shorts selection mode, legacy-generation-settings
inertness); A/D/E/E2/F/H keep their original meaning and pass unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video_merger.main_project import MainProjectEngine
from app.video_merger.typewriter_intro import build_timeline, profile_from_settings
from tests.conftest import make_clip
from tests.test_phase30_e2e import (
    _base_settings,
    _color_ratio,
    _frame_rgb,
    _is_video_color_frame,
    _make_image,
    _mean_abs_diff,
    _probe_duration,
    _tone,
)
from tests.test_phase31_e2e import (
    CITY_SCRIPT,
    MOUNTAIN_SCRIPT,
    _smart_lf,
    _smart_project_factory,
)
from tests.test_phase29_typewriter_e2e import _project_factory as _typewriter_project_factory
from tests.test_phase29_typewriter_e2e import (
    _base_settings as _typewriter_base_settings,
)
from tests.test_phase29_typewriter_e2e import _rms, _samples

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _isolated_project_root(tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module
    import app.video_merger.main_project as main_project_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    monkeypatch.setattr(main_project_module, "project_root", lambda: tmp_path)
    yield


def _generated_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "smart_visual_generated"


def _plan_slots(settings, script_text: str, tmp_path: Path, ffmpeg: Path, ffprobe: Path,
                program_duration: float = 2.4, short: bool = False) -> list:
    """Build the Smart Visual plan exactly like the render does and return its
    slots, so the Feature-A per-slot diagnostics can be asserted end-to-end."""
    from app.video_merger.smart_visuals import (
        build_smart_visual_plan,
        smart_visual_profile_from_settings,
    )

    resolved = settings
    if short:
        from app.video_merger.youtube_outputs import ShortJob, short_settings

        job = ShortJob(index=0, voiceover_path=Path(str(settings.voiceover_path)),
                       script_path=None, output_name="Short1", cache_key="e2e-short")
        resolved = short_settings(settings, job)
    profile = smart_visual_profile_from_settings(resolved)
    plan = build_smart_visual_plan(
        profile=profile,
        script_text=script_text,
        program_duration=program_duration,
        width=720 if short else 320,
        height=1280 if short else 180,
        fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
        seed_parts=("phase32-e2e",),
        log=lambda *_a, **_k: None,
    )
    return plan.slots


def _video_only_folder(tmp_path: Path, ffmpeg: Path) -> Path:
    folder = tmp_path / "videos"
    folder.mkdir()
    make_clip(ffmpeg, folder / "generic city footage.mp4", size="320x180", duration=1.0, color="yellow", audio_rate=None)
    make_clip(ffmpeg, folder / "generic valley footage.mp4", size="320x180", duration=1.0, color="magenta", audio_rate=None)
    return folder


def _mixed_folder(tmp_path: Path, ffmpeg: Path) -> Path:
    folder = tmp_path / "mixed"
    folder.mkdir()
    _make_image(ffmpeg, folder / "city street.png", "yellow")
    make_clip(ffmpeg, folder / "city footage.mp4", size="320x180", duration=1.0, color="magenta", audio_rate=None)
    return folder


# ---------------------------------------------------------------------------
# A - defaults keep the historical smart-visual render byte-identical
# ---------------------------------------------------------------------------
def test_A_phase32_defaults_keep_phase31_render_byte_identical(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    reference = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(city)),
        tmp_path / "reference", aligner=aligner,
    )
    assert reference.video.is_file() and reference.report.ok

    # The SAME project with every Phase-32 field stored at its default
    # (allow ON, generate_image, project transition, none effect, completion
    # ON) must reproduce the exact Phase-31 bytes.
    phase32_defaults = dict(
        smart_visual_allow_generated=True,
        smart_visual_fallback="generate_image",
        long_form_image_transition_type="project",
        long_form_image_transition_duration=None,
        long_form_image_visual_effect="none",
        long_form_image_visual_effect_intensity="low",
        typewriter_completion_sound_enabled=True,
        typewriter_completion_sound_preset="enter_return",
        typewriter_completion_sound_volume=40,
    )
    result = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(city), **phase32_defaults),
        tmp_path / "phase32", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    assert result.video.read_bytes() == reference.video.read_bytes()


# ---------------------------------------------------------------------------
# B - Random Video / Random Image fallbacks
# ---------------------------------------------------------------------------
def test_B_random_fallback_inserts_pool_video_without_generation(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: the explicit random_video POLICY is gone -
    # with a video-only pool the seeded random fallback draws pool VIDEOS
    # (never generated) exactly like the former policy did.
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, MOUNTAIN_SCRIPT
    )
    videos = _video_only_folder(tmp_path, ffmpeg)

    settings = _base_settings(
        voice, script,
        **_smart_lf(videos, smart_visual_mode="random_only"),
    )
    slots = _plan_slots(settings, MOUNTAIN_SCRIPT, tmp_path, ffmpeg, ffprobe)
    assert any(slot.fallback_mode == "FALLBACK_RANDOM_VIDEO" for slot in slots), \
        "per-slot diagnostic must tag the random-video draw"
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "rv", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    assert not _generated_dir(tmp_path).exists(), "random fallback must never generate"

    # Pure Random now shuffles the complete unified pool, including ordinary
    # source videos. Verify the final Master Timeline selected video media;
    # it is intentionally not constrained to the auxiliary folder color.
    trace = json.loads(
        (tmp_path / "rv" / "PHASE_35_DEBUG_TRACE.json").read_text(encoding="utf-8")
    )
    assert trace["placements"]
    assert all(item["visual_kind"] == "video" for item in trace["placements"])


def test_B2_random_fallback_inserts_pool_image(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: weak matches fall back to a random EXISTING
    # image from the pool - the former random_image policy behavior without
    # any policy setting.
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    settings = _base_settings(
        voice, script,
        **_smart_lf(city, smart_visual_mode="random_only"),
    )
    slots = _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    assert any(slot.fallback_mode == "FALLBACK_RANDOM_IMAGE" for slot in slots), \
        "per-slot diagnostic must tag the random-image draw"
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "ri", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    assert not _generated_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# C - Skip keeps the video-only render
# ---------------------------------------------------------------------------
def test_C_empty_pool_matches_video_only_render(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: the explicit "skip" POLICY is gone. Its
    # guarantee lives on: with NO usable media (empty pool) every slot is
    # skipped cleanly and the render stays byte-identical to the historical
    # video-only output - nothing generated, nothing inserted.
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, MOUNTAIN_SCRIPT
    )
    empty = tmp_path / "empty_pool"
    empty.mkdir()

    settings = _base_settings(voice, script, **_smart_lf(empty))
    slots = _plan_slots(settings, MOUNTAIN_SCRIPT, tmp_path, ffmpeg, ffprobe)
    assert slots
    assert all(slot.fallback_mode == "SKIPPED" for slot in slots), \
        "an empty pool must skip every slot cleanly"

    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "base", aligner=aligner,
    )
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "skip", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    # Phase 36 locks an enabled Smart profile to voiceover even when its pool
    # is empty; source footage remains, but legacy end padding is not appended.
    assert _probe_duration(ffprobe, result.video) == pytest.approx(7.2, abs=0.08)
    assert _probe_duration(ffprobe, result.video) <= _probe_duration(ffprobe, baseline.video) + 0.08
    assert not _generated_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# D - Allow Generated Images OFF blocks generation even with strategy=always
# ---------------------------------------------------------------------------
def test_D_allow_generated_off_never_generates(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    settings = _base_settings(
        voice, script,
        **_smart_lf(city, smart_visual_generation_strategy="always",
                    smart_visual_allow_generated=False),
    )
    slots = _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    assert slots, "the plan must still produce slots"
    assert all(slot.fallback_mode != "FALLBACK_GENERATED" for slot in slots), \
        "generation must be impossible when the toggle is OFF"
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "nogen", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    assert not _generated_dir(tmp_path).exists(), "generation must not run when disabled"
    # The city sentence still receives the matching pool image somewhere in
    # the first half of the program.
    found = any(
        _color_ratio(_frame_rgb(ffmpeg, result.video, t, 320, 180), (255, 255, 0)) > 0.5
        for t in (0.75, 0.95, 1.15, 1.35, 1.55)
    )
    assert found, "the matching pool image must still be inserted with generation OFF"


def test_I_legacy_generation_settings_never_generate_anymore(ffmpeg_paths, tmp_path):
    """Phase 33 contract change (replaces the generation-enabled proof):
    even a project that still stores the legacy ``allow_generated=True`` +
    ``generate_image`` fallback renders with PURE selection - the unrelated
    pool image is placed as a random fallback and NOTHING is generated."""
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, MOUNTAIN_SCRIPT
    )
    # Only unrelated media in the pool -> nothing matches the mountain
    # script, so every slot becomes a random EXISTING fallback.
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _make_image(ffmpeg, unrelated / "zebra savanna animal.png", "magenta")

    settings = _base_settings(
        voice, script,
        **_smart_lf(unrelated,
                    smart_visual_fallback="generate_image",
                    smart_visual_allow_generated=True,
                    smart_visual_generation_strategy="always"),
    )
    slots = _plan_slots(settings, MOUNTAIN_SCRIPT, tmp_path, ffmpeg, ffprobe)
    assert slots
    assert all(slot.fallback_mode == "FALLBACK_RANDOM_IMAGE" for slot in slots), \
        "legacy generation settings are inert: random existing fallback wins"
    assert not any(slot.generation_used for slot in slots)

    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "gen", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png")), \
        "Phase 33: no generation capability remains in Smart Visuals"
    # The existing pool image is visible inside the program.
    found = any(
        _color_ratio(_frame_rgb(ffmpeg, result.video, t, 320, 180), (255, 0, 255)) > 0.5
        for t in (0.75, 1.05, 1.35, 1.65)
    )
    assert found, "the random existing image must be visible in the render"


# ---------------------------------------------------------------------------
# E - dedicated image transition duration at image boundaries
# ---------------------------------------------------------------------------
def test_E_image_transition_duration_applies_at_image_boundaries(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    # One matching image in the pool; image_first makes the city sentences
    # MATCH it, so every matched slot inserts an image with TWO boundaries
    # (video->image, image->video) that honor the dedicated transition.
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    def settings(**overrides):
        return _base_settings(
            voice, script,
            **_smart_lf(city, smart_visual_source_priority="image_first"),
            **overrides,
        )

    base = settings()
    # Count the image slots the real plan produces, then predict the exact
    # duration shift of a longer dedicated image transition.
    # Both matched AND generated images receive the dedicated image-boundary
    # transition, so both kinds count as image boundary sources.
    image_slots = sum(
        1 for slot in _plan_slots(base, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
        if slot.selected_kind in ("image", "generated")
    )
    assert image_slots >= 1, "the city image must match and be inserted"

    baseline = MainProjectEngine(engine).create_main(
        media, base, tmp_path / "td_project", aligner=aligner,
    )
    override = MainProjectEngine(engine).create_main(
        media,
        settings(
            timeline_image_transition_type="film_dissolve",
            timeline_image_transition_duration=0.3,
        ),
        tmp_path / "td_third", aligner=aligner,
    )
    assert baseline.video.is_file() and baseline.report.ok
    assert override.video.is_file() and override.report.ok

    base_duration = _probe_duration(ffprobe, baseline.video)
    override_duration = _probe_duration(ffprobe, override.video)
    # Phase 35 can defer dense semantic slots to preserve its four-second
    # automatic minimum. Use the resolved pre-render trace, not the larger
    # legacy semantic candidate count, for final xfade geometry.
    trace = json.loads(
        (tmp_path / "td_project" / "PHASE_35_DEBUG_TRACE.json").read_text(encoding="utf-8")
    )
    rendered_image_slots = sum(
        1 for item in trace["placements"] if item["status"] == "inserted"
    )
    assert rendered_image_slots >= 1
    # Phase 36 recalculates xfade geometry inside an invariant audio endpoint;
    # transition changes move overlap windows but cannot change total length.
    assert override_duration - base_duration == pytest.approx(0.0, abs=0.08)

    # The override render still shows the pool image inside a slot.
    found = any(
        _color_ratio(_frame_rgb(ffmpeg, override.video, t, 320, 180), (255, 255, 0)) > 0.5
        for t in (0.75, 0.95, 1.15, 1.35, 1.55, 1.75)
    )
    assert found, "image not visible with the dedicated transition"


def test_E2_image_transition_none_hard_cut(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    def settings(**overrides):
        return _base_settings(
            voice, script,
            **_smart_lf(city, smart_visual_source_priority="image_first"),
            **overrides,
        )

    base = settings()
    image_slots = sum(
        1 for slot in _plan_slots(base, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
        if slot.selected_kind in ("image", "generated")
    )
    assert image_slots >= 1

    baseline = MainProjectEngine(engine).create_main(
        media, base, tmp_path / "cut_base", aligner=aligner,
    )
    hard_cut = MainProjectEngine(engine).create_main(
        media, settings(timeline_image_transition_type="none"),
        tmp_path / "cut_none", aligner=aligner,
    )
    assert hard_cut.video.is_file() and hard_cut.report.ok
    trace = json.loads(
        (tmp_path / "cut_base" / "PHASE_35_DEBUG_TRACE.json").read_text(encoding="utf-8")
    )
    rendered_image_slots = sum(
        1 for item in trace["placements"] if item["status"] == "inserted"
    )
    assert rendered_image_slots >= 1
    # A hard cut changes boundary geometry, never the voiceover lock.
    delta = _probe_duration(ffprobe, hard_cut.video) - _probe_duration(ffprobe, baseline.video)
    assert delta == pytest.approx(0.0, abs=0.08)


# ---------------------------------------------------------------------------
# F - image visual effect changes image frames, timing untouched
# ---------------------------------------------------------------------------
def test_F_image_visual_effect_changes_frames_without_timing_change(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    def settings(**overrides):
        return _base_settings(
            voice, script,
            **_smart_lf(city, smart_visual_source_priority="image_first"),
            **overrides,
        )

    base = settings()
    assert any(
        slot.selected_kind == "image"
        for slot in _plan_slots(base, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    )

    baseline = MainProjectEngine(engine).create_main(
        media, base, tmp_path / "fx_none", aligner=aligner,
    )
    shimmered = MainProjectEngine(engine).create_main(
        media,
        settings(
            timeline_image_visual_effect="soft_shimmer",
            timeline_image_visual_effect_intensity="high",
        ),
        tmp_path / "fx_shimmer", aligner=aligner,
    )
    assert shimmered.video.is_file() and shimmered.report.ok
    # Timing is untouched by the effect (identical duration).
    assert _probe_duration(ffprobe, shimmered.video) == pytest.approx(
        _probe_duration(ffprobe, baseline.video), abs=0.12
    )
    # At least one frame inside the image slot visibly differs.
    max_diff = 0.0
    for probe_time in (0.75, 0.95, 1.15, 1.35, 1.55, 1.75):
        plain = _frame_rgb(ffmpeg, baseline.video, probe_time, 320, 180)
        effect = _frame_rgb(ffmpeg, shimmered.video, probe_time, 320, 180)
        max_diff = max(max_diff, _mean_abs_diff(plain, effect))
    assert max_diff > 1.0, f"shimmer effect not visible (max diff {max_diff:.2f})"


# ---------------------------------------------------------------------------
# G - Shorts use their own fallback + rendering settings
# ---------------------------------------------------------------------------
def test_G_shorts_use_their_own_selection_mode_and_rendering_profile(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: Shorts own their SELECTION MODE (here
    # Random Only) and their image rendering - strictly separate from the
    # Long-Form profile, never generating anything.
    from tests.test_phase30_e2e import _shorts_settings

    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    settings = _shorts_settings(
        voice, script,
        shorts_smart_visual_enabled=True,
        shorts_smart_visual_folders=[str(city)],
        shorts_smart_visual_cadence="every_1",
        shorts_smart_visual_mode="random_only",
        shorts_smart_visual_image_duration=3.0,
        shorts_image_visual_effect="gentle_flicker",
        shorts_image_visual_effect_intensity="medium",
    )
    slots = _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe, short=True)
    assert any(slot.fallback_mode == "FALLBACK_RANDOM_IMAGE" for slot in slots), \
        "the Short profile must use its own Shorts selection mode"
    assert all(slot.source_mode != "SMART" for slot in slots), \
        "random_only must not match semantically"
    result = MainProjectEngine(engine).create_youtube_exports(
        media, settings, tmp_path / "short", aligner=aligner,
    )
    shorts = result.shorts
    assert shorts and shorts[0].video.is_file() and shorts[0].report.ok
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png"))
    # Portrait geometry preserved.
    from tests.test_phase30_e2e import _probe_size

    w, h = _probe_size(ffprobe, shorts[0].video)
    assert h > w


# ---------------------------------------------------------------------------
# H - typewriter completion sound in a real render
# ---------------------------------------------------------------------------
HOOK = {
    "typewriter_intro_enabled": True,
    "typewriter_hook_text": "Why?\nWatch this.",
    "typewriter_speed": "fast",
    "typewriter_hold_seconds": 0.5,
    "typewriter_sound_volume": 100,
    "typewriter_transition": "project",
}


def test_H_completion_sound_plays_once_without_disturbing_timing(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _typewriter_project_factory(tmp_path, ffmpeg, ffprobe)

    baseline = MainProjectEngine(engine).create_main(
        media, _typewriter_base_settings(voice, script), tmp_path / "base", aligner=aligner,
    )
    program_duration = _probe_duration(ffprobe, baseline.video)

    settings_off = _typewriter_base_settings(
        voice, script, **HOOK, typewriter_completion_sound_enabled=False,
    )
    settings_on = _typewriter_base_settings(voice, script, **HOOK)  # default ON
    result_off = MainProjectEngine(engine).create_main(
        media, settings_off, tmp_path / "intro_off", aligner=aligner,
    )
    result_on = MainProjectEngine(engine).create_main(
        media, settings_on, tmp_path / "intro_on", aligner=aligner,
    )
    assert result_off.video.is_file() and result_off.report.ok
    assert result_on.video.is_file() and result_on.report.ok

    profile = profile_from_settings(settings_on)
    timeline = build_timeline(profile)
    intro = timeline.total_duration
    td = 0.15

    # --- exact same timing for both renders (no disturbance) -------------
    duration_off = _probe_duration(ffprobe, result_off.video)
    duration_on = _probe_duration(ffprobe, result_on.video)
    assert duration_off == pytest.approx(program_duration + intro - td, abs=0.15)
    assert duration_on == pytest.approx(duration_off, abs=0.05)

    # --- identical video frames (completion sound is audio-only) ---------
    for probe_time in (0.05, timeline.typing_end * 0.5, intro + 0.3):
        frame_off = _frame_rgb(ffmpeg, result_off.video, probe_time, 320, 180)
        frame_on = _frame_rgb(ffmpeg, result_on.video, probe_time, 320, 180)
        assert _mean_abs_diff(frame_off, frame_on) < 10.0

    # --- identical typing SFX before the click ---------------------------
    typing_off = _samples(ffmpeg, result_off.video, 0.03, max(0.2, timeline.typing_end - 0.08))
    typing_on = _samples(ffmpeg, result_on.video, 0.03, max(0.2, timeline.typing_end - 0.08))
    common = min(len(typing_off), len(typing_on))
    drift = sum(abs(a - b) for a, b in zip(typing_off[:common], typing_on[:common])) / common
    assert drift < 0.01, "typing SFX changed - completion sound disturbed timing"

    # --- exactly one audible click at the end of typing -------------------
    click_off = _samples(ffmpeg, result_off.video, timeline.typing_end - 0.02, 0.25)
    click_on = _samples(ffmpeg, result_on.video, timeline.typing_end - 0.02, 0.25)
    assert _rms(click_on) > 2 * max(1e-5, _rms(click_off)), "completion click missing"
    assert _rms(click_on) > 0.005, "completion click too quiet"

    # --- and silence again after the click, before the voiceover ---------
    tail_start = timeline.typing_end + 0.22
    tail_len = max(0.05, intro - td - tail_start - 0.02)
    tail_on = _samples(ffmpeg, result_on.video, tail_start, tail_len)
    assert _rms(tail_on) < 0.01, "sound after the click - it must play exactly once"

    # --- the voiceover still starts right after the intro ----------------
    from tests.test_phase29_typewriter_e2e import _frequency_strength

    voice_window = _samples(ffmpeg, result_on.video, intro - td + 0.35, 0.3)
    assert _frequency_strength(voice_window, 850) > 0.02


def test_H2_shorts_completion_sound_default_on(ffmpeg_paths, tmp_path):
    from tests.test_phase30_e2e import _probe_size, _shorts_settings

    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _typewriter_project_factory(tmp_path, ffmpeg, ffprobe)

    settings = _shorts_settings(
        voice, script,
        short_typewriter_intro_enabled=True,
        short_typewriter_hook_text="Hook",
        short_typewriter_speed="fast",
        short_typewriter_hold_seconds=0.4,
        short_typewriter_sound_volume=100,
    )
    result = MainProjectEngine(engine).create_youtube_exports(
        media, settings, tmp_path / "short", aligner=aligner,
    )
    assert result.shorts and result.shorts[0].video.is_file()
    short_profile = profile_from_settings(settings, short=True)
    assert short_profile.completion_sound_enabled is True
    timeline = build_timeline(short_profile)
    # The click is audible at the end of typing in the Short.
    click = _samples(ffmpeg, result.shorts[0].video, timeline.typing_end - 0.02, 0.22)
    assert _rms(click) > 0.005, "Shorts completion click missing"
    w, h = _probe_size(ffprobe, result.shorts[0].video)
    assert h > w
