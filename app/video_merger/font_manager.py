from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .paths import project_root

FONT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("modern_sans_bold", "Modern Sans Bold – Noto Sans (Standard)"),
    ("inter", "Inter – Modern & Neutral (OFL)"),
    ("manrope", "Manrope – Technical & Friendly (OFL)"),
    ("lora", "Lora – Editorial Serif (OFL)"),
    ("roboto", "Roboto – Classic UI Sans (Apache 2.0)"),
    ("clean_sans", "Clean Sans – Noto Sans Regular"),
    ("eveleth_clean", "Eveleth Clean (licensed install / legal fallback)"),
)
# 1.2.4: additional professionally licensed families, bundled as static TTFs
# (Regular + Bold) with verifiable redistribution licenses (OFL / Apache-2.0).
_BUNDLED_FAMILIES: dict[str, tuple[str, str]] = {
    "inter": ("Inter-Regular.ttf", "Inter-Bold.ttf"),
    "manrope": ("Manrope-Regular.ttf", "Manrope-Bold.ttf"),
    "lora": ("Lora-Regular.ttf", "Lora-Bold.ttf"),
    "roboto": ("Roboto-Regular.ttf", "Roboto-Bold.ttf"),
}
_FAMILIES = {
    "modern_sans_bold": "Noto Sans",
    "inter": "Inter",
    "manrope": "Manrope",
    "lora": "Lora",
    "roboto": "Roboto",
    "clean_sans": "Noto Sans",
    "eveleth_clean": "Eveleth Clean",
}
_EVELETH_NAMES = {"eveleth clean", "eveleth clean regular", "evelethcleanregular"}

# Parsed font tables: path -> (unitsPerEm, cmap, hmtx-metrics, fallback_advance)
_FONT_TABLE_CACHE: dict[str, tuple[float, dict, dict, float]] = {}
# Measured widths: (path, text, size) -> pixels
_WIDTH_CACHE: dict[tuple[str, str, float], float] = {}


def _get_font_table(path: str) -> tuple[float, dict, dict, float]:
    if path in _FONT_TABLE_CACHE:
        return _FONT_TABLE_CACHE[path]
    from fontTools.ttLib import TTFont
    font = TTFont(path, lazy=True)
    units = float(font["head"].unitsPerEm)
    cmap = font.getBestCmap() or {}
    metrics = font["hmtx"].metrics
    fallback = float(metrics.get("space", (units * .33, 0))[0])
    _FONT_TABLE_CACHE[path] = (units, cmap, metrics, fallback)
    return _FONT_TABLE_CACHE[path]


@dataclass(frozen=True, slots=True)
class ResolvedFont:
    key: str
    family: str
    path: Path | None
    installed: bool
    fallback_used: bool
    proprietary: bool
    weight: str = "regular"

    def text_width(self, text: str, size: float) -> float:
        """Return measured pixel width using the selected font's real advances.

        Both the parsed font table and the resulting widths are cached: the
        GUI preview and the ASS builder measure the same words repeatedly,
        and TTFont parsing per call would make live updates feel slow.
        """
        measurement_path = self.path
        if measurement_path and measurement_path.is_file():
            key = (str(measurement_path), text, float(size))
            cached = _WIDTH_CACHE.get(key)
            if cached is not None:
                return cached
            try:
                table = _get_font_table(str(measurement_path))
                units, cmap, metrics, fallback = table
                total = 0.0
                for char in text:
                    glyph = cmap.get(ord(char))
                    total += float(metrics.get(glyph, (fallback, 0))[0]) if glyph else float(fallback)
                total *= float(size) / units
                if len(_WIDTH_CACHE) > 20000:
                    _WIDTH_CACHE.clear()
                _WIDTH_CACHE[key] = total
                return total
            except Exception:
                pass
        # Bounded headless fallback. Normal packaged operation uses the bundled
        # font files above; this only keeps diagnostics usable if a file is damaged.
        return sum((.34 if ch.isspace() else .76 if ch in "MWÄÖÜ@%" else .52) * size for ch in text)


def bundled_fonts_dir() -> Path:
    return project_root() / "tools" / "fonts"


def _font_file(bold: bool, family_key: str = "") -> Path:
    if family_key in _BUNDLED_FAMILIES:
        regular, bold_file = _BUNDLED_FAMILIES[family_key]
        return bundled_fonts_dir() / (bold_file if bold else regular)
    return bundled_fonts_dir() / ("NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf")


def _environment_installed_names() -> set[str]:
    raw = os.environ.get("VIDEOMERGER_INSTALLED_FONTS", "")
    return {item.strip().casefold() for item in raw.replace(";", ",").split(",") if item.strip()}


def _candidate_font_dirs() -> list[Path]:
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    windir = os.environ.get("WINDIR")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    dirs.extend([Path.home() / ".fonts", Path.home() / ".local" / "share" / "fonts"])
    return dirs


@lru_cache(maxsize=1)
def _discover_eveleth() -> tuple[str, Path | None] | None:
    for name in _environment_installed_names():
        normalized = " ".join(name.replace("-", " ").split())
        if normalized in _EVELETH_NAMES or "eveleth clean" in normalized:
            # The environment hook represents a family registered in the OS;
            # libass/Qt can resolve it without copying a proprietary binary.
            return "Eveleth Clean Regular", None
    for directory in _candidate_font_dirs():
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.suffix.casefold() not in {".ttf", ".otf", ".ttc"}:
                continue
            if "eveleth" not in path.name.casefold():
                continue
            try:
                from fontTools.ttLib import TTFont
                font = TTFont(str(path), lazy=True, fontNumber=0)
                names = {
                    record.toUnicode().strip()
                    for record in font["name"].names
                    if record.nameID in {1, 2, 4, 6}
                }
                font.close()
                if any("eveleth clean" in name.casefold() or "evelethcleanregular" in name.casefold() for name in names):
                    family = next((name for name in names if "eveleth clean regular" in name.casefold()), "Eveleth Clean Regular")
                    return family, path
            except Exception:
                continue
    return None


def resolve_font(key: str, bold: bool = False) -> ResolvedFont:
    """Resolve a logical font key to a concrete, legally bundled file.

    ``bold`` selects the weight file for the 1.2.4 families; the legacy keys
    keep their historical fixed weight (modern_sans_bold is always the bold
    Noto face, clean_sans the regular one) so existing behavior is untouched.
    """
    normalized = (key or "modern_sans_bold").strip().casefold()
    if normalized == "eveleth_clean":
        found = _discover_eveleth()
        # Tests and portable diagnostics may update this explicit process-only
        # hint after an earlier call, so check it without requiring cache reset.
        if any("eveleth clean" in name or "evelethcleanregular" in name for name in _environment_installed_names()):
            found = ("Eveleth Clean Regular", None)
        if found:
            family, path = found
            return ResolvedFont(normalized, family, path, True, False, True, "regular")
        return ResolvedFont(normalized, "Noto Sans", _font_file(True), False, True, True, "bold")
    if normalized in _BUNDLED_FAMILIES:
        family = _FAMILIES[normalized]
        path = _font_file(bold, normalized)
        return ResolvedFont(normalized, family, path, path.is_file(), not path.is_file(), False, "bold" if bold else "regular")
    if normalized == "clean_sans":
        return ResolvedFont(normalized, "Noto Sans", _font_file(False), True, False, False, "regular")
    return ResolvedFont("modern_sans_bold", "Noto Sans", _font_file(True), True, False, False, "bold")


def font_status(key: str) -> str:
    font = resolve_font(key)
    if font.proprietary and font.fallback_used:
        return "Eveleth UNLICENSED/NOT INSTALLED – legal Noto Sans fallback"
    if font.proprietary:
        return f"licensed installed family: {font.family}"
    return f"{font.family} ({font.path.name if font.path else 'installed'})"


def register_bundled_fonts_with_qt() -> list[str]:
    """Register all legally bundled OFL/Apache font files in this process."""
    try:
        from PySide6.QtGui import QFontDatabase
    except Exception:
        return []
    families: list[str] = []
    for path in (
        _font_file(False), _font_file(True),
        *(_font_file(False, key) for key in _BUNDLED_FAMILIES),
        *(_font_file(True, key) for key in _BUNDLED_FAMILIES),
    ):
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return families
