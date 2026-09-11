"""Derive one global-script section per voiceover for the individual Shorts.

A project may combine many voiceovers with ONE large global script:

* the Long-Form always uses the complete global script across the complete
  concatenated voiceover timeline (unchanged, authoritative);
* every individual Short must show only the part of that script which its own
  voiceover actually speaks — without the user splitting the script by hand
  and without aligning the complete global script against every Short.

The section boundaries are therefore acoustic rather than textual. The global
script is mapped ONCE onto the ordered multi-voiceover timeline — the very same
``LocalWordAligner.align_global`` mapping that the Long-Form uses, including its
cache identity — and each script word then belongs to the voiceover unit that is
playing when that word is spoken. Because the canonical word timeline follows
the authoritative script order with non-decreasing timestamps, every unit
receives one *contiguous* slice of the original script text (spelling,
punctuation and line breaks included) and the union of all sections is the
complete script: no valid script word is dropped, duplicated or re-ordered.

A unit whose voiceover speaks no part of the global script receives an empty
section. That is a real result, not an error: captioning such a Short with the
complete script would display text it never says.
"""

from __future__ import annotations

import hashlib
import os
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from pathlib import Path

from .models import WordTiming
from .paths import project_root

#: Derived script sections live beside the other private, regenerated render
#: inputs (staged ASS, per-job Stage-1 masters) and never in the user's Output
#: folder.
SECTION_DIRECTORY_NAME = "script_sections"


def unit_start_times(unit_durations: Sequence[float], inter_unit_pause: float = 0.0) -> list[float]:
    """Return the logical start time of every ordered voiceover unit.

    This is exactly the cumulative timeline used by the multi-voiceover
    alignment: spoken audio plus one configured pause between adjacent units,
    and no pause after the final unit.
    """
    pause = max(0.0, float(inter_unit_pause))
    starts: list[float] = []
    cursor = 0.0
    for index, duration in enumerate(unit_durations):
        starts.append(cursor)
        cursor += max(0.0, float(duration))
        if index < len(unit_durations) - 1:
            cursor += pause
    return starts


def split_global_script(
    script: str,
    words: Iterable[WordTiming],
    unit_durations: Sequence[float],
    inter_unit_pause: float = 0.0,
    unit_keys: Sequence[str] | None = None,
) -> list[str]:
    """Return one contiguous script section per ordered voiceover unit.

    ``words`` is the canonical word timeline of the complete global script over
    the complete concatenated timeline. A word belongs to the last unit that has
    already started when the word is spoken, so words that land inside an
    inter-unit pause stay with the preceding speech. The returned list always
    has one entry per unit; an entry is ``""`` when that voiceover speaks no
    part of the script.

    ``unit_keys`` optionally groups units that share one identity — the same
    voiceover file assigned to several rows. Grouped units receive the same
    section (the union of their character spans, in authoritative script order)
    because one audio file can only speak one part of the script, no matter how
    many output jobs reference it.
    """
    durations = [max(0.0, float(value)) for value in unit_durations]
    if not durations:
        return []
    if not script.strip():
        return [""] * len(durations)
    groups = (
        [str(key) for key in unit_keys]
        if unit_keys is not None and len(unit_keys) == len(durations)
        else [str(index) for index in range(len(durations))]
    )
    starts = unit_start_times(durations, inter_unit_pause)
    length = len(script)
    spans: dict[str, tuple[int, int]] = {}
    for word in words:
        try:
            moment = max(0.0, float(word.start))
        except (TypeError, ValueError):
            continue
        index = min(max(bisect_right(starts, moment) - 1, 0), len(durations) - 1)
        low = max(0, min(int(word.script_start), length))
        high = max(low, min(int(word.script_end), length))
        group = groups[index]
        current = spans.get(group)
        spans[group] = (low, high) if current is None else (min(current[0], low), max(current[1], high))
    sections: list[str] = []
    for group in groups:
        span = spans.get(group)
        if span is None:
            # This voiceover speaks no part of the global script.
            sections.append("")
            continue
        low, high = span
        sections.append(script[low:high].strip())
    return sections


def script_section_path(text: str, stem: str, directory: Path | None = None) -> Path:
    """Return the stable file that carries one derived script section.

    The name is content-addressed and an existing file is never rewritten, so
    repeated runs keep both the file path and its modification time identical.
    That matters because the section file is a regular Stage-1 script input:
    a new path or a new mtime would invalidate the render cache and repeat the
    voiceover alignment on every run.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    folder = Path(directory) if directory is not None else project_root() / "temp" / SECTION_DIRECTORY_NAME
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}_{digest}.txt"
    if not path.is_file():
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    return path
