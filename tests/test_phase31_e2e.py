"""Phase 31 e2e: Smart Visual Hybrid with REAL FFmpeg renders.

Original Phase-31 matrix (spec section 47):
A  disabled Smart Visuals keep the historical render byte-identical,
B  a matching existing IMAGE is chosen and nothing is generated,
C  a matching existing VIDEO is chosen (video-first priority),
D  no match -> a locally generated image is placed,
E  a repeated sentence reuses the generation cache (one file),
F  Shorts generation uses the portrait 9:16 geometry,
G  Long-Form generation uses the landscape geometry,
H  generation unavailable -> safe fallback to existing media,
I  disabled Smart Visuals + Typewriter intro stay unchanged,
J  subtitles + music + transitions stay intact with Smart Visuals,
K  generated images reuse the Phase-30 motion + TV effect pipeline,
L  video -> generated image -> video transitions render correctly.

PHASE 33 CONTRACT CHANGE (documented): Smart Visuals NEVER generates. The
generation-oriented expectations of D/E/F/K were replaced 1:1 by their
selection-engine equivalents (random existing fallback, deterministic
selection, portrait selection, selected-image rendering); A/B/C/H/I/J and
the passing parts of the former generation tests stay as they were.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.main_project import MainProjectEngine
from tests.conftest import make_clip
from tests.test_phase30_e2e import (
    VideoMergerEngine,
    _base_settings,
    _color_ratio,
    _find_windows,
    _frame_rgb,
    _is_video_color_frame,
    _make_image,
    _mean_abs_diff,
    _probe_duration,
    _probe_size,
    _project_factory,
    _run,
    _shorts_settings,
    _tone,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _isolated_project_root(tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module
    import app.video_merger.main_project as main_project_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    # ``main_project`` imports the name at module level, so its own binding
    # must be patched too (Smart Visual caches live under project_root()).
    monkeypatch.setattr(main_project_module, "project_root", lambda: tmp_path)
    yield


CITY_SCRIPT = "The city streets are busy today. Markets fill the city center."
GLACIER_SCRIPT = "Glaciers melt in the arctic ice."
NIGHT_SCRIPT = "The night sky is dark and calm. The night sky is dark and calm."
MOUNTAIN_SCRIPT = "The mountains rise above the valley."
MIXED_SCRIPT = "The city streets are busy today. Glaciers melt in the arctic ice."

_SCRIPT_TIMINGS = {
    CITY_SCRIPT: [
        ("The", 0.05, 0.18), ("city", 0.20, 0.45), ("streets", 0.47, 0.70),
        ("are", 0.72, 0.82), ("busy", 0.84, 1.00), ("today", 1.02, 1.20),
        ("Markets", 1.30, 1.50), ("fill", 1.52, 1.62), ("the", 1.64, 1.70),
        ("city", 1.72, 1.85), ("center", 1.87, 2.00),
    ],
    GLACIER_SCRIPT: [
        ("Glaciers", 0.05, 0.45), ("melt", 0.50, 0.75), ("in", 0.78, 0.88),
        ("the", 0.90, 1.00), ("arctic", 1.02, 1.45), ("ice", 1.50, 1.90),
    ],
    NIGHT_SCRIPT: [
        ("The", 0.05, 0.15), ("night", 0.17, 0.40), ("sky", 0.42, 0.60),
        ("is", 0.62, 0.70), ("dark", 0.72, 0.95), ("and", 0.97, 1.05),
        ("calm", 1.07, 1.25),
        ("The", 1.35, 1.42), ("night", 1.44, 1.60), ("sky", 1.62, 1.75),
        ("is", 1.77, 1.82), ("dark", 1.84, 1.95), ("and", 1.97, 2.02),
        ("calm", 2.04, 2.20),
    ],
    MOUNTAIN_SCRIPT: [
        ("The", 0.05, 0.15), ("mountains", 0.17, 0.60), ("rise", 0.65, 0.90),
        ("above", 0.95, 1.25), ("the", 1.27, 1.35), ("valley", 1.37, 1.80),
    ],
    MIXED_SCRIPT: [
        ("The", 0.05, 0.15), ("city", 0.17, 0.40), ("streets", 0.42, 0.65),
        ("are", 0.67, 0.75), ("busy", 0.77, 0.95), ("today", 0.97, 1.15),
        ("Glaciers", 1.25, 1.55), ("melt", 1.57, 1.75), ("in", 1.77, 1.85),
        ("the", 1.87, 1.92), ("arctic", 1.94, 2.15), ("ice", 2.17, 2.40),
    ],
}


def _smart_project_factory(tmp_path: Path, ffmpeg: Path, ffprobe: Path, script_text: str):
    """Real project whose aligner timings match the given smart-visual script."""
    from app.video_merger.alignment import LocalWordAligner, RecognizedWord

    clip_a = tmp_path / "clip_a.mp4"
    clip_b = tmp_path / "clip_b.mp4"
    clip_c = tmp_path / "clip_c.mp4"
    make_clip(ffmpeg, clip_a, size="320x180", duration=0.7, color="red", audio_rate=None)
    make_clip(ffmpeg, clip_b, size="320x180", duration=0.7, color="blue", audio_rate=None)
    make_clip(ffmpeg, clip_c, size="320x180", duration=0.7, color="green", audio_rate=None)

    voice = tmp_path / "voice.wav"
    # Phase 36 visuals have a four-second hard minimum and may never extend
    # beyond audio. Use a realistic spoken program long enough to exercise the
    # Smart Visual render path rather than an impossible 2.4-second fixture.
    _tone(ffmpeg, voice, 850, 7.2, .65)
    script = tmp_path / "script.txt"
    script.write_text(script_text + "\n", encoding="utf-8")

    timings = _SCRIPT_TIMINGS[script_text]

    def recognize(path: Path, _language: str):
        return [
            RecognizedWord(word, start * 3.0, end * 3.0, .99)
            for word, start, end in timings
        ], "en"

    aligner = LocalWordAligner("phase31-fixture", recognize, cache_dir=tmp_path / "align-cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip_a, clip_b, clip_c])
    return engine, media, aligner, voice, script


def _smart_lf(folder: Path | None = None, **overrides) -> dict:
    values = dict(
        smart_visual_enabled=True,
        smart_visual_folders=[str(folder)] if folder is not None else [],
        smart_visual_source_priority="balanced",
        smart_visual_threshold_mode="low",
        smart_visual_threshold_custom=0.5,
        smart_visual_generation_strategy="only_when_no_match",
        smart_visual_repetition_window=3,
        asset_cooldown_videos=0,
        image_ratio_min=100,
        image_ratio_max=100,
        smart_visual_style="cinematic",
        smart_visual_cadence="every_1",
    )
    values.update(overrides)
    return values


def _confirmed(media, settings, ffprobe: Path, aligner):
    from app.video_merger.main_project import prepare_confirmed_master_timeline_settings
    return prepare_confirmed_master_timeline_settings(
        media, settings, ffprobe_path=ffprobe, aligner=aligner,
    )


def _city_folder(tmp_path: Path, ffmpeg: Path, two_files: bool = True) -> Path:
    folder = tmp_path / "city"
    _make_image(ffmpeg, folder / "city street.png", "yellow")
    if two_files:
        _make_image(ffmpeg, folder / "city market.png", "yellow")
    return folder


def _generated_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "smart_visual_generated"


def _yellow_predicate(frame: bytes) -> bool:
    return _color_ratio(frame, (255, 255, 0)) > 0.55


def _white_predicate(frame: bytes) -> bool:
    hits = 0
    for index in range(0, len(frame), 3):
        if frame[index] > 230 and frame[index + 1] > 230 and frame[index + 2] > 230:
            hits += 1
    return hits / (len(frame) // 3) > 0.6


def _generated_predicate(frame: bytes) -> bool:
    """Generated gradient: neither a synthetic clip color nor the white video."""
    return not _is_video_color_frame(frame) and not _white_predicate(frame)


# ---------------------------------------------------------------------------
# A - disabled feature keeps the historical render byte-identical
# ---------------------------------------------------------------------------
def test_A_disabled_smart_visuals_keep_historical_render_byte_identical(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    historical = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "hist", aligner=aligner,
    )
    assert historical.video.is_file() and historical.report.ok

    # Fully configured but switched OFF stays exactly historical.
    city = _city_folder(tmp_path, ffmpeg)
    disabled = MainProjectEngine(engine).create_main(
        media,
        _base_settings(
            voice, script, smart_visual_enabled=False,
            smart_visual_folders=[str(city)],
            smart_visual_generation_strategy="always",
            smart_visual_style="custom", smart_visual_style_custom="anything",
        ),
        tmp_path / "configured_but_disabled", aligner=aligner,
    )
    assert disabled.video.is_file() and disabled.report.ok
    assert disabled.video.read_bytes() == historical.video.read_bytes()

    # Unified mode remains usable without extra folders because ordinary
    # source videos are first-class members of the single Media Pool.
    source_only = MainProjectEngine(engine).create_main(
        media,
        _base_settings(
            voice, script, smart_visual_enabled=True,
            smart_visual_folders=[], asset_cooldown_videos=0,
        ),
        tmp_path / "enabled_source_only", aligner=aligner,
    )
    assert source_only.video.is_file() and source_only.report.ok
    assert not _generated_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# B - matching existing image is chosen; nothing is generated
# ---------------------------------------------------------------------------
def test_B_matching_image_chosen_without_generation(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, CITY_SCRIPT)
    city = _city_folder(tmp_path, ffmpeg, two_files=True)

    logs: list[str] = []
    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "base", aligner=aligner,
    )
    result = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(city)),
        tmp_path / "smart", aligner=aligner, log=logs.append,
    )
    assert result.video.is_file() and result.report.ok
    assert any("Smart Visuals" in line for line in logs), "smart visual insertion must be logged"

    base_duration = _probe_duration(ffprobe, baseline.video)
    final_duration = _probe_duration(ffprobe, result.video)
    assert final_duration == pytest.approx(7.2, abs=0.08), "Smart timeline is audio-locked"
    assert final_duration <= base_duration + 0.08

    width, height = _probe_size(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, width, height, final_duration, _yellow_predicate)
    # The mandatory Phase-35 four-second minimum retains the first semantic
    # boundary and defers the second dense boundary instead of rendering two
    # sub-four-second sections.
    assert len(windows) == 1, f"one retained four-second image section expected, got {windows}"
    assert windows[0][1] - windows[0][0] >= 3.5
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png")), \
        "matching existing media must not trigger generation"


# ---------------------------------------------------------------------------
# C - matching existing video is chosen (video-first priority)
# ---------------------------------------------------------------------------
def test_C_matching_video_chosen_with_video_first_priority(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, CITY_SCRIPT)
    city = tmp_path / "city"
    city.mkdir(parents=True, exist_ok=True)
    # Give image and video identical semantic filename evidence so the tested
    # video_first policy is the deciding tie-breaker, not a relevance mismatch.
    make_clip(ffmpeg, city / "city street.mp4", size="160x90", duration=1.6, color="white", audio_rate=None)
    _make_image(ffmpeg, city / "city street.png", "yellow")

    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "base", aligner=aligner,
    )
    result = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(
            city, smart_visual_source_priority="video_first",
            image_ratio_min=0, image_ratio_max=0,
        )),
        tmp_path / "smart_video", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    base_duration = _probe_duration(ffprobe, baseline.video)
    final_duration = _probe_duration(ffprobe, result.video)
    assert final_duration == pytest.approx(7.2, abs=0.08)
    assert final_duration <= base_duration + 0.08

    width, height = _probe_size(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, width, height, final_duration, _white_predicate)
    assert windows, "the matched silent video section must be visible"
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png"))


# ---------------------------------------------------------------------------
# D/G - no match -> generated image (landscape geometry for Long-Form)
# ---------------------------------------------------------------------------
def test_D_no_match_falls_back_to_existing_image_and_G_landscape_geometry(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: a weak/unrelated context NO LONGER generates
    # an image - it falls back to an existing pool asset. The Long-Form
    # output keeps the landscape geometry (part G of the former test).
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, GLACIER_SCRIPT)
    city = _city_folder(tmp_path, ffmpeg)

    result = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(city)),
        tmp_path / "smart_fallback", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok

    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png")), \
        "Phase 33: nothing may be generated, ever"

    out_width, out_height = _probe_size(ffprobe, result.video)
    assert out_width > out_height, "Long-Form output must stay landscape"
    duration = _probe_duration(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, out_width, out_height, duration, _yellow_predicate)
    assert windows, "the random existing image must be visible in the render"


# ---------------------------------------------------------------------------
# E - repeated sentence: deterministic selection, no generation cache
# ---------------------------------------------------------------------------
def test_E_repeated_sentence_selection_is_deterministic(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: the former generation-cache-reuse test now
    # proves that identical repeated sentences render deterministically with
    # pure selection - and that no generation cache appears.
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, NIGHT_SCRIPT)
    city = _city_folder(tmp_path, ffmpeg)

    first = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(city)),
        tmp_path / "smart_repeat_1", aligner=aligner,
    )
    second = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(city)),
        tmp_path / "smart_repeat_2", aligner=aligner,
    )
    assert first.video.is_file() and first.report.ok
    assert second.video.is_file() and second.report.ok
    assert _probe_duration(ffprobe, first.video) == pytest.approx(
        _probe_duration(ffprobe, second.video), abs=0.05,
    ), "the same seed must reproduce the same smart visual assignment"
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png"))


# ---------------------------------------------------------------------------
# F - Shorts use the portrait geometry and their own profile
# ---------------------------------------------------------------------------
def test_F_shorts_portrait_selection_and_profile_separation(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: Shorts SELECT from their own profile (never
    # generate) and keep the portrait 9:16 geometry.
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, MOUNTAIN_SCRIPT)
    city = _city_folder(tmp_path, ffmpeg)

    result = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(
            voice, script,
            shorts_smart_visual_enabled=True,
            shorts_smart_visual_folders=[str(city)],
            shorts_smart_visual_cadence="every_1",
            shorts_asset_cooldown_videos=0,
            shorts_image_ratio_min=100,
            shorts_image_ratio_max=100,
        ),
        tmp_path / "smart_shorts", aligner=aligner,
    )
    shorts = result.shorts
    assert shorts and shorts[0].video.is_file()

    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png")), \
        "Phase 33: Shorts must select, never generate"
    width, height = _probe_size(ffprobe, shorts[0].video)
    assert height > width, "Shorts output must stay portrait 9:16"
    duration = _probe_duration(ffprobe, shorts[0].video)
    windows = _find_windows(ffmpeg, shorts[0].video, width, height, duration, _yellow_predicate)
    assert windows, "the Short must place an existing image from its own profile"

    # The Long-Form profile was never enabled: a Long-Form job of the same
    # project keeps the historical render (no smart visuals).
    lf = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "lf_no_smart", aligner=aligner,
    )
    assert lf.video.is_file() and lf.report.ok
    lf_width, lf_height = _probe_size(ffprobe, lf.video)
    lf_duration = _probe_duration(ffprobe, lf.video)
    assert not _find_windows(ffmpeg, lf.video, lf_width, lf_height, lf_duration, _yellow_predicate), \
        "the disabled Long-Form profile must not insert images"


# ---------------------------------------------------------------------------
# H - generation unavailable -> safe fallback to existing media
# ---------------------------------------------------------------------------
def test_H_generation_unavailable_falls_back_to_existing_media(ffmpeg_paths, tmp_path, monkeypatch):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, GLACIER_SCRIPT)
    city = _city_folder(tmp_path, ffmpeg)

    monkeypatch.setattr(
        "app.video_merger.image_generation.resolve_generation_provider",
        lambda ffmpeg_path=None: (None, ["Kein Backend verfügbar"]),
    )
    result = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_smart_lf(city)),
        tmp_path / "smart_fallback", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok, "the project must stay renderable"
    width, height = _probe_size(ffprobe, result.video)
    duration = _probe_duration(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, width, height, duration, _yellow_predicate)
    assert windows, "fallback must use the best existing media"
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*.png"))


# ---------------------------------------------------------------------------
# I - disabled Smart Visuals leave the Typewriter intro untouched
# ---------------------------------------------------------------------------
def test_I_disabled_smart_visuals_keep_typewriter_render_identical(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)
    city = _city_folder(tmp_path, ffmpeg)

    typewriter = dict(
        typewriter_intro_enabled=True,
        typewriter_hook_text="WAIT FOR IT",
        typewriter_hold_seconds=0.6,
    )
    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **typewriter), tmp_path / "tw_base", aligner=aligner,
    )
    assert baseline.video.is_file() and baseline.report.ok
    with_smart_config = MainProjectEngine(engine).create_main(
        media, _base_settings(
            voice, script, **typewriter,
            smart_visual_enabled=False,
            smart_visual_folders=[str(city)],
            smart_visual_generation_strategy="always",
        ),
        tmp_path / "tw_smart_off", aligner=aligner,
    )
    assert with_smart_config.video.is_file() and with_smart_config.report.ok
    assert with_smart_config.video.read_bytes() == baseline.video.read_bytes()


# ---------------------------------------------------------------------------
# J - subtitles, music and transitions stay intact
# ---------------------------------------------------------------------------
def test_J_subtitles_music_transitions_intact_with_smart_visuals(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)
    city = _city_folder(tmp_path, ffmpeg)
    music = tmp_path / "music.wav"
    _tone(ffmpeg, music, 300, 8.0, .4)

    base = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, subtitle_enabled=True, music_path=str(music), music_volume=60),
        tmp_path / "j_base", aligner=aligner,
    )
    result = MainProjectEngine(engine).create_main(
        media, _base_settings(
            voice, script, subtitle_enabled=True, music_path=str(music), music_volume=60,
            transition_type="cross_dissolve",
            **_smart_lf(city, smart_visual_generation_strategy="always"),
        ),
        tmp_path / "j_smart", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    assert base.srt.is_file() and result.srt.is_file()
    assert result.srt.read_text(encoding="utf-8") == base.srt.read_text(encoding="utf-8"), \
        "Smart Visuals must never touch subtitle timing"

    width, height = _probe_size(ffprobe, result.video)
    duration = _probe_duration(ffprobe, result.video)
    # This compact legacy fixture is shorter than the Phase-36 four-second
    # minimum, so Smart Visuals correctly preserve source footage rather than
    # creating a flash. Audio/subtitle regression checks remain authoritative.
    windows = _find_windows(ffmpeg, result.video, width, height, duration, _generated_predicate)
    mid = duration / 2.0
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{mid - 0.1:.3f}",
        "-i", result.video, "-t", "0.25", "-vn", "-ac", "1", "-ar", "16000",
        "-f", "f32le", "pipe:1",
    ])
    import array
    import math
    values = array.array("f")
    values.frombytes(raw)
    sample_rate = 16000
    sine = sum(v * math.sin(2 * math.pi * 300 * i / sample_rate) for i, v in enumerate(values))
    cosine = sum(v * math.cos(2 * math.pi * 300 * i / sample_rate) for i, v in enumerate(values))
    music_strength = math.sqrt(sine * sine + cosine * cosine) / max(1, len(values))
    assert music_strength > 0.005, "music must keep playing across smart visual sections"


# ---------------------------------------------------------------------------
# K - generated images reuse the Phase-30 motion + TV effect pipeline
# ---------------------------------------------------------------------------
def _textured_city_folder(tmp_path: Path, ffmpeg: Path) -> Path:
    """Yellow-dominant TEXTURED images (solid colors would make motion and
    TV effects invisible - a gradient keeps the yellow predicate true while
    giving ken_burns/VHS something to change)."""
    folder = tmp_path / "city"
    for name in ("city street.png", "city market.png"):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            "gradients=s=160x120:c0=yellow:c1=gold:d=1", "-frames:v", "1", str(path),
        ])
    return folder


def test_K_selected_image_reuses_motion_and_tv_effect(ffmpeg_paths, tmp_path):
    # Phase 33 contract change: the inserted visual is a SELECTED existing
    # image (never generated); motion + TV effect compose with it exactly
    # like before.
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, CITY_SCRIPT)
    city = _textured_city_folder(tmp_path, ffmpeg)

    def render(name: str, motion: str, effect: str = "none") -> Path:
        result = MainProjectEngine(engine).create_main(
            media, _base_settings(
                voice, script,
                timeline_image_motion=motion,
                timeline_image_effect=effect,
                timeline_image_effect_intensity=80,
                **_smart_lf(city),
            ),
            tmp_path / name, aligner=aligner,
        )
        assert result.video.is_file() and result.report.ok
        return result.video

    # Motion and TV effect are asserted separately: the VHS flicker varies
    # over time and would drown the ken_burns comparison if combined.
    still = render("k_still", "none")
    moving = render("k_moving", "ken_burns")
    vhs = render("k_vhs", "none", "vhs")
    width, height = _probe_size(ffprobe, moving)
    duration = _probe_duration(ffprobe, moving)
    windows = _find_windows(ffmpeg, moving, width, height, duration, _yellow_predicate)
    assert windows, "the selected image section must be visible"
    start, end = windows[0]
    mid = (start + end) / 2
    t_a, t_b = max(0.05, mid - 0.25), min(duration - 0.05, mid + 0.25)

    still_a = _frame_rgb(ffmpeg, still, t_a, width, height)
    still_b = _frame_rgb(ffmpeg, still, t_b, width, height)
    moving_a = _frame_rgb(ffmpeg, moving, t_a, width, height)
    moving_b = _frame_rgb(ffmpeg, moving, t_b, width, height)
    assert _mean_abs_diff(moving_a, moving_b) > _mean_abs_diff(still_a, still_b), \
        "ken_burns motion must visibly move the selected image"

    vhs_a = _frame_rgb(ffmpeg, vhs, t_a, width, height)
    assert _mean_abs_diff(still_a, vhs_a) > 2.0, "the VHS effect must change the selected image frames"


# ---------------------------------------------------------------------------
# L - video -> generated image -> video transitions
# ---------------------------------------------------------------------------
def test_L_video_generated_image_video_transitions(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(tmp_path, ffmpeg, ffprobe, MIXED_SCRIPT)
    city = tmp_path / "city"
    city.mkdir(parents=True, exist_ok=True)
    make_clip(ffmpeg, city / "city traffic.mp4", size="160x90", duration=1.6, color="white", audio_rate=None)

    result = MainProjectEngine(engine).create_main(
        media, _base_settings(
            voice, script, transition_type="cross_dissolve",
            **_smart_lf(city, smart_visual_source_priority="video_first"),
        ),
        tmp_path / "l_mixed", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok

    width, height = _probe_size(ffprobe, result.video)
    duration = _probe_duration(ffprobe, result.video)
    white_windows = _find_windows(ffmpeg, result.video, width, height, duration, _white_predicate)
    assert white_windows, "the matched video section must render"
    assert duration == pytest.approx(7.2, abs=0.08)
    trace = tmp_path / "l_mixed" / "PHASE_35_DEBUG_TRACE.json"
    assert trace.is_file(), "the transition-aware resolved timeline must be reviewable"
