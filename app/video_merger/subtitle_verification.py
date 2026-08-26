from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import VideoMergerError
from .models import AlignmentResult
from .platform_utils import hidden_process_flags, safe_subprocess_env


def _verification_time(start: float, end: float) -> float:
    if end <= start:
        return max(0.0, start + 0.02)
    return max(0.0, start + min((end - start) * 0.5, 0.18))


def create_visual_verification_frames(
    ffmpeg: Path,
    video: Path,
    alignment: AlignmentResult,
    paths: dict[str, Path],
) -> list[Path]:
    """Extract real output frames during first/middle/final spoken words.

    The frames come from the already encoded final MP4, not from a preview or
    source clip. Successful FFmpeg subtitle-filter execution plus these decoded
    frames provides an auditable visual artifact for the actual user workflow.
    """
    if not alignment.words:
        raise VideoMergerError("Keine Wörter für visuelle Subtitle-Verifikation vorhanden.")
    indexes = [0, len(alignment.words) // 2, len(alignment.words) - 1]
    labels = ["first", "middle", "final"]
    outputs: list[Path] = []
    for label, index in zip(labels, indexes):
        word = alignment.words[index]
        output = paths[label]
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{_verification_time(word.start, word.end):.6f}",
            "-i", str(video), "-map", "0:v:0", "-frames:v", "1", "-update", "1",
            str(output),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VideoMergerError(
                f"Verifikationsbild für {label}/{word.text} konnte nicht erzeugt werden: {exc}"
            ) from exc
        if result.returncode != 0 or not output.is_file() or output.stat().st_size < 100:
            output.unlink(missing_ok=True)
            raise VideoMergerError(
                f"Verifikationsbild für {label}/{word.text} ist fehlgeschlagen: "
                f"{result.stderr.strip() or 'keine gültige PNG-Ausgabe'}"
            )
        if not output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            output.unlink(missing_ok=True)
            raise VideoMergerError(f"Verifikationsbild für {label}/{word.text} ist keine gültige PNG-Datei.")
        outputs.append(output)
    return outputs
