from __future__ import annotations

import json
import subprocess

import pytest

from app.video_merger.engine import VideoMergerEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from tests.conftest import make_clip


def _run(command):
    result = subprocess.run(
        command, capture_output=True, timeout=120,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _center_rgb(ffmpeg, output, at):
    raw = _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(output),
        "-frames:v", "1", "-vf", "crop=1:1:80:45,format=rgb24", "-f", "rawvideo", "pipe:1",
    ])
    assert len(raw) == 3
    return tuple(raw)


@pytest.mark.e2e
@pytest.mark.parametrize(
    "transition",
    ["smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"],
)
def test_all_four_transitions_render_with_synced_aac_audio(ffmpeg_paths, tmp_path, transition):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / f"inputs_{transition}"
    make_clip(ffmpeg, folder / "01_red.mp4", duration=0.8, color="red", audio_rate=44100)
    make_clip(ffmpeg, folder / "02_blue.mp4", duration=0.8, color="blue", audio_rate=48000)

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([folder / "01_red.mp4", folder / "02_blue.mp4"])
    settings = ExportSettings(
        aspect="16:9", resolution="160x90", transition_type=transition,
        transition_ease="ease_in_out", transition_duration=0.3,
        encoding="CPU", crf=26, preset="fast", normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    output = tmp_path / f"render_{transition}.mp4"
    report = engine.export(media, settings, resolved, output)
    assert report.ok and report.has_audio
    # ExportSettings intentionally defaults duration_before_merge to 0.70.
    # Direct engine callers pass raw media, so export() applies that canonical
    # pre-merge playback rate before rebuilding transition geometry. Each
    # 0.8 s clip therefore occupies 0.8 / 0.70 s and the 0.3 s overlap is
    # subtracted once: 2 * (0.8 / 0.70) - 0.3 = 1.985714 s.
    expected_duration = 2 * (0.8 / settings.duration_before_merge) - 0.3
    assert report.duration == pytest.approx(expected_duration, abs=0.08)

    probe = json.loads(_run([
        str(ffprobe), "-v", "error", "-show_streams", "-of", "json", str(output)
    ]).decode("utf-8"))
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert audio["codec_name"] == "aac"
    assert int(audio["sample_rate"]) == 48000
    assert audio["channels"] == 2


@pytest.mark.e2e
def test_cross_dissolve_has_continuous_professional_ab_blend_without_flash(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "cross_visual"
    make_clip(ffmpeg, folder / "01_red.mp4", duration=0.8, color="red")
    make_clip(ffmpeg, folder / "02_blue.mp4", duration=0.8, color="blue")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([folder / "01_red.mp4", folder / "02_blue.mp4"])
    settings = ExportSettings(
        resolution="160x90", transition_type="cross_dissolve",
        transition_ease="ease_in_out", transition_duration=0.4,
        encoding="CPU", crf=20, preset="fast", normalize_audio=False,
    )
    resolved = engine.make_plan(media, settings)
    graph = engine.builder.build_filter_graph(media, settings, resolved)
    assert "xfade=transition=custom" in graph
    assert "gblur=" not in graph
    output = tmp_path / "cross_visual.mp4"
    assert engine.export(media, settings, resolved, output).ok

    # The authoritative default duration_before_merge=0.70 stretches each
    # raw 0.8 s clip to 1.142857 s. Export rebuilds the plan, so this 0.4 s
    # transition spans 0.742857..1.142857 s rather than the stale raw-clock
    # 0.4..0.8 s window. Sample that rendered window to verify the actual A→B
    # blend and rule out a white flash or black gap.
    assert "offset=0.742857" in engine.last_filter_graph
    before = _center_rgb(ffmpeg, output, 0.76)
    middle = _center_rgb(ffmpeg, output, 0.94)
    after = _center_rgb(ffmpeg, output, 1.12)
    assert before[0] > before[2]
    assert after[2] > after[0]
    assert middle[0] > 45 and middle[2] > 45
    assert middle[1] < 80 and max(middle) < 210 and sum(middle) > 120
    assert before != middle != after
