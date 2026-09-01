"""Validation and deterministic FFmpeg styling for Stage-2 image insertion.

Image insertion is deliberately separate from Quote/Flyer artwork.  It is a
single optional, silent composition section and is never included in the
Stage-1 render cache identity.
"""

from __future__ import annotations

from pathlib import Path

from .errors import VideoMergerError
from .project_assets import require_asset

IMAGE_INSERTION_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_POSITIONS = {"after_intro", "before_outro"}
IMAGE_FIT_MODES = {"fit", "fill", "crop"}
IMAGE_FILTERS = {
    "natural": "",
    # These are intentionally small, fixed adjustments rather than random
    # grain/LUT lookups.  The same settings always produce the same pixels on
    # every supported FFmpeg build.
    "cinematic": "eq=contrast=1.08:saturation=1.08:brightness=0.01",
    "moody": "eq=contrast=1.14:saturation=0.82:brightness=-0.03",
    "film": "eq=contrast=1.06:saturation=0.92:gamma=0.98,colorbalance=rs=0.03:gs=0.01:bs=-0.01",
    "dark_editorial": "eq=contrast=1.12:saturation=0.78:brightness=-0.08,colorbalance=rs=-0.01:gs=-0.005:bs=0.02",
}


def image_insertion_path(value: str | Path) -> Path:
    """Resolve and validate one user-selected insertion image."""
    try:
        path = Path(value).expanduser().resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise VideoMergerError("Image Insertion fehlt oder ist kein gültiger Dateipfad.") from exc
    require_asset(path, "Image Insertion", IMAGE_INSERTION_EXTENSIONS)
    return path


def normalize_image_position(value: str) -> str:
    aliases = {
        "after intro": "after_intro",
        "after_intro": "after_intro",
        "intro": "after_intro",
        "before outro": "before_outro",
        "before_outro": "before_outro",
        "outro": "before_outro",
    }
    return aliases.get(str(value or "").strip().casefold(), "after_intro")


def normalize_image_fit_mode(value: str) -> str:
    value = str(value or "").strip().casefold()
    return value if value in IMAGE_FIT_MODES else "fit"


def normalize_image_filter(value: str) -> str:
    value = str(value or "").strip().casefold().replace(" ", "_")
    return value if value in IMAGE_FILTERS else "natural"


def clamp_image_duration(value: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 4.0
    return max(0.5, min(60.0, value))


def clamp_image_zoom(value: int | float) -> int:
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        value = 100
    return max(100, min(300, value))


def image_filter_expression(value: str) -> str:
    """Return the fixed, deterministic filter chain for a selected look."""
    return IMAGE_FILTERS[normalize_image_filter(value)]
