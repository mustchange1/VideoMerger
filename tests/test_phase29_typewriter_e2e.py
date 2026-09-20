"""Phase 29 e2e: Typewriter Hook Intro with REAL FFmpeg renders.

Verifies on actual encoded output (Long-Form AND Shorts):
* progressive typed text in the generated intro,
* deterministic typewriter SFX synced with the typing,
* the transition into the program (project transition system),
* exact final duration = program + intro - transition overlap,
* disabled/empty intros keep the historical render byte-identical,
* strict Long-Form / Shorts independence in combined exports,
* subtitle sidecars stay untouched,
* both music modes,
* intro asset cache reuse.
"""
from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.typewriter_intro import build_timeline, profile_from_settings
from app.video_merger.youtube_outputs import EXPORT_MODE_COMBINED, EXPORT_MODE_SHORTS
from tests.conftest import make_clip

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _isolated_project_root(tmp_path, monkeypatch):
    """Intro asset cache and every project_root() consumer stay in tmp."""
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


def _samples(ffmpeg: Path, media: Path, start: float, duration: float = .20) -> list[float]:
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", media,
        "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1",
    ])
    values = array.array("f")
    values.frombytes(raw)
    return list(values)


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def _frequency_strength(values: list[float], frequency: float, sample_rate: int = 16000) -> float:
    sine = sum(value * math.sin(2 * math.pi * frequency * index / sample_rate) for index, value in enumerate(values))
    cosine = sum(value * math.cos(2 * math.pi * frequency * index / sample_rate) for index, value in enumerate(values))
    return math.sqrt(sine * sine + cosine * cosine) / max(1, len(values))


def _frame_rgb(ffmpeg: Path, media: Path, time: float, width: int, height: int) -> bytes:
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{time:.4f}", "-i", media,
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    expected = width * height * 3
    assert len(raw) >= expected, f"frame decode failed at {time:.3f}s"
    return raw[:expected]


def _bright_ratio(frame: bytes) -> float:
    """Share of clearly non-background pixels (the typed white text)."""
    bright = 0
    for index in range(0, len(frame), 3):
        if frame[index] + frame[index + 1] + frame[index + 2] > 160:
            bright += 1
    return bright / (len(frame) // 3)


def _mean_abs_diff(frame_a: bytes, frame_b: bytes) -> float:
    total = sum(abs(a - b) for a, b in zip(frame_a[::16], frame_b[::16]))
    return total / max(1, len(frame_a[::16]))


def _probe_duration(ffprobe: Path, media: Path) -> float:
    from app.video_merger.media_analyzer import MediaAnalyzer

    return MediaAnalyzer(ffprobe).analyze(media).duration


def _probe_size(ffprobe: Path, media: Path) -> tuple[int, int]:
    from app.video_merger.media_analyzer import MediaAnalyzer

    info = MediaAnalyzer(ffprobe).analyze(media)
    return int(info.width), int(info.height)


def _project_factory(tmp_path: Path, ffmpeg: Path, ffprobe: Path):
    """Real clips, real voiceover, fixture aligner - a minimal real project."""
    clip_a = tmp_path / "clip_a.mp4"
    clip_b = tmp_path / "clip_b.mp4"
    make_clip(ffmpeg, clip_a, size="320x180", duration=0.7, color="red", audio_rate=None)
    make_clip(ffmpeg, clip_b, size="320x180", duration=0.7, color="blue", audio_rate=None)

    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, 850, 1.2, .65)
    script = tmp_path / "script.txt"
    script.write_text("The hook types itself before this sentence is spoken.", encoding="utf-8")
    timings = [
        ("The", .05, .15), ("hook", .17, .30), ("types", .32, .45),
        ("itself", .47, .62), ("before", .64, .78), ("this", .80, .90),
        ("sentence", .92, 1.05), ("is", 1.07, 1.12), ("spoken", 1.13, 1.19),
    ]

    def recognize(path: Path, _language: str):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    aligner = LocalWordAligner("phase29-fixture", recognize, cache_dir=tmp_path / "align-cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip_a, clip_b])
    return engine, media, aligner, voice, script


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


HOOK = {"typewriter_intro_enabled": True, "typewriter_hook_text": "Why?\nWatch this.",
        "typewriter_speed": "fast", "typewriter_hold_seconds": 0.3,
        "typewriter_sound_volume": 100, "typewriter_transition": "project"}


def test_disabled_intro_keeps_the_historical_render_byte_identical(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)

    historical = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "out_hist", aligner=aligner,
    )
    assert historical.video.is_file() and historical.report.ok

    explicit_off = dict(
        typewriter_intro_enabled=False, typewriter_hook_text="Never rendered",
        typewriter_speed="slow", typewriter_sound_preset="off",
        short_typewriter_intro_enabled=False, short_typewriter_hook_text="Nope",
    )
    disabled = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **explicit_off), tmp_path / "out_off", aligner=aligner,
    )
    assert disabled.video.is_file() and disabled.report.ok
    assert disabled.video.read_bytes() == historical.video.read_bytes()


def test_empty_hook_text_with_enabled_flag_behaves_disabled(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)

    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "out_base", aligner=aligner,
    )
    empty_enabled = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, typewriter_intro_enabled=True,
                              typewriter_hook_text="   \n  "),
        tmp_path / "out_empty", aligner=aligner,
    )
    assert empty_enabled.video.read_bytes() == baseline.video.read_bytes()


def test_long_form_real_render_progressive_text_synced_sfx_and_transition(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)

    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script), tmp_path / "out_base", aligner=aligner,
    )
    program_duration = _probe_duration(ffprobe, baseline.video)

    settings = _base_settings(voice, script, **HOOK)
    logs: list[str] = []
    result = MainProjectEngine(engine).create_main(
        media, settings, tmp_path / "out_intro", aligner=aligner, log=logs.append,
    )
    assert result.video.is_file() and result.report.ok
    assert any("Typewriter Hook Intro" in line for line in logs)

    profile = profile_from_settings(settings)
    timeline = build_timeline(profile)
    intro = timeline.total_duration
    td = 0.15
    final_duration = _probe_duration(ffprobe, result.video)
    assert final_duration == pytest.approx(program_duration + intro - td, abs=0.15)

    # --- progressive text: the visible text mass grows while typing -------
    width, height = _probe_size(ffprobe, result.video)
    early = _frame_rgb(ffmpeg, result.video, timeline.typing_end * 0.25, width, height)
    late = _frame_rgb(ffmpeg, result.video, timeline.typing_end * 0.95, width, height)
    # Sample INSIDE the hold (before the transition starts at intro - td).
    hold_time = timeline.typing_end + min(0.05, profile.hold_seconds * 0.4)
    hold_frame = _frame_rgb(ffmpeg, result.video, hold_time, width, height)
    early_ratio, late_ratio = _bright_ratio(early), _bright_ratio(late)
    assert early_ratio > 0.0005, "no typed text visible early in the intro"
    assert late_ratio > early_ratio * 1.25, (
        f"text must grow while typing: early={early_ratio:.4f} late={late_ratio:.4f}"
    )
    assert _bright_ratio(hold_frame) == pytest.approx(late_ratio, abs=0.02)

    # --- synced typewriter SFX: audible clicks while typing, silent hold --
    typing_window = _samples(ffmpeg, result.video, 0.03, max(0.2, timeline.typing_end - 0.1))
    # Strictly inside the silent hold: the acrossfade begins at intro - td.
    hold_window = _samples(
        ffmpeg, result.video, timeline.typing_end + 0.02,
        max(0.05, profile.hold_seconds - td - 0.04),
    )
    assert _rms(typing_window) > 3 * max(1e-5, _rms(hold_window)), "typing sounds missing"
    assert _rms(hold_window) < 0.01, "hold must be silent (no residual sound)"

    # --- the voiceover starts only after the intro ------------------------
    intro_window = _samples(ffmpeg, result.video, 0.03, max(0.2, intro - 0.1))
    assert _frequency_strength(intro_window, 850) < 0.005
    voice_window = _samples(ffmpeg, result.video, intro - td + 0.35, 0.3)
    assert _frequency_strength(voice_window, 850) > 0.02

    # --- transition: blend between intro and program, clean program after -
    blend = _frame_rgb(ffmpeg, result.video, intro - td / 2, width, height)
    program_first = _frame_rgb(ffmpeg, baseline.video, 0.02, width, height)
    assert _mean_abs_diff(blend, hold_frame) > 8.0, "no visible transition blend"
    assert _mean_abs_diff(blend, program_first) > 8.0
    after = _frame_rgb(ffmpeg, result.video, intro - td + 0.25, width, height)
    program_at = _frame_rgb(ffmpeg, baseline.video, 0.25, width, height)
    assert _mean_abs_diff(after, program_at) < 10.0, "program not clean after intro"

    # --- no clip replay at the end -----------------------------------------
    end_introed = _frame_rgb(ffmpeg, result.video, final_duration - 0.15, width, height)
    end_baseline = _frame_rgb(ffmpeg, baseline.video, program_duration - 0.15, width, height)
    assert _mean_abs_diff(end_introed, end_baseline) < 10.0


def test_shorts_real_render_with_own_profile(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)

    def short_settings(**overrides):
        values = dict(
            export_mode=EXPORT_MODE_SHORTS, aspect="9:16", resolution="180x320",
            encoding="CPU", preset="fast", crf=24, normalize_audio=False,
            voiceover_paths=[str(voice)],
            original_audio_mode="mute", final_pause=0.2, subtitle_enabled=False,
            transition_type="cross_dissolve", transition_duration=0.12,
            short_intro_seconds=0.0, short_outro_seconds=0.0,
            long_form_intro_seconds=0.0, long_form_outro_seconds=0.0,
        )
        values.update(overrides)
        return ExportSettings(**values)

    baseline = MainProjectEngine(engine).create_youtube_exports(
        media, short_settings(), tmp_path / "shorts_base", aligner=aligner,
    )
    base_video = baseline.shorts[0].video
    program_duration = _probe_duration(ffprobe, base_video)

    intro_overrides = {
        "short_typewriter_intro_enabled": True,
        "short_typewriter_hook_text": "POV:\nYou found this",
        "short_typewriter_speed": "fast",
        "short_typewriter_hold_seconds": 0.3,
        "short_typewriter_sound_volume": 100,
        "short_typewriter_position": "Upper-Middle",
    }
    result = MainProjectEngine(engine).create_youtube_exports(
        media, short_settings(**intro_overrides), tmp_path / "shorts_intro", aligner=aligner,
    )
    intro_video = result.shorts[0].video
    assert intro_video.is_file()

    # Resolve exactly the Shorts profile fields (the orchestrator maps them
    # 1:1 onto each Short job's canonical fields, covered by the unit tests).
    profile = profile_from_settings(short_settings(**intro_overrides), short=True)
    timeline = build_timeline(profile)
    intro = timeline.total_duration
    final_duration = _probe_duration(ffprobe, intro_video)
    assert final_duration == pytest.approx(program_duration + intro - 0.12, abs=0.2)
    assert profile.position == "Upper-Middle"

    # Vertical geometry: 9:16 frame, typed text visible. The Shorts
    # orchestrator enforces the vertical YouTube preset, so probe the real
    # encoded size instead of assuming the requested resolution.
    short_w, short_h = _probe_size(ffprobe, intro_video)
    assert short_h > short_w, "Shorts output must stay vertical"
    early = _frame_rgb(ffmpeg, intro_video, timeline.typing_end * 0.3, short_w, short_h)
    late = _frame_rgb(ffmpeg, intro_video, timeline.typing_end * 0.95, short_w, short_h)
    assert _bright_ratio(late) > _bright_ratio(early) > 0.0005

    # Hold frame keeps the fully typed text, then the transition blends into
    # the program, and after it NO intro overlay may remain.
    td = 0.12
    hold_frame = _frame_rgb(
        ffmpeg, intro_video, timeline.typing_end + min(0.04, profile.hold_seconds * 0.3),
        short_w, short_h,
    )
    assert _bright_ratio(hold_frame) == pytest.approx(_bright_ratio(late), abs=0.01)
    blend = _frame_rgb(ffmpeg, intro_video, intro - td / 2, short_w, short_h)
    assert _mean_abs_diff(blend, hold_frame) > 8.0, "no visible Shorts transition blend"
    after = _frame_rgb(ffmpeg, intro_video, intro - td + 0.2, short_w, short_h)
    base_ref = _frame_rgb(ffmpeg, base_video, 0.2, short_w, short_h)
    assert _mean_abs_diff(after, base_ref) < 12.0, (
        "Shorts program not clean after intro (residual intro overlay)"
    )

    # Synced typewriter SFX inside the intro, silence in the hold.
    typing_window = _samples(ffmpeg, intro_video, 0.03, max(0.2, timeline.typing_end - 0.1))
    assert _rms(typing_window) > 0.005
    hold_audio = _samples(
        ffmpeg, intro_video, timeline.typing_end + 0.02,
        max(0.05, profile.hold_seconds - td - 0.04),
    )
    assert _rms(typing_window) > 2 * max(1e-5, _rms(hold_audio))


def test_combined_export_profiles_never_leak(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)

    def combined(**overrides):
        values = dict(
            export_mode=EXPORT_MODE_COMBINED,
            resolution="320x180", encoding="CPU", preset="fast", crf=24,
            normalize_audio=False, voiceover_paths=[str(voice)],
            original_audio_mode="mute", final_pause=0.2,
            subtitle_enabled=False, transition_type="cross_dissolve",
            transition_duration=0.12, short_intro_seconds=0.0, short_outro_seconds=0.0,
            long_form_intro_seconds=0.0, long_form_outro_seconds=0.0,
        )
        values.update(overrides)
        return ExportSettings(**values)

    baseline = MainProjectEngine(engine).create_youtube_exports(
        media, combined(), tmp_path / "comb_base", aligner=aligner,
    )
    short_only = MainProjectEngine(engine).create_youtube_exports(
        media,
        combined(short_typewriter_intro_enabled=True,
                 short_typewriter_hook_text="Short hook",
                 short_typewriter_speed="fast", short_typewriter_hold_seconds=0.2,
                 typewriter_hook_text="LF hook that must NOT render"),
        tmp_path / "comb_short", aligner=aligner,
    )
    # The Long-Form output stays byte-identical: the Shorts-only intro and the
    # (disabled) Long-Form hook text change nothing there.
    assert short_only.long_form.video.read_bytes() == baseline.long_form.video.read_bytes()
    # The Short carries its own intro.
    base_short_duration = _probe_duration(ffprobe, baseline.shorts[0].video)
    intro_short_duration = _probe_duration(ffprobe, short_only.shorts[0].video)
    assert intro_short_duration > base_short_duration + 0.3


def test_subtitle_sidecars_and_alignment_stay_untouched(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)
    subtitle_settings = dict(
        subtitle_enabled=True, subtitle_language="English", subtitle_style="long_1",
    )
    baseline = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **subtitle_settings),
        tmp_path / "sub_base", aligner=aligner,
    )
    introed = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **subtitle_settings, **HOOK),
        tmp_path / "sub_intro", aligner=aligner,
    )
    # The subtitle sidecars describe the spoken program and never change.
    assert introed.srt.read_bytes() == baseline.srt.read_bytes()
    assert introed.vtt.read_bytes() == baseline.vtt.read_bytes()
    # Burned output exists and is longer by intro - transition.
    profile = profile_from_settings(_base_settings(voice, script, **HOOK))
    timeline = build_timeline(profile)
    base_duration = _probe_duration(ffprobe, baseline.video)
    intro_duration = _probe_duration(ffprobe, introed.video)
    assert intro_duration == pytest.approx(base_duration + timeline.total_duration - 0.15, abs=0.2)


def test_music_modes_during_intro(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)
    music = tmp_path / "music.wav"
    _tone(ffmpeg, music, 300, 30.0, 8.0)  # full-scale sine (lavfi sine is quiet)

    def with_music(**overrides):
        return _base_settings(
            voice, script, music_path=str(music), music_volume=60,
            normalize_audio=False, **overrides,
        )

    default_result = MainProjectEngine(engine).create_main(
        media, with_music(**HOOK), tmp_path / "music_default", aligner=aligner,
    )
    profile = profile_from_settings(with_music(**HOOK))
    timeline = build_timeline(profile)
    intro_window = _samples(ffmpeg, default_result.video, 0.03, max(0.25, timeline.typing_end - 0.1))
    assert _frequency_strength(intro_window, 300) < 0.008, (
        "default mode: music must start with the main video, not under the intro"
    )

    continue_result = MainProjectEngine(engine).create_main(
        media, with_music(typewriter_music_mode="continue_during_intro", **HOOK),
        tmp_path / "music_continue", aligner=aligner,
    )
    continue_window = _samples(ffmpeg, continue_result.video, 0.03, max(0.25, timeline.typing_end - 0.1))
    assert _frequency_strength(continue_window, 300) > 0.02, (
        "continue mode: music must already play under the intro"
    )


def test_intro_asset_cache_reused_between_renders(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    engine, media, aligner, voice, script = _project_factory(tmp_path, ffmpeg, ffprobe)

    def asset_files() -> list[Path]:
        cache_root = Path("cache") / "typewriter"
        try:
            from app.video_merger.paths import project_root

            cache_root = project_root() / "cache" / "typewriter"
        except Exception:
            pass
        return sorted(cache_root.rglob("*")) if cache_root.is_dir() else []

    before = asset_files()
    first = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **HOOK), tmp_path / "cache_a", aligner=aligner,
    )
    after_first = asset_files()
    assert len(after_first) > len(before), "intro assets must be cached"
    second = MainProjectEngine(engine).create_main(
        media, _base_settings(voice, script, **HOOK), tmp_path / "cache_b", aligner=aligner,
    )
    after_second = asset_files()
    assert after_second == after_first, "identical intro must reuse the cached asset"
    assert first.video.is_file() and second.video.is_file()
