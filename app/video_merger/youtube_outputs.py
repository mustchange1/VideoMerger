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

import math
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import VideoMergerError
from .image_timeline import (
    clamp_image_timeline_duration,
    clamp_intensity,
    normalize_duration_mode,
    normalize_flicker_speed,
    normalize_insertion_mode,
    normalize_motion,
    normalize_tv_effect,
)
from .music_tracks import effective_short_music_tracks, normalize_sequence_mode
from .models import (
    DEFAULT_TRANSITION_TYPE,
    LONG_FORM_INTRO_SECONDS,
    LONG_FORM_MUSIC_VOLUME,
    LONG_FORM_OUTRO_SECONDS,
    LONG_FORM_TRANSITION_DURATION,
    MAX_MUSIC_VOLUME_PERCENT,
    SHORT_INTRO_SECONDS,
    SHORTS_MUSIC_VOLUME,
    SHORTS_TRANSITION_DURATION,
    TRANSITION_DURATION_LEGACY_DEFAULT,
    ExportSettings,
)
from .opening_effects import OPENING_EFFECT_NONE, normalize_opening_effect
from .project_assets import read_script
from .short_groups import (
    build_short_plan,
    short_cache_key,
    short_music_for,
    short_music_volume_for,
    short_voiceover_pause,
)
from .subtitle_presets import get_preset
from .subtitles import normalize_subtitle_animation
from .transition_effects import normalize_transition
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
#: configurable :data:`~app.video_merger.models.SHORT_OUTRO_SECONDS` (0.7 s),
#: which is the Short's visual outro now — the value is *replaced*, never added,
#: so a Short can never contain a duplicated visible ending. Because the new
#: default equals this historical value, the same guaranteed video-only tail is
#: simply reused as the semantic outro. The constant stays as the fallback for
#: legacy settings objects that predate the new field, and it documents the
#: timing guarantee that still holds: the spoken audio is the authoritative
#: duration, the caption timeline ends with
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


def output_music_volume(
    settings: object, value: object, *, label: str, default: int,
) -> int:
    """Resolve one output's independent background music volume in percent.

    Long-Form and Shorts each own their volume: ``value`` is the
    output-specific setting and always wins when it is configured. ``None`` (a
    project saved before the split, or a direct API caller) falls back to the
    shared :attr:`~app.video_merger.models.ExportSettings.music_volume`, so an
    existing project keeps exactly the loudness it was saved with; a project
    without any of the two receives the 44 % default for both outputs.

    The resolved gain applies to the complete video: the music starts at
    program time 0.000 s, plays under the voiceover and continues through the
    visual outro until the final video endpoint.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        value = getattr(settings, "music_volume", default)
    try:
        percent = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise VideoMergerError(
            f"{label} must be a percentage (0-{MAX_MUSIC_VOLUME_PERCENT}), not {value!r}."
        ) from exc
    if not math.isfinite(percent):
        raise VideoMergerError(f"{label} must be a finite percentage, not {value!r}.")
    return int(max(0, min(MAX_MUSIC_VOLUME_PERCENT, round(percent))))


def output_transition_type(settings: object, value: object, *, label: str) -> str:
    """Resolve one output's transition family.

    An explicit output-specific choice wins; an unconfigured value falls back to
    the shared project transition and finally to the Cross Dissolve default. All
    existing transition types stay available — nothing is removed here.
    """
    text = str(value or "").strip() or str(getattr(settings, "transition_type", "") or "").strip()
    normalized = normalize_transition(text or DEFAULT_TRANSITION_TYPE)
    if not normalized:  # pragma: no cover - normalize_transition never returns ""
        raise VideoMergerError(f"{label} is not a supported transition.")
    return normalized


def output_transition_duration(
    settings: object, value: object, *, label: str, default: float,
) -> float:
    """Resolve one output's transition duration in seconds.

    An explicit output-specific duration always wins. Otherwise the shared
    duration of an existing project or API caller is honored as the migration
    fallback — except when it still carries the historical shared default of
    :data:`~app.video_merger.models.TRANSITION_DURATION_LEGACY_DEFAULT`, which
    is indistinguishable from "never configured" and therefore receives the new
    per-output default (2.0 s for both Long-Form and Shorts).

    ``0`` is valid (hard cut). Negative, non-numeric or infinite values are
    rejected instead of being silently clamped, exactly like the visual
    sections; :func:`app.video_merger.target.safe_transition_durations` still
    bounds the effective value by the real clip durations.
    """
    seconds: float
    if value is None or (isinstance(value, str) and not value.strip()):
        shared = getattr(settings, "transition_duration", None)
        try:
            shared_seconds = None if shared is None else float(shared)
        except (TypeError, ValueError):
            shared_seconds = None
        if shared_seconds is None:
            seconds = default
        elif abs(shared_seconds - TRANSITION_DURATION_LEGACY_DEFAULT) > 1e-9:
            seconds = shared_seconds
        else:
            seconds = default
    else:
        try:
            seconds = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise VideoMergerError(
                f"{label} must be a number of seconds (0 = hard cut), not {value!r}."
            ) from exc
    if not math.isfinite(seconds):
        raise VideoMergerError(f"{label} must be a finite number of seconds, not {value!r}.")
    if seconds < 0.0:
        raise VideoMergerError(
            f"{label} cannot be negative ({seconds:.3f} s); use 0 for a hard cut."
        )
    return round(seconds, 3)


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

    The complete audio contract of one job is expressed here as well:
    ``[video start .. video end]`` is the rendered picture, ``[voiceover start ..
    spoken end]`` is the only part with voiceover and subtitles, and
    ``[music start .. music end]`` is the *complete* video, so configured
    background music plays from 0.000 s through both visual sections until the
    final frame. Without a configured track nothing is invented: the visual
    sections simply stay silent.
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
    def video_start(self) -> float:
        """Program time of the first rendered frame (always 0.000 s)."""
        return 0.0

    @property
    def video_end(self) -> float:
        """Program time of the last rendered frame == the complete target."""
        return self.target

    @property
    def music_start(self) -> float:
        """Background music starts with the very first frame (0.000 s).

        A configured track is never delayed by the visual intro: it plays from
        the video start, continues under the voiceover and keeps playing through
        the visual outro, so the timeline contains no silent gap in front of the
        first spoken word and no silent ending after the last one.
        """
        return 0.0

    @property
    def music_end(self) -> float:
        """Background music ends with the final frame of the video."""
        return self.target

    @property
    def audio_program(self) -> float:
        """Window of the *spoken* program: voiceover and clip-original audio.

        Both are trimmed at the spoken end, so the visual outro never contains
        voiceover — exactly like the historical end padding. Background music is
        deliberately NOT bounded by this value: it covers the complete video
        (:attr:`music_start` → :attr:`music_end`).
        """
        return self.intro + self.spoken

    def log_lines(self, *, music_configured: bool = False) -> list[str]:
        """Concise timeline log for one job (no per-frame spam)."""
        music_line = (
            (
                f"Music: start {self.music_start:.3f} s · end {self.music_end:.3f} s (video end) · "
                "continuous through the visual intro, the voiceover and the visual outro"
            )
            if music_configured
            else "Music: not configured – no background music, the visual sections stay silent"
        )
        return [
            (
                f"Timeline: Intro {self.intro:.3f} s (visual only) · "
                f"Voiceover start {self.voiceover_start:.3f} s · "
                f"Spoken {self.spoken:.3f} s · Spoken end {self.spoken_end:.3f} s · "
                f"Outro {self.outro:.3f} s (visual only) · "
                f"Video start {self.video_start:.3f} s · Video end {self.video_end:.3f} s"
            ),
            music_line,
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
    """One independent Short render, including its authoritative audio unit.

    ``voiceover_path``/``script_path`` stay the FIRST member of the Short, so
    every existing consumer (script sections, transcript sidecars, cache keys,
    logs) keeps working unchanged. ``voiceover_paths``/``script_paths`` list all
    members of a grouped Short in the authoritative list order; for the default
    one-unit Short they hold exactly that single unit.
    """

    index: int
    voiceover_path: Path
    script_path: Path | None
    output_name: str
    cache_key: str
    voiceover_paths: tuple[Path, ...] = ()
    script_paths: tuple[Path | None, ...] = ()

    @property
    def members(self) -> tuple[Path, ...]:
        """All voiceover units of this Short, in render order."""
        return self.voiceover_paths or (self.voiceover_path,)

    @property
    def member_scripts(self) -> tuple[Path | None, ...]:
        """Script of every member, positionally paired with :attr:`members`."""
        return self.script_paths or (self.script_path,)

    @property
    def grouped(self) -> bool:
        """True when several scripts/voiceovers render as this ONE Short."""
        return len(self.members) > 1


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
    """Create one stable job per planned Short, independent of scripts.

    Without configured groups this stays exactly the historical behaviour: one
    job per voiceover unit, numbered from its position in the ordered list,
    named ``NNN`` and keyed with the same cache formula. A configured group
    becomes ONE job carrying every member voiceover in list order, so the
    existing multi-voiceover pipeline renders it on a single video, voiceover,
    subtitle and music timeline (never as concatenated finished Shorts).
    """
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
    for plan in build_short_plan(units, getattr(settings, "short_script_groups", ())):
        # The index is deliberately part of the identity even when two rows
        # point to the same file. Shorts are output jobs, not deduplicated audio
        # assets, and must never share a cache result across rows. A grouped
        # Short keeps the index of its first member and folds every member into
        # its cache digest instead, so membership changes invalidate the entry.
        member_scripts = tuple(
            scripts[position - 1] if 0 < position <= len(scripts) else None
            for position in plan.positions
        )
        jobs.append(
            ShortJob(
                plan.index,
                plan.units[0],
                member_scripts[0],
                plan.output_name,
                short_cache_key(plan),
                voiceover_paths=plan.units,
                script_paths=member_scripts,
            )
        )
    return jobs


def long_form_settings(settings: ExportSettings) -> ExportSettings:
    """Return the landscape settings used by the Long-Form branch.

    ``music_path`` is deliberately left untouched: it is the Long-Form (and
    basic merge) background music, and the separate Shorts track below is
    never mixed into a landscape render. The Long-Form music *volume* and the
    Long-Form *transition* are resolved here into the canonical render fields,
    so they are completely independent from the Shorts values.
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
        # Phase 27: the Long-Form Duration Before Merge stays the canonical
        # multiplier for this job; the independent Shorts value never leaks in.
        duration_before_merge=float(getattr(settings, "duration_before_merge", 0.70)),
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
        # Independent Long-Form audio and transition settings. The resolved
        # values are written into the canonical fields the renderer, the cache
        # fingerprint and the timeline mathematics read, so the Long-Form job can
        # never pick up a Shorts value (and vice versa).
        music_volume=output_music_volume(
            settings,
            getattr(settings, "long_form_music_volume", None),
            label="Long-Form Music Volume",
            default=LONG_FORM_MUSIC_VOLUME,
        ),
        transition_type=output_transition_type(
            settings,
            getattr(settings, "long_form_transition_type", ""),
            label="Long-Form Transition",
        ),
        transition_duration=output_transition_duration(
            settings,
            getattr(settings, "long_form_transition_duration", None),
            label="Long-Form Transition Duration",
            default=LONG_FORM_TRANSITION_DURATION,
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
        # Phase 30: the Long-Form image timeline profile. The canonical
        # insertion/effect values are stored at the Long-Form profile fields;
        # the Long-Form folder list becomes this job's image source pool.
        # Disabled mode or no folders keeps the historical rendering and all
        # cache identities untouched. Shorts values never leak into this job.
        timeline_image_folders=[
            str(folder)
            for folder in (getattr(settings, "long_form_image_folders", None) or [])
            if str(folder).strip()
        ],
        timeline_image_mode=normalize_insertion_mode(
            getattr(settings, "timeline_image_mode", "disabled")
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
        if job.grouped:
            # One entry per member keeps the positional/stem pairing contract of
            # the multi-voiceover pipeline: an unassigned member stays an empty
            # slot (filtered downstream) instead of shifting the next script.
            scripts = [str(path) if path is not None else "" for path in job.member_scripts]
        else:
            scripts = [str(job.script_path)] if job.script_path is not None else []
        script_path = str(job.script_path) if job.script_path is not None else ""
        if script_mode == "matched":
            # A grouped Short ships ONE transcript containing every member's text
            # in render order. Matched subtitle alignment keeps using the
            # per-member ``script_paths`` above, and create_main ignores
            # ``global_script_path`` in matched mode, so this feeds only the
            # ``.txt`` sidecar. A single Short keeps the historical empty value.
            global_script = section if (job.grouped and section) else ""
        else:
            global_script = script_path
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
    # Phase 27: the ordered SHORTS music sequence for this job. The per-Short
    # override wins and is a one-track sequence; otherwise the shared Shorts
    # sequence (or its legacy single ``short_music_path`` migration) is used.
    # The Long-Form ``music_tracks`` that flowed through ``settings`` is
    # REPLACED here — a Short never plays the Long-Form sequence.
    shorts_music_sequence = effective_short_music_tracks(settings, job.voiceover_path)
    # Phase 28: independent Shorts Duration After Merge multiplier. ``None``
    # (every pre-Phase-28 project) keeps the historical pass-through: the
    # shared ``duration_after_merge`` value and enablement flag are used
    # exactly as before. A configured Shorts value wins and enables itself,
    # while the Long-Form job never reads it. The value stays a playback-rate
    # multiplier — never a literal time — and keeps the shared 0.25–4.0 bound,
    # with the GUI exposing up to 3.5 for fast vertical pacing.
    shorts_after_merge_raw = getattr(settings, "shorts_duration_after_merge", None)
    try:
        shorts_after_merge_configured = (
            None if shorts_after_merge_raw is None else float(shorts_after_merge_raw)
        )
    except (TypeError, ValueError):
        shorts_after_merge_configured = None
    if shorts_after_merge_configured is None:
        shorts_after_merge_value = float(getattr(settings, "duration_after_merge", 1.0) or 1.0)
        shorts_after_merge_enabled = bool(getattr(settings, "duration_after_merge_enabled", False))
    else:
        shorts_after_merge_value = shorts_after_merge_configured
        shorts_after_merge_enabled = abs(shorts_after_merge_configured - 1.0) > 1e-9
    from .subtitles import clamp_font_size_percent
    # Phase 27: the independent Shorts Duration Before Merge multiplier. The
    # semantics stay exactly historical: it is a playback-rate multiplier
    # (setpts=PTS/x), never a time value. Legacy projects without the Shorts
    # field fall back to the long-form value, i.e. the one saved multiplier.
    try:
        shorts_before_merge = float(
            getattr(settings, "duration_before_merge_shorts", None)
            or getattr(settings, "duration_before_merge", 0.70)
        )
    except (TypeError, ValueError):
        shorts_before_merge = 0.70
    # Canonical visual-only sections of one Short. The configurable Short outro
    # REPLACES the historical fixed 0.7 s ending (never adds to it), and it keeps
    # that guaranteed video-only tail for legacy settings objects that do not
    # carry the new field yet. The new Short default IS that same 0.7 s, so the
    # historical mechanism is reused cleanly as the semantic outro instead of
    # existing twice: one explicit tail, one visible ending.
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
        # All members of this Short on ONE timeline. For the default one-unit
        # Short this is exactly the historical single-entry list.
        voiceover_paths=[str(unit) for unit in job.members],
        voiceover_path=str(job.voiceover_path),
        script_paths=scripts,
        script_path=script_path,
        global_script_path=global_script,
        subtitle_enabled=subtitle_enabled,
        # Strictly separate background music: a Short plays only its own
        # selected track(s), and an unselected Shorts track means no music at
        # all. The Long-Form sequence above is never mixed into a vertical
        # render. A per-Short override (keyed by this Short's first voiceover,
        # so a grouped Short owns exactly one track) replaces the shared
        # Shorts selection for this job only. Phase 27 keeps the full ordered
        # Shorts sequence on the job: ``music_path`` stays the legacy
        # first/current track for compatibility while ``music_tracks`` carries
        # the complete sequence the renderer loops as one unit. Both derive
        # from the same resolved sequence, so they can never disagree.
        music_path=str(shorts_music_sequence[0]["path"]) if shorts_music_sequence else "",
        music_tracks=list(shorts_music_sequence),
        # Phase 28: the Shorts sequence mode is strictly separate from the
        # Long-Form one; the whole-sequence loop remains the default, so an
        # untouched project renders exactly like before.
        music_sequence_mode=normalize_sequence_mode(
            getattr(settings, "short_music_sequence_mode", None)
        ),
        # Phase 28: independent Shorts Duration After Merge. A configured
        # Shorts multiplier (up to 3.5 for very fast vertical pacing) wins and
        # enables itself; ``None`` keeps the exact historical behavior — the
        # shared value and enablement flag flow through unchanged.
        duration_after_merge=shorts_after_merge_value,
        duration_after_merge_enabled=shorts_after_merge_enabled,
        # Independent Shorts audio and transition settings, resolved into the
        # canonical fields of THIS job only: a Short never inherits the
        # Long-Form music volume or transition, and the Long-Form job never
        # inherits these values. Like the Long-Form, the Shorts track starts at
        # 0.000 s and plays through the visual outro to the final frame.
        music_volume=output_music_volume(
            settings,
            short_music_volume_for(settings, job.voiceover_path),
            label="Shorts Music Volume",
            default=SHORTS_MUSIC_VOLUME,
        ),
        transition_type=output_transition_type(
            settings,
            getattr(settings, "shorts_transition_type", ""),
            label="Shorts Transition",
        ),
        transition_duration=output_transition_duration(
            settings,
            getattr(settings, "shorts_transition_duration", None),
            label="Shorts Transition Duration",
            default=SHORTS_TRANSITION_DURATION,
        ),
        # A single-unit Short is one acoustic unit: inter-unit silence belongs
        # only to the combined Long-Form timeline, never to an individual Short.
        # A grouped Short IS a combined voiceover timeline (Script A, pause,
        # Script B) on ONE Short, so it keeps the project's configured pause.
        voiceover_pause=short_voiceover_pause(settings, job.grouped),
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
        # Phase 27: independent Shorts font size and Duration Before Merge.
        # Both are mapped onto the job's canonical generic fields, so the
        # complete Stage-1 pipeline keeps ONE subtitle profile and ONE merge
        # multiplier per job; Long-Form values never change because of this.
        subtitle_font_size=clamp_font_size_percent(
            getattr(settings, "short_subtitle_font_size", 100)
        ),
        duration_before_merge=shorts_before_merge,
        # Phase 29: the Shorts Typewriter Hook Intro is strictly separate from
        # the Long-Form one. Its values are resolved onto the canonical intro
        # fields of THIS job only, exactly like the other short_* settings;
        # the Long-Form job keeps reading its own unprefixed values and the
        # two profiles can never leak into each other.
        typewriter_intro_enabled=bool(getattr(settings, "short_typewriter_intro_enabled", False)),
        typewriter_hook_text=str(getattr(settings, "short_typewriter_hook_text", "") or ""),
        typewriter_speed=str(getattr(settings, "short_typewriter_speed", "auto")),
        typewriter_sound_frequency=str(getattr(settings, "short_typewriter_sound_frequency", "every_character")),
        typewriter_sound_preset=str(getattr(settings, "short_typewriter_sound_preset", "typewriter_1")),
        typewriter_sound_volume=int(getattr(settings, "short_typewriter_sound_volume", 30)),
        typewriter_cursor_enabled=bool(getattr(settings, "short_typewriter_cursor_enabled", True)),
        typewriter_position=str(getattr(settings, "short_typewriter_position", "Center")),
        typewriter_h_align=str(getattr(settings, "short_typewriter_h_align", "Center")),
        typewriter_font=str(getattr(settings, "short_typewriter_font", "modern_sans_bold")),
        typewriter_font_size=int(getattr(settings, "short_typewriter_font_size", 100)),
        typewriter_bold=bool(getattr(settings, "short_typewriter_bold", True)),
        typewriter_color=str(getattr(settings, "short_typewriter_color", "#FFFFFF")),
        typewriter_outline_enabled=bool(getattr(settings, "short_typewriter_outline_enabled", True)),
        typewriter_shadow_enabled=bool(getattr(settings, "short_typewriter_shadow_enabled", False)),
        typewriter_box_enabled=bool(getattr(settings, "short_typewriter_box_enabled", False)),
        typewriter_box_opacity=int(getattr(settings, "short_typewriter_box_opacity", 55)),
        typewriter_box_padding=int(getattr(settings, "short_typewriter_box_padding", 40)),
        typewriter_background_image_enabled=bool(getattr(settings, "short_typewriter_background_image_enabled", False)),
        typewriter_background_image_path=str(getattr(settings, "short_typewriter_background_image_path", "") or ""),
        typewriter_background_darken=int(getattr(settings, "short_typewriter_background_darken", 0)),
        typewriter_background_blur=bool(getattr(settings, "short_typewriter_background_blur", False)),
        typewriter_background_zoom=bool(getattr(settings, "short_typewriter_background_zoom", False)),
        typewriter_hold_seconds=float(getattr(settings, "short_typewriter_hold_seconds", 0.5)),
        typewriter_transition=str(getattr(settings, "short_typewriter_transition", "project")),
        typewriter_music_mode=str(getattr(settings, "short_typewriter_music_mode", "start_with_video") or "start_with_video"),
        # Phase 30: the Shorts image timeline profile is strictly separate
        # from the Long-Form one. Its own folder list and its own insertion
        # rules are resolved onto the canonical per-job fields of THIS Short;
        # the Long-Form job keeps reading its unprefixed values and the two
        # profiles can never leak into each other. Disabled mode or no
        # folders keeps the historical rendering byte-identical.
        timeline_image_folders=[
            str(folder)
            for folder in (getattr(settings, "shorts_image_folders", None) or [])
            if str(folder).strip()
        ],
        timeline_image_mode=normalize_insertion_mode(
            getattr(settings, "shorts_image_mode", "disabled")
        ),
        timeline_image_every_n=int(getattr(settings, "shorts_image_every_n", 4) or 4),
        timeline_image_share_percent=int(getattr(settings, "shorts_image_share_percent", 20) or 20),
        timeline_image_min_video_gap=int(getattr(settings, "shorts_image_min_video_gap", 2) or 2),
        timeline_image_duration_mode=normalize_duration_mode(
            getattr(settings, "shorts_image_duration_mode", "fixed")
        ),
        timeline_image_duration=clamp_image_timeline_duration(
            getattr(settings, "shorts_image_duration", 2.5)
        ),
        timeline_image_duration_min=clamp_image_timeline_duration(
            getattr(settings, "shorts_image_duration_min", 2.0)
        ),
        timeline_image_duration_max=clamp_image_timeline_duration(
            getattr(settings, "shorts_image_duration_max", 4.0)
        ),
        timeline_image_motion=normalize_motion(getattr(settings, "shorts_image_motion", "zoom_in")),
        timeline_image_effect=normalize_tv_effect(getattr(settings, "shorts_image_effect", "off")),
        timeline_image_effect_intensity=clamp_intensity(
            getattr(settings, "shorts_image_effect_intensity", 20)
        ),
        timeline_image_flicker_speed=normalize_flicker_speed(
            getattr(settings, "shorts_image_flicker_speed", "normal")
        ),
        global_tv_effect=normalize_tv_effect(getattr(settings, "shorts_global_tv_effect", "off")),
        global_tv_effect_intensity=clamp_intensity(
            getattr(settings, "shorts_global_tv_effect_intensity", 20)
        ),
        global_tv_flicker_speed=normalize_flicker_speed(
            getattr(settings, "shorts_global_tv_flicker_speed", "normal")
        ),
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
