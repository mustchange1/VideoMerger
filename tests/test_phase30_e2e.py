"""Phase 30 e2e: Image Timeline & Visual Effects with REAL FFmpeg renders.

Verifies on actual encoded output (Long-Form AND Shorts):
* disabled feature keeps the historical render byte-identical,
* images become genuine timeline elements between video clips (mixed chain),
* image duration/motion/image-only TV effect are visible in real frames,
* the image-only effect never touches video sections,
* the global TV overlay changes the whole program exactly once, audio copied,
* transitions work at video<->image boundaries,
* subtitles keep their exact timing over image sections,
* music continues across image boundaries,
* Long-Form and Shorts profiles are strictly independent,
* identical settings produce identical renders (determinism).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.youtube_outputs import EXPORT_MODE_SHORTS
from tests.conftest import make_clip

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _isolated_project_root(tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    yield


def _run(command: list[object], timeout: int = 300) -> bytes:
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _tone(ffmpeg: Path, path: Path, frequency: int, duration: float, volume: float = .55) -> None:
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=f={frequency}:r=48000:d={duration}", "-af", f"volume={volume}",
        "-c:a", "pcm_s16le", path,
    ])


def _make_image(ffmpeg: Path, path: Path, color: str, size: str = "160x120") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"color=c={color}:s={size}:d=0.1", "-frames:v", "1", path,
    ])


def _probe_duration(ffprobe: Path, media: Path) -> float:
    from app.video_merger.media_analyzer import MediaAnalyzer

    return MediaAnalyzer(ffprobe).analyze(media).duration


def _probe_size(ffprobe: Path, media: Path) -> tuple[int, int]:
    from app.video_merger.media_analyzer import MediaAnalyzer

    info = MediaAnalyzer(ffprobe).analyze(media)
    return int(info.width), int(info.height)


def _frame_rgb(ffmpeg: Path, media: Path, time: float, width: int, height: int) -> bytes:
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{time:.4f}", "-i", media,
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    expected = width * height * 3
    assert len(raw) >= expected, f"frame decode failed at {time:.3f}s"
    return raw[:expected]


def _mean_abs_diff(frame_a: bytes, frame_b: bytes) -> float:
    total = sum(abs(a - b) for a, b in zip(frame_a[::16], frame_b[::16]))
    return total / max(1, len(frame_a[::16]))


def _color_ratio(frame: bytes, rgb: tuple[int, int, int], tolerance: int = 60) -> float:
    """Share of pixels close to the given color (identifies the test image)."""
    hits = 0
    for index in range(0, len(frame), 3):
        if (
            abs(frame[index] - rgb[0]) <= tolerance
            and abs(frame[index + 1] - rgb[1]) <= tolerance
            and abs(frame[index + 2] - rgb[2]) <= tolerance
        ):
            hits += 1
    return hits / (len(frame) // 3)


def _is_video_color_frame(frame: bytes) -> bool:
    """True when a frame is dominated by one of the three synthetic clips."""

    def dominant(test) -> float:
        hits = 0
        for index in range(0, len(frame), 3):
            if test(frame[index], frame[index + 1], frame[index + 2]):
                hits += 1
        return hits / (len(frame) // 3)

    red = dominant(lambda r, g, b: r > 170 and g < 90 and b < 90)
    blue = dominant(lambda r, g, b: b > 140 and r < 110 and g < 110)
    green = dominant(lambda r, g, b: g > 90 and r < 110 and b < 110)
    return max(red, blue, green) > 0.5


def _find_windows(
    ffmpeg: Path, media: Path, width: int, height: int, duration: float,
    is_target, step: float = 0.12,
) -> list[tuple[float, float]]:
    """Scan the encoded program for sections matching the frame predicate."""
    windows: list[tuple[float, float]] = []
    start: float | None = None
    time = 0.05
    previous = -1.0
    while time < duration - 0.05:
        frame = _frame_rgb(ffmpeg, media, time, width, height)
        if is_target(frame) and start is None:
            start = time
        if not is_target(frame) and start is not None:
            windows.append((start, previous))
            start = None
        previous = time
        time += step
    if start is not None:
        windows.append((start, previous))
    return windows


def _solid_image_predicate(frame: bytes) -> bool:
    """Yellow OR magenta pool image (both colors of the test pool)."""
    return (
        _color_ratio(frame, IMAGE_YELLOW) > 0.55
        or _color_ratio(frame, IMAGE_MAGENTA) > 0.55
    )


def _pattern_image_predicate(frame: bytes) -> bool:
    """Textured testsrc image: not dominated by any synthetic video color."""
    return not _is_video_color_frame(frame)


def _project_factory(tmp_path: Path, ffmpeg: Path, ffprobe: Path, image_folders: bool = True):
    """Real clips, real voiceover, real image pool - a minimal real project."""
    clip_a = tmp_path / "clip_a.mp4"
    clip_b = tmp_path / "clip_b.mp4"
    clip_c = tmp_path / "clip_c.mp4"
    make_clip(ffmpeg, clip_a, size="320x180", duration=0.7, color="red", audio_rate=None)
    make_clip(ffmpeg, clip_b, size="320x180", duration=0.7, color="blue", audio_rate=None)
    make_clip(ffmpeg, clip_c, size="320x180", duration=0.7, color="green", audio_rate=None)

    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, 850, 2.0, .65)
    script = tmp_path / "script.txt"
    script.write_text("Images join the timeline now\n", encoding="utf-8")

    image_folder = tmp_path / "Bildmaterial Ü"
    if image_folders:
        _make_image(ffmpeg, image_folder / "Gelb 1.png", "yellow")
        _make_image(ffmpeg, image_folder / "Magenta 2.png", "magenta")
        (image_folder / "unsupported.gif").write_bytes(b"not an image")
        (image_folder / "notes.txt").write_text("ignored", encoding="utf-8")
    # Textured image pool for motion visibility: a uniform color cannot show
    # zoom/pan, so motion tests use a real pattern image.
    pattern_folder = tmp_path / "Muster"
    if image_folders:
        pattern_folder.mkdir(parents=True, exist_ok=True)
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=320x240:d=0.1", "-frames:v", "1", str(pattern_folder / "Muster 1.png"),
        ])

    timings = [
        ("Images", .05, .30), ("join", .32, .55), ("the", .57, .72),
        ("timeline", .74, 1.10), ("now", 1.12, 1.40),
    ]

    def recognize(path: Path, _language: str):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    aligner = LocalWordAligner("phase30-fixture", recognize, cache_dir=tmp_path / "align-cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip_a, clip_b, clip_c])
    return engine, media, aligner, voice, script, image_folder, pattern_folder


def _base_settings(voice: Path, script: Path, **overrides) -> ExportSettings:
    values = dict(
        resolution="320x180", encoding="CPU", preset="fast", crf=24, normalize_audio=False,
        transition_type="cross_dissolve", transition_duration=0.15,
        voiceover_path=str(voice), script_path=str(script),
        original_audio_mode="mute", final_pause=0.2,
        subtitle_enabled=False, subtitle_language="English",
    )
    values.update(overrides)
    return ExportSettings(**values)


IMAGE_YELLOW = (255, 255, 0)
IMAGE_MAGENTA = (255, 0, 255)


def _every_n_settings(image_folder: Path, **overrides) -> dict:
    values = dict(
        timeline_image_mode="every_n",
        timeline_image_every_n=1,
        timeline_image_min_video_gap=1,
        timeline_image_folders=[str(image_folder)],
        timeline_image_duration=1.2,
        timeline_image_motion="none",
    )
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# Disabled feature == historical behavior
# ---------------------------------------------------------------------------
def test_disabled_image_timeline_keeps_historical_render_byte_identical(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    historical = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "out_hist", aligner=aligner,
    )
    assert historical.video.is_file() and historical.report.ok

    # Configured folders but disabled mode, AND enabled mode with empty
    # folders: both must stay exactly the historical video-only render.
    for name, overrides in (
        ("folders_but_disabled", dict(
            timeline_image_folders=[str(image_folder)], timeline_image_mode="disabled",
            timeline_image_motion="ken_burns", timeline_image_effect="vhs",
            global_tv_effect="off",
        )),
        ("enabled_but_no_folders", dict(
            timeline_image_mode="every_n", timeline_image_every_n=1,
            timeline_image_folders=[],
        )),
    ):
        result = MainProjectEngine(engine).create_main(
            media, _base_settings(voice, script, **overrides), tmp_path / name, aligner=aligner,
        )
        assert result.video.is_file() and result.report.ok
        assert result.video.read_bytes() == historical.video.read_bytes(), name


# ---------------------------------------------------------------------------
# Long-Form: images as genuine timeline elements
# ---------------------------------------------------------------------------
def test_long_form_images_between_videos_duration_and_visual(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "out_base", aligner=aligner,
    )
    base_duration = _probe_duration(ffprobe, baseline.video)

    logs: list[str] = []
    result = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_every_n_settings(image_folder)),
        tmp_path / "out_images", aligner=aligner, log=logs.append,
    )
    assert result.video.is_file() and result.report.ok
    assert any("Bild-Timeline" in line for line in logs), "image insertion must be logged"

    # The mixed program is longer by the net image time.
    final_duration = _probe_duration(ffprobe, result.video)
    assert final_duration > base_duration + 1.0

    width, height = _probe_size(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, width, height, final_duration, _solid_image_predicate)
    assert len(windows) == 2, "two images between three clips (every_n=1, gap=1)"
    for start, end in windows:
        # The cross-dissolve blends shrink the clearly-detectable core by up
        # to one transition duration per side; the core never exceeds the
        # configured duration.
        assert 1.2 - 0.45 <= end - start <= 1.25, "configured image duration"

    # Video sections stay the real clips: before the first image the frame is
    # the red clip, identical to the historical render at the same timestamp.
    early = _frame_rgb(ffmpeg, result.video, 0.15, width, height)
    early_base = _frame_rgb(ffmpeg, baseline.video, 0.15, width, height)
    assert _mean_abs_diff(early, early_base) < 4.0


def test_long_form_zoom_motion_visible_in_real_frames(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    def render(name: str, motion: str) -> Path:
        result = MainProjectEngine(engine).create_main(
            media, _base_settings(
                voice, script, **_every_n_settings(pattern_folder, timeline_image_motion=motion),
            ),
            tmp_path / name, aligner=aligner,
        )
        assert result.video.is_file() and result.report.ok
        return result.video

    still = render("out_motion_none", "none")
    zoom = render("out_motion_zoom", "zoom_in")
    width, height = _probe_size(ffprobe, zoom)
    duration = _probe_duration(ffprobe, zoom)
    windows = _find_windows(ffmpeg, zoom, width, height, duration, _pattern_image_predicate)
    assert windows, "pattern image section must be visible"
    start, end = windows[0]
    mid = (start + end) / 2

    still_a = _frame_rgb(ffmpeg, still, mid - 0.25, width, height)
    still_b = _frame_rgb(ffmpeg, still, mid + 0.25, width, height)
    assert _mean_abs_diff(still_a, still_b) < 4.0, "motion=none keeps the image static"

    zoom_a = _frame_rgb(ffmpeg, zoom, mid - 0.25, width, height)
    zoom_b = _frame_rgb(ffmpeg, zoom, mid + 0.25, width, height)
    assert _mean_abs_diff(zoom_a, zoom_b) > 8.0, "slow zoom must change the frame over time"


def test_long_form_image_effect_only_touches_image_sections(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    plain = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_every_n_settings(image_folder)),
        tmp_path / "out_plain", aligner=aligner,
    )
    crt = MainProjectEngine(engine).create_main(
        media, _base_settings(
            voice, script,
            **_every_n_settings(image_folder, timeline_image_effect="crt_scanlines",
                                timeline_image_effect_intensity=60),
        ),
        tmp_path / "out_crt", aligner=aligner,
    )
    assert plain.video.is_file() and crt.video.is_file()
    width, height = _probe_size(ffprobe, crt.video)
    duration = _probe_duration(ffprobe, crt.video)
    windows = _find_windows(ffmpeg, crt.video, width, height, duration, _solid_image_predicate)
    assert windows
    mid = (windows[0][0] + windows[0][1]) / 2

    plain_frame = _frame_rgb(ffmpeg, plain.video, mid, width, height)
    crt_frame = _frame_rgb(ffmpeg, crt.video, mid, width, height)
    assert _mean_abs_diff(plain_frame, crt_frame) > 2.0, "image TV effect must be visible"

    # Video section BEFORE the first image: untouched by the image effect.
    early_plain = _frame_rgb(ffmpeg, plain.video, 0.15, width, height)
    early_crt = _frame_rgb(ffmpeg, crt.video, 0.15, width, height)
    assert _mean_abs_diff(early_plain, early_crt) < 2.0


def test_long_form_broadcast_effect_renders_on_images_and_as_overlay(ffmpeg_paths, tmp_path):
    """The time-based Broadcast Interference effect must render both scopes.

    Regression guard: the moving band is the only expression that depends on
    frame time inside ``geq`` (variable ``T``), so it is exercised here as an
    image-only effect AND as the global overlay in one real render.
    """
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    plain = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **_every_n_settings(image_folder)),
        tmp_path / "out_plain_b", aligner=aligner,
    )
    logs: list[str] = []
    result = MainProjectEngine(engine).create_main(
        media, _base_settings(
            voice, script,
            **_every_n_settings(image_folder, timeline_image_effect="broadcast",
                                timeline_image_effect_intensity=100,
                                timeline_image_flicker_speed="fast"),
            global_tv_effect="broadcast", global_tv_effect_intensity=100,
        ),
        tmp_path / "out_broadcast", aligner=aligner, log=logs.append,
    )
    assert result.video.is_file() and result.report.ok
    assert any("Global TV Overlay" in line for line in logs)
    width, height = _probe_size(ffprobe, result.video)
    duration = _probe_duration(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, width, height, duration, _solid_image_predicate)
    assert windows, "image sections survive the broadcast effect"
    mid = (windows[0][0] + windows[0][1]) / 2
    plain_frame = _frame_rgb(ffmpeg, plain.video, mid, width, height)
    broadcast_frame = _frame_rgb(ffmpeg, result.video, mid, width, height)
    assert _mean_abs_diff(plain_frame, broadcast_frame) > 2.0, "broadcast must visibly affect the image"
    # The overlay also changes a pure video frame (time-varying band + noise).
    plain_video = _frame_rgb(ffmpeg, plain.video, 0.15, width, height)
    overlay_video = _frame_rgb(ffmpeg, result.video, 0.15, width, height)
    assert _mean_abs_diff(plain_video, overlay_video) > 1.0, "global broadcast overlay must affect video frames"


def test_long_form_global_overlay_changes_program_and_copies_audio(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    plain = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "out_plain", aligner=aligner,
    )
    logs: list[str] = []
    overlay = MainProjectEngine(engine).create_main(
        media, _base_settings(
            voice, script, global_tv_effect="crt_scanlines", global_tv_effect_intensity=80,
        ),
        tmp_path / "out_global", aligner=aligner, log=logs.append,
    )
    assert overlay.video.is_file() and overlay.report.ok
    assert any("Global TV Overlay" in line for line in logs)

    width, height = _probe_size(ffprobe, overlay.video)
    plain_frame = _frame_rgb(ffmpeg, plain.video, 0.2, width, height)
    overlay_frame = _frame_rgb(ffmpeg, overlay.video, 0.2, width, height)
    assert _mean_abs_diff(plain_frame, overlay_frame) > 2.0, "global overlay must affect video frames"

    # Duration unchanged, audio stream copied byte-for-byte.
    assert _probe_duration(ffprobe, overlay.video) == pytest.approx(
        _probe_duration(ffprobe, plain.video), abs=0.05
    )
    plain_audio = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", plain.video,
                        "-vn", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"])
    overlay_audio = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", overlay.video,
                          "-vn", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"])
    assert plain_audio == overlay_audio, "the overlay pass must not re-encode or alter audio"


def test_long_form_video_image_transitions_and_determinism(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    settings = _base_settings(
        voice, script, transition_type="smooth_blur",
        **_every_n_settings(image_folder, timeline_image_motion="zoom_out"),
    )
    first = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "out_t1", aligner=aligner,
    )
    second = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "out_t2", aligner=aligner,
    )
    assert first.video.is_file() and first.report.ok
    assert second.video.is_file() and second.report.ok
    # smooth_blur at video<->image boundaries rendered without error and the
    # identical settings produced an identical program.
    assert first.video.read_bytes() == second.video.read_bytes()


def test_long_form_subtitles_keep_exact_timing_over_images(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    base = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, subtitle_enabled=True),
        tmp_path / "srt_base", aligner=aligner,
    )
    with_images = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, subtitle_enabled=True,
                              **_every_n_settings(image_folder)),
        tmp_path / "srt_images", aligner=aligner,
    )
    assert base.srt.is_file() and with_images.srt.is_file()
    assert with_images.srt.read_text(encoding="utf-8") == base.srt.read_text(encoding="utf-8")
    # The burned variant renders over image sections as well.
    assert with_images.video.stat().st_size > 0 and with_images.report.ok


def test_long_form_music_continues_across_image_boundaries(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    music = tmp_path / "music.wav"
    _tone(ffmpeg, music, 300, 8.0, .4)
    settings = _base_settings(
        voice, script, music_path=str(music), music_volume=60,
        **_every_n_settings(image_folder),
    )
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "out_music", aligner=aligner,
    )
    assert result.video.is_file() and result.report.ok
    width, height = _probe_size(ffprobe, result.video)
    duration = _probe_duration(ffprobe, result.video)
    windows = _find_windows(ffmpeg, result.video, width, height, duration, _solid_image_predicate)
    assert windows
    mid = (windows[0][0] + windows[0][1]) / 2
    # The music tone must be audible INSIDE the image section - no gap or
    # restart at the image boundary.
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
    assert music_strength > 0.005, "the music tone must keep playing across the image section"


# ---------------------------------------------------------------------------
# Shorts: strictly separate profile, same real-render guarantees
# ---------------------------------------------------------------------------
def _shorts_settings(voice: Path, script: Path, **overrides) -> ExportSettings:
    values = dict(
        export_mode=EXPORT_MODE_SHORTS,
        resolution="Auto", encoding="CPU", preset="fast", crf=24, normalize_audio=False,
        transition_type="cross_dissolve", transition_duration=0.12,
        voiceover_path=str(voice), script_path=str(script),
        original_audio_mode="mute", final_pause=0.2,
        subtitle_enabled=False, subtitle_language="English",
        short_intro_seconds=0.0, short_outro_seconds=0.0,
        long_form_intro_seconds=0.0, long_form_outro_seconds=0.0,
    )
    values.update(overrides)
    return ExportSettings(**values)


def test_shorts_disabled_profile_keeps_historical_render_byte_identical(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    baseline = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(voice, script), tmp_path / "shorts_base", aligner=aligner,
    )
    base_video = baseline.shorts[0].video

    # Long-Form image configuration must NEVER reach a Short.
    lf_only = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(
            voice, script,
            long_form_image_folders=[str(image_folder)],
            timeline_image_mode="every_n", timeline_image_every_n=1,
        ),
        tmp_path / "shorts_lf_only", aligner=aligner,
    )
    assert lf_only.shorts[0].video.read_bytes() == base_video.read_bytes()


def test_shorts_images_with_own_profile_and_geometry(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    baseline = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(voice, script), tmp_path / "shorts_base2", aligner=aligner,
    )
    base_duration = _probe_duration(ffprobe, baseline.shorts[0].video)

    result = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(
            voice, script,
            shorts_image_folders=[str(image_folder)],
            shorts_image_mode="every_n", shorts_image_every_n=1,
            shorts_image_min_video_gap=1,
            shorts_image_duration=1.0,
            shorts_image_motion="pan",
            shorts_image_effect="vhs", shorts_image_effect_intensity=35,
            # A Long-Form profile that must be ignored by the Short.
            timeline_image_mode="disabled",
            long_form_image_folders=[],
        ),
        tmp_path / "shorts_images", aligner=aligner,
    )
    short_video = result.shorts[0].video
    assert short_video.is_file()
    width, height = _probe_size(ffprobe, short_video)
    assert height > width, "Shorts must stay vertical"
    final_duration = _probe_duration(ffprobe, short_video)
    assert final_duration > base_duration + 0.8

    # The image appears in the vertical program (cover-fit fills the 9:16
    # frame, so the pool color dominates the whole frame).
    windows = _find_windows(ffmpeg, short_video, width, height, final_duration, _solid_image_predicate)
    assert windows, "image must be visible in the Shorts render"
    start, end = windows[0]
    assert 1.0 - 0.45 <= end - start <= 1.05

    # Pan motion must move the frame: the textured pattern pool renders once
    # static and once with pan; the first image section starts right after the
    # fitted first clip (1.0 s minus the 0.12 s transition overlap).
    shorts_motion_base = dict(
        shorts_image_folders=[str(pattern_folder)],
        shorts_image_mode="every_n", shorts_image_every_n=1,
        shorts_image_min_video_gap=1, shorts_image_duration=1.0,
    )
    motion_none = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(voice, script, **shorts_motion_base, shorts_image_motion="none"),
        tmp_path / "shorts_m_none", aligner=aligner,
    ).shorts[0].video
    motion_pan = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(voice, script, **shorts_motion_base, shorts_image_motion="pan"),
        tmp_path / "shorts_m_pan", aligner=aligner,
    ).shorts[0].video
    section_mid = 0.88 + 0.5
    frame_none = _frame_rgb(ffmpeg, motion_none, section_mid, width, height)
    frame_pan = _frame_rgb(ffmpeg, motion_pan, section_mid, width, height)
    assert _mean_abs_diff(frame_none, frame_pan) > 6.0, "pan motion must move the Shorts frame"


def test_shorts_uses_own_duration_not_long_form_value(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script, image_folder, pattern_folder = _project_factory(tmp_path, ffmpeg, ffprobe)

    # Long-Form duration 0.5 (would be tiny), Shorts duration 1.5 - the Short
    # must follow the Shorts value exclusively.
    result = MainProjectEngine(engine).create_youtube_exports(
        media, _shorts_settings(
            voice, script,
            shorts_image_folders=[str(image_folder)],
            shorts_image_mode="every_n", shorts_image_every_n=1,
            shorts_image_min_video_gap=1, shorts_image_duration=1.5,
            timeline_image_duration=0.5, timeline_image_mode="every_n",
            timeline_image_every_n=1, long_form_image_folders=[str(image_folder)],
        ),
        tmp_path / "shorts_dur", aligner=aligner,
    )
    short_video = result.shorts[0].video
    width, height = _probe_size(ffprobe, short_video)
    duration = _probe_duration(ffprobe, short_video)
    windows = _find_windows(ffmpeg, short_video, width, height, duration, _solid_image_predicate)
    assert windows
    start, end = windows[0]
    assert 1.5 - 0.45 <= end - start <= 1.55, "Shorts must use the Shorts duration"
