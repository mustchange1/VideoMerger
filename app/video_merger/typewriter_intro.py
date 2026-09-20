"""Phase 29: Typewriter Hook Intro - dedicated, isolated intro stage.

Pipeline (requirement 17):

    User Text -> Typewriter Timeline -> Intro Visual Render
              -> Typewriter SFX Timeline -> Intro Audio Mix
              -> Existing Video/Transition Pipeline

Design guarantees:

* The intro is a SEPARATE generated segment, never subtitle content. The
  subtitle timing/alignment system and ASR are never touched.
* Every value is a pure function of the resolved settings: timing, sound
  events, variant selection and frames are fully deterministic and
  reproducible (no wall-clock, no RNG).
* When the intro is disabled - or enabled with empty text - this module is
  never invoked by the pipeline; the historical render path stays untouched.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

from .font_manager import resolve_font
from .transition_effects import normalize_transition

# ---------------------------------------------------------------------------
# Canonical option sets (one name per visual choice, no duplicates)
# ---------------------------------------------------------------------------
TYPEWRITER_SPEEDS: tuple[str, ...] = ("slow", "normal", "fast", "auto")
TYPEWRITER_SOUND_FREQUENCIES: tuple[str, ...] = (
    "every_character", "every_word", "word_boundary", "off",
)
TYPEWRITER_SOUND_PRESETS: tuple[str, ...] = (
    "typewriter_1", "typewriter_2", "mechanical", "soft_keyboard", "off",
)
#: Exactly five canonical vertical positions; each maps to ONE rendered spot.
TYPEWRITER_POSITIONS: tuple[str, ...] = (
    "Top", "Upper-Middle", "Center", "Lower-Middle", "Bottom",
)
TYPEWRITER_H_ALIGNS: tuple[str, ...] = ("Left", "Center", "Right")
TYPEWRITER_MUSIC_MODES: tuple[str, ...] = ("start_with_video", "continue_during_intro")

#: Vertical center of the text block as a fraction of the frame height.
POSITION_FRACTIONS: dict[str, float] = {
    "Top": 0.12,
    "Upper-Middle": 0.31,
    "Center": 0.50,
    "Lower-Middle": 0.69,
    "Bottom": 0.88,
}
#: Horizontal margin (fraction of width) for Left/Right alignment.
H_MARGIN_RATIO = 0.07
#: Maximum text block width (fraction of frame width) before wrapping.
MAX_TEXT_WIDTH_RATIO = 0.86
DEFAULT_BACKGROUND_COLOR = (16, 16, 20)

# Per-character typing intervals in seconds for the fixed speed modes.
SPEED_INTERVALS: dict[str, float] = {"slow": 0.13, "normal": 0.075, "fast": 0.042}
#: Deterministic extra pause after a typed space (natural word rhythm).
SPACE_PAUSE_FACTOR = 0.55
#: Deterministic extra pause for a manual line break character.
LINE_BREAK_PAUSE_FACTOR = 0.8
#: Auto mode bounds so short hooks never drag and long hooks never race.
AUTO_MIN_INTERVAL = 0.030
AUTO_MAX_INTERVAL = 0.12
AUTO_TYPING_CAP_SECONDS = 20.0
MIN_TYPING_SECONDS = 0.35
MAX_HOLD_SECONDS = 10.0
MIN_TOTAL_SECONDS = 0.8
SFX_SAMPLE_RATE = 48000
#: Number of keystroke variants per preset (deterministic anti-machinegun).
KEY_VARIANTS = 4


def normalize_speed(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in TYPEWRITER_SPEEDS else "auto"


def normalize_sound_frequency(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in TYPEWRITER_SOUND_FREQUENCIES else "every_character"


def normalize_sound_preset(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in TYPEWRITER_SOUND_PRESETS else "typewriter_1"


def normalize_position(value: object) -> str:
    text = str(value or "").strip()
    lowered = text.casefold().replace(" ", "-")
    aliases = {
        "top": "Top", "upper-middle": "Upper-Middle", "uppermiddle": "Upper-Middle",
        "center": "Center", "centre": "Center", "middle": "Center",
        "lower-middle": "Lower-Middle", "lowermiddle": "Lower-Middle",
        "bottom": "Bottom",
    }
    return aliases.get(lowered, "Center")


def normalize_h_align(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text in {"left", "l"}:
        return "Left"
    if text in {"right", "r"}:
        return "Right"
    return "Center"


def normalize_music_mode(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in TYPEWRITER_MUSIC_MODES else "start_with_video"


def clamp_sound_volume(value: object) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 30


def clamp_hold_seconds(value: object) -> float:
    try:
        return max(0.0, min(MAX_HOLD_SECONDS, float(value)))
    except (TypeError, ValueError):
        return 0.5


def parse_color(value: object) -> tuple[int, int, int]:
    """Parse #RGB/#RRGGBB (or a plain RGB tuple) into 8-bit channels."""
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return tuple(max(0, min(255, int(c))) for c in value)  # type: ignore[return-value]
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 6:
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
        except ValueError:
            pass
    return (255, 255, 255)


@dataclass(frozen=True, slots=True)
class TypewriterProfile:
    """One fully normalized, render-ready intro configuration."""

    enabled: bool
    text: str
    speed: str
    sound_frequency: str
    sound_preset: str
    sound_volume: int
    cursor_enabled: bool
    position: str
    h_align: str
    font: str
    font_size: int
    bold: bool
    color: tuple[int, int, int]
    outline_enabled: bool
    shadow_enabled: bool
    box_enabled: bool
    box_opacity: int
    box_padding: int
    background_image_enabled: bool
    background_image_path: str
    background_darken: int
    background_blur: bool
    background_zoom: bool
    hold_seconds: float
    transition: str
    music_mode: str

    @property
    def active(self) -> bool:
        """Enabled AND usable: empty/whitespace text behaves as disabled."""
        return bool(self.enabled and self.text.strip())


#: Every field that influences the rendered intro asset (cache identity).
_IDENTITY_FIELDS = tuple(f.name for f in TypewriterProfile.__dataclass_fields__.values())  # type: ignore[attr-defined]


def profile_from_settings(settings: object, *, short: bool = False) -> TypewriterProfile:
    """Resolve one profile's typewriter settings (Long-Form or Shorts).

    Reads ONLY the requested profile's fields - the two profiles can never
    leak into each other.
    """
    prefix = "short_typewriter_" if short else "typewriter_"

    def get(name: str, default: object = None) -> object:
        return getattr(settings, prefix + name, default)

    text = str(get("hook_text", "") or "")
    return TypewriterProfile(
        enabled=bool(get("intro_enabled", False)),
        text=text,
        speed=normalize_speed(get("speed", "auto")),
        sound_frequency=normalize_sound_frequency(get("sound_frequency")),
        sound_preset=normalize_sound_preset(get("sound_preset")),
        sound_volume=clamp_sound_volume(get("sound_volume", 30)),
        cursor_enabled=bool(get("cursor_enabled", True)),
        position=normalize_position(get("position", "Center")),
        h_align=normalize_h_align(get("h_align", "Center")),
        font=str(get("font", "modern_sans_bold") or "modern_sans_bold"),
        font_size=max(50, min(200, int(get("font_size", 100) or 100))),
        bold=bool(get("bold", True)),
        color=parse_color(get("color", "#FFFFFF")),
        outline_enabled=bool(get("outline_enabled", True)),
        shadow_enabled=bool(get("shadow_enabled", False)),
        box_enabled=bool(get("box_enabled", False)),
        box_opacity=max(0, min(100, int(get("box_opacity", 55) or 55))),
        box_padding=max(0, min(200, int(get("box_padding", 40) or 40))),
        background_image_enabled=bool(get("background_image_enabled", False)),
        background_image_path=str(get("background_image_path", "") or "").strip(),
        background_darken=max(0, min(80, int(get("background_darken", 0) or 0))),
        background_blur=bool(get("background_blur", False)),
        background_zoom=bool(get("background_zoom", False)),
        hold_seconds=clamp_hold_seconds(get("hold_seconds", 0.5)),
        transition=str(get("transition", "project") or "project"),
        music_mode=normalize_music_mode(get("music_mode", "start_with_video")),
    )


# ---------------------------------------------------------------------------
# Timeline (pure, deterministic)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TypeEvent:
    """One typed character: its appearance time and cumulative typed text."""

    time: float
    char_index: int  # index into the VISIBLE character stream
    char: str
    plays_sound: bool
    is_space: bool


@dataclass(frozen=True, slots=True)
class TypewriterTimeline:
    events: tuple[TypeEvent, ...]
    lines: tuple[str, ...]  # wrapped/line-broken layout of the FULL text
    line_of_char: tuple[int, ...]  # visible char index -> layout line
    pos_in_line: tuple[int, ...]  # visible char index -> position in its line
    typing_end: float
    total_duration: float
    interval: float
    sound_times: tuple[float, ...]
    sound_is_space: tuple[bool, ...]

    @property
    def char_count(self) -> int:
        return len(self.line_of_char)


def auto_interval(char_count: int) -> float:
    """Natural typing speed: bounded so short hooks stay snappy and long
    hooks stay readable. Pure function of the character count.

    A hard typing-time cap keeps even extreme texts finite: whenever the
    natural interval would type longer than the cap, the interval is shrunk
    so the complete text still finishes within it (always deterministic).
    """
    if char_count <= 0:
        return SPEED_INTERVALS["normal"]
    raw = 0.030 + 0.0011 * char_count
    interval = max(AUTO_MIN_INTERVAL, min(AUTO_MAX_INTERVAL, raw))
    capped = AUTO_TYPING_CAP_SECONDS / char_count
    return min(interval, capped) if interval * char_count > AUTO_TYPING_CAP_SECONDS else interval


def build_timeline(profile: TypewriterProfile) -> TypewriterTimeline:
    """Expand the hook text into the deterministic typing timeline.

    Guarantees (requirement 19): every visible character appears exactly
    once, in order, nothing is cut, duplicated or skipped; Unicode code
    points are preserved; manual ``\\n`` breaks are honored as line breaks
    (typed as a pause, never shown); spaces are typed and audible.
    """
    raw_text = profile.text.replace("\r\n", "\n").replace("\r", "\n")
    # Manual line breaks stay part of the layout (they are the logical line
    # groups) but are never rendered glyphs: a break types as one short
    # deterministic pause right after the last character before it.
    logical_lines = [logical.replace("\t", " ") for logical in raw_text.split("\n")]
    visible_chars: list[str] = []
    has_line_break_after: list[bool] = []
    for line_index, logical in enumerate(logical_lines):
        for pos, ch in enumerate(logical):
            visible_chars.append(ch)
            has_line_break_after.append(
                line_index < len(logical_lines) - 1 and pos == len(logical) - 1
            )
    text = "".join(visible_chars)
    n = len(visible_chars)

    interval = SPEED_INTERVALS.get(profile.speed) or auto_interval(n)
    if profile.speed == "auto":
        interval = auto_interval(n)

    # Sound plan (deterministic, derived from word structure).
    frequency = profile.sound_frequency if profile.sound_preset != "off" else "off"
    word_starts: set[int] = set()
    word_ends: set[int] = set()
    in_word = False
    for index, ch in enumerate(visible_chars):
        is_word_char = ch != " "
        if is_word_char and not in_word:
            word_starts.add(index)
            in_word = True
        elif not is_word_char and in_word:
            word_ends.add(index - 1)
            in_word = False
    if in_word:
        word_ends.add(n - 1)

    events: list[TypeEvent] = []
    sound_times: list[float] = []
    sound_is_space: list[bool] = []
    t = 0.0
    for index, ch in enumerate(visible_chars):
        plays = False
        if frequency == "every_character":
            plays = True
        elif frequency == "every_word":
            plays = index in word_starts
        elif frequency == "word_boundary":
            plays = index in word_ends
        events.append(TypeEvent(
            time=round(t, 6), char_index=index, char=ch,
            plays_sound=plays, is_space=ch == " ",
        ))
        if plays:
            sound_times.append(round(t, 6))
            sound_is_space.append(ch == " ")
        t += interval
        if ch == " ":
            t += interval * SPACE_PAUSE_FACTOR
        if has_line_break_after[index]:
            t += interval * LINE_BREAK_PAUSE_FACTOR
    typing_end = t
    total = max(typing_end + profile.hold_seconds, MIN_TOTAL_SECONDS)

    # Layout: honor manual breaks (the logical line groups computed above);
    # width-aware wrapping of long lines happens later with real font metrics
    # in build_layout(), shared by production render and GUI preview.
    lines = list(logical_lines)
    line_of_char: list[int] = []
    pos_in_line: list[int] = []
    for line_index, logical in enumerate(logical_lines):
        for pos in range(len(logical)):
            line_of_char.append(line_index)
            pos_in_line.append(pos)
    if not visible_chars:
        lines = [""]

    return TypewriterTimeline(
        events=tuple(events),
        lines=tuple(lines),
        line_of_char=tuple(line_of_char),
        pos_in_line=tuple(pos_in_line),
        typing_end=round(typing_end, 6),
        total_duration=round(total, 6),
        interval=round(interval, 6),
        sound_times=tuple(sound_times),
        sound_is_space=tuple(sound_is_space),
    )


# ---------------------------------------------------------------------------
# Typewriter SFX synthesis (deterministic, dependency-free)
# ---------------------------------------------------------------------------
def _lcg(seed: int):
    """Tiny deterministic LCG - reproducible noise without random module."""
    state = seed & 0x7FFFFFFF or 12345

    def next_float() -> float:
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    return next_float


#: Per-preset acoustic character: (center_hz, thump_hz, length_s, click_gain,
#: thump_gain, ring_hz). Values are fixed design constants, never random.
_PRESET_ACOUSTICS: dict[str, tuple[float, float, float, float, float, float]] = {
    "typewriter_1": (2400.0, 120.0, 0.022, 0.9, 0.7, 0.0),
    "typewriter_2": (1600.0, 100.0, 0.016, 0.7, 0.8, 0.0),
    "mechanical": (1200.0, 80.0, 0.030, 0.6, 1.0, 620.0),
    "soft_keyboard": (900.0, 70.0, 0.014, 0.3, 0.5, 0.0),
}


def _synthesize_keystroke(preset: str, variant: int, *, space: bool) -> list[float]:
    """One deterministic keystroke sample (mono float, SFX_SAMPLE_RATE)."""
    center, thump_hz, length, click_gain, thump_gain, ring_hz = _PRESET_ACOUSTICS[preset]
    if space:
        thump_hz *= 0.75
        length *= 1.35
        click_gain *= 0.6
    samples = int(SFX_SAMPLE_RATE * length)
    rnd = _lcg(0x9E3779B1 ^ (hash((preset, variant, space)) & 0x7FFFFFFF))
    noise = [rnd() * 2.0 - 1.0 for _ in range(samples)]
    # Simple one-pole lowpass toward the preset's center frequency.
    alpha = 1.0 - math.exp(-2.0 * math.pi * center / SFX_SAMPLE_RATE)
    lp = 0.0
    out: list[float] = []
    for i in range(samples):
        lp += alpha * (noise[i] - lp)
        env = math.exp(-i / (samples * 0.18))
        click = lp * click_gain * env
        thump_env = math.exp(-i / (samples * 0.35))
        thump = math.sin(2.0 * math.pi * thump_hz * i / SFX_SAMPLE_RATE) * thump_gain * thump_env * 0.5
        ring = 0.0
        if ring_hz > 0:
            ring = math.sin(2.0 * math.pi * ring_hz * i / SFX_SAMPLE_RATE) * 0.18 * math.exp(-i / (samples * 0.5))
        out.append(click + thump + ring)
    return out


_KEYSTROKE_CACHE: dict[tuple[str, int, bool], list[float]] = {}


def keystroke_sample(preset: str, variant: int, *, space: bool) -> list[float]:
    key = (preset, variant, space)
    if key not in _KEYSTROKE_CACHE:
        _KEYSTROKE_CACHE[key] = _synthesize_keystroke(preset, variant, space=space)
    return _KEYSTROKE_CACHE[key]


def variant_for_char(char_index: int, char: str) -> int:
    """Deterministic, text-dependent variant choice - varies repeated sounds
    without any randomness."""
    return (char_index * 7 + ord(char)) % KEY_VARIANTS


def synthesize_sfx_wav(
    timeline: TypewriterTimeline,
    profile: TypewriterProfile,
    path: Path,
) -> Path:
    """Render the complete deterministic SFX track to a 16-bit stereo WAV."""
    total_samples = max(1, int(round(timeline.total_duration * SFX_SAMPLE_RATE)))
    buffer = [0.0] * total_samples
    if profile.sound_preset != "off" and profile.sound_frequency != "off":
        gain = profile.sound_volume / 100.0
        events_by_time = {event.time: event for event in timeline.events}
        for sound_time, is_space in zip(timeline.sound_times, timeline.sound_is_space):
            event = events_by_time.get(sound_time)
            char_index = event.char_index if event else 0
            char = event.char if event else " "
            variant = 0 if is_space else variant_for_char(char_index, char)
            sample = keystroke_sample(profile.sound_preset, variant, space=is_space)
            start = int(round(sound_time * SFX_SAMPLE_RATE))
            peak = 0.9 if not is_space else 1.0
            for offset, value in enumerate(sample):
                target = start + offset
                if 0 <= target < total_samples:
                    buffer[target] += value * gain * peak
    # Clamp and interleave to stereo.
    frames = bytearray()
    for value in buffer:
        clamped = max(-1.0, min(1.0, value))
        pcm = int(clamped * 32767)
        frames += struct.pack("<h", pcm) * 2
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SFX_SAMPLE_RATE)
        wav_file.writeframes(bytes(frames))
    return path


# ---------------------------------------------------------------------------
# Cache identity
# ---------------------------------------------------------------------------
TYPEWRITER_CACHE_SCHEMA = "phase29-typewriter-v1"


def typewriter_identity(
    profile: TypewriterProfile,
    width: int,
    height: int,
    fps: float,
) -> str:
    """SHA-256 over everything that can change the rendered intro asset."""
    values: dict[str, object] = {"schema": TYPEWRITER_CACHE_SCHEMA}
    for name in _IDENTITY_FIELDS:
        value = getattr(profile, name)
        if isinstance(value, tuple):
            value = list(value)
        values[name] = value
    values.update({"width": int(width), "height": int(height), "fps": round(float(fps), 6)})
    canonical = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Layout + frame rendering (Qt) - shared by production render and preview
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TypewriterLayout:
    """One measured, wrapped layout of the hook at a concrete frame size."""

    wrapped_lines: tuple[str, ...]
    line_of_char: tuple[int, ...]   # visible char index -> wrapped line index
    pos_in_line: tuple[int, ...]    # visible char index -> column in that line
    line_height: float
    block_x: float
    block_top: float
    block_width: float
    block_height: float
    font_size_px: float
    line_widths: tuple[float, ...]


_gui_application = None


def _ensure_gui_application() -> None:
    """Text shaping requires a QGuiApplication.

    Inside the GUI one already exists; headless render paths (CLI exports,
    tests, One-Click) get one minimal offscreen-safe instance exactly once.
    """
    global _gui_application
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None and _gui_application is None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _gui_application = QGuiApplication([])


def _qfont(profile: TypewriterProfile, font_size_px: float):
    from PySide6.QtGui import QFont

    _ensure_gui_application()
    resolved = resolve_font(profile.font, bold=profile.bold)
    font = QFont(resolved.family)
    font.setPointSizeF(max(4.0, font_size_px * 72.0 / 96.0))
    font.setBold(profile.bold)
    if resolved.path is not None and Path(resolved.path).is_file():
        try:
            from PySide6.QtGui import QFontDatabase

            QFontDatabase.addApplicationFont(str(resolved.path))
        except Exception:
            pass
    return font, resolved


def build_layout(
    profile: TypewriterProfile,
    timeline: TypewriterTimeline,
    width: int,
    height: int,
):
    """Greedy word wrapping with REAL font metrics (deterministic)."""
    from PySide6.QtGui import QFontMetricsF

    base = min(width, height)
    font_size_px = base * 0.052 * (profile.font_size / 100.0)
    font, _resolved = _qfont(profile, font_size_px)
    metrics = QFontMetricsF(font)
    line_height = float(metrics.height()) * 1.22
    max_width = width * MAX_TEXT_WIDTH_RATIO

    wrapped: list[str] = []
    line_of_char: list[int] = []
    pos_in_line: list[int] = []
    cursor = 0
    for logical in timeline.lines:
        words: list[str] = []
        current = ""
        for ch in logical + " ":
            if ch == " ":
                words.append(current)
                current = ""
            else:
                current += ch
        line = ""
        line_start_cursor = cursor
        for word in words:
            candidate = (line + " " + word).strip() if line else word
            if not line or metrics.horizontalAdvance(candidate) <= max_width:
                line = candidate
            else:
                wrapped.append(line)
                line = word
        wrapped.append(line)
        # Map visible characters of this logical line onto the wrapped lines.
        line_index_base = len(wrapped) - 1
        offset_in_logical = 0
        wrapped_cursor = line_start_cursor
        for w_index, w_line in enumerate(wrapped[line_index_base - (len(wrapped) - line_index_base - 1):]):
            pass
        # Simpler deterministic mapping: walk the logical text against the
        # wrapped lines (each wrapped line is a contiguous slice).
        remaining = logical
        w_idx = len(wrapped) - 1
        # Find how many wrapped lines belong to this logical line.
        consumed = 0
        local_lines: list[str] = []
        idx = len(wrapped) - 1
        total_len = len(logical)
        accounted = 0
        while accounted < total_len or (total_len == 0 and not local_lines):
            wl = wrapped[idx]
            local_lines.insert(0, wl)
            accounted += len(wl.replace(" ", "", 0))  # placeholder, fixed below
            break
        # Rebuild the mapping directly by re-walking (authoritative):
        local_lines = []
        acc = 0
        idx = len(wrapped) - 1
        while True:
            wl = wrapped[idx]
            local_lines.insert(0, wl)
            acc += len(wl) + 1  # +1 for the joining space between wrapped lines
            if acc >= len(logical) or idx == 0:
                break
            idx -= 1
            if len(local_lines) > 200:
                break
        char_cursor = line_start_cursor
        joined = " ".join(local_lines)
        for pos in range(len(logical)):
            wrapped_index = len(wrapped) - len(local_lines)
            # Position within joined text equals pos (spaces preserved).
            line_no = wrapped_index
            col = pos
            for li, wl in enumerate(local_lines):
                start_li = sum(len(x) + 1 for x in local_lines[:li])
                if pos < start_li + len(wl) or li == len(local_lines) - 1:
                    line_no = wrapped_index + li
                    col = pos - start_li
                    break
            line_of_char.append(line_no)
            pos_in_line.append(max(0, col))
            char_cursor += 1
        cursor += len(logical)

    line_widths = tuple(float(metrics.horizontalAdvance(line)) for line in wrapped)
    block_width = max(line_widths) if line_widths else 0.0
    block_height = line_height * max(1, len(wrapped))
    center_y = POSITION_FRACTIONS[profile.position] * height
    block_top = center_y - block_height / 2.0
    if profile.h_align == "Left":
        block_x = width * H_MARGIN_RATIO
    elif profile.h_align == "Right":
        block_x = width * (1.0 - H_MARGIN_RATIO) - block_width
    else:
        block_x = (width - block_width) / 2.0
    return TypewriterLayout(
        wrapped_lines=tuple(wrapped),
        line_of_char=tuple(line_of_char),
        pos_in_line=tuple(pos_in_line),
        line_height=line_height,
        block_x=float(block_x),
        block_top=float(block_top),
        block_width=float(block_width),
        block_height=float(block_height),
        font_size_px=float(font_size_px),
        line_widths=line_widths,
    )
# ---------------------------------------------------------------------------
# Frame drawing (shared by production render and the GUI preview)
# ---------------------------------------------------------------------------
CURSOR_PERIOD = 0.8
CURSOR_DUTY = 0.55


def cursor_visible_at(time: float) -> bool:
    """Deterministic blink: fixed period/duty, phase locked to t=0."""
    return (time % CURSOR_PERIOD) < CURSOR_PERIOD * CURSOR_DUTY


def typed_count_at(timeline: TypewriterTimeline, time: float) -> int:
    """How many characters are visible at ``time`` (never skips/repeats)."""
    count = 0
    for event in timeline.events:
        if event.time <= time + 1e-9:
            count += 1
        else:
            break
    return count


class TypewriterBackground:
    """Prepared background (cover-fit image or dark neutral), intro-scoped."""

    def __init__(self, profile: TypewriterProfile, width: int, height: int):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage

        self._qt = {"Qt": Qt}
        self.image = None
        self.enabled = False
        if profile.background_image_enabled and profile.background_image_path:
            source = QImage()
            path = Path(profile.background_image_path)
            if path.is_file() and source.load(str(path)):
                scaled = source.scaled(
                    width, height,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = max(0, (scaled.width() - width) // 2)
                y = max(0, (scaled.height() - height) // 2)
                self.image = scaled.copy(x, y, width, height)
                if profile.background_blur:
                    small = self.image.scaled(
                        max(1, width // 10), max(1, height // 10),
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self.image = small.scaled(
                        width, height,
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                self.enabled = True

    def paint(self, painter, width: int, height: int, time: float,
              total_duration: float, zoom_enabled: bool) -> None:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor

        if self.enabled and self.image is not None:
            if zoom_enabled and total_duration > 0:
                progress = max(0.0, min(1.0, time / total_duration))
                scale = 1.0 + 0.06 * progress
            else:
                scale = 1.0
            w = width * scale
            h = height * scale
            painter.drawImage(
                QRectF((width - w) / 2.0, (height - h) / 2.0, w, h), self.image
            )
        else:
            r, g, b = DEFAULT_BACKGROUND_COLOR
            painter.fillRect(0, 0, width, height, QColor(r, g, b))


def _typed_prefix_counts(timeline: TypewriterTimeline, layout: TypewriterLayout,
                         typed_count: int) -> list[int]:
    counts = [0] * len(layout.wrapped_lines)
    for char_index in range(typed_count):
        if char_index < len(layout.line_of_char):
            line_index = layout.line_of_char[char_index]
            counts[line_index] += 1
    return counts


def draw_typewriter_frame(
    painter,
    profile: TypewriterProfile,
    timeline: TypewriterTimeline,
    layout: TypewriterLayout,
    background: TypewriterBackground,
    width: int,
    height: int,
    time: float,
) -> None:
    """Draw one deterministic intro frame at ``time`` seconds."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QPainterPath, QPen

    painter.setRenderHint(painter.RenderHint.Antialiasing, True)
    painter.setRenderHint(painter.RenderHint.SmoothPixmapTransform, True)
    background.paint(painter, width, height, time, timeline.total_duration,
                     profile.background_zoom)
    if profile.background_darken > 0:
        alpha = int(255 * profile.background_darken / 80.0)
        painter.fillRect(0, 0, width, height, QColor(0, 0, 0, min(255, alpha)))

    font, _resolved = _qfont(profile, layout.font_size_px)
    painter.setFont(font)
    typed_count = typed_count_at(timeline, time)
    prefix_counts = _typed_prefix_counts(timeline, layout, typed_count)
    color = QColor(*profile.color)
    pad = layout.font_size_px * profile.box_padding / 100.0

    if profile.box_enabled and typed_count > 0:
        box = QRectF(
            layout.block_x - pad, layout.block_top - pad * 0.8,
            layout.block_width + pad * 2, layout.block_height + pad * 1.6,
        )
        box_color = QColor(0, 0, 0, int(255 * profile.box_opacity / 100.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(box_color)
        painter.drawRoundedRect(box, pad * 0.45, pad * 0.45)

    line_lefts: list[float] = []
    for index, line in enumerate(layout.wrapped_lines):
        if profile.h_align == "Left":
            left = layout.block_x
        elif profile.h_align == "Right":
            left = layout.block_x + (layout.block_width - layout.line_widths[index])
        else:
            left = layout.block_x + (layout.block_width - layout.line_widths[index]) / 2.0
        line_lefts.append(left)

    baseline_shift = layout.line_height * 0.78
    text_path = QPainterPath()
    metrics = painter.fontMetrics()
    for index, line in enumerate(layout.wrapped_lines):
        shown = line[: prefix_counts[index]]
        if not shown:
            continue
        top = layout.block_top + index * layout.line_height
        text_path.addText(line_lefts[index], top + baseline_shift, font, shown)

    if not text_path.isEmpty():
        if profile.shadow_enabled:
            offset = max(1.0, layout.font_size_px * 0.045)
            shadow = QColor(0, 0, 0, 110)
            for dx, dy in ((offset, offset), (offset * 0.5, offset * 0.5)):
                painter.fillPath(text_path.translated(dx, dy), shadow)
        if profile.outline_enabled:
            from PySide6.QtGui import QPainterPathStroker

            stroker = QPainterPathStroker()
            stroker.setWidth(max(1.5, layout.font_size_px * 0.085))
            stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            outline = stroker.createStroke(text_path)
            painter.fillPath(outline, QColor(0, 0, 0, 215))
        painter.fillPath(text_path, color)

    if profile.cursor_enabled:
        if typed_count == 0:
            cursor_line = 0
            cursor_x = line_lefts[0] if line_lefts else layout.block_x
        else:
            last_index = typed_count - 1
            if last_index < len(layout.line_of_char):
                cursor_line = layout.line_of_char[last_index]
                column = layout.pos_in_line[last_index] + 1
            else:
                cursor_line = len(layout.wrapped_lines) - 1
                column = len(layout.wrapped_lines[cursor_line])
            line_text = layout.wrapped_lines[cursor_line]
            advance = metrics.horizontalAdvance(line_text[:column])
            cursor_x = line_lefts[cursor_line] + advance
        if cursor_visible_at(time):
            top = layout.block_top + cursor_line * layout.line_height
            cursor_rect = QRectF(
                cursor_x + layout.font_size_px * 0.06,
                top + layout.line_height * 0.11,
                max(2.0, layout.font_size_px * 0.5),
                layout.line_height * 0.78,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            cursor_color = QColor(*profile.color)
            cursor_color.setAlpha(235)
            painter.fillRect(cursor_rect, cursor_color)


# ---------------------------------------------------------------------------
# Production asset rendering (deterministic; cached)
# ---------------------------------------------------------------------------
def render_intro_asset(
    profile: TypewriterProfile,
    timeline: TypewriterTimeline,
    width: int,
    height: int,
    fps: float,
    cache_dir: Path,
    ffmpeg: Path | str,
    *,
    progress_cb=None,
) -> tuple[Path, Path]:
    """Render the intro video + SFX wav into the intro asset cache.

    Returns ``(video_path, audio_path)``. Both outputs are pure functions of
    the profile identity, so cache hits are byte-reusable.
    """
    import subprocess

    from PySide6.QtGui import QImage

    identity = typewriter_identity(profile, width, height, fps)
    asset_dir = Path(cache_dir) / identity[:2]
    asset_dir.mkdir(parents=True, exist_ok=True)
    video_path = asset_dir / f"{identity}.mp4"
    audio_path = asset_dir / f"{identity}.wav"

    if video_path.is_file() and audio_path.is_file() and video_path.stat().st_size > 0:
        return video_path, audio_path

    synthesize_sfx_wav(timeline, profile, audio_path)

    layout = build_layout(profile, timeline, width, height)
    background = TypewriterBackground(profile, width, height)
    total_frames = max(2, int(round(timeline.total_duration * fps)))

    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-r", f"{fps}",
        "-s", f"{width}x{height}", "-i", "-",
        "-c:v", "libx264", "-preset", "medium", "-crf", "17",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(video_path),
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    image = QImage(width, height, QImage.Format.Format_RGB32)
    try:
        from PySide6.QtGui import QPainter

        for frame in range(total_frames):
            time = frame / fps
            image.fill(0xFF000000)
            painter = QPainter(image)
            try:
                draw_typewriter_frame(
                    painter, profile, timeline, layout, background,
                    width, height, time,
                )
            finally:
                painter.end()
            rgb = image.convertToFormat(QImage.Format.Format_RGB888)
            process.stdin.write(bytes(rgb.constBits().tobytes()))
            if progress_cb and frame % max(1, total_frames // 40) == 0:
                progress_cb(f"Typewriter-Intro: Frame {frame + 1}/{total_frames}")
        process.stdin.close()
        stderr = process.stderr.read() if process.stderr else b""
        process.wait(timeout=600)
    except Exception:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except Exception:
            pass
        process.kill()
        try:
            process.wait(timeout=10)
        except Exception:
            pass
        raise
    if process.returncode != 0 or not video_path.is_file():
        detail = (stderr or b"").decode("utf-8", "replace").strip()[:400]
        raise RuntimeError(f"Typewriter-Intro-Rendering fehlgeschlagen: {detail}")
    return video_path, audio_path


# ---------------------------------------------------------------------------
# Merge of intro + program (reuses the existing transition system)
# ---------------------------------------------------------------------------
def merge_intro_command(
    ffmpeg: str,
    intro_video: Path,
    intro_audio: Path,
    program_video: Path,
    output_path: Path,
    *,
    fps: float,
    width: int,
    height: int,
    transition_key: str,
    transition_ease: str,
    transition_duration: float,
    intro_duration: float,
    program_duration: float,
    encoder_args: list[str],
    music_path: Path | None = None,
    music_volume: float | None = None,
) -> tuple[list[str], float]:
    """ffmpeg command that prepends the intro with the selected transition.

    Returns ``(command, effective_transition_duration)``. Uses the SAME
    xfade expression system as the clip transitions (requirement 12) and an
    ``acrossfade`` audio blend; the intro's tail is a silent hold, so the
    program audio fades in cleanly.

    ``music_path`` implements the optional "music continues during intro"
    mode deterministically inside the filtergraph: the first ``intro_duration``
    seconds of the music file are mixed under the intro SFX at the project's
    music volume. The default mode (no music path) leaves the program's
    historical music placement untouched.
    """
    from .transition_effects import xfade_expression

    td = float(transition_duration)
    td = max(0.05, min(td, intro_duration - 0.05, program_duration - 0.05))
    offset = max(0.0, intro_duration - td)
    expression = xfade_expression(transition_key, transition_ease)

    inputs = [
        "-i", str(intro_video),
        "-i", str(program_video),
        "-i", str(intro_audio),
    ]
    intro_audio_label = "[2:a]"
    if music_path is not None:
        inputs += ["-i", str(music_path)]
        volume = max(0.0, min(1.0, float(music_volume or 0.0) / 100.0))
        intro_audio_label = "[introa]"
        music_chain = (
            f"[3:a]aresample=48000,atrim=0:{intro_duration:.6f},"
            f"asetpts=PTS-STARTPTS,volume={volume:.6f}[twmusic];"
            f"[2:a]aresample=48000[twsfx];"
            f"[twsfx][twmusic]amix=inputs=2:normalize=0[introa];"
        )
    else:
        music_chain = f"[2:a]aresample=48000[introa];"
    # Both xfade inputs MUST share one timebase, frame rate, pixel format and
    # SAR - the intro is rendered at exactly these values, the program is
    # normalized defensively here as well.
    video_filter = (
        f"[0:v]settb=AVTB,fps={fps},format=yuv420p,setsar=1[intro];"
        f"[1:v]settb=AVTB,fps={fps},format=yuv420p,setsar=1[prog];"
        f"[intro][prog]xfade=transition=custom:expr='{expression}'"
        f":duration={td:.6f}:offset={offset:.6f},format=yuv420p,setsar=1[vout];"
        f"{music_chain}"
        f"[introa][1:a]acrossfade=d={td:.6f}:c1=tri:c2=tri[aout]"
    )
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", video_filter,
        "-map", "[vout]", "-map", "[aout]",
        *encoder_args,
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k",
        "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-max_muxing_queue_size", "4096",
        "-metadata:s:v:0", "rotate=0",
        str(output_path),
    ]
    return command, td
