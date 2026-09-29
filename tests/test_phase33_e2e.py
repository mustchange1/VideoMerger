"""Phase 33 e2e: Smart Visual selection engine with REAL FFmpeg renders.

Verifies on actual encoded output:
1  Smart Match places semantically matched existing images and keeps the
   subtitle/voiceover timing untouched,
2  weak matches fall back to a random EXISTING asset - never generation,
3  uniqueness: no duplicate asset while the pool is sufficient; exhausted
   pools may reuse but never back-to-back,
4  the single Image Duration (default 5.0 s) drives the inserted time,
5  Randomize (nonce) produces a different but still valid assignment,
6  Mostly Random mixes smart + random placements,
7  Random Only never matches semantically,
8  Shorts keep their own profile and portrait geometry,
9  a disabled/empty Smart Visual render stays byte-identical to the
   historical video-only output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.main_project import MainProjectEngine
from tests.conftest import make_clip
from tests.test_phase30_e2e import (
    _base_settings,
    _color_ratio,
    _find_windows,
    _frame_rgb,
    _make_image,
    _probe_duration,
    _probe_size,
    _shorts_settings,
    _tone,
)
from tests.test_phase31_e2e import (
    CITY_SCRIPT,
    GLACIER_SCRIPT,
    MOUNTAIN_SCRIPT,
    _create_confirmed,
    _create_confirmed_youtube,
    _smart_lf,
    _smart_project_factory,
    _yellow_predicate,
)

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
    from app.video_merger.smart_visuals import (
        build_smart_visual_plan,
        smart_visual_profile_from_settings,
    )

    resolved = settings
    if short:
        from app.video_merger.youtube_outputs import ShortJob, short_settings

        job = ShortJob(index=0, voiceover_path=Path(str(settings.voiceover_path)),
                       script_path=None, output_name="Short1", cache_key="p33-short")
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
        seed_parts=("phase33-e2e", f"nonce={profile.randomize_nonce}"),
        log=lambda *_a, **_k: None,
    )
    return plan.slots


# ---------------------------------------------------------------------------
# 1 - Smart Match: real render, matched images, timing untouched
# ---------------------------------------------------------------------------
def test_smart_match_places_matched_images_and_keeps_timing(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")
    _make_image(ffmpeg, city / "city market.png", "yellow")
    (city / "smart_metadata.json").write_text(
        '{"city street.png": {"description": "busy downtown streets full of markets"},'
        ' "city market.png": {"description": "a market square in the city center"}}',
        encoding="utf-8",
    )

    settings = _base_settings(voice, script, **_smart_lf(city, smart_visual_mode="smart_match"))
    slots = _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    smart = [slot for slot in slots if slot.source_mode == "SMART"]
    assert smart, "the city sentences must match the city media"
    assert all(not slot.generation_used for slot in slots)

    baseline = _create_confirmed(
        engine, media, _base_settings(voice, script), tmp_path / "base", aligner=aligner,
    )
    result = _create_confirmed(
        engine, media, settings, tmp_path / "smart", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    assert baseline.srt.is_file() and result.srt.is_file()
    assert result.srt.read_text(encoding="utf-8") == baseline.srt.read_text(encoding="utf-8"), \
        "Smart Visuals must never touch subtitle timing"
    base_duration = _probe_duration(ffprobe, baseline.video)
    final_duration = _probe_duration(ffprobe, result.video)
    assert final_duration == pytest.approx(7.2, abs=0.08), "Smart timeline is audio-locked"
    assert final_duration <= base_duration + 0.08
    width, height = _probe_size(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, width, height, final_duration, _yellow_predicate)
    assert windows, "the matched image sections must be visible"
    assert not _generated_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# 2 - weak match: random existing fallback, never generation
# ---------------------------------------------------------------------------
def test_weak_match_falls_back_to_existing_and_never_generates(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, GLACIER_SCRIPT
    )
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _make_image(ffmpeg, unrelated / "zebra savanna.png", "magenta")
    _make_image(ffmpeg, unrelated / "kitchen tools.png", "magenta")

    settings = _base_settings(voice, script, **_smart_lf(unrelated, smart_visual_mode="smart_match"))
    slots = _plan_slots(settings, GLACIER_SCRIPT, tmp_path, ffmpeg, ffprobe)
    assert slots
    for slot in slots:
        assert slot.source_mode in {"RANDOM", ""}
        assert not slot.generation_used

    result = _create_confirmed(
        engine, media, settings, tmp_path / "weak", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    width, height = _probe_size(ffprobe, result.video)
    duration = _probe_duration(ffprobe, result.video)
    windows = _find_windows(
        ffmpeg, result.video, width, height, duration,
        lambda frame: _color_ratio(frame, (255, 0, 255)) > 0.55,
    )
    assert windows, "the random existing image must be visible"
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*"))


# ---------------------------------------------------------------------------
# 3 - uniqueness in the real plan (sufficient pool / exhausted pool)
# ---------------------------------------------------------------------------
def test_no_duplicate_assets_while_pool_is_sufficient(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    _engine, _media, _aligner, _voice, _script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    for index in range(4):
        _make_image(ffmpeg, city / f"city spot {index}.png", "yellow")

    settings = _base_settings(
        _voice, _script,
        **_smart_lf(city, smart_visual_mode="random_only"),
    )
    slots = _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    paths = [slot.selected_path for slot in slots if slot.selected_kind]
    assert len(paths) >= 2
    assert len(set(paths)) == len(paths), "every asset at most once while unused remain"


FOUR_SENTENCE_SCRIPT = (
    "The city streets are busy. Markets fill the center. "
    "Lights glow at night. Crowds gather near rivers."
)
# Register aligner timings for the longer script (all within the 2.4 s voice).
from tests.test_phase31_e2e import _SCRIPT_TIMINGS

_SCRIPT_TIMINGS[FOUR_SENTENCE_SCRIPT] = [
    ("The", 0.05, 0.12), ("city", 0.14, 0.30), ("streets", 0.32, 0.50),
    ("are", 0.52, 0.58), ("busy", 0.60, 0.75),
    ("Markets", 0.80, 0.95), ("fill", 0.97, 1.05), ("the", 1.07, 1.12),
    ("center", 1.14, 1.30),
    ("Lights", 1.35, 1.48), ("glow", 1.50, 1.62), ("at", 1.64, 1.68),
    ("night", 1.70, 1.85),
    ("Crowds", 1.90, 2.00), ("gather", 2.02, 2.12), ("near", 2.14, 2.20),
    ("rivers", 2.22, 2.32),
]


def test_exhausted_pool_reuses_without_back_to_back_repeats(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    _engine, _media, _aligner, _voice, _script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, FOUR_SENTENCE_SCRIPT
    )
    tiny = tmp_path / "tiny"
    _make_image(ffmpeg, tiny / "only one.png", "yellow")
    _make_image(ffmpeg, tiny / "only two.png", "yellow")

    settings = _base_settings(
        _voice, _script,
        **_smart_lf(tiny, smart_visual_mode="random_only"),
    )
    slots = _plan_slots(settings, FOUR_SENTENCE_SCRIPT, tmp_path, ffmpeg, ffprobe)
    paths = [slot.selected_path for slot in slots if slot.selected_kind]
    assert len(paths) > 2, "the exhausted pool is reused"
    for previous, current in zip(paths, paths[1:]):
        assert previous != current, "never the same asset twice in a row"

    result = _create_confirmed(
        _engine, _media, settings, tmp_path / "exhaust", aligner=_aligner,
    )
    assert result.video.is_file() and result.report.ok


# ---------------------------------------------------------------------------
# 4 - Phase-35 automatic speech duration supersedes the legacy fixed setting
# ---------------------------------------------------------------------------
def test_automatic_speech_duration_ignores_legacy_fixed_image_duration(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    def render(name: str, duration: float) -> float:
        result = _create_confirmed(
            engine, media,
            _base_settings(voice, script, **_smart_lf(
                city, smart_visual_mode="random_only",
                smart_visual_image_duration=duration,
            )),
            tmp_path / name, aligner=aligner,
        )
        assert result.video.is_file() and result.report.ok
        return _probe_duration(ffprobe, result.video)

    # Count how many image slots the plan actually inserts, then predict the
    # exact duration shift of the single image-duration setting.
    settings = _base_settings(voice, script, **_smart_lf(
        city, smart_visual_mode="random_only", smart_visual_image_duration=1.0,
    ))
    image_slots = sum(
        1 for slot in _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
        if slot.selected_kind
    )
    assert image_slots >= 1

    baseline = _create_confirmed(
        engine, media, _base_settings(voice, script), tmp_path / "base", aligner=aligner,
    )
    base_duration = _probe_duration(ffprobe, baseline.video)
    short = render("dur_short", 1.0)
    long = render("dur_long", 5.0)
    assert short == pytest.approx(7.2, abs=0.08)
    assert long == pytest.approx(7.2, abs=0.08)
    assert short <= base_duration + 0.08
    # Canonical semantic-section durations are acoustic. The historical fixed
    # image-duration preference cannot lengthen or shorten the audio lock.
    assert long - short == pytest.approx(0.0, abs=0.12)


def test_default_image_duration_is_5_seconds(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    from app.video_merger.smart_visuals import smart_visual_profile_from_settings
    from app.video_merger.models import ExportSettings

    # The engine default for NEW configurations is 5.0 s.
    assert smart_visual_profile_from_settings(
        ExportSettings(smart_visual_enabled=True, smart_visual_folders=["x"])
    ).image_duration == pytest.approx(5.0)
    # An explicitly saved duration is preserved.
    assert smart_visual_profile_from_settings(
        ExportSettings(smart_visual_enabled=True, smart_visual_folders=["x"],
                       smart_visual_image_duration=2.0)
    ).image_duration == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 5 - Randomize: new valid assignment, same timeline
# ---------------------------------------------------------------------------
def test_randomize_produces_a_different_valid_assignment(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    _engine, _media, _aligner, _voice, _script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    for index in range(4):
        _make_image(ffmpeg, city / f"city spot {index}.png", "yellow")

    base_settings = _base_settings(
        _voice, _script, **_smart_lf(city, smart_visual_mode="random_only"),
    )
    randomized = _base_settings(
        _voice, _script,
        **_smart_lf(city, smart_visual_mode="random_only", smart_visual_randomize_nonce=7),
    )
    base_slots = _plan_slots(base_settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    alt_slots = _plan_slots(randomized, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    base_paths = [slot.selected_path for slot in base_slots if slot.selected_kind]
    alt_paths = [slot.selected_path for slot in alt_slots if slot.selected_kind]
    assert base_paths and alt_paths
    assert base_paths != alt_paths, "Randomize explores a different assignment"
    # Both assignments stay unique and keep the slot windows (timeline).
    assert len(set(base_paths)) == len(base_paths)
    assert len(set(alt_paths)) == len(alt_paths)
    assert [(round(s.start, 3), round(s.end, 3)) for s in base_slots] == \
           [(round(s.start, 3), round(s.end, 3)) for s in alt_slots]

    # The randomized render still succeeds end-to-end.
    result = _create_confirmed(
        _engine, _media, randomized, tmp_path / "randomized", aligner=_aligner,
    )
    assert result.video.is_file() and result.report.ok


# ---------------------------------------------------------------------------
# 6/7 - Mostly Random mixes, Random Only never matches
# ---------------------------------------------------------------------------
def test_mostly_random_mixes_smart_and_random(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    _engine, _media, _aligner, _voice, _script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")
    _make_image(ffmpeg, city / "city market.png", "yellow")
    (city / "smart_metadata.json").write_text(
        '{"city street.png": {"description": "busy downtown streets and markets"},'
        ' "city market.png": {"description": "the central market of the city"}}',
        encoding="utf-8",
    )

    settings = _base_settings(
        _voice, _script,
        **_smart_lf(city, smart_visual_mode="smart_inserts", smart_visual_insert_percent=50),
    )
    slots = _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    filled = [slot for slot in slots if slot.selected_kind]
    assert filled
    sources = {slot.source_mode for slot in filled}
    assert "RANDOM" in sources, "Mostly Random keeps random placements"
    assert not any(slot.generation_used for slot in slots)


def test_random_only_never_matches_semantically(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    _engine, _media, _aligner, _voice, _script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "city street.png", "yellow")

    settings = _base_settings(
        _voice, _script, **_smart_lf(city, smart_visual_mode="random_only"),
    )
    slots = _plan_slots(settings, CITY_SCRIPT, tmp_path, ffmpeg, ffprobe)
    filled = [slot for slot in slots if slot.selected_kind]
    assert filled
    assert all(slot.source_mode == "RANDOM" for slot in filled)
    assert all(slot.score == 0.0 for slot in filled)


# ---------------------------------------------------------------------------
# 8 - Shorts keep their own profile and portrait geometry
# ---------------------------------------------------------------------------
def test_shorts_selection_keeps_portrait_geometry(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, MOUNTAIN_SCRIPT
    )
    city = tmp_path / "city"
    _make_image(ffmpeg, city / "mountain view.png", "yellow")

    result = _create_confirmed_youtube(
        engine, media, _shorts_settings(
            voice, script,
            shorts_smart_visual_enabled=True,
            shorts_smart_visual_folders=[str(city)],
            shorts_smart_visual_mode="smart_match",
            shorts_smart_visual_cadence="every_1",
            shorts_asset_cooldown_videos=0,
            shorts_image_ratio_min=100,
            shorts_image_ratio_max=100,
        ),
        tmp_path / "p33_shorts", aligner=aligner,
    )
    shorts = result.shorts
    assert shorts and shorts[0].video.is_file()
    width, height = _probe_size(ffprobe, shorts[0].video)
    assert height > width, "Shorts stay portrait"
    duration = _probe_duration(ffprobe, shorts[0].video)
    windows = _find_windows(ffmpeg, shorts[0].video, width, height, duration, _yellow_predicate)
    assert windows, "the Short must place its own profile's image"
    generated = _generated_dir(tmp_path)
    assert not generated.exists() or not list(generated.glob("*"))


# ---------------------------------------------------------------------------
# 9 - disabled Smart Visuals keep the historical render byte-identical
# ---------------------------------------------------------------------------
def test_disabled_smart_visuals_keep_historical_render_byte_identical(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _smart_project_factory(
        tmp_path, ffmpeg, ffprobe, CITY_SCRIPT
    )
    baseline = _create_confirmed(
        engine, media, _base_settings(voice, script), tmp_path / "base", aligner=aligner,
    )
    # Phase-33 fields present but the feature disabled -> untouched render.
    result = _create_confirmed(
        engine, media, _base_settings(
            voice, script,
            smart_visual_enabled=False,
            smart_visual_mode="smart_match",
            smart_visual_image_duration=5.0,
            smart_visual_insert_percent=25,
            smart_visual_randomize_nonce=3,
        ),
        tmp_path / "disabled", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    assert result.video.read_bytes() == baseline.video.read_bytes()
