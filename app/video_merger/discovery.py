from __future__ import annotations

import os
from collections.abc import Sequence
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

    def order_many(self, folders: list[Path], detected: list[Path]) -> list[Path]: ...


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


def _configured_folders(value: Path | str | Sequence[Path | str]) -> list[Path]:
    """Normalize one legacy root or an explicit list of source folders.

    A single legacy root remains a direct-file source for backward
    compatibility. An explicit list is the configured multi-folder workflow;
    each listed folder is scanned exactly once and no nested project is
    discovered implicitly.
    """
    if isinstance(value, (str, os.PathLike, Path)):
        return [Path(value).expanduser().resolve()]
    folders: list[Path] = []
    for entry in value:
        path = Path(entry).expanduser().resolve()
        if _path_key(path) not in {_path_key(item) for item in folders}:
            folders.append(path)
    return folders


def _validate_folder(root: Path) -> None:
    if not root.exists():
        raise VideoMergerError(f"Der Eingabeordner existiert nicht: {root}")
    if not root.is_dir():
        raise VideoMergerError(f"Der Eingabepfad ist kein Ordner: {root}")


def discover_videos(
    folder: Path | str | Sequence[Path | str],
    order_store: OrderStore | None = None,
    excluded_paths: set[str] | None = None,
) -> list[Path]:
    """Discover direct clips from one or multiple configured source folders.

    The returned paths retain their source identity through ``Path.parent``;
    ``MediaAnalyzer`` copies that identity into ``MediaInfo.source_folder``.
    For a legacy single root, direct clips are scanned exactly as before. For
    an explicit list, all listed folders are scanned and no nested folders are
    implied.
    Each folder has its own persisted order, and stores supporting
    ``order_many`` additionally persist the combined multi-folder order.
    """
    roots = _configured_folders(folder)
    if not roots:
        raise VideoMergerError("Mindestens ein Eingabeordner muss konfiguriert sein.")
    excluded = {_path_key(path) for path in (excluded_paths or set())}
    groups: list[tuple[Path, list[Path]]] = []
    seen: set[str] = set()
    for root in roots:
        _validate_folder(root)
        direct = _detect_current_folder(root, excluded)
        candidates: list[Path] = [root]
        for source in candidates:
            files = direct
            unique = [path for path in files if _path_key(path) not in seen]
            if not unique:
                continue
            seen.update(_path_key(path) for path in unique)
            if order_store is not None:
                unique = order_store.order(source, unique)
            groups.append((source, unique))
    files = [path for _source, group in groups for path in group]
    if order_store is not None and len(groups) > 1 and hasattr(order_store, "order_many"):
        try:
            files = order_store.order_many([source for source, _group in groups], files)
        except (AttributeError, TypeError):
            # Third-party/test stores implementing only the original protocol
            # keep the proven per-folder behavior.
            pass
    if not files:
        raise VideoMergerError(
            "Keine geeigneten Videodateien direkt in den Eingabeordnern gefunden. Unterstützt werden u. a. "
            "MP4, MOV, MKV, AVI, WebM und M4V. Ausgaben, Vorschauen, versteckte und temporäre Dateien werden ausgeschlossen."
        )
    return files
