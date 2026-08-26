from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(value: str) -> str:
    value = _INVALID_WINDOWS.sub("_", value).strip().rstrip(". ")
    if value.casefold().endswith(".mp4"):
        value = value[:-4]
    if not value:
        return ""
    if value.upper() in _RESERVED:
        value = f"_{value}"
    return value[:180].rstrip(". ")


def make_output_path(output_folder: Path | str, aspect: str, custom_name: str = "", now: datetime | None = None) -> Path:
    folder = Path(output_folder)
    folder.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename(custom_name)
    if not base:
        stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
        aspect_token = "16x9" if aspect == "16:9" else "9x16"
        base = f"merged_{aspect_token}_{stamp}"
    candidate = folder / f"{base}.mp4"
    suffix = 2
    while candidate.exists():
        candidate = folder / f"{base}_{suffix}.mp4"
        suffix += 1
    return candidate
