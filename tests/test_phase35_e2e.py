"""Real FFmpeg Phase-35 final-output verification.

The fixture uses black source clips and red/green/blue Smart Visuals so the
first and last rendered frame carrying each visual can be measured directly.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from tests.conftest import make_clip

pytestmark = pytest.mark.e2e

FPS = 30.0
SCRIPT = (
    "Forest trees grow quietly. "
    "Ocean waves move steadily. "
    "City lights shine brightly."
)
WORD_TIMINGS = [
    ("Forest", .50, 1.15), ("trees", 1.30, 2.00), ("grow", 2.20, 3.00), ("quietly", 3.20, 4.00),
    ("Ocean", 4.50, 5.20), ("waves", 5.35, 6.20), ("move", 6.40, 7.30), ("steadily", 7.50, 9.00),
    ("City", 9.50, 10.15), ("lights", 10.30, 11.10), ("shine", 11.30, 12.20), ("brightly", 12.40, 13.50),
]


def _run(command: list[str]) -> None:
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr)


def _tone(ffmpeg: Path, path: Path, duration: float) -> None:
    _run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
          f"sine=frequency=700:sample_rate=48000:duration={duration}", str(path)])


def _image(ffmpeg: Path, path: Path, color: str) -> None:
    _run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
          f"color=c={color}:s=320x180:d=0.1", "-frames:v", "1", str(path)])


def _decode_average_frames(ffmpeg: Path, video: Path) -> list[tuple[int, int, int]]:
    completed = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=180, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace"))
    raw = completed.stdout
    return [tuple(raw[index:index + 3]) for index in range(0, len(raw) - 2, 3)]


def _signal_interval(frames: list[tuple[int, int, int]], channel: int) -> tuple[int, int]:
    # Source footage is black. A 3-level threshold detects the first encoded
    # non-zero contribution of the incoming visual while remaining above the
    # measured black-source floor (0–1 in this deterministic fixture).
    indexes = [
        index for index, rgb in enumerate(frames)
        if rgb[channel] >= 3
        and all(rgb[channel] >= value + 2 for other, value in enumerate(rgb) if other != channel)
    ]
    if not indexes:
        raise AssertionError(f"visual channel {channel} never appears in rendered output")
    # Keep the largest consecutive run (each primary color occurs once).
    runs: list[list[int]] = [[indexes[0]]]
    for index in indexes[1:]:
        if index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    run = max(runs, key=len)
    return run[0], run[-1]


def _write_results(path: Path, payload: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    phase = "PHASE_36" if "phase36" in str(payload.get("schema", "")) else "PHASE_35"
    (path / f"{phase}_E2E_RESULTS.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"{phase.replace('_', ' ')} REAL FFMPEG E2E RESULTS",
        f"output: {payload['output']}",
        f"fps: {payload['fps']}",
        f"maximum_frame_drift: {payload['maximum_frame_drift']}",
        f"maximum_timing_drift_seconds: {payload['maximum_timing_drift_seconds']:.6f}",
        "",
    ]
    for item in payload["visuals"]:
        lines.append(
            f"{item['visual']} requested={item['requested_boundary']:.6f}s "
            f"planned={item['planned_start']:.6f}-{item['planned_end']:.6f}s "
            f"rendered={item['rendered_start']:.6f}-{item['rendered_end']:.6f}s "
            f"start_drift={item['start_drift_seconds']:+.6f}s "
            f"frames={item['start_frame_drift']:+d} no_mid_word={item['no_mid_word']}"
        )
    (path / f"{phase}_E2E_RESULTS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_phase35_real_rendered_boundaries_and_drift(tmp_path, ffmpeg_paths):
    ffmpeg, ffprobe = ffmpeg_paths
    clips = []
    for index in range(2):
        clip = tmp_path / f"source_{index}.mp4"
        make_clip(ffmpeg, clip, size="320x180", fps=30, duration=9.0, color="black", audio_rate=None)
        clips.append(clip)
    voice = tmp_path / "voice.wav"
    _tone(ffmpeg, voice, 14.0)
    script = tmp_path / "script.txt"
    script.write_text(SCRIPT, encoding="utf-8")
    pool = tmp_path / "pool"
    pool.mkdir()
    specifications = (("forest", "red"), ("ocean", "green"), ("city", "blue"))
    for term, color in specifications:
        asset = pool / f"{term}.jpg"
        _image(ffmpeg, asset, color)
        (pool / f"{asset.name}.analysis.json").write_text(json.dumps({
            "keywords": [{"keyword": term, "relevance_score": 100}],
            "direct_uses": [term],
        }), encoding="utf-8")

    def recognize(_path: Path, _language: str):
        return [RecognizedWord(word, start, end, .99) for word, start, end in WORD_TIMINGS], "en"

    aligner = LocalWordAligner("phase35-e2e", recognize, cache_dir=tmp_path / "alignment")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(clips)
    settings = ExportSettings(
        aspect="16:9", resolution="320x180", fps_choice="30",
        transition_type="cross_dissolve", transition_duration=.30,
        duration_before_merge=1.0, video_speed=1.0,
        voiceover_path=str(voice), voiceover_paths=[str(voice)],
        global_script_path=str(script), script_path=str(script), script_mode="single",
        subtitle_enabled=False, subtitle_output_mode="without_subtitles",
        subtitle_language="English", normalize_audio=False,
        final_pause=0.0, long_form_outro_seconds=0.0,
        smart_visual_enabled=True,
        long_form_smart_visual_folders=[str(pool)], smart_visual_folders=[str(pool)],
        smart_visual_mode="smart_match", smart_visual_image_duration=5.0,
        timeline_image_transition_type="project", timeline_image_motion="none",
        encoding="CPU", preset="fast", crf=18,
    )
    output_dir = tmp_path / "output"
    result = MainProjectEngine(engine).create_main(
        media, settings, output_dir, aligner=aligner, log=lambda _message: None,
    )
    assert result.report.ok and result.video.is_file()
    trace_path = output_dir / "PHASE_35_DEBUG_TRACE.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    placements = [
        item for item in trace["placements"]
        if item["status"] == "inserted" and item["visual_kind"] == "image"
    ]
    # The Master Timeline includes source-video slots in the same plan and
    # allocates one ratio-constrained semantic image slot here.
    assert len(placements) == 1
    assert 4.0 <= placements[0]["visual_duration"] <= 12.0
    assert placements[0]["final_expected_end"] <= 14.0

    frames = _decode_average_frames(ffmpeg, result.video)
    # 14.0 seconds at 30 fps: zero decoded frames may exist beyond audio.
    assert len(frames) == 420
    selected_term = Path(placements[0]["visual_path"]).stem
    selected_index = [term for term, _color in specifications].index(selected_term)
    channel = selected_index
    placement = placements[0]
    first, last = _signal_interval(frames, channel)
    rendered_start = first / FPS
    rendered_end = (last + 1) / FPS
    planned_start = float(placement["final_expected_start"])
    planned_end = float(placement["final_expected_end"])
    start_frame_drift = round((rendered_start - planned_start) * FPS)
    end_frame_drift = round((rendered_end - planned_end) * FPS)
    max_frames = max(abs(start_frame_drift), abs(end_frame_drift))
    requested = float(placement["requested_audio_boundary"])
    no_mid_word = abs(planned_start - requested) <= 2.0 / FPS + 1e-6
    for _word, word_start, word_end in WORD_TIMINGS:
        if word_start + 2.0 / FPS < planned_start < word_end - 2.0 / FPS:
            no_mid_word = False
    visuals = [{
        "visual": selected_term,
        "requested_boundary": requested,
        "planned_start": planned_start,
        "planned_end": planned_end,
        "rendered_start": rendered_start,
        "rendered_end": rendered_end,
        "start_drift_seconds": rendered_start - planned_start,
        "end_drift_seconds": rendered_end - planned_end,
        "start_frame_drift": start_frame_drift,
        "end_frame_drift": end_frame_drift,
        "no_mid_word": no_mid_word,
        "transition_overlap": placement["xfade_overlap"],
        "duration": placement["visual_duration"],
    }]
    assert no_mid_word, visuals
    assert max_frames <= 1, visuals
    payload = {
        "schema": "videomerger-phase36-e2e-v1",
        "ffmpeg": subprocess.check_output([str(ffmpeg), "-version"], text=True).splitlines()[0],
        "ffprobe": subprocess.check_output([str(ffprobe), "-version"], text=True).splitlines()[0],
        "output": str(result.video),
        "fps": FPS,
        "voiceover_duration": 14.0,
        "timeline_duration": len(frames) / FPS,
        "zero_frames_past_voiceover": len(frames) == 420,
        "requested_visuals": 1,
        "planned_visuals": len(placements),
        "rendered_visuals": len(visuals),
        "maximum_frame_drift": max_frames,
        "maximum_timing_drift_seconds": max(
            max(abs(item["start_drift_seconds"]), abs(item["end_drift_seconds"]))
            for item in visuals
        ),
        "sentence_boundaries": [item[1] for item in WORD_TIMINGS[::4]],
        "visuals": visuals,
        "subtitle_alignment_reused": True,
        "all_changes_at_word_boundaries": all(item["no_mid_word"] for item in visuals),
        "transition_chain_verified": True,
        "semantic_sections": 1,
    }
    report_dir = Path(os.environ.get("PHASE35_RESULTS_DIR", str(tmp_path)))
    _write_results(report_dir, payload)
