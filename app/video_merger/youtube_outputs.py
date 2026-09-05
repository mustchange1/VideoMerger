"""Planning and settings helpers for YouTube Long-Form and Shorts exports.

The planner is intentionally independent from FFmpeg. It gives the GUI, CLI
and the real render orchestrator one deterministic contract: one Long-Form
job, or one Short job for every ordered voiceover unit. A global script remains
one global source; it is never copied into the project's matched-script list.
The Long-Form always receives the complete global script, while an individual
Short receives only the section its own voiceover speaks — derived once by the
orchestrator (see :mod:`script_sections`) and handed to :func:`short_settings`.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import VideoMergerError
from .models import (
    LONG_FORM_INTRO_SECONDS,
    LONG_FORM_OUTRO_SECONDS,
    SHORT_INTRO_SECONDS,
    ExportSettings,
)
from .opening_effects import OPENING_EFFECT_NONE, normalize_opening_effect
from .project_assets import read_script
from .subtitle_presets import get_preset
from .subtitles import normalize_subtitle_animation
from .voiceover_order import normalize_voiceover_order_mode, voiceover_order_indices

EXPORT_MODE_LONG_FORM = "long_form"
EXPORT_MODE_SHORTS = "shorts"
EXPORT_MODE_COMBINED = "long_form_and_shorts"
EXPORT_MODES = (EXPORT_MODE_LONG_FORM, EXPORT_MODE_SHORTS, EXPORT_MODE_COMBINED)
EXPORT_MODE_LABELS = {
    EXPORT_MODE_LONG_FORM: "YouTube Long-Form",
    EXPORT_MODE_SHORTS: "YouTube Shorts",
    EXPORT_MODE_COMBINED: "YouTube Long-Form + YouTube Shorts",
}

#: Historical fixed Short ending (Phase 21): every Short ended with this much
#: video-only material after its own voiceover. It is superseded by the
#: configurable :data:`~app.video_merger.models.SHORT_OUTRO_SECONDS` (1.5 s),
#: which is the Short's visual outro now — the value is *replaced*, never added,
#: so a Short can never contain a duplicated visible ending. The constant stays
#: as the guaranteed video-only tail for legacy settings objects that predate
#: the new field, and it documents the timing guarantee that still holds: the
#: spoken audio is the authoritative duration, the caption timeline ends with
#: it, and the outro material comes from the existing video timeline logic (clip
#: selection, transitions, Hold/Loop and chunking), not from a new renderer.
SHORT_ENDING_SECONDS = 0.7


def visual_section_seconds(value: object, *, label: str) -> float:
    """Validate one visual-only section duration.

    ``0`` is a valid, explicit "no section". Negative, non-numeric or infinite
    values are rejected instead of being silently clamped, because a wrong
    section length would desynchronize the voiceover-driven timeline.
    """
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise VideoMergerError(
            f"{label} must be a number of seconds (0 disables it), not {value!r}."
        ) from exc
    if not math.isfinite(seconds):
        raise VideoMergerError(f"{label} must be a finite number of seconds, not {value!r}.")
    if seconds < 0.0:
        raise VideoMergerError(
            f"{label} cannot be negative ({seconds:.3f} s); use 0 to disable the section."
        )
    return round(seconds, 3)


def effective_intro_seconds(settings: object) -> float:
    """Return the canonical visual-only intro of one render.

    ``visual_intro_seconds`` is the single source of truth: the Long-Form and
    Short planners below copy their own user-facing value into it, and the
    timeline, the audio graph, the subtitle offset and the cache fingerprint all
    read this one number.
    """
    return visual_section_seconds(
        getattr(settings, "visual_intro_seconds", 0.0), label="Visual intro"
    )


def effective_outro_seconds(settings: object) -> float:
    """Return the canonical visual-only outro (the single ``final_pause`` tail).

    The legacy Main Video End Padding and the explicit Long-Form/Short outro
    setting are the *same* timeline section, so there is exactly one tail and it
    can never be applied twice.
    """
    return visual_section_seconds(getattr(settings, "final_pause", 0.0), label="Visual outro")


@dataclass(frozen=True, slots=True)
class MainTimeline:
    """Canonical structure of one voiceover-driven Main Video.

    ``[visual intro][voiceover + normal video][visual outro]``

    This is the single source of truth for the voiceover start, the spoken end,
    the subtitle window and the render target of a job — the video timeline, the
    audio graph, the subtitle offset, the Shorts pool reservation and the log all
    derive from these three numbers, so no pathway can disagree with another.
    Both visual sections contain moving material from the normal video timeline
    (never black or unintentionally frozen frames) and no voiceover audio; the
    caption timeline covers the spoken part only.
    """

    intro: float
    spoken: float
    outro: float

    @property
    def voiceover_start(self) -> float:
        """Program time at which the first voiceover sample is audible."""
        return self.intro

    @property
    def spoken_end(self) -> float:
        """Program time at which the last voiceover sample ends."""
        return self.intro + self.spoken

    #: Subtitles start exactly with the voiceover and end exactly with it.
    subtitle_start = voiceover_start
    subtitle_end = spoken_end

    @property
    def target(self) -> float:
        """Complete video duration the clip selection must cover."""
        return self.intro + self.spoken + self.outro

    @property
    def audio_program(self) -> float:
        """Window for music and clip-original audio.

        Music may already play during the visual intro (it starts at program
        time 0) and, exactly like the historical end padding, stops when the
        spoken content ends — the visual outro stays without music.
        """
        return self.intro + self.spoken

    def log_lines(self) -> list[str]:
        """Concise timeline log for one job (no per-frame spam)."""
        return [
            (
                f"Timeline: Intro {self.intro:.3f} s (visual only) · "
                f"Voiceover start {self.voiceover_start:.3f} s · "
                f"Spoken {self.spoken:.3f} s · Spoken end {self.spoken_end:.3f} s · "
                f"Outro {self.outro:.3f} s (visual only) · Target {self.target:.3f} s"
            ),
            (
                f"Subtitles: start {self.subtitle_start:.3f} s · end {self.subtitle_end:.3f} s · "
                "no caption in the visual intro or outro"
            ),
        ]


def main_timeline(settings: object, voice_total: float) -> MainTimeline:
    """Build the canonical timeline of one render from its settings."""
    spoken = visual_section_seconds(voice_total, label="Voiceover timeline")
    return MainTimeline(
        intro=effective_intro_seconds(settings),
        spoken=spoken,
        outro=effective_outro_seconds(settings),
    )


@dataclass(frozen=True, slots=True)
class ShortJob:
    """One independent Short render, including its authoritative audio unit."""

    index: int
    voiceover_path: Path
    script_path: Path | None
    output_name: str
    cache_key: str


def normalize_export_mode(value: str | None) -> str:
    raw = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "long_form": EXPORT_MODE_LONG_FORM,
        "longform": EXPORT_MODE_LONG_FORM,
        "youtube_long_form": EXPORT_MODE_LONG_FORM,
        "shorts": EXPORT_MODE_SHORTS,
        "short": EXPORT_MODE_SHORTS,
        "youtube_shorts": EXPORT_MODE_SHORTS,
        "long_form_and_shorts": EXPORT_MODE_COMBINED,
        "longform_and_shorts": EXPORT_MODE_COMBINED,
        "combined": EXPORT_MODE_COMBINED,
        "both": EXPORT_MODE_COMBINED,
        "youtube_long_form_and_youtube_shorts": EXPORT_MODE_COMBINED,
    }
    return aliases.get(raw, EXPORT_MODE_LONG_FORM)


def _paths(values: list[str]) -> list[Path]:
    return [Path(value).expanduser().resolve() for value in values if str(value).strip()]


def _effective_voiceovers(settings: ExportSettings) -> list[Path]:
    values = list(getattr(settings, "voiceover_paths", []) or [])
    if not values and str(getattr(settings, "voiceover_path", "") or "").strip():
        values = [settings.voiceover_path]
    paths = _paths(values)
    indices = voiceover_order_indices(
        paths, normalize_voiceover_order_mode(getattr(settings, "voiceover_order_mode", "natural"))
    )
    return [paths[index] for index in indices]


def _matched_scripts(settings: ExportSettings, ordered_units: list[Path]) -> list[Path | None]:
    raw = list(getattr(settings, "script_paths", []) or [])
    if not raw and str(getattr(settings, "script_path", "") or "").strip():
        raw = [settings.script_path]
    scripts = _paths(raw)
    by_stem = {path.stem.casefold(): path for path in scripts}
    original_units = _paths(list(getattr(settings, "voiceover_paths", []) or []))
    if not original_units and str(getattr(settings, "voiceover_path", "") or "").strip():
        original_units = _paths([settings.voiceover_path])
    positional = len(scripts) == len(original_units)
    # The normal GUI contract matches by basename first, then preserves a
    # complete positional assignment for legacy projects.
    result: list[Path | None] = []
    for unit in ordered_units:
        result.append(by_stem.get(unit.stem.casefold()))
    if positional:
        by_original = {unit: scripts[index] for index, unit in enumerate(original_units)}
        result = [path or by_original.get(unit) for path, unit in zip(result, ordered_units)]
    return result


def build_short_jobs(settings: ExportSettings) -> list[ShortJob]:
    """Create one stable job per configured voiceover, independent of scripts."""
    units = _effective_voiceovers(settings)
    matched = str(getattr(settings, "script_mode", "single")).casefold() in {"matched", "individual"}
    global_value = str(getattr(settings, "global_script_path", "") or "").strip()
    if not global_value and not matched:
        raw_scripts = list(getattr(settings, "script_paths", []) or [])
        if not raw_scripts and str(getattr(settings, "script_path", "") or "").strip():
            raw_scripts = [settings.script_path]
        global_value = raw_scripts[0] if raw_scripts else ""
    global_path = Path(global_value).expanduser().resolve() if global_value else None
    scripts = _matched_scripts(settings, units) if matched else [global_path] * len(units)
    jobs: list[ShortJob] = []
    for index, (unit, script) in enumerate(zip(units, scripts), start=1):
        # The index is deliberately part of the identity even when two rows
        # point to the same file. Shorts are output jobs, not deduplicated audio
        # assets, and must never share a cache result across rows.
        digest = hashlib.sha256(f"short:{index}:{unit}".encode("utf-8")).hexdigest()[:16]
        jobs.append(ShortJob(index, unit, script, f"{index:03d}", f"youtube-short-{index:03d}-{digest}"))
    return jobs


def long_form_settings(settings: ExportSettings) -> ExportSettings:
    """Return the landscape settings used by the Long-Form branch.

    ``music_path`` is deliberately left untouched: it is the Long-Form (and
    basic merge) background music, and the separate Shorts track below is
    never mixed into a landscape render.
    """
    style = str(getattr(settings, "subtitle_style", "long_1") or "long_1")
    # The generic subtitle controls are the Long-Form profile. A stale Short
    # preset from a 9:16 project must never leak into the landscape job; valid
    # saved Long-Form overrides remain untouched.
    preset = get_preset(style)
    if preset.key != style or preset.collection != "long":
        style = "long_1"
    return replace(
        settings,
        export_mode=EXPORT_MODE_LONG_FORM,
        aspect="16:9",
        output_preset="youtube_landscape",
        subtitle_style=style,
        # Canonical visual-only sections of the Long-Form timeline. ``final_pause``
        # IS the Long-Form outro: one single tail field, so the legacy Main Video
        # End Padding and this explicit outro setting can never stack into a
        # duplicated visible ending.
        visual_intro_seconds=visual_section_seconds(
            getattr(settings, "long_form_intro_seconds", LONG_FORM_INTRO_SECONDS),
            label="Long-Form Intro",
        ),
        final_pause=visual_section_seconds(
            getattr(settings, "long_form_outro_seconds", LONG_FORM_OUTRO_SECONDS),
            label="Long-Form Outro",
        ),
        # Long-Form animations stay selectable as they are, but a deprecated
        # Outline Highlight from an old project is migrated to a clean effect.
        subtitle_animation=normalize_subtitle_animation(
            getattr(settings, "subtitle_animation", ""), "long"
        ),
        # The subtle opening effect belongs to the Main Video (Long-Form).
        opening_effect=normalize_opening_effect(
            getattr(settings, "opening_effect", OPENING_EFFECT_NONE)
        ),
        render_variant_key="youtube-long-form",
    )


#: ``script_section`` sentinel: this voiceover speaks no part of the global
#: script, so its Short is an audio-only job and must not caption text that the
#: complete script happens to contain.
NO_SCRIPT_SECTION = "no_script_section"


def short_settings(
    settings: ExportSettings,
    job: ShortJob,
    script_section: Path | str | None = None,
) -> ExportSettings:
    """Return isolated vertical settings for one Short job.

    ``script_section`` carries the derived part of a global script for this
    job's voiceover (see :mod:`script_sections`): a path uses exactly that
    section as the Short's authoritative script, :data:`NO_SCRIPT_SECTION`
    leaves the Short without captions, and the default ``None`` keeps the
    project's own script configuration (individual/matched scripts, or a
    project whose sections could not be derived).
    """
    script_mode = "matched" if str(getattr(settings, "script_mode", "single")).casefold() in {"matched", "individual"} else "single"
    # ``section`` is ``None`` when no section was derived (keep the project's own
    # script configuration), ``""`` when this voiceover speaks no part of the
    # global script, and otherwise the resolved path of the derived section.
    section: str | None = None
    if script_section is not None:
        section = (
            "" if str(script_section) == NO_SCRIPT_SECTION
            else str(Path(script_section).expanduser().resolve())
        )
    if script_mode == "matched" or section is None:
        scripts = [str(job.script_path)] if job.script_path is not None else []
        script_path = str(job.script_path) if job.script_path is not None else ""
        global_script = "" if script_mode == "matched" else script_path
    else:
        scripts = [section] if section else []
        script_path = section
        global_script = section
    # Single Global Script mode cannot render subtitles without a script, so a
    # voiceover with no spoken section becomes an explicit audio-only Short
    # instead of failing the complete export run.
    subtitle_enabled = bool(settings.subtitle_enabled)
    if script_mode == "single" and section is not None and not section:
        subtitle_enabled = False
    style = str(getattr(settings, "short_subtitle_style", "short_1") or "short_1")
    if get_preset(style).collection != "short":
        style = "short_1"
    # Canonical visual-only sections of one Short. The configurable Short outro
    # REPLACES the historical fixed 0.7 s ending (never adds to it), and it keeps
    # that guaranteed video-only tail for legacy settings objects that do not
    # carry the new field yet.
    short_intro = visual_section_seconds(
        getattr(settings, "short_intro_seconds", SHORT_INTRO_SECONDS), label="Short Intro"
    )
    legacy_outro = getattr(settings, "short_outro_seconds", None)
    short_outro = (
        SHORT_ENDING_SECONDS
        if legacy_outro is None
        else visual_section_seconds(legacy_outro, label="Short Outro")
    )
    return replace(
        settings,
        export_mode=EXPORT_MODE_SHORTS,
        aspect="9:16",
        output_preset="youtube_vertical",
        resolution="Auto",
        voiceover_paths=[str(job.voiceover_path)],
        voiceover_path=str(job.voiceover_path),
        script_paths=scripts,
        script_path=script_path,
        global_script_path=global_script,
        subtitle_enabled=subtitle_enabled,
        # Strictly separate background music: a Short plays only its own
        # selected track, and an unselected Shorts track means no music at all.
        # The Long-Form track above is never mixed into a vertical render.
        music_path=str(getattr(settings, "short_music_path", "") or ""),
        # A Short is one acoustic unit. Inter-unit silence belongs only to the
        # combined Long-Form timeline, never to an individual Short.
        voiceover_pause=0.0,
        # The spoken audio stays the authoritative duration. The Short begins
        # with its own visual-only intro and continues with video-only material
        # for its visual outro; both come from the normal video timeline.
        visual_intro_seconds=short_intro,
        final_pause=short_outro,
        subtitle_style=style,
        # Word Highlight is not available for Shorts and Outline Highlight is
        # unsafe: both migrate here, at the job boundary, so no Short can render
        # a removed or deprecated animation even from an old project file.
        subtitle_animation=normalize_subtitle_animation(
            getattr(settings, "short_subtitle_animation", ""), "short"
        ),
        # The opening visual effect is a Main Video (Long-Form) feature.
        opening_effect=OPENING_EFFECT_NONE,
        subtitle_font=str(getattr(settings, "short_subtitle_font", "inter") or "inter"),
        subtitle_position=str(getattr(settings, "short_subtitle_position", "Bottom Center") or "Bottom Center"),
        render_variant_key=job.cache_key,
    )


#: Every Short receives one plain-text sidecar with its own script text.
SHORT_SCRIPT_TEXT_SUFFIX = ".txt"


def short_script_text_path(video_path: Path | str) -> Path:
    """Return the ``.txt`` sidecar path that belongs to one Short video.

    The sidecar always follows the *final* video name, including a name that was
    bumped because the target file already existed, so video and text can never
    drift apart and the stable per-Short numbering stays identical.
    """
    return Path(video_path).with_suffix(SHORT_SCRIPT_TEXT_SUFFIX)


def write_short_script_text(
    video_path: Path | str,
    script_path: Path | str | None,
) -> Path | None:
    """Write the exact script text a Short uses next to its rendered video.

    The text is read back from the script that the Short's own render settings
    already resolved — the derived global-script section, its basename-matched
    individual script, or the project's single global script. Nothing is
    transcribed again: no ASR, no alignment and no second subtitle pass runs for
    the sidecar, so the file always matches the spoken/captioned content of that
    Short and never contains text from another Short.

    Returns the written path, or ``None`` when the Short has no script text at
    all (an explicit audio-only Short), in which case no sidecar is created.
    """
    source = str(script_path or "").strip()
    if not source:
        return None
    text = read_script(Path(source).expanduser())
    target = short_script_text_path(video_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # One trailing newline keeps the file a well-formed text file; the script
    # text itself is written exactly as derived (spelling, punctuation and line
    # breaks included) and never re-wrapped or normalized.
    target.write_text(text + "\n", encoding="utf-8")
    return target
