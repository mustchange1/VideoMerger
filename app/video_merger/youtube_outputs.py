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
from dataclasses import dataclass, replace
from pathlib import Path

from .models import ExportSettings
from .project_assets import read_script
from .subtitle_presets import get_preset
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

#: Every YouTube Short ends with this much video-only material after its own
#: voiceover has finished: no speech, no voiceover audio and no subtitle — the
#: spoken audio stays the authoritative duration and the caption timeline ends
#: with it, so the ending can never show text from this or another Short. The
#: additional material comes from the existing video timeline logic (clip
#: selection, transitions, Hold/Loop and chunking), not from a new renderer.
SHORT_ENDING_SECONDS = 0.7


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
        # The spoken audio stays the authoritative duration; the Short simply
        # continues with video-only material for the fixed visual ending.
        final_pause=SHORT_ENDING_SECONDS,
        subtitle_style=style,
        subtitle_animation=str(getattr(settings, "short_subtitle_animation", "word_highlight") or "word_highlight"),
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
