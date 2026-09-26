"""Phase 30: Image Timeline & Visual Effects - dedicated, isolated module.

Images become genuine visual timeline elements between normal video clips:

    Video A -> Video B -> Image 1 -> Video C -> Image 2 -> ...

Design guarantees:

* Strictly additive: when the feature is disabled (default) no code path in
  this module touches the render and historical behavior stays byte-identical.
* Long-Form and Shorts are strictly separate profiles; an empty folder list
  keeps the historical video-only behavior for that profile.
* Deterministic: every shuffle, jitter and duration draw is a pure function
  of the configured seed (Python ``random.Random`` with a derived seed).
* Images reuse the EXISTING timeline/transition machinery: they are inserted
  as ``is_image_insertion`` MediaInfo entries; the existing command builder
  renders them (cover-fit + motion + image-only TV effect) and the existing
  transition engine blends every boundary. No second transition system.
* The global TV overlay is one single post-render pass over the complete
  program (videos + images + transitions), applied exactly once.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

# ---------------------------------------------------------------------------
# Canonical option sets
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
IMAGE_INSERTION_MODES: tuple[str, ...] = ("disabled", "every_n", "percentage")
IMAGE_MOTIONS: tuple[str, ...] = ("none", "zoom_in", "zoom_out", "ken_burns", "pan")
IMAGE_TV_EFFECTS: tuple[str, ...] = ("off", "crt_scanlines", "vhs", "broadcast")
FLICKER_SPEEDS: tuple[str, ...] = ("slow", "normal", "fast")
IMAGE_DURATION_CHOICES: tuple[float, ...] = (1.0, 2.0, 2.5, 3.0, 4.0, 5.0)
IMAGE_DURATION_MODES: tuple[str, ...] = ("fixed", "range")

#: Phase 32: dedicated image transition types. "project" follows the
#: profile's video transition (the historical behavior); the other keys are
#: exactly the existing stable transition engine's families plus "none"
#: (a hard cut). No second transition engine is introduced.
IMAGE_TRANSITION_CHOICES: tuple[str, ...] = (
    "project", "cross_dissolve", "smooth_blur", "film_dissolve",
    "additive_dissolve", "none",
)
#: Phase 32: subtle image visual-effect presets. Every effect is a
#: deterministic pure function of time/frame - identical settings always
#: render identical frames. "crt_broadcast" reuses the Phase-30 CRT chain.
IMAGE_VISUAL_EFFECTS: tuple[str, ...] = (
    "none", "soft_shimmer", "gentle_flicker", "film_flicker",
    "crt_broadcast", "soft_glow_pulse",
)
IMAGE_VISUAL_INTENSITIES: tuple[str, ...] = ("low", "medium", "high")
#: Design-tuned intensity factors (polished, never flashy). Each effect
#: scales its subtle base amplitude by this factor.
IMAGE_VISUAL_INTENSITY_FACTORS: dict[str, float] = {
    "low": 0.60, "medium": 1.00, "high": 1.50,
}

MIN_IMAGE_DURATION = 0.5
MAX_IMAGE_DURATION = 15.0
DEFAULT_IMAGE_DURATION = 2.5
DEFAULT_IMAGE_EVERY_N = 4
DEFAULT_IMAGE_SHARE = 20
DEFAULT_MIN_VIDEO_GAP = 2
DEFAULT_EFFECT_INTENSITY = 20
#: Headroom for motion (12% overscan so zoom/pan never reveal empty space).
MOTION_HEADROOM = 0.12
FLICKER_SPEED_VALUES: dict[str, float] = {"slow": 0.15, "normal": 0.35, "fast": 0.70}


def normalize_insertion_mode(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in IMAGE_INSERTION_MODES else "disabled"


def normalize_motion(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in IMAGE_MOTIONS else "zoom_in"


def normalize_tv_effect(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in IMAGE_TV_EFFECTS else "off"


def normalize_flicker_speed(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in FLICKER_SPEEDS else "normal"


def normalize_duration_mode(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in IMAGE_DURATION_MODES else "fixed"


def normalize_image_transition_choice(value: object) -> str:
    """Phase 32: canonical image transition choice ("project" default)."""
    key = str(value or "").strip().casefold()
    return key if key in IMAGE_TRANSITION_CHOICES else "project"


def clamp_image_transition_duration(value: object) -> float | None:
    """Phase 32: explicit image transition duration or None (= project)."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0.0:
        return None
    return round(max(0.1, min(5.0, parsed)), 3)


def normalize_visual_effect(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in IMAGE_VISUAL_EFFECTS else "none"


def normalize_visual_effect_intensity(value: object) -> str:
    key = str(value or "").strip().casefold()
    return key if key in IMAGE_VISUAL_INTENSITIES else "low"


def clamp_image_timeline_duration(value: object) -> float:
    try:
        return max(MIN_IMAGE_DURATION, min(MAX_IMAGE_DURATION, float(value)))
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_DURATION


def clamp_intensity(value: object) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return DEFAULT_EFFECT_INTENSITY


def _natural_key(text: str):
    """Unicode-safe natural sort: numbers inside names compare numerically."""
    parts: list[object] = []
    for chunk in re.split(r"(\d+)", text):
        parts.append(int(chunk) if chunk.isdigit() else chunk.casefold())
    return parts


# ---------------------------------------------------------------------------
# Resolved per-profile configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ImageTimelineProfile:
    """One fully normalized image-timeline configuration (LF or Shorts)."""

    folders: tuple[str, ...]
    mode: str
    every_n: int
    share_percent: int
    min_video_gap: int
    duration_mode: str
    duration: float
    duration_min: float
    duration_max: float
    motion: str
    effect: str
    effect_intensity: int
    flicker_speed: str
    global_effect: str
    global_intensity: int
    global_flicker_speed: str
    # Phase 32: dedicated image transition + subtle image visual effect.
    # Defaults reproduce the historical behavior exactly: "project" keeps
    # the profile's video transition at every image boundary, None follows
    # the project transition duration and "none" adds no effect chain.
    transition_type: str = "project"
    transition_duration: float | None = None
    visual_effect: str = "none"
    visual_effect_intensity: str = "low"

    @property
    def active(self) -> bool:
        """Enabled mode AND at least one usable folder configured."""
        return (
            self.mode != "disabled"
            and any(str(folder).strip() for folder in self.folders)
        )

    @property
    def phase32_active(self) -> bool:
        """True when ANY Phase-32 image setting deviates from the
        historical defaults - only then may identities gain extra keys."""
        return (
            self.transition_type != "project"
            or self.transition_duration is not None
            or self.visual_effect != "none"
        )


def profile_from_settings(settings: object) -> ImageTimelineProfile:
    """Resolve the canonical (per-job) image-timeline fields.

    ``long_form_settings()``/``short_settings()`` map each profile's own
    fields onto these canonical fields BEFORE this call, so the two profiles
    can never leak into each other.
    """
    folders = list(getattr(settings, "timeline_image_folders", None) or [])

    def num(name: str, default: float) -> float:
        try:
            return float(getattr(settings, name, default) or default)
        except (TypeError, ValueError):
            return default

    every_n = max(1, min(100, int(num("timeline_image_every_n", DEFAULT_IMAGE_EVERY_N))))
    share = max(1, min(90, int(num("timeline_image_share_percent", DEFAULT_IMAGE_SHARE))))
    gap = max(1, min(50, int(num("timeline_image_min_video_gap", DEFAULT_MIN_VIDEO_GAP))))
    duration = clamp_image_timeline_duration(num("timeline_image_duration", DEFAULT_IMAGE_DURATION))
    duration_min = clamp_image_timeline_duration(num("timeline_image_duration_min", 2.0))
    duration_max = clamp_image_timeline_duration(num("timeline_image_duration_max", 4.0))
    if duration_min > duration_max:
        duration_min, duration_max = duration_max, duration_min
    return ImageTimelineProfile(
        folders=tuple(str(folder).strip() for folder in folders if str(folder).strip()),
        mode=normalize_insertion_mode(getattr(settings, "timeline_image_mode", "disabled")),
        every_n=every_n,
        share_percent=share,
        min_video_gap=gap,
        duration_mode=normalize_duration_mode(getattr(settings, "timeline_image_duration_mode", "fixed")),
        duration=duration,
        duration_min=duration_min,
        duration_max=duration_max,
        motion=normalize_motion(getattr(settings, "timeline_image_motion", "zoom_in")),
        effect=normalize_tv_effect(getattr(settings, "timeline_image_effect", "off")),
        effect_intensity=clamp_intensity(getattr(settings, "timeline_image_effect_intensity", DEFAULT_EFFECT_INTENSITY)),
        flicker_speed=normalize_flicker_speed(getattr(settings, "timeline_image_flicker_speed", "normal")),
        global_effect=normalize_tv_effect(getattr(settings, "global_tv_effect", "off")),
        global_intensity=clamp_intensity(getattr(settings, "global_tv_effect_intensity", DEFAULT_EFFECT_INTENSITY)),
        global_flicker_speed=normalize_flicker_speed(getattr(settings, "global_tv_flicker_speed", "normal")),
        # Phase 32: canonical per-job image transition + visual effect.
        transition_type=normalize_image_transition_choice(
            getattr(settings, "timeline_image_transition_type", "project")
        ),
        transition_duration=clamp_image_transition_duration(
            getattr(settings, "timeline_image_transition_duration", None)
        ),
        visual_effect=normalize_visual_effect(
            getattr(settings, "timeline_image_visual_effect", "none")
        ),
        visual_effect_intensity=normalize_visual_effect_intensity(
            getattr(settings, "timeline_image_visual_effect_intensity", "low")
        ),
    )


# ---------------------------------------------------------------------------
# Folder scanning (Unicode-safe, tolerant)
# ---------------------------------------------------------------------------
def scan_image_files(folders: tuple[str, ...]) -> list[Path]:
    """Collect supported image files from all folders, deterministic order.

    Missing folders and unsupported/unreadable files are skipped gracefully.
    Unicode paths and filenames are preserved exactly.
    """
    collected: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        root = Path(str(folder).strip()).expanduser()
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: _natural_key(p.name))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if entry.suffix.casefold() not in IMAGE_EXTENSIONS:
                    continue
                resolved = entry.resolve()
                identity = str(resolved)
                if identity in seen:
                    continue
                seen.add(identity)
                collected.append(resolved)
            except OSError:
                continue
    return collected


# ---------------------------------------------------------------------------
# Deterministic image pool (shuffle passes, no immediate repeats)
# ---------------------------------------------------------------------------
class ImagePool:
    """Draw images like a real source pool.

    Within one complete pass every available image is drawn exactly once
    (shuffled); when the pass is exhausted a fresh shuffle starts. The first
    draw of a new pass is never identical to the last draw of the previous
    pass, so an image is never repeated immediately. All randomness comes
    from one seeded ``random.Random`` instance - fully deterministic.
    """

    def __init__(self, files: list[Path], rng: Random):
        self._files = list(files)
        self._rng = rng
        self._queue: list[Path] = []
        self._last_drawn: Path | None = None

    @property
    def size(self) -> int:
        return len(self._files)

    def _refill(self) -> None:
        if not self._files:
            self._queue = []
            return
        batch = list(self._files)
        self._rng.shuffle(batch)
        if self._last_drawn is not None and len(batch) > 1 and batch[0] == self._last_drawn:
            other = next(index for index, item in enumerate(batch) if item != batch[0])
            batch[0], batch[other] = batch[other], batch[0]
        self._queue = batch

    def draw(self) -> Path | None:
        if not self._files:
            return None
        if not self._queue:
            self._refill()
        item = self._queue.pop(0)
        self._last_drawn = item
        return item


def derive_image_seed(*parts: object) -> int:
    """Deterministic seed from stable identity parts (no wall-clock)."""
    payload = "|".join(str(part) for part in parts)
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12], 16)


# ---------------------------------------------------------------------------
# Insertion planning (pure, deterministic)
# ---------------------------------------------------------------------------
def plan_insertion_positions(
    video_count: int,
    profile: ImageTimelineProfile,
    rng: Random,
) -> list[int]:
    """Return occurrence indices AFTER which an image is inserted.

    Every-N: after every N-th video occurrence. Percentage: the timeline
    targets approximately ``share``% image elements, spread evenly with a
    small deterministic jitter so no unnatural clusters form. Both modes
    enforce the minimum video gap between images and never insert before the
    first or after the last video.
    """
    if video_count < 2 or not profile.active:
        return []
    max_last = video_count - 2  # insert between occurrence i and i+1

    def enforce_gap(positions: list[int]) -> list[int]:
        gap = max(1, profile.min_video_gap)
        kept: list[int] = []
        for position in positions:
            position = max(0, min(max_last, position))
            if kept and position - kept[-1] < gap:
                continue
            kept.append(position)
        return kept

    if profile.mode == "every_n":
        step = max(1, profile.every_n)
        positions = list(range(step - 1, video_count - 1, step))
        return enforce_gap(positions)

    if profile.mode == "percentage":
        share = max(1, min(90, profile.share_percent))
        target = int(round(share / max(1, 100 - share) * video_count))
        target = max(1, min(target, max_last + 1))
        stride = (max_last + 1) / target
        positions: list[int] = []
        for slot in range(target):
            base = int(round(stride * (slot + 0.5))) - 1
            jitter_span = max(0, int(stride / 2) - 1)
            jitter = rng.randint(-jitter_span, jitter_span) if jitter_span > 0 else 0
            positions.append(max(0, min(max_last, base + jitter)))
        positions.sort()
        return enforce_gap(positions)

    return []


def image_duration_for(profile: ImageTimelineProfile, rng: Random) -> float:
    """Deterministic per-slot duration: fixed value or seeded range draw."""
    if profile.duration_mode == "range":
        value = rng.uniform(profile.duration_min, profile.duration_max)
        return round(value, 3)
    return float(profile.duration)


# ---------------------------------------------------------------------------
# Identity / caching
# ---------------------------------------------------------------------------
IMAGE_TIMELINE_SCHEMA = "phase30-image-timeline-v1"


def image_plan_identity(
    profile: ImageTimelineProfile,
    image_paths: list[Path],
    positions: list[int],
    widths_heights_fps: tuple[int, int, float],
) -> str:
    """SHA-256 covering everything that changes the image-bearing timeline."""
    from .render_cache import file_signature

    payload = {
        "schema": IMAGE_TIMELINE_SCHEMA,
        "mode": profile.mode,
        "every_n": profile.every_n,
        "share_percent": profile.share_percent,
        "min_video_gap": profile.min_video_gap,
        "duration_mode": profile.duration_mode,
        "duration": profile.duration,
        "duration_min": profile.duration_min,
        "duration_max": profile.duration_max,
        "motion": profile.motion,
        "effect": profile.effect,
        "effect_intensity": profile.effect_intensity,
        "flicker_speed": profile.flicker_speed,
        "positions": list(positions),
        "width": int(widths_heights_fps[0]),
        "height": int(widths_heights_fps[1]),
        "fps": round(float(widths_heights_fps[2]), 6),
        "images": [file_signature(path) for path in image_paths],
    }
    # Phase 32: dedicated image transition + visual effect extend the
    # identity ONLY when they deviate from the historical defaults, so
    # every pre-Phase-32 plan keeps its exact identity and cache reuse.
    if profile.phase32_active:
        payload["phase32"] = {
            "transition_type": profile.transition_type,
            "transition_duration": profile.transition_duration,
            "visual_effect": profile.visual_effect,
            "visual_effect_intensity": profile.visual_effect_intensity,
        }
    canonical = _canonical(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def global_effect_identity(profile: ImageTimelineProfile) -> str:
    payload = {
        "schema": IMAGE_TIMELINE_SCHEMA,
        "global_effect": profile.global_effect,
        "global_intensity": profile.global_intensity,
        "global_flicker_speed": profile.global_flicker_speed,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _canonical(payload: dict) -> str:
    import json

    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Probing + MediaInfo factory
# ---------------------------------------------------------------------------
def probe_image_size(path: Path, ffprobe_path: str | Path) -> tuple[int, int]:
    """Return (width, height) of an image file; (0, 0) when unreadable."""
    import subprocess

    from .platform_utils import hidden_process_flags, safe_subprocess_env

    try:
        completed = subprocess.run(
            [
                str(ffprobe_path), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=20,
            creationflags=hidden_process_flags(), env=safe_subprocess_env(),
        )
        line = (completed.stdout or "").strip().splitlines()[0] if completed.stdout.strip() else ""
        width_text, _, height_text = line.partition(",")
        return int(float(width_text)), int(float(height_text))
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
        return 0, 0


def make_image_media(
    *,
    path: Path,
    duration: float,
    width: int,
    height: int,
    fps: float,
    size: tuple[int, int],
    transition_type: str,
    profile: "ImageTimelineProfile",
):
    """Build a genuine timeline image entry from a Phase-30 image spec.

    The existing image input path in the command builder loops the still for
    the full section duration, so the entry needs no probing at render time;
    ``size`` only feeds geometry diagnostics and the cache fingerprint.

    Phase 32: when the profile carries an explicit image transition type
    (anything other than "project"), it becomes the element's boundary
    transition - video boundaries keep the project transition untouched.
    The dedicated visual effect fields stay empty (historical) unless the
    profile enables one, so unchanged projects keep byte-identical items.
    """
    from .models import MediaInfo

    fps_fraction = f"{int(round(float(fps)))}/1"
    boundary_transition = (
        profile.transition_type
        if profile.transition_type not in ("", "project")
        else transition_type
    )
    visual_effect = normalize_visual_effect(profile.visual_effect)
    return MediaInfo(
        path=path,
        duration=float(duration),
        width=int(size[0] or 0),
        height=int(size[1] or 0),
        effective_width=int(size[0] or 0),
        effective_height=int(size[1] or 0),
        fps=float(fps),
        fps_fraction=fps_fraction,
        video_codec="image2",
        pixel_format="yuv420p",
        sar="1:1",
        dar=f"{width}:{height}" if height else "16:9",
        is_image_insertion=True,
        image_fit_mode="fill",
        image_zoom=100,
        image_filter="natural",
        image_transition_type=boundary_transition,
        image_motion=profile.motion,
        image_effect=profile.effect,
        image_effect_intensity=profile.effect_intensity,
        image_flicker_speed=profile.flicker_speed,
        image_timeline_insertion=True,
        image_visual_effect="" if visual_effect == "none" else visual_effect,
        image_visual_effect_intensity=(
            "" if visual_effect == "none"
            else normalize_visual_effect_intensity(profile.visual_effect_intensity)
        ),
    )


# ---------------------------------------------------------------------------
# Plan application
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ImageTimelineResult:
    media: list = field(default_factory=list)
    inserted: list = field(default_factory=list)  # (position, path, duration)
    identity: str = ""

    @property
    def count(self) -> int:
        return len(self.inserted)


def apply_image_timeline(
    render_media: list,
    profile: ImageTimelineProfile,
    *,
    width: int,
    height: int,
    fps: float,
    transition_type: str,
    seed_parts: tuple,
    ffprobe_path: str | Path,
    log=print,
) -> ImageTimelineResult:
    """Insert planned timeline images into the fitted video sequence.

    Runs AFTER the voiceover-driven video fit, so fitting, ordering, loops
    and continuity logic never see the images. When the profile is inactive
    or no usable image exists, the incoming sequence is returned unchanged
    and the identity stays empty (historical cache identity preserved).
    """
    result = ImageTimelineResult(media=list(render_media))
    if not profile.active:
        return result
    files = scan_image_files(profile.folders)
    if not files:
        log("Phase 30 Bild-Timeline: keine unterstützten Bilddateien gefunden - Video-Timeline bleibt unverändert.")
        return result
    # Phase 31: silently inserted smart-visual video elements are not source
    # videos; without Smart Visuals the flag never exists, so a historical
    # sequence keeps the exact same video count and positions.
    video_count = sum(
        1 for item in render_media
        if not item.is_image_insertion and not getattr(item, "smart_visual_insertion", False)
    )
    if video_count < 2:
        return result
    seed = derive_image_seed(*seed_parts, width, height, round(float(fps), 6))
    rng = Random(seed)
    positions = plan_insertion_positions(video_count, profile, rng)
    if not positions:
        return result
    pool = ImagePool(files, rng)
    duration_rng = Random(seed ^ 0x9E3779B9)

    specs: list[tuple[int, Path, float]] = []
    position_set = list(positions)
    for position in position_set:
        path = pool.draw()
        if path is None:
            continue
        specs.append((position, path, image_duration_for(profile, duration_rng)))

    new_media: list = []
    shift = 0
    video_ordinal = -1
    inserted_paths: list[Path] = []
    for item in render_media:
        new_media.append(item)
        # Phase 31: positions address GENUINE source videos, so interleaved
        # smart-visual elements never shift where a generic image lands. With
        # Smart Visuals disabled the ordinal equals the historical index, so
        # the placement (and every identity derived from it) is unchanged.
        if not item.is_image_insertion and not getattr(item, "smart_visual_insertion", False):
            video_ordinal += 1
            while specs and specs[0][0] == video_ordinal:
                _, path, duration = specs.pop(0)
                new_media.append(make_image_media(
                    path=path, duration=duration, width=width, height=height, fps=fps,
                    size=probe_image_size(path, ffprobe_path),
                    transition_type=transition_type, profile=profile,
                ))
                inserted_paths.append(path)
                result.inserted.append((video_ordinal + shift, path, duration))
                shift += 1
    result.media = new_media
    result.identity = image_plan_identity(
        profile, inserted_paths, position_set, (width, height, fps),
    )
    log(
        f"Phase 30 Bild-Timeline: {result.count} Bild(er) eingefügt "
        f"(Modus {profile.mode}, {len(files)} Datei(en) im Pool, Seed {seed})."
    )
    return result


# ---------------------------------------------------------------------------
# Filter expression builders (production + preview share these)
# ---------------------------------------------------------------------------
def motion_dimensions(width: int, height: int, headroom: float = MOTION_HEADROOM) -> tuple[int, int]:
    def ceil2(value: float) -> int:
        rounded = int(value + 0.999)
        return rounded + (rounded % 2)
    return ceil2(width * (1.0 + headroom)), ceil2(height * (1.0 + headroom))


def cover_crop_chain(width: int, height: int) -> str:
    """AR-preserving cover fit: scale up until covered, then center-crop."""
    return (
        f"scale=w={width}:h={height}:force_original_aspect_ratio=increase"
        f":force_divisible_by=2:flags=lanczos,"
        f"crop=w={width}:h={height}:x=(iw-ow)/2:y=(ih-oh)/2"
    )


def motion_zoompan_chain(
    motion: str,
    width: int,
    height: int,
    duration: float,
    fps_expr: str,
) -> tuple[str, int] | None:
    """Production motion: a deterministic ``zoompan`` pass over the still.

    Returns ``(zoompan_filter, frame_count)`` or ``None`` for motion=none.
    ``zoompan`` evaluates its expressions against the output frame counter
    ``on``, so identical settings always produce identical frames - the whole
    motion is a pure function of (image, duration, fps, motion kind).
    """
    motion = normalize_motion(motion)
    if motion == "none":
        return None
    frames = max(2, int(round(float(duration) * _fps_value(fps_expr))))
    last = max(frames - 1, 1)
    m = MOTION_HEADROOM
    progress = f"(on/{last})"
    if motion == "zoom_in":
        z_expr = f"1+{m}*{progress}"
        x_expr, y_expr = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    elif motion == "zoom_out":
        z_expr = f"1+{m}*(1-{progress})"
        x_expr, y_expr = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    elif motion == "pan":
        z_expr = f"1+{m}"
        x_expr, y_expr = f"(iw-iw/zoom)*{progress}", "(ih-ih/zoom)/2"
    else:  # ken_burns: zoom in while drifting to the lower-right corner
        z_expr = f"1+{m}*{progress}"
        x_expr, y_expr = f"(iw-iw/zoom)*{progress}", f"(ih-ih/zoom)*{progress}"
    chain = (
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}'"
        f":d={frames}:s={width}x{height}:fps={fps_expr}"
    )
    return chain, frames


def _fps_value(fps_expr: str) -> float:
    try:
        if "/" in str(fps_expr):
            num, _, den = str(fps_expr).partition("/")
            return float(num) / float(den or 1)
        return float(fps_expr)
    except (TypeError, ValueError, ZeroDivisionError):
        return 30.0


def motion_preview_crop(
    motion: str,
    width: int,
    height: int,
    progress: float,
) -> str:
    """Static motion preview at one fixed progress value (0..1).

    The preview renders a SINGLE frame, so a constant crop window is exact:
    it is the same geometry the production ``zoompan`` pass shows at that
    progress, computed from the overscanned cover frame.
    """
    motion = normalize_motion(motion)
    if motion == "none":
        return ""
    head_w, head_h = motion_dimensions(width, height)
    m = MOTION_HEADROOM
    p = max(0.0, min(1.0, float(progress)))

    def even(expr: str) -> str:
        return f"2*trunc(({expr})/2)"

    if motion == "zoom_in":
        factor = 1.0 - m * p
    elif motion == "zoom_out":
        factor = 1.0 - m * (1.0 - p)
    else:  # pan and ken_burns keep the full headroom window
        factor = 1.0 - m
    crop_w = even(f"iw*{factor:.6f}")
    crop_h = even(f"ow*{height}/{width}")
    if motion == "pan":
        x_expr, y_expr = f"(iw-ow)*{p:.6f}", "(ih-oh)/2"
    elif motion == "ken_burns":
        x_expr, y_expr = f"(iw-ow)*{p:.6f}", f"(ih-oh)*{p:.6f}"
    else:
        x_expr, y_expr = "(iw-ow)/2", "(ih-oh)/2"
    return (
        f"scale=w={head_w}:h={head_h}:flags=lanczos,"
        f"crop=w='{crop_w}':h='{crop_h}':x='{x_expr}':y='{y_expr}',"
        f"scale=w={width}:h={height}:flags=lanczos"
    )


def motion_chain(
    motion: str,
    width: int,
    height: int,
    duration: float,
    *,
    progress_expr: str | None = None,
) -> str:
    """Preview helper: static motion geometry at a fixed progress.

    Production rendering uses :func:`motion_zoompan_chain`; this wrapper keeps
    the preview API (a constant progress between 0 and 1) and returns the
    corresponding still crop for one frame.
    """
    try:
        progress = float(progress_expr) if progress_expr is not None else 0.5
    except (TypeError, ValueError):
        progress = 0.5
    return motion_preview_crop(motion, width, height, progress)


def tv_effect_chain(
    effect: str,
    intensity: int,
    flicker_speed: str,
    height: int,
    *,
    scope: str = "image",
) -> str:
    """Deterministic TV-effect filter chain.

    scope="image"  -> subtle effect for a single short image section
    scope="global" -> gentler single pass over the complete program
    """
    effect = normalize_tv_effect(effect)
    if effect == "off":
        return ""
    k = clamp_intensity(intensity) / 100.0
    if scope == "global":
        k *= 0.7
    if k <= 0.0:
        return ""
    flick = FLICKER_SPEED_VALUES[normalize_flicker_speed(flicker_speed)]
    h = max(2, int(height))
    if effect == "crt_scanlines":
        depth = min(0.45, 0.45 * k)
        return (
            f"geq=lum='if(mod(Y\\,3)\\,p(X\\,Y)\\,p(X\\,Y)*(1-{depth:.3f}))'"
            f":cb='p(X\\,Y)':cr='p(X\\,Y)'"
        )
    if effect == "vhs":
        noise = max(2, int(round(16 * k)))
        return (
            f"noise=alls={noise}:allf=t+u,"
            f"eq=saturation={1 - 0.18 * k:.3f}:contrast={1 - 0.06 * k:.3f}"
        )
    # broadcast interference: deterministic moving band + light noise.
    # ``geq``'s time variable is the uppercase ``T`` (seconds); the band
    # position is a pure function of (Y, T), so identical inputs always
    # produce identical overlays.
    band_top = round(0.03 * h)
    band_bottom = round(0.08 * h)
    dim = f"{min(0.55, 0.55 * k):.3f}"
    lift = round(28 * k)
    return (
        f"noise=alls={max(2, int(round(8 * k)))}:allf=t,"
        f"geq=lum='if(between(mod(Y+{flick:.3f}*T*{h}\\,{h})\\,{band_top}\\,{band_bottom})"
        f"\\,p(X\\,Y)*(1-{dim})+{lift}\\,p(X\\,Y))':cb='p(X\\,Y)':cr='p(X\\,Y)'"
    )


# ---------------------------------------------------------------------------
# Phase 32: subtle image visual-effect presets (image sections only)
# ---------------------------------------------------------------------------
def image_visual_effect_chain(
    effect: str,
    intensity: str,
    height: int,
) -> str:
    """Deterministic subtle visual effect for an inserted image section.

    Every chain is a pure function of time (``T`` seconds) and frame
    geometry - identical settings always render identical frames, and the
    whole motion/effect stack stays reproducible. Amplitudes stay in the
    "polished, never flashy" band; the coarse Low/Medium/High intensity
    scales each effect's designed base amplitude. ``none`` returns "" and
    adds nothing to the historical chain.

    Effects apply ONLY to image sections (the command builder calls this
    exclusively for timeline/smart image items): normal videos, subtitles
    and audio are never touched.
    """
    effect = normalize_visual_effect(effect)
    if effect == "none":
        return ""
    k = IMAGE_VISUAL_INTENSITY_FACTORS[normalize_visual_effect_intensity(intensity)]
    if effect == "soft_shimmer":
        # Very subtle slow diagonal light band drifting across the image.
        amplitude = round(4.5 * k, 2)  # luminance units out of 255
        return (
            f"geq=lum='min(255\\,max(0\\,p(X\\,Y)+{amplitude:.2f}"
            f"*sin((X/W+Y/H)*6.2832+T*1.3)))':cb='p(X\\,Y)':cr='p(X\\,Y)'"
        )
    if effect == "gentle_flicker":
        # Tiny natural brightness breathing (two deterministic sines that
        # never align into a visible strobe).
        amp_a = round(0.012 * k, 4)
        amp_b = round(0.006 * k, 4)
        return (
            f"eq=brightness='{amp_a:.4f}*sin(2*PI*0.9*T)+{amp_b:.4f}*sin(2*PI*2.3*T+1.1)'"
        )
    if effect == "film_flicker":
        # Projector-like exposure instability: a shutter-synchronized step
        # (deterministic, frame-quantized) plus a slow exposure drift and a
        # very slight saturation variation. No geometric shaking.
        step = round(0.014 * k, 4)
        drift = round(0.007 * k, 4)
        sat = round(0.05 * k, 3)
        return (
            f"eq=brightness='if(mod(floor(T*24)\\,2)\\,{step:.4f}\\,-{step * 0.6:.4f})"
            f"+{drift:.4f}*sin(2*PI*0.35*T)'"
            f":saturation='1+{sat:.3f}*sin(2*PI*0.22*T+0.7)'"
        )
    if effect == "crt_broadcast":
        # Reuses the Phase-30 CRT scanline chain (the existing engine) at a
        # gentle image-scope strength, plus a faint analog flicker.
        scanlines = tv_effect_chain(
            "crt_scanlines", int(round(16 * k)), "normal", height, scope="image"
        )
        flicker_amp = round(0.008 * k, 4)
        flicker = f"eq=brightness='{flicker_amp:.4f}*sin(2*PI*1.7*T)'"
        return f"{scanlines},{flicker}" if scanlines else flicker
    # soft_glow_pulse: a very slow, subtle bloom-style brightness/glow pulse.
    pulse = round(0.020 * k, 4)
    sat_pulse = round(0.04 * k, 3)
    return (
        f"eq=brightness='{pulse:.4f}*(0.5+0.5*sin(2*PI*0.45*T))'"
        f":saturation='1+{sat_pulse:.3f}*(0.5+0.5*sin(2*PI*0.45*T))'"
    )
