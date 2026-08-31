from __future__ import annotations

TRANSITION_OPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "smooth_blur",
        "Smooth Blur Crossfade",
        "Moderner Blur-Crossfade: A wird weich, B erscheint weich und wird scharf.",
    ),
    (
        "cross_dissolve",
        "Cross Dissolve",
        "Klassischer professioneller NLE-Dissolve mit weicher Überblendkurve.",
    ),
    (
        "film_dissolve",
        "Film Dissolve",
        "Cineastische Annäherung mit linear-light-artiger Luma-Mischung und subtiler Weichheit.",
    ),
    (
        "additive_dissolve",
        "Additive Dissolve",
        "Kontrollierte additive Luma-Mischung mit dezenter Helligkeitsbetonung.",
    ),
)

EASE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("linear", "Linear"),
    ("ease_in", "Ease In"),
    ("ease_out", "Ease Out"),
    ("ease_in_out", "Ease In + Ease Out"),
)

_VALID_TRANSITIONS = {key for key, _label, _description in TRANSITION_OPTIONS}
_VALID_EASES = {key for key, _label in EASE_OPTIONS}


def normalize_transition(value: str) -> str:
    return value if value in _VALID_TRANSITIONS else "cross_dissolve"


def normalize_ease(value: str) -> str:
    return value if value in _VALID_EASES else "ease_in_out"


def transition_label(value: str) -> str:
    key = normalize_transition(value)
    return next(label for option, label, _description in TRANSITION_OPTIONS if option == key)


def transition_description(value: str) -> str:
    key = normalize_transition(value)
    return next(description for option, _label, description in TRANSITION_OPTIONS if option == key)


def ease_expression(value: str) -> str:
    key = normalize_ease(value)
    if key == "linear":
        return "P"
    if key == "ease_in":
        return "P*P"
    if key == "ease_out":
        return "1-(1-P)*(1-P)"
    # Smoothstep: zero slope at both ends and a balanced midpoint.
    return "P*P*(3-2*P)"


def xfade_expression(transition: str, ease: str) -> str:
    """Return an FFmpeg xfade custom expression for prepared yuv420p frames.

    Film and additive modes are documented visual approximations. They do not
    claim to reproduce proprietary NLE internals.
    """
    kind = normalize_transition(transition)
    # FFmpeg xfade's P is 1 at the start and 0 at the end. Convert it to the
    # conventional forward progress q=1-P before applying the user curve so
    # every expression moves from input A to input B.
    forward_ease = ease_expression(ease).replace("P", "(1-P)")
    e = f"({forward_ease})"
    base = f"A*(1-{e})+B*{e}"
    if kind == "film_dissolve":
        # Approximate a linear-light film dissolve on luma while chroma uses a
        # normal eased dissolve. Inputs are explicitly 8-bit yuv420p.
        film_luma = f"255*pow(pow(A/255,2)*(1-{e})+pow(B/255,2)*{e},0.5)"
        return f"if(eq(PLANE,0),{film_luma},{base})"
    if kind == "additive_dissolve":
        # Tasteful midpoint lift only on luma; 8% avoids flash/strobe behavior.
        midpoint = f"4*{e}*(1-{e})"
        additive_luma = f"min(255,{base}+0.08*min(A,B)*{midpoint})"
        return f"if(eq(PLANE,0),{additive_luma},{base})"
    return base


def transition_blur_sigma(transition: str, width: int, height: int) -> float:
    kind = normalize_transition(transition)
    if kind == "smooth_blur":
        return max(8.0, min(24.0, min(width, height) / 90.0))
    if kind == "film_dissolve":
        return max(1.5, min(4.0, min(width, height) / 360.0))
    return 0.0
