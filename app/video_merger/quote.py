"""Generated Quote Card section (1.2.4 intro, full style system in 1.3.0).

The quote card is a *synthetic* section: it is rendered entirely by FFmpeg
filter primitives (color source, subtle cinematic vignette, optional hairline
accent / paper grain, resolution-aware ``drawtext`` lines, optional subtle
zoom) and is always silent. It never receives an ``-i`` input, never enters
the voiceover/music/subtitle timeline and must therefore not shift the timing
of the Main section's captions.

1.3.0 styles (five polished, editorial looks — default is the cleanest and
most readable one):

* ``clean_editorial`` (DEFAULT): warm white / soft beige background, elegant
  serif typography, premium editorial layout, generous whitespace, subtle
  cinematic vignette and a restrained hairline accent.
* ``warm_cinematic``: deep warm brown-black with vignette + subtle film grain.
* ``soft_paper``: soft beige paper tone with a delicate grain texture.
* ``minimal_film``: neutral near-black, maximum reduction, no accent.
* ``elegant_contrast``: charcoal with warm ivory text and a gold hairline.

Manual controls (all optional, defaults preserve a clean readable card):
font, font size (%), font weight, text color, background color, style,
subtle zoom (%), position, safe-area padding (%), duration and transition
duration.

Layout rules (cinematic / editorial / minimal):
* single focal point, slightly above the mathematical center (optical lift);
* at most two font families (display text + attribution, same family,
  different size/weight tone) and never an ultra-thin face (bold file);
* automatic line breaks at word boundaries only — words are never split —
  with visual balance, language-aware hyphen-free wrapping and no lone-word
  final line where a neighbor exists;
* resolution-aware sizing across 1080p/1440p/4K landscape and 9:16 portrait.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .filter_escape import escape_drawtext_text, filter_file_value
from .font_manager import resolve_font
from .paths import project_root

# --------------------------------------------------------------------------- #
# Style system
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QuoteStyleSpec:
    """Visual identity of one quote card style (colors are 0xRRGGBB)."""

    key: str
    label: str
    description: str
    background_hex: str
    text_hex: str
    attribution_hex: str
    hairline_hex: str | None
    grain: bool
    default_font: str


QUOTE_STYLES: dict[str, QuoteStyleSpec] = {
    "clean_editorial": QuoteStyleSpec(
        key="clean_editorial", label="Clean Editorial",
        description=(
            "Warmweißer / weich-beiger Hintergrund, elegante Serifen-Typografie, "
            "großzügiger Weißraum, dezente Vignette und feine Akzentlinie. "
            "Das lesbarste, cleanlyste Standard-Design."
        ),
        background_hex="0xF6F1E7", text_hex="0x232019", attribution_hex="0x6E6353",
        hairline_hex="0xB4A488", grain=False, default_font="lora",
    ),
    "warm_cinematic": QuoteStyleSpec(
        key="warm_cinematic", label="Warm Cinematic",
        description=(
            "Warmer, tiefer Braun-Schwarzton mit Vignette und feinem Filmkorn – "
            "ruhig, atmosphärisch, cineastisch."
        ),
        background_hex="0x171208", text_hex="0xF3E9D2", attribution_hex="0xB39B74",
        hairline_hex="0x8A7350", grain=True, default_font="lora",
    ),
    "soft_paper": QuoteStyleSpec(
        key="soft_paper", label="Soft Paper",
        description=(
            "Weiches Beige-Papier mit zarter Korn-Textur und warmer, ruhiger "
            "Typografie – natürlich und zurückhaltend."
        ),
        background_hex="0xEFE7DA", text_hex="0x3A342B", attribution_hex="0x7C7263",
        hairline_hex=None, grain=True, default_font="inter",
    ),
    "minimal_film": QuoteStyleSpec(
        key="minimal_film", label="Minimal Film",
        description=(
            "Neutraler Near-Black-Hintergrund, reines Weiß, ohne Akzent und ohne "
            "Korn – maximale Reduktion, moderner Film-Look."
        ),
        background_hex="0x0E1013", text_hex="0xF5F6F8", attribution_hex="0x9AA3AD",
        hairline_hex=None, grain=False, default_font="manrope",
    ),
    "elegant_contrast": QuoteStyleSpec(
        key="elegant_contrast", label="Elegant Contrast",
        description=(
            "Edler Kontrast: Charcoal-Hintergrund, warmes Ivory, goldene "
            "Akzentlinie und dezente Vignette – präsent und premium."
        ),
        background_hex="0x14161B", text_hex="0xF2E9DA", attribution_hex="0xC9B37E",
        hairline_hex="0xC9B37E", grain=False, default_font="lora",
    ),
}

DEFAULT_QUOTE_STYLE = "clean_editorial"

# Backwards-compatible module constants: the 1.2.4 names now describe the
# default style (Clean Editorial). The renderer itself reads the resolved
# per-style values from :class:`QuoteLayout`.
BACKGROUND_HEX = QUOTE_STYLES[DEFAULT_QUOTE_STYLE].background_hex
TEXT_HEX = QUOTE_STYLES[DEFAULT_QUOTE_STYLE].text_hex
ATTRIBUTION_HEX = QUOTE_STYLES[DEFAULT_QUOTE_STYLE].attribution_hex
HAIRLINE_HEX = QUOTE_STYLES[DEFAULT_QUOTE_STYLE].hairline_hex or "0xB4A488"


def get_quote_style(key: str) -> QuoteStyleSpec:
    return QUOTE_STYLES.get((key or "").strip(), QUOTE_STYLES[DEFAULT_QUOTE_STYLE])


def normalize_color(value: str, fallback: str) -> str:
    """Normalize a user color (``#RRGGBB`` / ``0xRRGGBB``) to ``0xRRGGBB``."""
    text = (value or "").strip()
    if not text:
        return fallback
    if text.startswith("#"):
        text = "0x" + text[1:]
    if not text.lower().startswith("0x"):
        text = "0x" + text
    digits = text[2:]
    if len(digits) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in digits):
        return fallback
    return "0x" + digits.upper()


QUOTE_FONT_SIZE_RATIO = 0.052      # of min(width, height)
QUOTE_FONT_SIZE_MIN = 28
QUOTE_FONT_SIZE_MAX = 170
QUOTE_FONT_SIZE_FLOOR = 24         # when shrinking to keep at most 4 lines
MAX_QUOTE_LINES = 4
# Safe-area padding (default 8 % of each edge) keeps the 1.2.4 default usable
# line widths: (1 - 2*0.08) * 6/7 = 0.72 landscape, * 20/21 = 0.80 portrait.
QUOTE_SAFE_PADDING_DEFAULT = 8.0
_LINE_RATIO_LANDSCAPE = 6.0 / 7.0
_LINE_RATIO_PORTRAIT = 20.0 / 21.0
QUOTE_MAX_LINE_RATIO_LANDSCAPE = (1.0 - 2 * QUOTE_SAFE_PADDING_DEFAULT / 100.0) * _LINE_RATIO_LANDSCAPE
QUOTE_MAX_LINE_RATIO_PORTRAIT = (1.0 - 2 * QUOTE_SAFE_PADDING_DEFAULT / 100.0) * _LINE_RATIO_PORTRAIT
QUOTE_OPTICAL_LIFT = 0.02          # fraction of height lifted above center
QUOTE_LINE_LEADING = 1.38


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _drawtext_escape(text: str) -> str:
    """Escape a literal value for an *unquoted* drawtext ``text`` option.

    The filtergraph is parsed in two passes (see :mod:`filter_escape`), so
    an unquoted value needs the two-level table.  This delegates to
    :func:`filter_escape.escape_drawtext_text`, which is the only variant
    that can represent apostrophes (a quoted span can never contain one,
    because pass 1's quote mode ends at the first raw ``'``).  The value is
    therefore deliberately left unquoted in the generated filtergraph.
    """
    return escape_drawtext_text(text)


@dataclass(frozen=True, slots=True)
class QuoteLayout:
    """Resolution-specific, fully deterministic quote card geometry."""

    text: str
    attribution: str
    font_key: str
    font_family: str
    font_path: Path | None
    font_size: int
    attribution_size: int
    lines: tuple[str, ...]
    line_height: int
    center_x: int
    line_top: int                      # top of the first text line
    hairline_x: int | None
    hairline_y: int | None
    hairline_width: int | None
    hairline_height: int | None
    attribution_y: int | None
    width: int
    height: int
    # 1.3.0 style/visual resolution (renderer + preview read these).
    style_key: str = DEFAULT_QUOTE_STYLE
    background_hex: str = BACKGROUND_HEX
    text_hex: str = TEXT_HEX
    attribution_hex: str = ATTRIBUTION_HEX
    hairline_hex: str | None = HAIRLINE_HEX
    grain: bool = False
    zoom_percent: float = 0.0
    safe_padding_percent: float = QUOTE_SAFE_PADDING_DEFAULT
    font_weight: str = "bold"

    @property
    def total_block_height(self) -> int:
        return len(self.lines) * self.line_height


def _balanced_lines(
    words: list[str],
    measure,
    limit: float,
    max_lines: int,
) -> list[str]:
    """Balanced wrap: fewest lines that fit within *limit*, equalized.

    Among all wraps that use the *minimum possible* number of lines (no word
    is ever split), the split that minimizes the widest line is chosen, so
    lines come out visually balanced instead of greedy first-fit (which can
    leave a short first line, a full middle line and a stubby tail).  If even
    *max_lines* cannot contain the text within *limit* the legacy greedy
    wrap is returned so callers keep their existing overflow behaviour.
    Deterministic: ties resolve to the earliest (smallest) split point.
    """
    n = len(words)
    if n == 0:
        return [""]
    widths = [measure(w) for w in words]
    space = measure(" ")

    # seg[j][i] = rendered width of words[j:i] (inclusive space gaps).
    seg = [[0.0] * (n + 1) for _ in range(n + 1)]
    for j in range(n):
        acc = 0.0
        for i in range(j + 1, n + 1):
            acc = widths[i - 1] if i == j + 1 else acc + space + widths[i - 1]
            seg[j][i] = acc

    inf = float("inf")
    dp = [[inf] * (n + 1) for _ in range(max_lines + 1)]
    split = [[-1] * (n + 1) for _ in range(max_lines + 1)]
    dp[0][0] = 0.0
    for k in range(1, max_lines + 1):
        row = dp[k]
        prev_row = dp[k - 1]
        prev_split = split[k - 1]  # noqa: F841 (kept for clarity)
        k_split = split[k]
        for i in range(k, n + 1):  # each of the k lines needs >= 1 word
            best = inf
            best_j = -1
            for j in range(k - 1, i):
                base = prev_row[j]
                if base == inf:
                    continue
                cand = base if base >= seg[j][i] else seg[j][i]
                if cand < best:  # strict < => earliest split wins ties
                    best = cand
                    best_j = j
            row[i] = best
            k_split[i] = best_j

    k_star = 0
    for k in range(1, max_lines + 1):
        if dp[k][n] <= limit:
            k_star = k
            break
    if k_star == 0:
        # Nothing fits inside `limit` even with max_lines: legacy greedy.
        lines: list[str] = []
        current = ""
        for word in words:
            trial = word if not current else f"{current} {word}"
            if measure(trial) <= limit:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    lines = []
    i = n
    for k in range(k_star, 0, -1):
        j = split[k][i]
        lines.append(" ".join(words[j:i]))
        i = j
    lines.reverse()
    return lines


def layout_quote(
    text: str,
    attribution: str,
    font_key: str,
    width: int,
    height: int,
    *,
    style_key: str = DEFAULT_QUOTE_STYLE,
    font_size_percent: float = 100.0,
    font_weight: str = "bold",
    text_color: str = "",
    background_color: str = "",
    zoom_percent: float = 0.0,
    position: str = "center",
    safe_padding_percent: float = QUOTE_SAFE_PADDING_DEFAULT,
) -> QuoteLayout:
    """Compute the quote card layout for one target resolution.

    Uses the *same* font-metric measurement (real glyph advances from the
    bundled font file) as the subtitle renderer, so the GUI preview and the
    burned-in result share one source of truth.  All 1.3.0 style controls are
    keyword-only with geometry-preserving defaults.
    """
    style = get_quote_style(style_key)
    words = " ".join((text or "").split())
    attribution = " ".join((attribution or "").split())
    font = resolve_font(font_key or style.default_font, bold=(font_weight != "regular"))

    size = int(_clamp(round(min(width, height) * QUOTE_FONT_SIZE_RATIO
                             * _clamp(float(font_size_percent), 60.0, 160.0) / 100.0),
                      QUOTE_FONT_SIZE_MIN, QUOTE_FONT_SIZE_MAX))
    landscape = width >= height
    padding = _clamp(float(safe_padding_percent), 3.0, 15.0)
    usable = 1.0 - 2.0 * padding / 100.0
    max_line_w = width * usable * (_LINE_RATIO_LANDSCAPE if landscape else _LINE_RATIO_PORTRAIT)

    def balanced(at_size: int) -> list[str]:
        return _balanced_lines(
            words.split(" "),
            lambda line_text: font.text_width(line_text, at_size),
            max_line_w,
            MAX_QUOTE_LINES,
        )

    lines = balanced(size)
    # Very long quotes shrink (bounded) instead of overflowing the block.
    while len(lines) > MAX_QUOTE_LINES and size > QUOTE_FONT_SIZE_FLOOR:
        size -= 2
        lines = balanced(size)

    # Avoid a lone-word final line when the neighbor has room to give one back
    # (mirrors the subtitle singleton repair; never invents or drops words).
    while len(lines) >= 3 and len(lines[-1].split(" ")) == 1 and len(lines[-2].split(" ")) >= 2:
        left_words = lines[-2].split(" ")
        if len(left_words) <= 1:
            break
        candidate = f"{lines[-1]} {left_words[-1]}"
        if font.text_width(candidate, size) <= max_line_w:
            left_words.pop()
            lines[-2] = " ".join(left_words)
            lines[-1] = candidate
        else:
            break

    line_height = round(size * QUOTE_LINE_LEADING)
    block_h = len(lines) * line_height
    attr_size = int(_clamp(round(size * 0.42), 14, 96)) if attribution else 0
    attr_gap = round(size * 0.45) if attribution else 0
    total_h = block_h + attr_gap + attr_size

    # Position: center (default; optical lift above the mathematical center),
    # upper (relaxed upper third) or lower (calm lower area). All positions
    # stay inside the safe area.
    pos = position if position in {"center", "upper", "lower"} else "center"
    if pos == "upper":
        block_top = round(height * 0.30) - round(height * QUOTE_OPTICAL_LIFT)
    elif pos == "lower":
        block_top = round(height * 0.62) - round(height * QUOTE_OPTICAL_LIFT)
    else:
        block_top = round((height - total_h) / 2) - round(height * QUOTE_OPTICAL_LIFT)
    block_top = max(round(height * padding / 100.0), block_top)

    hairline_width = max(24, round(width * 0.035))
    hairline_height = max(2, round(min(width, height) * 0.0018))
    hairline_y = block_top - round(size * 0.7)
    if hairline_y < round(height * 0.03):
        hairline_y = None  # keep the accent out of the safe-margin corner

    return QuoteLayout(
        text=words,
        attribution=attribution,
        font_key=(font.key or font_key or style.default_font),
        font_family=font.family,
        font_path=font.path,
        font_size=size,
        attribution_size=attr_size,
        lines=tuple(lines),
        line_height=line_height,
        center_x=width // 2,
        line_top=block_top,
        hairline_x=(width - hairline_width) // 2 if hairline_y is not None else None,
        hairline_y=hairline_y,
        hairline_width=hairline_width if hairline_y is not None else None,
        hairline_height=hairline_height if hairline_y is not None else None,
        attribution_y=block_top + block_h + attr_gap if attribution else None,
        width=width,
        height=height,
        style_key=style.key,
        background_hex=normalize_color(background_color, style.background_hex),
        text_hex=normalize_color(text_color, style.text_hex),
        attribution_hex=style.attribution_hex,
        hairline_hex=style.hairline_hex,
        grain=style.grain,
        zoom_percent=_clamp(float(zoom_percent), 0.0, 10.0),
        safe_padding_percent=padding,
        font_weight="regular" if font_weight == "regular" else "bold",
    )


def _drawtext(
    text: str,
    font_path: Path | None,
    size: int,
    x: int | str,
    y: int,
    color: str,
) -> str:
    pieces: list[str] = []
    if font_path and font_path.is_file():
        pieces.append(f"fontfile={_filter_path_for(font_path)}")
    pieces.extend([
        f"text={_drawtext_escape(text)}",
        # IMPORTANT: expansion=none keeps the text byte-for-byte literal.
        # The modern FFmpeg text-expansion engine (n8.x) treats ANY '%' that
        # is not part of a %{...} expression as a hard error and then draws
        # NOTHING for that line (verified on n8.1.2-44: '100%%' ->
        # "Stray % near '%'", line silently missing).  With expansion=none a
        # single '%' passes through unchanged, so no percent doubling is
        # needed anywhere in the pipeline (works on FFmpeg >= 4.1 builds).
        "expansion=none",
        f"fontsize={size}",
        f"fontcolor={color}",
        f"x='{x}'",
        f"y={int(y)}",
    ])
    return "drawtext=" + ":".join(pieces)


def _filter_path_for(path: Path) -> str:
    """Filter value for the quote-card font file.

    1.3.0: same Windows-proof strategy as the subtitles filter — the bundled
    font lies under the project root (the FFmpeg working directory), so the
    graph receives a plain relative ASCII path (``tools/fonts/Lora-Bold.ttf``);
    absolute fallbacks are unquoted + two-level escaped (apostrophe-safe).
    """
    return filter_file_value(path, project_root())


def quote_video_chain(layout: QuoteLayout, width: int, height: int,
                      fps: float, duration: float, label: str) -> list[str]:
    """Build the single filter chain rendering the generated quote card.

    The chain starts at a ``color`` source (no ``-i`` input) and ends at the
    requested output label (``base{index}``), so the caller can reuse the
    normal transition handling. Audio is produced separately by the caller's
    standard silence branch (``anullsrc``).

    1.3.0: the composed card receives the style's cinematic treatment
    (subtle vignette on every style, optional paper/film grain) and an
    optional subtle zoom (``zoompan``, one output frame per input frame so
    the section duration is preserved exactly).
    """
    rate = _number(max(1.0, float(fps or 30.0)))
    parts: list[str] = [
        f"color=c={layout.background_hex}:s={width}x{height}:r={rate}:"
        f"d={_number(duration)}",
        "format=yuv420p,setsar=1",
        # Subtle cinematic vignette (45° angle keeps the edges only lightly
        # darkened; mode=forward darkens outward from the center). Present
        # in every style — on the light editorial cards it reads as gentle
        # paper shading rather than a dark frame.
        "vignette=PI/4:mode=forward",
    ]
    if layout.grain:
        # Deliberately faint texture (film grain / paper). allf=t+u keeps it
        # temporal (living) instead of a static pattern.
        parts.append("noise=alls=5:allf=t+u")
    if layout.hairline_y is not None and layout.hairline_hex:
        parts.append(
            f"drawbox=x={layout.hairline_x}:y={layout.hairline_y}:"
            f"w={layout.hairline_width}:h={layout.hairline_height}:"
            f"c={layout.hairline_hex}:t=fill"
        )
    for offset, line_text in enumerate(layout.lines):
        y = layout.line_top + offset * layout.line_height
        parts.append(_drawtext(line_text, layout.font_path, layout.font_size,
                               "(w-text_w)/2", y, layout.text_hex))
    if layout.attribution and layout.attribution_y is not None:
        parts.append(_drawtext(layout.attribution, layout.font_path,
                               layout.attribution_size, "(w-text_w)/2",
                               layout.attribution_y, layout.attribution_hex))
    zoom_percent = _clamp(float(layout.zoom_percent or 0.0), 0.0, 10.0)
    if zoom_percent > 0.05:
        # Subtle cinematic zoom: 1.0 -> 1+zoom% across the whole card, center
        # anchored. d=1 keeps one output frame per input frame and fps pins
        # the output cadence, so the section duration is bit-exact.
        frames = max(1, round(float(fps or 30.0) * max(0.1, float(duration))))
        target = 1.0 + zoom_percent / 100.0
        step = (target - 1.0) / frames
        parts.append(
            f"zoompan=z='min(zoom+{_number(step)},{_number(target)})':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:fps={rate}:s={width}x{height},format=yuv420p,setsar=1"
        )
    parts[-1] += f"[{label}]"
    return [",".join(parts)]


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")
