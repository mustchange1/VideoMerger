"""Deterministic ordering for multi-unit voiceover timelines.

The GUI stores the active list itself, while this module provides the same
ordering rules for CLI, persisted projects, diagnostics, and headless exports.
Matched scripts are reordered by the exact same permutation as their audio
units; a global script is never duplicated or paired with individual units.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .project_order import natural_sort_key


VOICEOVER_ORDER_MODES = ("natural", "mtime_oldest", "mtime_newest", "manual")


def normalize_voiceover_order_mode(value: str | None) -> str:
    value = str(value or "natural").strip().casefold()
    aliases = {
        "default": "natural",
        "alphabetical": "natural",
        "name": "natural",
        "oldest": "mtime_oldest",
        "modification_oldest": "mtime_oldest",
        "modification-date-oldest": "mtime_oldest",
        "modification date – oldest first": "mtime_oldest",
        "newest": "mtime_newest",
        "modification_newest": "mtime_newest",
        "modification-date-newest": "mtime_newest",
        "modification date – newest first": "mtime_newest",
    }
    value = aliases.get(value, value)
    return value if value in VOICEOVER_ORDER_MODES else "natural"


def _path_tie_key(path: Path) -> tuple[list[tuple[int, str | int]], str]:
    resolved = str(path.expanduser().resolve())
    return natural_sort_key(path.name), resolved.casefold()


def voiceover_order_indices(paths: Sequence[Path | str], mode: str | None = "natural") -> list[int]:
    """Return a stable permutation for ``paths``.

    ``manual`` is deliberately identity: the persisted list is already the
    user's explicit order and always has priority over automatic sorting.
    Missing files sort deterministically as timestamp zero, but are still
    retained so validation can report the missing asset rather than silently
    dropping a timeline unit.
    """
    indices = list(range(len(paths)))
    mode = normalize_voiceover_order_mode(mode)
    if mode == "manual":
        return indices
    if mode == "natural":
        return sorted(
            indices,
            key=lambda index: _path_tie_key(Path(paths[index])),
        )

    def mtime(index: int) -> int:
        try:
            return Path(paths[index]).expanduser().stat().st_mtime_ns
        except OSError:
            return 0

    if mode == "mtime_oldest":
        return sorted(indices, key=lambda index: (mtime(index), *_path_tie_key(Path(paths[index]))))
    return sorted(indices, key=lambda index: (-mtime(index), *_path_tie_key(Path(paths[index]))))


def order_voiceover_paths(paths: Sequence[Path | str], mode: str | None = "natural") -> list[Path]:
    values = [Path(path).expanduser().resolve() for path in paths]
    return [values[index] for index in voiceover_order_indices(values, mode)]


def order_voiceover_pairs(
    paths: Sequence[Path | str], scripts: Sequence[Path | str], mode: str | None = "natural",
) -> tuple[list[Path], list[Path], list[int]]:
    """Order audio and matched scripts with one shared stable permutation."""
    values = [Path(path).expanduser().resolve() for path in paths]
    script_values = [Path(path).expanduser().resolve() for path in scripts]
    indices = voiceover_order_indices(values, mode)
    return (
        [values[index] for index in indices],
        [script_values[index] for index in indices if index < len(script_values)],
        indices,
    )
