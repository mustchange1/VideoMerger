from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from .video_merger.diagnostics import run_diagnostics
from .video_merger.discovery import discover_videos
from .video_merger.engine import VideoMergerEngine
from .video_merger.main_project import MainProjectEngine
from .video_merger.models import ExportSettings
from .video_merger.paths import locate_ffmpeg, project_root
from .video_merger.platform_utils import hidden_process_flags, safe_subprocess_env


def _make_clip(ffmpeg: Path, path: Path, size: str, color: str, with_audio: bool) -> None:
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d=0.8"]
    if with_audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=0.8"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac"]
    command += [str(path)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, creationflags=hidden_process_flags(), env=safe_subprocess_env())
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def main() -> int:
    print("VideoMerger setup self-test")
    diagnostics = run_diagnostics(test_encoders=False)
    for item in diagnostics:
        print(f"  [{'OK' if item.ok else 'FAIL'}] {item.name}: {item.detail}")
    if not all(item.ok for item in diagnostics):
        return 1
    ffmpeg, ffprobe = locate_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="VideoMerger-Selftest-") as directory:
        root = Path(directory)
        inputs = root / "Eingabe ä mit Leerzeichen"
        inputs.mkdir()
        _make_clip(ffmpeg, inputs / "1 Test ä.mp4", "160x90", "red", True)
        _make_clip(ffmpeg, inputs / "2 Hochformat.mp4", "90x160", "blue", False)
        engine = VideoMergerEngine(ffmpeg, ffprobe)
        media = engine.analyze(discover_videos(inputs))
        settings = ExportSettings(
            aspect="9:16", resolution="90x160", transition_duration=0.2,
            background_blur=6, encoding="CPU", crf=28, preset="fast", normalize_audio=False,
        )
        resolved = engine.make_plan(media, settings)
        report = engine.export(media, settings, resolved, root / "selftest.mp4")
        if not report.ok:
            print("  [FAIL] FFmpeg End-to-End")
            return 1
        print(f"  [OK] FFmpeg End-to-End: {report.width}x{report.height}, {report.duration:.2f} s, Audio vorhanden")

        # 1.2.1 setup guard: exercise the actual Voiceover + Script -> local
        # word timing -> SRT/VTT -> single-pass burn-in -> final-frame workflow.
        evidence = project_root() / "test_evidence" / "1.2.1" / "subtitle_workflow" / "assets"
        known_voice = evidence / "KnownVoiceover.wav"
        known_script = evidence / "script.txt"
        known_background = evidence / "background.mp4"
        if not all(path.is_file() for path in (known_voice, known_script, known_background)):
            print("  [FAIL] Subtitle End-to-End: release evidence fixture missing")
            return 1
        subtitle_media = engine.analyze([known_background])
        subtitle_settings = ExportSettings(
            resolution="320x180", encoding="CPU", preset="fast", crf=28,
            normalize_audio=False, voiceover_path=str(known_voice), script_path=str(known_script),
            subtitle_enabled=False, subtitle_language="English", subtitle_model="small",
            subtitle_style="long_1", allow_alignment_warnings=True, final_pause=.5,
        )
        subtitle_result = MainProjectEngine(engine).create_main(
            subtitle_media, subtitle_settings, root / "subtitle-selftest"
        )
        subtitle_files = [
            subtitle_result.video, subtitle_result.srt, subtitle_result.vtt,
            subtitle_result.canonical_timeline,
            subtitle_result.video_no_subtitles,
            *subtitle_result.verification_frames,
        ]
        if not all(path and Path(path).is_file() for path in subtitle_files):
            print("  [FAIL] SUBTITLE GENERATION FAILED: output artifact missing")
            return 1
        if "subtitles=filename=" not in engine.last_filter_graph:
            print("  [FAIL] SUBTITLE GENERATION FAILED: burn-in filter missing")
            return 1
        print(
            "  [OK] Subtitle End-to-End: local word timing, SRT, VTT, canonical timeline, "
            "burned final MP4, no-subtitles variant and first/middle/final frames"
        )
    print("All systems ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
