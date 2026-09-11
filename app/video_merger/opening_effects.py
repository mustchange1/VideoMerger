"""Optional subtle opening visual effect for the Main Video.

A deliberately small, robust effect set — no animation editor. The effect is a
pure video filter applied to the already assembled visual timeline *before*
subtitles are burned in, so captions stay crisp and unscaled and nothing about
timing changes:

* it touches the opening portion of the program only,
* it always returns to a neutral 1.00x frame at the end of its window, so every
  later frame passes through untouched (a same-size ``scale`` is a lossless copy
  and a full-size centred ``crop`` is a no-op) — the effect can never leave the
  program permanently reframed,
* it never creates black or frozen frames, never duplicates audio and never
  changes the voiceover-driven target duration, because it does not add, drop or
  re-time a single frame.

Implementation note: the zoom is a per-frame ``scale`` (``eval=frame`` with the
``t`` variable) followed by a centred fixed-size ``crop``. ``crop`` alone cannot
zoom — its output size is resolved once at init — and ``zoompan`` re-times
frames, which would put the exact duration guarantee of the voiceover-driven
timeline at risk. Both expressions are verified against a real FFmpeg render.
"""

from __future__ import annotations

OPENING_EFFECT_NONE = "none"
OPENING_EFFECT_ZOOM_IN = "zoom_in"
OPENING_EFFECT_ZOOM_OUT = "zoom_out"

OPENING_EFFECTS: tuple[tuple[str, str], ...] = (
    (OPENING_EFFECT_NONE, "None"),
    (OPENING_EFFECT_ZOOM_IN, "Gentle Zoom In"),
    (OPENING_EFFECT_ZOOM_OUT, "Gentle Zoom Out"),
)
OPENING_EFFECT_KEYS = frozenset({key for key, _label in OPENING_EFFECTS})
OPENING_EFFECT_LABELS = {key: label for key, label in OPENING_EFFECTS}

#: Peak magnification of the opening zoom. 5 % is clearly visible as a gentle
#: professional entrance and far below anything distracting.
OPENING_EFFECT_ZOOM = 0.05
#: Window used when the project has no visual intro at all, so the effect still
#: has an opening portion to work on. The window never exceeds the program.
OPENING_EFFECT_SECONDS = 3.0
#: Shortest meaningful window; anything below this is treated as "no effect"
#: because a zoom over a few frames would only look like a jump cut.
MIN_OPENING_EFFECT_SECONDS = 0.5


def _number(value: float) -> str:
    """Same compact number formatting the command builder emits."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def normalize_opening_effect(value: object) -> str:
    """Normalize a stored/CLI opening-effect value; unknown values mean None."""
    key = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "": OPENING_EFFECT_NONE,
        "off": OPENING_EFFECT_NONE,
        "disabled": OPENING_EFFECT_NONE,
        "zoomin": OPENING_EFFECT_ZOOM_IN,
        "gentle_zoom_in": OPENING_EFFECT_ZOOM_IN,
        "zoom_in_slow": OPENING_EFFECT_ZOOM_IN,
        "zoomout": OPENING_EFFECT_ZOOM_OUT,
        "gentle_zoom_out": OPENING_EFFECT_ZOOM_OUT,
        "zoom_out_slow": OPENING_EFFECT_ZOOM_OUT,
    }
    key = aliases.get(key, key)
    return key if key in OPENING_EFFECT_KEYS else OPENING_EFFECT_NONE


def opening_effect_window(intro_seconds: float, program_seconds: float = 0.0) -> float:
    """Return the opening portion the effect covers, in seconds.

    The visual intro *is* the opening portion. Without an intro the fixed
    default window keeps the effect usable, and an explicit program length caps
    it so a short program never spends its whole runtime inside the effect.
    """
    try:
        intro = max(0.0, float(intro_seconds))
    except (TypeError, ValueError):
        intro = 0.0
    window = intro if intro > MIN_OPENING_EFFECT_SECONDS else OPENING_EFFECT_SECONDS
    try:
        program = float(program_seconds)
    except (TypeError, ValueError):
        program = 0.0
    if program > 0.0:
        window = min(window, program)
    return window if window >= MIN_OPENING_EFFECT_SECONDS else 0.0


def opening_effect_filter(
    effect: str,
    width: int,
    height: int,
    window: float,
    *,
    time_offset: float = 0.0,
) -> str:
    """Return the filter chain for one opening effect, or ``""`` when disabled.

    ``time_offset`` is the start of the rendered window inside the complete
    program (segmented/chunked rendering). Adding it to ``t`` keeps one
    continuous ramp across every segment instead of restarting the zoom in each
    chunk; segments after the window simply evaluate to a neutral 1.00x frame.

    * ``zoom_out`` starts at the peak magnification and pulls back to 1.00x —
      the classic subtle opening reveal.
    * ``zoom_in`` eases from 1.00x up to the peak and settles back to 1.00x, so
      the push-in never ends in a jump when the window closes.
    """
    key = normalize_opening_effect(effect)
    if key == OPENING_EFFECT_NONE or window < MIN_OPENING_EFFECT_SECONDS:
        return ""
    if width <= 0 or height <= 0:
        return ""
    zoom = _number(OPENING_EFFECT_ZOOM)
    span = _number(max(MIN_OPENING_EFFECT_SECONDS, window))
    clock = "t" if time_offset <= 1e-9 else f"(t+{_number(time_offset)})"
    ramp = (
        f"(1-min({clock}/{span},1))"
        if key == OPENING_EFFECT_ZOOM_OUT
        else f"sin(PI*min({clock}/{span},1))"
    )
    factor = f"(1+{zoom}*{ramp})"
    # Even output sizes keep yuv420p valid. Order matters and was verified with
    # real FFmpeg 6.0 and 7.0.2 renders: NOTHING may sit between the per-frame
    # ``scale`` and the fixed-size ``crop``. With ``setsar=1``/``format=`` in
    # between, a ramp whose first frame is larger than a later one (Gentle Zoom
    # Out) reproducibly segfaults FFmpeg, because that intermediate filter
    # negotiates its frame pool from the first frame size. ``setsar=1`` after
    # the crop is safe — the crop output size is constant — and still removes
    # the sub-percent SAR drift a rounded zoom introduces.
    return (
        f"scale=w='trunc(iw*{factor}/2)*2':h='trunc(ih*{factor}/2)*2':"
        f"eval=frame:flags=lanczos,"
        f"crop=w={int(width)}:h={int(height)}:x=(iw-ow)/2:y=(ih-oh)/2,setsar=1"
    )
