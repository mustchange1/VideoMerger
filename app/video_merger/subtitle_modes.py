"""Canonical subtitle output modes shared by GUI, CLI, cache and renderers.

The legacy names are kept readable so existing project files continue to load.
The user-facing contract is now deliberately small:

* ``with_subtitles`` renders the burned-in subtitle variant plus SRT/VTT
  sidecars, but never creates a clean sibling video.
* ``without_subtitles`` skips alignment and subtitle rendering entirely.
* ``with_and_without_subtitles`` creates both burned and clean video variants.
"""

from __future__ import annotations

# User-facing values.
SUBTITLE_OUTPUT_WITH = "with_subtitles"
SUBTITLE_OUTPUT_WITHOUT = "without_subtitles"
SUBTITLE_OUTPUT_BOTH = "with_and_without_subtitles"

# Compatibility aliases used by older Python callers. COMBINED intentionally
# points at the new default so ``ExportSettings()`` and old assertions describe
# the same default selection. The old JSON value is still normalized below.
SUBTITLE_OUTPUT_COMBINED = SUBTITLE_OUTPUT_WITH
SUBTITLE_OUTPUT_BURNED_ONLY = "burned_only"
SUBTITLE_OUTPUT_LEGACY_COMBINED = "burned_and_sidecars"

# The public selector intentionally contains exactly the three requested
# choices. Legacy values remain accepted by ``normalize_subtitle_output_mode``
# but are not offered as new-project choices.
SUBTITLE_OUTPUT_MODES = (
    SUBTITLE_OUTPUT_WITH,
    SUBTITLE_OUTPUT_WITHOUT,
    SUBTITLE_OUTPUT_BOTH,
)

SUBTITLE_OUTPUT_LABELS = {
    SUBTITLE_OUTPUT_WITH: "With Subtitles (burned + SRT + VTT; no clean copy)",
    SUBTITLE_OUTPUT_WITHOUT: "Without Subtitles",
    SUBTITLE_OUTPUT_BOTH: "With and Without Subtitles",
    SUBTITLE_OUTPUT_BURNED_ONLY: "With Subtitles (burned-in only; legacy)",
    SUBTITLE_OUTPUT_LEGACY_COMBINED: "With and Without Subtitles (legacy value)",
}


def normalize_subtitle_output_mode(value: str | None) -> str:
    raw = str(value or "").strip().casefold()
    aliases = {
        # New contract.
        "with_subtitles": SUBTITLE_OUTPUT_WITH,
        "with subtitles": SUBTITLE_OUTPUT_WITH,
        "with subtitles (burned + srt + vtt; no clean copy)": SUBTITLE_OUTPUT_WITH,
        "with_subtitles_srt_vtt": SUBTITLE_OUTPUT_WITH,
        "with_and_without_subtitles": SUBTITLE_OUTPUT_BOTH,
        "with and without subtitles": SUBTITLE_OUTPUT_BOTH,
        "with_and_without": SUBTITLE_OUTPUT_BOTH,
        "both": SUBTITLE_OUTPUT_BOTH,
        # Previous releases: preserve their clean-master behavior when a saved
        # project explicitly contains the old value.
        "burned_and_sidecars": SUBTITLE_OUTPUT_BOTH,
        "burned_in_subtitles + srt + vtt": SUBTITLE_OUTPUT_BOTH,
        "with burned-in subtitles + srt + vtt": SUBTITLE_OUTPUT_BOTH,
        "with burned-in subtitles + srt + vtt (default)": SUBTITLE_OUTPUT_BOTH,
        # ``default`` now means the new default, not the old dual output.
        "default": SUBTITLE_OUTPUT_WITH,
        "burned_only": SUBTITLE_OUTPUT_BURNED_ONLY,
        "burned-in-only": SUBTITLE_OUTPUT_BURNED_ONLY,
        "burned in only": SUBTITLE_OUTPUT_BURNED_ONLY,
        "with burned-in subtitles only": SUBTITLE_OUTPUT_BURNED_ONLY,
        "burned": SUBTITLE_OUTPUT_BURNED_ONLY,
        "without_subtitles": SUBTITLE_OUTPUT_WITHOUT,
        "without subtitles": SUBTITLE_OUTPUT_WITHOUT,
        "none": SUBTITLE_OUTPUT_WITHOUT,
        "off": SUBTITLE_OUTPUT_WITHOUT,
    }
    return aliases.get(raw, SUBTITLE_OUTPUT_WITH)


def subtitle_render_requested(mode: str, source_requested: bool) -> bool:
    """Whether a script request should create/burn a subtitle timeline."""
    return bool(source_requested and normalize_subtitle_output_mode(mode) != SUBTITLE_OUTPUT_WITHOUT)


def subtitle_sidecars_requested(mode: str) -> bool:
    """Whether SRT/VTT files belong in the user-facing output bundle."""
    return normalize_subtitle_output_mode(mode) in {
        SUBTITLE_OUTPUT_WITH,
        SUBTITLE_OUTPUT_BOTH,
    }


def subtitle_clean_variant_requested(mode: str) -> bool:
    """Whether a second, subtitle-free video must be rendered and retained."""
    return normalize_subtitle_output_mode(mode) == SUBTITLE_OUTPUT_BOTH
