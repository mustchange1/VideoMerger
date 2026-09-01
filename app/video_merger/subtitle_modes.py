"""Canonical subtitle output modes shared by GUI, CLI, cache and renderers."""

from __future__ import annotations

SUBTITLE_OUTPUT_COMBINED = "burned_and_sidecars"
SUBTITLE_OUTPUT_BURNED_ONLY = "burned_only"
SUBTITLE_OUTPUT_WITHOUT = "without_subtitles"
SUBTITLE_OUTPUT_MODES = (
    SUBTITLE_OUTPUT_COMBINED,
    SUBTITLE_OUTPUT_BURNED_ONLY,
    SUBTITLE_OUTPUT_WITHOUT,
)
SUBTITLE_OUTPUT_LABELS = {
    SUBTITLE_OUTPUT_COMBINED: "With Burned-in Subtitles + SRT + VTT",
    SUBTITLE_OUTPUT_BURNED_ONLY: "With Burned-in Subtitles only",
    SUBTITLE_OUTPUT_WITHOUT: "Without Subtitles",
}


def normalize_subtitle_output_mode(value: str | None) -> str:
    raw = str(value or "").strip().casefold()
    aliases = {
        "burned_and_sidecars": SUBTITLE_OUTPUT_COMBINED,
        "burned_in_subtitles + srt + vtt": SUBTITLE_OUTPUT_COMBINED,
        "with burned-in subtitles + srt + vtt": SUBTITLE_OUTPUT_COMBINED,
        "with burned-in subtitles + srt + vtt (default)": SUBTITLE_OUTPUT_COMBINED,
        "with_subtitles_srt_vtt": SUBTITLE_OUTPUT_COMBINED,
        "default": SUBTITLE_OUTPUT_COMBINED,
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
    return aliases.get(raw, SUBTITLE_OUTPUT_COMBINED)


def subtitle_render_requested(mode: str, source_requested: bool) -> bool:
    """Whether a script request should create/burn a subtitle timeline."""
    return bool(source_requested and normalize_subtitle_output_mode(mode) != SUBTITLE_OUTPUT_WITHOUT)


def subtitle_sidecars_requested(mode: str) -> bool:
    return normalize_subtitle_output_mode(mode) == SUBTITLE_OUTPUT_COMBINED
