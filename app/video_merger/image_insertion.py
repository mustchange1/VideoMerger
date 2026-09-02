"""Validation and deterministic FFmpeg styling for the Add Image section.

Add Image is deliberately separate from Quote/Flyer artwork.  It is a single
optional, silent Stage-2 composition section.  The legacy Image Insertion
names remain accepted so existing projects and CLI scripts keep their exact
meaning while the GUI exposes the clearer Before Main/After Main wording.
"""

from __future__ import annotations

from pathlib import Path

from .errors import VideoMergerError
from .project_assets import require_asset

IMAGE_INSERTION_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
# Canonical Add Image boundary names. ``after_intro`` and ``before_outro``
# are retained as read/write compatibility aliases for existing projects.
IMAGE_POSITION_BEFORE_MAIN = "before_main"
IMAGE_POSITION_AFTER_MAIN = "after_main"
CANONICAL_IMAGE_POSITIONS = {IMAGE_POSITION_BEFORE_MAIN, IMAGE_POSITION_AFTER_MAIN}
# Public accepted-value set retains the two legacy persisted names as well.
IMAGE_POSITIONS = CANONICAL_IMAGE_POSITIONS | {"after_intro", "before_outro"}
IMAGE_POSITION_ALIASES = {
    "after intro": IMAGE_POSITION_BEFORE_MAIN,
    "after_intro": IMAGE_POSITION_BEFORE_MAIN,
    "intro": IMAGE_POSITION_BEFORE_MAIN,
    "before main": IMAGE_POSITION_BEFORE_MAIN,
    "before main video": IMAGE_POSITION_BEFORE_MAIN,
    "before_main": IMAGE_POSITION_BEFORE_MAIN,
    "main before": IMAGE_POSITION_BEFORE_MAIN,
    "before outro": IMAGE_POSITION_AFTER_MAIN,
    "before_outro": IMAGE_POSITION_AFTER_MAIN,
    "outro": IMAGE_POSITION_AFTER_MAIN,
    "after main": IMAGE_POSITION_AFTER_MAIN,
    "after main video": IMAGE_POSITION_AFTER_MAIN,
    "after_main": IMAGE_POSITION_AFTER_MAIN,
    "main after": IMAGE_POSITION_AFTER_MAIN,
}
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
    """Return one of the canonical boundaries used by Stage-2 assembly."""
    return IMAGE_POSITION_ALIASES.get(
        str(value or "").strip().casefold(), IMAGE_POSITION_BEFORE_MAIN
    )


def normalize_image_transition(value: str) -> str:
    """Normalize the Add Image transition using the shared transition catalog."""
    from .transition_effects import normalize_transition

    return normalize_transition(str(value or "").strip().casefold())


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
