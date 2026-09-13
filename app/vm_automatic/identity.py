"""Deterministic job identity and audio/script pairing for VM Automatic.

The pairing convention is derived from the existing VideoMerger workflow:

* the GUI auto-matches a voiceover to its script via ``audio.with_suffix(".txt")``
  (``MainWindow._auto_match_script``) — i.e. same basename, different extension;
* scripts are ``.txt / .text / .md`` (``project_assets.read_script``);
* voiceovers are the supported audio formats (``project_assets.AUDIO_EXTENSIONS``).

A job is therefore identified by the *normalized stem* of its files:

    Topic_001.mp3  +  Topic_001.txt  =  job "topic_001"

Matching is deterministic (casefolded stem, exact extension whitelist).
Nothing is ever paired by fuzzy similarity: ``Topic_001.mp3`` + ``Topic_2.txt``
are two different (incomplete) jobs and never merged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..video_merger.discovery import _GENERATED_PREFIXES, _TEMP_MARKERS
from ..video_merger.project_assets import AUDIO_EXTENSIONS

SCRIPT_EXTENSIONS = {".txt", ".text", ".md"}

# Built-in temporary/transient markers (superset of VideoMerger's own rules).
DEFAULT_IGNORE_MARKERS: tuple[str, ...] = _TEMP_MARKERS + (
    ".tmp", ".part", "~", ".crdownload", ".download",
)


def normalize_stem(filename: str) -> str:
    """``Topic_001.mp3`` → ``topic_001`` (casefolded stem, deterministic)."""
    return Path(filename).stem.casefold()


def is_audio_name(name: str) -> bool:
    return Path(name).suffix.casefold() in AUDIO_EXTENSIONS


def is_script_name(name: str) -> bool:
    return Path(name).suffix.casefold() in SCRIPT_EXTENSIONS


def is_ignored_name(name: str, extra_markers: tuple[str, ...] | list[str] = ()) -> bool:
    """Hidden, transient, generated-output or temp-file names are never jobs."""
    if name.startswith((".", "~")):
        return True
    folded = name.casefold()
    if folded.startswith(_GENERATED_PREFIXES):
        return True
    markers = DEFAULT_IGNORE_MARKERS + tuple(marker.casefold() for marker in extra_markers if marker)
    return any(marker in folded for marker in markers)


@dataclass(frozen=True)
class Pairing:
    """The pairing result for one normalized job id."""

    job_id: str
    display_id: str          # original stem as first seen (user-friendly name)
    audio: Path | None
    script: Path | None
    audio_conflicts: tuple[Path, ...] = ()   # additional audio files with the same stem
    script_conflicts: tuple[Path, ...] = ()

    @property
    def complete(self) -> bool:
        return self.audio is not None and self.script is not None

    @property
    def state_label(self) -> str:
        if self.audio_conflicts or self.script_conflicts:
            return "CONFLICT"
        if self.audio is None and self.script is None:
            return "EMPTY"
        if self.audio is None:
            return "WAITING_FOR_AUDIO"
        if self.script is None:
            return "WAITING_FOR_SCRIPT"
        return "READY"
