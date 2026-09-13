"""Scan the VM Automatic watch folder and pair audio/script files into jobs.

Only direct (top-level) files are considered – nested folders are never
interpreted as jobs. The result is a deterministic mapping of normalized
job ids to :class:`~app.vm_automatic.identity.Pairing` objects.
"""
from __future__ import annotations

import os
from pathlib import Path

from .identity import (
    Pairing, is_audio_name, is_ignored_name, is_script_name, normalize_stem,
)


def list_job_files(folder: Path | str) -> list[Path]:
    """All direct, non-ignored files in the watch folder (detector order)."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        return []
    found: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                found.append(root / entry.name)
    except OSError:
        return []
    return found


def scan_watch_folder(folder: Path | str, extra_ignore_markers: tuple[str, ...] = ()) -> dict[str, Pairing]:
    """Group the watch folder's direct files into jobs by normalized stem.

    * audio/script names are whitelisted by extension (VideoMerger formats);
    * hidden/transient/generated names are ignored (never become jobs);
    * multiple files with the same stem are kept as conflicts (first in
      natural order wins, the rest are recorded for logging/attention).
    """
    root = Path(folder).expanduser().resolve()
    pairings: dict[str, Pairing] = {}
    if not root.is_dir():
        return pairings

    from ..video_merger.project_order import natural_sort_key

    audios: dict[str, list[Path]] = {}
    scripts: dict[str, list[Path]] = {}
    display: dict[str, str] = {}

    for path in sorted(list_job_files(root), key=lambda p: natural_sort_key(p.name)):
        name = path.name
        if is_ignored_name(name, extra_ignore_markers):
            continue
        stem = normalize_stem(name)
        if not stem:
            continue
        display.setdefault(stem, path.stem)
        if is_audio_name(name):
            audios.setdefault(stem, []).append(path)
        elif is_script_name(name):
            scripts.setdefault(stem, []).append(path)
        # Anything else (e.g. a .jpg in the inbox) is not part of any job:
        # it is ignored, not an error – it may be user scratch material.

    stems = set(audios) | set(scripts)
    for stem in stems:
        audio_list = audios.get(stem, [])
        script_list = scripts.get(stem, [])
        pairings[stem] = Pairing(
            job_id=stem,
            display_id=display.get(stem, stem),
            audio=audio_list[0] if audio_list else None,
            script=script_list[0] if script_list else None,
            audio_conflicts=tuple(audio_list[1:]),
            script_conflicts=tuple(script_list[1:]),
        )
    return pairings
