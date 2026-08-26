"""Real quality presets for the 1.2.3 release.

``quality_preset`` is authoritative: it maps to actual encoder arguments used
by FFmpeg. ``custom`` keeps the explicit low-level CRF/preset values from the
advanced settings. The GUI always shows the active quality label so a preset
can never be a cosmetic-only selection.
"""

from __future__ import annotations

QUALITY_PRESETS: dict[str, dict[str, object]] = {
    "maximum": {
        "label": "Maximum Quality",
        "crf": 16,
        "preset": "slow",
        "description": "libx264 CRF 16, preset slow, High Profile, yuv420p",
    },
    "high": {
        "label": "High Quality",
        "crf": 18,
        "preset": "slow",
        "description": "libx264 CRF 18, preset slow, High Profile, yuv420p",
    },
    "balanced": {
        "label": "Balanced",
        "crf": 20,
        "preset": "medium",
        "description": "libx264 CRF 20, preset medium, High Profile, yuv420p",
    },
    "fast": {
        "label": "Fast / Draft",
        "crf": 23,
        "preset": "fast",
        "description": "libx264 CRF 23, preset fast, High Profile, yuv420p",
    },
}

QUALITY_KEYS = tuple(QUALITY_PRESETS)


def quality_label(key: str | None) -> str:
    if key in QUALITY_PRESETS:
        return str(QUALITY_PRESETS[key]["label"])
    return "Custom"


def effective_quality(settings) -> tuple[int, str, str]:
    """Return (crf, preset, label) for the settings' quality preset.

    ``custom`` falls through to the explicit low-level values so existing
    fine-grained configurations keep working.
    """
    key = getattr(settings, "quality_preset", "maximum") or "maximum"
    if key in QUALITY_PRESETS:
        entry = QUALITY_PRESETS[key]
        return int(entry["crf"]), str(entry["preset"]), str(entry["label"])
    return int(settings.crf), str(settings.preset), "Custom"
