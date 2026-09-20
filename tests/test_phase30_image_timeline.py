"""Phase 30: Image Timeline & Visual Effects - unit tests.

Covers folder scanning, the deterministic image pool, insertion planning
(Every-N / Percentage / Disabled), duration modes, LF/Shorts independence,
migration of old projects, the per-item timeline image model, the command
builder image chain (motion + image-only TV effect), continuity mathematics,
and cache-identity guarantees (disabled feature keeps historical digests).
"""
from __future__ import annotations

import json
from pathlib import Path
from random import Random

import pytest

from app.video_merger import image_timeline as it
from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.models import ExportSettings, MediaInfo
from app.video_merger.render_cache import stage1_fingerprint
from app.video_merger.target import resolve_export
from app.video_merger.youtube_outputs import long_form_settings, short_settings
from tests.conftest import fake_media

PNG = b"\x89PNG\r\n\x1a\n"  # content does not matter; scanning is name/stat based


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------
def _write(path: Path, data: bytes = PNG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_scan_collects_supported_formats_only(tmp_path: Path):
    folder = tmp_path / "Bilder"
    for name in ("a.png", "b.jpg", "c.jpeg", "d.webp", "e.bmp"):
        _write(folder / name)
    for name in ("video.mp4", "notes.txt", "anim.gif", "a.tiff"):
        _write(folder / name)
    files = it.scan_image_files((str(folder),))
    assert [p.name for p in files] == ["a.png", "b.jpg", "c.jpeg", "d.webp", "e.bmp"]


def test_scan_keeps_unicode_names_and_natural_order(tmp_path: Path):
    folder = tmp_path / "Ünïcode Projekt"
    for name in ("Bild 10.png", "Bild 2.png", "Ärger.jpg", "straße.webp"):
        _write(folder / name)
    files = it.scan_image_files((str(folder),))
    names = [p.name for p in files]
    assert names == sorted(names, key=it._natural_key)
    assert "Bild 2.png" in names and "Bild 10.png" in names
    assert names.index("Bild 2.png") < names.index("Bild 10.png")
    assert "Ärger.jpg" in names and "straße.webp" in names


def test_scan_skips_missing_folders_and_deduplicates(tmp_path: Path):
    folder = tmp_path / "real"
    _write(folder / "x.png")
    files = it.scan_image_files((str(tmp_path / "missing"), str(folder), str(folder)))
    assert [p.name for p in files] == ["x.png"]


# ---------------------------------------------------------------------------
# Pool behavior
# ---------------------------------------------------------------------------
def test_pool_full_pass_before_reshuffle_and_no_immediate_repeat():
    files = [Path(f"/tmp/img{i}.png") for i in range(4)]
    pool = it.ImagePool(files, Random(42))
    draws = [pool.draw() for _ in range(12)]
    assert all(d is not None for d in draws)
    for start in (0, 4, 8):  # every pass contains each image exactly once
        assert len(set(map(str, draws[start:start + 4]))) == 4
    for first, second in zip(draws, draws[1:]):
        assert first != second


def test_pool_deterministic_for_same_seed():
    files = [Path(f"/tmp/i{i}.png") for i in range(6)]
    draws1 = [it.ImagePool(files, Random(7)).draw() for _ in range(9)]
    draws2 = [it.ImagePool(files, Random(7)).draw() for _ in range(9)]
    assert draws1 == draws2


def test_pool_single_file_never_repeats_immediately():
    pool = it.ImagePool([Path("/tmp/only.png")], Random(1))
    assert pool.draw() == Path("/tmp/only.png")
    assert pool.draw() == Path("/tmp/only.png")  # only choice is allowed


# ---------------------------------------------------------------------------
# Insertion planning
# ---------------------------------------------------------------------------
def _profile(**overrides) -> it.ImageTimelineProfile:
    base = dict(
        folders=("/tmp/images",), mode="every_n", every_n=4, share_percent=20,
        min_video_gap=2, duration_mode="fixed", duration=2.5, duration_min=2.0,
        duration_max=4.0, motion="zoom_in", effect="off", effect_intensity=20,
        flicker_speed="normal", global_effect="off", global_intensity=20,
        global_flicker_speed="normal",
    )
    base.update(overrides)
    return it.ImageTimelineProfile(**base)


def test_disabled_mode_never_inserts():
    assert it.plan_insertion_positions(20, _profile(mode="disabled"), Random(1)) == []


def test_every_n_positions_and_never_after_last_video():
    positions = it.plan_insertion_positions(17, _profile(every_n=4), Random(1))
    assert positions == [3, 7, 11, 15]
    assert max(positions) <= 15  # occurrence 16 is the last -> no image after it


def test_min_video_gap_enforced():
    positions = it.plan_insertion_positions(20, _profile(every_n=1, min_video_gap=3), Random(1))
    assert all(b - a >= 3 for a, b in zip(positions, positions[1:]))
    assert positions[0] == 0 or positions[0] >= 0


def test_percentage_mode_deterministic_with_gap_and_bounds():
    profile = _profile(mode="percentage", share_percent=20, min_video_gap=2)
    p1 = it.plan_insertion_positions(12, profile, Random(it.derive_image_seed("seed")))
    p2 = it.plan_insertion_positions(12, profile, Random(it.derive_image_seed("seed")))
    assert p1 == p2
    assert p1, "20 % of 12 clips inserts at least one image"
    assert all(0 <= p <= 10 for p in p1)
    assert all(b - a >= 2 for a, b in zip(p1, p1[1:]))


def test_percentage_share_scales_count():
    profile = _profile(mode="percentage", min_video_gap=1)
    low = it.plan_insertion_positions(40, _profile(mode="percentage", share_percent=10, min_video_gap=1), Random(5))
    high = it.plan_insertion_positions(40, _profile(mode="percentage", share_percent=50, min_video_gap=1), Random(5))
    assert len(high) > len(low)


def test_too_few_videos_never_inserts():
    assert it.plan_insertion_positions(1, _profile(), Random(1)) == []
    assert it.plan_insertion_positions(0, _profile(), Random(1)) == []


def test_duration_modes():
    assert it.image_duration_for(_profile(duration=2.5), Random(3)) == 2.5
    d1 = it.image_duration_for(_profile(duration_mode="range"), Random(9))
    d2 = it.image_duration_for(_profile(duration_mode="range"), Random(9))
    assert d1 == d2 and 2.0 <= d1 <= 4.0


# ---------------------------------------------------------------------------
# Profile resolution, defaults, migration, LF/Shorts independence
# ---------------------------------------------------------------------------
def test_fresh_settings_yield_safe_defaults():
    profile = it.profile_from_settings(ExportSettings())
    assert profile.mode == "disabled" and not profile.active
    assert profile.duration == 2.5 and profile.min_video_gap == 2
    assert profile.motion == "zoom_in"
    assert profile.effect == "off" and profile.effect_intensity == 20
    assert profile.global_effect == "off" and profile.global_intensity == 20
    assert profile.folders == ()


def test_old_project_without_image_keys_loads_defaults(tmp_path: Path):
    from app.video_merger.settings_store import SettingsStore

    store = SettingsStore(tmp_path / "settings.json")
    store.path.write_text(json.dumps({"aspect": "16:9", "transition_type": "cross_dissolve"}), encoding="utf-8")
    loaded = store.load()
    assert loaded.timeline_image_mode == "disabled"
    assert loaded.long_form_image_folders == [] and loaded.shorts_image_folders == []
    assert loaded.timeline_image_duration == 2.5
    assert loaded.global_tv_effect == "off"


def test_short_settings_maps_shorts_profile_and_long_form_stays_clean():
    settings = ExportSettings(
        long_form_image_folders=["/tmp/lf"],
        shorts_image_folders=["/tmp/sh"],
        timeline_image_mode="every_n",
        timeline_image_every_n=5,
        shorts_image_mode="percentage",
        shorts_image_share_percent=30,
        shorts_image_motion="pan",
        shorts_image_effect="vhs",
        shorts_image_effect_intensity=40,
        shorts_global_tv_effect="broadcast",
        shorts_global_tv_effect_intensity=25,
    )
    from app.video_merger.youtube_outputs import ShortJob

    job = ShortJob(index=1, voiceover_path=Path("/tmp/voice.mp3"), script_path=None,
                   output_name="001", cache_key="k")
    short = short_settings(settings, job)
    assert short.timeline_image_folders == ["/tmp/sh"]
    assert short.timeline_image_mode == "percentage"
    assert short.timeline_image_share_percent == 30
    assert short.timeline_image_motion == "pan"
    assert short.timeline_image_effect == "vhs"
    assert short.timeline_image_effect_intensity == 40
    assert short.global_tv_effect == "broadcast"
    assert short.global_tv_effect_intensity == 25

    long_form = long_form_settings(settings)
    assert long_form.timeline_image_folders == ["/tmp/lf"]
    assert long_form.timeline_image_mode == "every_n"
    assert long_form.timeline_image_every_n == 5
    # Long-Form keeps the canonical global overlay, never the Shorts one.
    assert long_form.global_tv_effect == "off"
    assert long_form.timeline_image_motion == "zoom_in"


def test_disabled_shorts_profile_respects_zero():
    settings = ExportSettings(long_form_image_folders=["/tmp/lf"], timeline_image_mode="every_n")
    from app.video_merger.youtube_outputs import ShortJob

    job = ShortJob(index=1, voiceover_path=Path("/tmp/v.mp3"), script_path=None,
                   output_name="001", cache_key="k")
    short = short_settings(settings, job)
    assert short.timeline_image_mode == "disabled"  # never inherits the LF profile
    assert short.timeline_image_folders == []


def test_empty_folders_deactivate_profile():
    settings = ExportSettings(timeline_image_mode="every_n", timeline_image_folders=[])
    assert it.profile_from_settings(settings).active is False
    settings2 = ExportSettings(timeline_image_mode="every_n", timeline_image_folders=["/tmp/x"])
    assert it.profile_from_settings(settings2).active is True


# ---------------------------------------------------------------------------
# MediaInfo factory + apply_image_timeline
# ---------------------------------------------------------------------------
def _videos(count: int = 6) -> list[MediaInfo]:
    return [fake_media(f"clip{i}.mp4", duration=3.0) for i in range(count)]


def test_apply_inactive_returns_identical_sequence():
    media = _videos()
    result = it.apply_image_timeline(
        media, it.profile_from_settings(ExportSettings()),
        width=1920, height=1080, fps=30.0, transition_type="cross_dissolve",
        seed_parts=("test",), ffprobe_path="/nonexistent/ffprobe",
    )
    assert result.media is not media and result.media == media
    assert result.count == 0 and result.identity == ""


def test_apply_inserts_genuine_image_elements_between_videos(tmp_path: Path):
    folder = tmp_path / "pool"
    for i in range(3):
        _write(folder / f"bild{i}.png")
    profile = it.profile_from_settings(ExportSettings(
        timeline_image_mode="every_n", timeline_image_every_n=2,
        timeline_image_min_video_gap=1, timeline_image_folders=[str(folder)],
    ))
    media = _videos(6)
    result = it.apply_image_timeline(
        media, profile, width=1920, height=1080, fps=30.0,
        transition_type="smooth_blur", seed_parts=("lf", "natural", "auto"),
        ffprobe_path="/nonexistent/ffprobe",
    )
    assert result.count == 2  # after occurrence 1 and 3 (every 2nd, none after last)
    kinds = [item.is_image_insertion for item in result.media]
    assert kinds == [False, False, True, False, False, True, False, False]
    for item in result.media:
        if item.is_image_insertion:
            assert item.image_timeline_insertion is True
            assert item.audio.present is False
            assert item.image_transition_type == "smooth_blur"  # project transition reused
            assert item.image_motion == "zoom_in"
            assert item.duration == 2.5
    assert result.identity, "active insertion contributes a cache identity"
    # deterministic for identical seed parts
    result2 = it.apply_image_timeline(
        media, profile, width=1920, height=1080, fps=30.0,
        transition_type="smooth_blur", seed_parts=("lf", "natural", "auto"),
        ffprobe_path="/nonexistent/ffprobe",
    )
    assert [str(i.path) for i in result.media] == [str(i.path) for i in result2.media]
    assert [i.duration for i in result.media] == [i.duration for i in result2.media]
    assert result.identity == result2.identity


def test_apply_never_places_two_images_adjacent(tmp_path: Path):
    folder = tmp_path / "pool"
    for i in range(2):
        _write(folder / f"img{i}.png")
    profile = it.profile_from_settings(ExportSettings(
        timeline_image_mode="percentage", timeline_image_share_percent=60,
        timeline_image_min_video_gap=1, timeline_image_folders=[str(folder)],
    ))
    result = it.apply_image_timeline(
        _videos(10), profile, width=1920, height=1080, fps=30.0,
        transition_type="cross_dissolve", seed_parts=("x",),
        ffprobe_path="/nonexistent/ffprobe",
    )
    kinds = [item.is_image_insertion for item in result.media]
    for first, second in zip(kinds, kinds[1:]):
        assert not (first and second), "two images may never sit next to each other"


# ---------------------------------------------------------------------------
# Command builder: timeline image chain vs unchanged Stage-2 Add Image
# ---------------------------------------------------------------------------
def _timeline_image_item(duration: float = 2.5, **overrides) -> MediaInfo:
    from app.video_merger.image_timeline import make_image_media, ImageTimelineProfile

    profile = _profile(**{
        "motion": "zoom_in", "effect": "crt_scanlines", "effect_intensity": 30,
        "flicker_speed": "normal", **overrides,
    })
    return make_image_media(
        path=Path("/tmp/Bild ä.png"), duration=duration, width=1920, height=1080,
        fps=30.0, size=(800, 600), transition_type="cross_dissolve", profile=profile,
    )


def test_timeline_image_graph_contains_cover_motion_effect_and_silence():
    media = [fake_media("a.mp4"), _timeline_image_item(), fake_media("b.mp4")]
    settings = ExportSettings(resolution="1920x1080")
    resolved = resolve_export(media, settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolved)
    # cover fit + deterministic zoompan motion + CRT effect
    assert "force_original_aspect_ratio=increase" in graph
    assert "zoompan=z='1+0.12*(on/" in graph
    assert "geq=lum=" in graph
    assert "anullsrc" in graph  # the image stays silent
    # videos must NOT receive the image motion or TV effect
    statements = graph.split(";")
    video_lines = [line for line in statements if line.startswith("[pre0]") or line.startswith("[pre2]")]
    assert video_lines, "video statements must exist"
    for line in video_lines:
        assert "min(t/" not in line and "geq=" not in line


def test_stage2_add_image_graph_unchanged_by_phase30():
    from app.video_merger.models import AudioInfo

    stage2_image = MediaInfo(
        path=Path("/tmp/stage2.png"), duration=2.0, width=800, height=600,
        effective_width=800, effective_height=600, fps=30.0, fps_fraction="30/1",
        video_codec="png", pixel_format="yuv420p", sar="1:1", dar="",
        audio=AudioInfo(), is_image_insertion=True,
    )
    media = [fake_media("a.mp4"), stage2_image, fake_media("b.mp4")]
    settings = ExportSettings(resolution="1920x1080")
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolve_export(media, settings))
    # Stage-2 framing is the historical fit/pad chain - no Phase-30 motion or TV effect
    assert "force_original_aspect_ratio=decrease" in graph
    assert "geq=" not in graph
    assert "min(t/" not in graph.replace("window", "")


def test_timeline_image_motion_variants_build():
    for motion in ("none", "zoom_in", "zoom_out", "ken_burns", "pan"):
        media = [fake_media("a.mp4"), _timeline_image_item(motion=motion, effect="off"), fake_media("b.mp4")]
        settings = ExportSettings(resolution="1920x1080")
        graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolve_export(media, settings))
        assert "setsar=1" in graph
        if motion == "none":
            assert "zoompan=" not in graph
        else:
            assert "zoompan=z='" in graph
            assert ":d=" in graph and f"s=1920x1080" in graph


# ---------------------------------------------------------------------------
# Continuity / transition mathematics with images
# ---------------------------------------------------------------------------
def test_transition_math_covers_images_and_caps_boundary():
    media = [fake_media("a.mp4", duration=3.0), _timeline_image_item(duration=1.0), fake_media("b.mp4", duration=3.0)]
    settings = ExportSettings(resolution="1920x1080", transition_duration=2.0)
    resolved = resolve_export(media, settings)
    assert len(resolved.effective_durations) == 3 and len(resolved.transitions) == 2
    for transition, left, right in zip(resolved.transitions, resolved.effective_durations, resolved.effective_durations[1:]):
        assert transition <= 0.45 * left + 1e-9
        assert transition <= 0.45 * right + 1e-9
    chain = sum(resolved.effective_durations) - sum(resolved.transitions)
    assert chain == pytest.approx(3.0 + 1.0 + 3.0 - sum(resolved.transitions))


def test_effective_target_covers_extended_chain():
    videos = [fake_media("a.mp4", duration=4.0), fake_media("b.mp4", duration=4.0), fake_media("c.mp4", duration=4.0)]
    with_image = [videos[0], _timeline_image_item(duration=2.5), videos[1], videos[2]]
    settings = ExportSettings(resolution="1920x1080")
    probe = resolve_export(videos, settings)
    probe_with = resolve_export(with_image, settings)
    assert probe_with.expected_duration > probe.expected_duration


# ---------------------------------------------------------------------------
# Cache identity guarantees
# ---------------------------------------------------------------------------
def test_disabled_feature_keeps_historical_stage1_fingerprint():
    media = _videos(3)
    settings = ExportSettings(resolution="1920x1080")
    resolved = resolve_export(media, settings)
    digest_before, payload_before = stage1_fingerprint(media, settings, resolved)
    digest_after, payload_after = stage1_fingerprint(
        media, settings, resolved, timeline_images=None, global_tv_effect=None,
    )
    assert digest_before == digest_after
    assert payload_before == payload_after
    assert "timeline_images" not in payload_after and "global_tv_effect" not in payload_after


def test_active_feature_changes_stage1_fingerprint_only_when_configured():
    media = _videos(3)
    settings = ExportSettings(resolution="1920x1080")
    resolved = resolve_export(media, settings)
    base_digest, _ = stage1_fingerprint(media, settings, resolved)
    images_digest, images_payload = stage1_fingerprint(media, settings, resolved, timeline_images="abc123")
    global_digest, global_payload = stage1_fingerprint(media, settings, resolved, global_tv_effect="def456")
    assert images_digest != base_digest and global_digest != base_digest
    assert images_payload["timeline_images"] == "abc123"
    assert global_payload["global_tv_effect"] == "def456"
    assert "global_tv_effect" not in images_payload
    assert "timeline_images" not in global_payload


def test_media_payload_extends_only_for_timeline_images():
    from app.video_merger.render_cache import _media_payload

    video = fake_media("a.mp4")
    payload = _media_payload(video)
    assert "image_timeline_insertion" not in payload  # historical shape preserved
    image = _timeline_image_item()
    image_payload = _media_payload(image)
    assert image_payload["image_timeline_insertion"] is True
    assert image_payload["image_motion"] == "zoom_in"
    assert image_payload["image_effect"] == "crt_scanlines"


# ---------------------------------------------------------------------------
# Example settings validation
# ---------------------------------------------------------------------------
def test_example_settings_contains_validated_phase30_keys():
    path = Path(__file__).resolve().parents[1] / "config" / "example_settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    fields = ExportSettings.__dataclass_fields__
    unknown = [key for key in data if key not in fields]
    assert not unknown, f"example_settings.json contains unknown keys: {unknown}"
    for key in (
        "long_form_image_folders", "shorts_image_folders", "timeline_image_mode",
        "timeline_image_every_n", "timeline_image_share_percent",
        "timeline_image_min_video_gap", "timeline_image_duration_mode",
        "timeline_image_duration", "timeline_image_duration_min",
        "timeline_image_duration_max", "timeline_image_motion",
        "timeline_image_effect", "timeline_image_effect_intensity",
        "timeline_image_flicker_speed", "global_tv_effect",
        "global_tv_effect_intensity", "global_tv_flicker_speed",
        "shorts_image_mode", "shorts_image_every_n", "shorts_image_share_percent",
        "shorts_image_min_video_gap", "shorts_image_duration_mode",
        "shorts_image_duration", "shorts_image_duration_min",
        "shorts_image_duration_max", "shorts_image_motion", "shorts_image_effect",
        "shorts_image_effect_intensity", "shorts_image_flicker_speed",
        "shorts_global_tv_effect", "shorts_global_tv_effect_intensity",
        "shorts_global_tv_flicker_speed",
    ):
        assert key in data, key
        assert key in fields, key


def test_filter_chains_deterministic_and_scoped():
    chain1 = it.tv_effect_chain("vhs", 20, "normal", 1080, scope="image")
    chain2 = it.tv_effect_chain("vhs", 20, "normal", 1080, scope="image")
    assert chain1 == chain2
    assert it.tv_effect_chain("off", 100, "fast", 1080) == ""
    assert it.tv_effect_chain("vhs", 0, "normal", 1080) == ""
    assert it.motion_chain("none", 1920, 1080, 2.5) == ""
    assert "scale=w=" in it.motion_chain("pan", 720, 1280, 2.0)
