"""1.2.4: generated Quote Card section (Stage 2 only).

The quote card is a *synthetic* section: it is rendered entirely by FFmpeg
filter primitives (color source, subtle vignette, hairline accent,
resolution-aware ``drawtext`` lines) and is always silent. It never receives
an ``-i`` input, never enters the voiceover/music/subtitle timeline and must
therefore not shift the timing of the Main section's captions.

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

from .filter_escape import escape_drawtext_text, escape_quoted_value
from .font_manager import resolve_font

# Dark, neutral, high-contrast default palette. The background is intentionally
# a neutral near-black rather than pure black; the architecture keeps this as
# data so a custom background (image/color) can replace it later without
# touching the layout math.
BACKGROUND_HEX = "0x0d1117"
TEXT_HEX = "0xf5f7fa"
ATTRIBUTION_HEX = "0x9fb0c3"
HAIRLINE_HEX = "0x46586e"

QUOTE_FONT_SIZE_RATIO = 0.052      # of min(width, height)
QUOTE_FONT_SIZE_MIN = 28
QUOTE_FONT_SIZE_MAX = 170
QUOTE_FONT_SIZE_FLOOR = 24         # when shrinking to keep at most 4 lines
MAX_QUOTE_LINES = 4
QUOTE_MAX_LINE_RATIO_LANDSCAPE = 0.72
QUOTE_MAX_LINE_RATIO_PORTRAIT = 0.80
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
) -> QuoteLayout:
    """Compute the quote card layout for one target resolution.

    Uses the *same* font-metric measurement (real glyph advances from the
    bundled font file) as the subtitle renderer, so the GUI preview and the
    burned-in result share one source of truth.
    """
    words = " ".join((text or "").split())
    attribution = " ".join((attribution or "").split())
    font = resolve_font(font_key or "inter", bold=True)
    landscape = width >= height

    size = int(_clamp(round(min(width, height) * QUOTE_FONT_SIZE_RATIO),
                      QUOTE_FONT_SIZE_MIN, QUOTE_FONT_SIZE_MAX))
    max_line_w = width * (QUOTE_MAX_LINE_RATIO_LANDSCAPE if landscape
                          else QUOTE_MAX_LINE_RATIO_PORTRAIT)

    def balanced(at_size: int) -> list[str]:
        return _balanced_lines(
            words.split(" "),
            lambda text: font.text_width(text, at_size),
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
    block_top = round((height - total_h) / 2) - round(height * QUOTE_OPTICAL_LIFT)

    hairline_width = max(24, round(width * 0.035))
    hairline_height = max(2, round(min(width, height) * 0.0018))
    hairline_y = block_top - round(size * 0.7)
    if hairline_y < round(height * 0.03):
        hairline_y = None  # keep the accent out of the safe-margin corner

    return QuoteLayout(
        text=words,
        attribution=attribution,
        font_key=(font.key or font_key or "inter"),
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
        pieces.append(f"fontfile='{_filter_path_for(font_path)}'")
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
    """Escape a path for a *quoted* filter option value (single level).

    The surrounding single quotes protect spaces; the inner escapes protect
    a Windows drive colon (``C:``).  A quoted span is copied verbatim by
    pass 1 and escaped only by pass 2, so single-level escaping is correct
    here — unlike the unquoted drawtext ``text`` value.
    """
    normalized = str(path.expanduser().resolve()).replace("\\", "/")
    return escape_quoted_value(normalized)


def quote_video_chain(layout: QuoteLayout, width: int, height: int,
                      fps: int, duration: float, label: str) -> list[str]:
    """Build the single filter chain rendering the generated quote card.

    The chain starts at a ``color`` source (no ``-i`` input) and ends at the
    requested output label (``base{index}``), so the caller can reuse the
    normal transition handling. Audio is produced separately by the caller's
    standard silence branch (``anullsrc``).
    """
    parts: list[str] = [
        f"color=c={BACKGROUND_HEX}:s={width}x{height}:r={int(max(1, fps))}:"
        f"d={_number(duration)}",
        "format=yuv420p,setsar=1",
        # Subtle cinematic vignette (45° angle keeps the edges only lightly
        # darkened; mode=forward darkens outward from the center).
        "vignette=PI/4:mode=forward",
    ]
    if layout.hairline_y is not None:
        parts.append(
            f"drawbox=x={layout.hairline_x}:y={layout.hairline_y}:"
            f"w={layout.hairline_width}:h={layout.hairline_height}:"
            f"c={HAIRLINE_HEX}:t=fill"
        )
    for offset, line_text in enumerate(layout.lines):
        y = layout.line_top + offset * layout.line_height
        parts.append(_drawtext(line_text, layout.font_path, layout.font_size,
                               "(w-text_w)/2", y, TEXT_HEX))
    if layout.attribution and layout.attribution_y is not None:
        parts.append(_drawtext(layout.attribution, layout.font_path,
                               layout.attribution_size, "(w-text_w)/2",
                               layout.attribution_y, ATTRIBUTION_HEX))
    parts[-1] += f"[{label}]"
    return [",".join(parts)]


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")
