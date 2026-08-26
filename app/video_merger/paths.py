from __future__ import annotations

import os
import shutil
from pathlib import Path

from .errors import DependencyError


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ensure_project_directories() -> None:
    root = project_root()
    for name in ("input", "output", "temp", "logs", "config", "tools"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _binary_names() -> tuple[str, str]:
    return ("ffmpeg.exe", "ffprobe.exe") if os.name == "nt" else ("ffmpeg", "ffprobe")


def locate_ffmpeg() -> tuple[Path, Path]:
    ffmpeg_name, ffprobe_name = _binary_names()
    candidates: list[Path] = []
    configured = os.environ.get("VIDEOMERGER_FFMPEG_DIR")
    if configured:
        candidates.append(Path(configured))
    root = project_root()
    candidates.extend([
        root / "tools" / "ffmpeg" / "bin",
        root / "tools" / "ffmpeg",
    ])
    for directory in candidates:
        ffmpeg = directory / ffmpeg_name
        ffprobe = directory / ffprobe_name
        if ffmpeg.is_file() and ffprobe.is_file():
            return ffmpeg.resolve(), ffprobe.resolve()
    # Windows must use the project-local installation (or an explicitly set
    # VIDEOMERGER_FFMPEG_DIR). This prevents an unrelated executable on PATH
    # from silently replacing the tested build.
    if os.name != "nt":
        sys_ffmpeg = shutil.which(ffmpeg_name)
        sys_ffprobe = shutil.which(ffprobe_name)
        if sys_ffmpeg and sys_ffprobe:
            return Path(sys_ffmpeg).resolve(), Path(sys_ffprobe).resolve()
    raise DependencyError(
        "Das lokale FFmpeg/FFprobe wurde nicht gefunden. Bitte setup_windows.ps1 ausführen. "
        "VIDEOMERGER_FFMPEG_DIR darf alternativ bewusst auf einen vollständigen bin-Ordner zeigen."
    )
