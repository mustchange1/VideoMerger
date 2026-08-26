from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from .errors import VideoMergerError

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".mpg", ".mpeg", ".mts", ".m2ts", ".ts", ".wmv",
}
_GENERATED_PREFIXES = (
    "merged_", "preview_transition_", "videomerger_preview_", "mainvideo_", "finalvideo_"
)
_TEMP_MARKERS = (".partial.", ".part.", ".tmp.", ".download.", ".crdownload.")


class OrderStore(Protocol):
    def order(self, folder: Path | str, detected: list[Path]) -> list[Path]: ...


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _is_safe_input_name(name: str) -> bool:
    folded = name.casefold()
    if name.startswith((".", "~")):
        return False
    if folded.startswith(_GENERATED_PREFIXES):
        return False
    if any(marker in folded for marker in _TEMP_MARKERS):
        return False
    return Path(name).suffix.casefold() in SUPPORTED_EXTENSIONS


def _detect_current_folder(root: Path, excluded: set[str]) -> list[Path]:
    """Return direct files in detector order, never nested or alphabetized."""
    detected: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_file() or not _is_safe_input_name(entry.name):
                    continue
                path = root / entry.name
                if _path_key(path) not in excluded:
                    detected.append(path)
    except OSError as exc:
        raise VideoMergerError(f"Der Eingabeordner konnte nicht gelesen werden: {root}: {exc}") from exc
    return detected


def discover_videos(
    folder: Path | str,
    order_store: OrderStore | None = None,
    excluded_paths: set[str] | None = None,
) -> list[Path]:
    root = Path(folder).expanduser().resolve()
    if not root.exists():
        raise VideoMergerError(f"Der Eingabeordner existiert nicht: {root}")
    if not root.is_dir():
        raise VideoMergerError(f"Der Eingabepfad ist kein Ordner: {root}")
    excluded = {_path_key(path) for path in (excluded_paths or set())}
    # Deliberately scan one directory level only. Nested projects are never
    # merged implicitly.
    files = _detect_current_folder(root, excluded)
    if order_store is not None:
        files = order_store.order(root, files)
    if not files:
        raise VideoMergerError(
            "Keine geeigneten Videodateien direkt im Eingabeordner gefunden. Unterstützt werden u. a. "
            "MP4, MOV, MKV, AVI, WebM und M4V. Ausgaben, Vorschauen, versteckte und temporäre Dateien werden ausgeschlossen."
        )
    return files
