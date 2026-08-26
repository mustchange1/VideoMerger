from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubtitlePreset:
    key: str
    label: str
    collection: str
    max_words: int
    max_chars: int
    font_ratio: float
    bold: bool
    box: bool
    outline_ratio: float
    accent: str
    progressive: bool
    description: str


SUBTITLE_PRESETS: tuple[SubtitlePreset, ...] = (
    SubtitlePreset("long_1", "LONG FORM 1 – Clean Editorial", "long", 10, 74, 0.046, False, False, 0.0022, "&H0039D9FF", True, "Balanced sentence-oriented editorial phrases with stable one/two-line geometry."),
    SubtitlePreset("long_2", "LONG FORM 2 – Documentary Box", "long", 10, 72, 0.045, False, True, 0.0014, "&H0039D9FF", True, "Readable white text in a restrained translucent dark box."),
    SubtitlePreset("long_3", "LONG FORM 3 – Minimal Cinematic", "long", 11, 78, 0.043, False, False, 0.0016, "&H00E8C46A", True, "Minimal cinematic phrases with subtle shadow and no box."),
    SubtitlePreset("long_4", "LONG FORM 4 – Subtle Highlight", "long", 9, 68, 0.047, False, False, 0.0022, "&H0039D9FF", False, "Stable phrases with a single synchronized accent word."),
    SubtitlePreset("long_5", "LONG FORM 5 – Podcast / Interview", "long", 9, 68, 0.048, True, True, 0.0014, "&H006FD7FF", True, "Conservative, stable spoken-word captions for interviews."),
    SubtitlePreset("short_1", "SHORT FORM 1 – Kinetic Chunk", "short", 5, 32, 0.060, True, False, 0.0030, "&H0039D9FF", False, "Stable 2–5 word chunks with controlled synchronized emphasis."),
    SubtitlePreset("short_2", "SHORT FORM 2 – Bold Highlight", "short", 5, 30, 0.064, True, False, 0.0032, "&H0052E8FF", False, "Large bold mobile captions with one accent color."),
    SubtitlePreset("short_3", "SHORT FORM 3 – Clean Pop", "short", 5, 34, 0.058, True, True, 0.0018, "&H0039D9FF", True, "Modern restrained phrase updates in a stable region."),
    SubtitlePreset("short_4", "SHORT FORM 4 – Karaoke Lite", "short", 6, 36, 0.058, True, False, 0.0028, "&H0000E8FF", False, "Spatially stable phrases with precise current-word highlighting."),
    SubtitlePreset("short_5", "SHORT FORM 5 – Impact", "short", 4, 26, 0.068, True, False, 0.0035, "&H0039D9FF", False, "High-impact 1–4 word groups without random movement."),
)

PRESET_BY_KEY = {preset.key: preset for preset in SUBTITLE_PRESETS}


def get_preset(key: str) -> SubtitlePreset:
    return PRESET_BY_KEY.get(key, PRESET_BY_KEY["long_1"])


def default_preset(aspect: str) -> str:
    return "short_1" if aspect == "9:16" else "long_1"
