from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence


_SUBTITLE_DEFAULT = object()

#: Explicit visual-only timeline sections. Every voiceover-driven Main Video is
#: built as ``[visual intro][voiceover + normal video][visual outro]``. Both
#: sections show moving material from the normal video timeline (never black or
#: unintentionally frozen frames), carry no voiceover audio and show no
#: subtitle: the spoken audio stays the timing authority, so captions run from
#: the voiceover start to the spoken end only.
#:
#: Long-Form and Shorts have independent defaults; the values below are the
#: user-facing settings. :mod:`youtube_outputs` copies the collection-appropriate
#: pair into the canonical render-time fields (``visual_intro_seconds`` and
#: ``final_pause``) for every Long-Form/Short job, so the timeline mathematics,
#: the subtitle offset and the cache fingerprint each read exactly one value.
#:
#: Both visual sections are ``[visual + music]``: configured background music
#: already plays from video time 0.000 s and keeps playing through the outro
#: until the final video endpoint, while voiceover and subtitles are confined to
#: the spoken part in between.
LONG_FORM_INTRO_SECONDS = 1.5
LONG_FORM_OUTRO_SECONDS = 1.5
SHORT_INTRO_SECONDS = 0.7
SHORT_OUTRO_SECONDS = 0.7
#: Upper bound used by the GUI spin boxes. The model itself accepts any finite
#: value >= 0; 0.0 disables a section completely.
MAX_VISUAL_SECTION_SECONDS = 60.0

#: Output-specific audio and transition defaults. Long-Form and Shorts keep
#: fully independent music volume and transition settings: changing one output
#: never changes the other. The values below are what a new project, a new GUI
#: session, the CLI defaults and a project file without these keys receive.
DEFAULT_TRANSITION_TYPE = "cross_dissolve"
LONG_FORM_TRANSITION_DURATION = 2.0
SHORTS_TRANSITION_DURATION = 2.0
#: Historic shared transition duration. It remains the canonical field default
#: for the basic/Main-Video merge path and doubles as the "was never set"
#: marker when resolving an output-specific duration (see
#: :func:`app.video_merger.youtube_outputs.output_transition_duration`).
TRANSITION_DURATION_LEGACY_DEFAULT = 1.0
#: Background music gain in percent. 44 % is approximately +6 dB over the
#: former 22 % linear gain and stays below the 100 % voiceover gain.
MUSIC_VOLUME_PERCENT = 44
LONG_FORM_MUSIC_VOLUME = MUSIC_VOLUME_PERCENT
SHORTS_MUSIC_VOLUME = MUSIC_VOLUME_PERCENT
#: Upper bound of the music gain accepted by the render graph
#: (``command_builder._percent_gain`` clamps to 1.5 == 150 %).
MAX_MUSIC_VOLUME_PERCENT = 150


@dataclass(slots=True)
class AudioInfo:
    present: bool = False
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""


@dataclass(slots=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    effective_width: int
    effective_height: int
    fps: float
    fps_fraction: str
    video_codec: str
    pixel_format: str
    sar: str
    dar: str
    rotation: int = 0
    audio: AudioInfo = field(default_factory=AudioInfo)
    is_hdr: bool = False
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    warnings: list[str] = field(default_factory=list)
    # Set when a voiceover-driven timeline trims or extends this clip. The
    # visible ``duration`` is the timeline duration; source_duration remains
    # the actual decodable source length for trim/hold/loop decisions.
    source_duration: float = 0.0
    # Canonical source identity used by folder-aware selection. It is the
    # resolved directory containing this clip, not merely the display name.
    # Empty values from older metadata caches migrate to ``path.parent``.
    source_folder: str = ""
    # 1.3.0: per-occurrence playback rate (global Main Video speed and/or the
    # Smart Last-Clip Stretch). 1.0 = untouched source timing; < 1.0 slows the
    # clip down (stretch), > 1.0 speeds it up. The render graph applies this
    # via setpts on the video chain and atempo on the clip's own audio.
    playback_rate: float = 1.0
    # Add Image is a real, silent Stage-2 image input with its own flag and
    # settings payload, so image composition changes are always explicit.
    is_image_insertion: bool = False
    image_fit_mode: str = "fit"  # fit | fill | crop
    image_zoom: int = 100
    image_filter: str = "natural"
    # Transition family for the boundary adjacent to this Add Image item.
    image_transition_type: str = ""

    @property
    def display_name(self) -> str:
        return self.path.name

    @property
    def aspect_ratio(self) -> float:
        return self.effective_width / self.effective_height if self.effective_height else 0.0


@dataclass(slots=True)
class ExportSettings:
    aspect: str = "16:9"
    resolution: str = "Auto"
    fit_mode: str = "contain_blur"
    # Canonical transition of the render that is currently being planned. The
    # basic/Main-Video merge path keeps these shared values, while every YouTube
    # job receives its own output-specific pair below (``youtube_outputs`` copies
    # the resolved value into these canonical fields before the render, the cache
    # fingerprint and the timeline mathematics read them).
    transition_type: str = DEFAULT_TRANSITION_TYPE
    transition_ease: str = "ease_in_out"
    transition_duration: float = TRANSITION_DURATION_LEGACY_DEFAULT
    # Output-specific transition settings (Long-Form and Shorts are fully
    # independent). An empty string / ``None`` means "not configured": the
    # shared value of an existing project or API caller is used as the migration
    # fallback, and a project without any of them receives the new defaults
    # Cross Dissolve / 2.0 s for both outputs. Combined mode and One-Click use
    # the Long-Form pair for the Long-Form job and the Shorts pair for every
    # Short, so changing one output never changes the other.
    long_form_transition_type: str = ""
    long_form_transition_duration: float | None = None
    shorts_transition_type: str = ""
    shorts_transition_duration: float | None = None
    background_blur: int = 30
    background_darkness: int = 10
    background_zoom: int = 100
    normalize_audio: bool = True
    fps_choice: str = "Auto"
    encoding: str = "Auto"
    crf: int = 18
    preset: str = "slow"
    output_name: str = ""
    allow_hdr_unsafe: bool = False

    # 1.2.3 output/quality presets. ``maximum`` / ``youtube_landscape`` are the
    # real application defaults and must map to actual encoder and project
    # configuration, never to a cosmetic label. ``custom`` preserves the
    # explicit low-level CRF/preset/resolution fields.
    quality_preset: str = "maximum"  # maximum | high | balanced | fast | custom
    output_preset: str = "youtube_landscape"  # youtube_landscape | youtube_vertical | custom

    # 1.2 project roles and two-stage workflow. Paths are strings so the
    # existing JSON SettingsStore can persist/reopen a project without custom
    # serializers; empty strings mean that an optional role is unassigned.
    workflow_stage: str = "basic"  # basic | main | outro
    voiceover_path: str = ""
    script_path: str = ""
    # ``music_path`` is the Long-Form/basic background music. YouTube Shorts
    # use their own track: the two selections are strictly separate, so the
    # Long-Form music never plays in a Short and an empty Shorts track means
    # that Short simply has no background music. Volume, preset, ducking,
    # looping and trimming stay shared behavior for whichever track is active.
    music_path: str = ""
    short_music_path: str = ""
    main_video_path: str = ""
    outro_path: str = ""
    intro_path: str = ""

    # 1.2.3/Phase 4 multiple voiceover/script units. ``voiceover_paths`` is
    # always the ordered authoritative audio list; ``script_paths`` is one
    # global entry in single mode and one basename-matched entry per unit in
    # matched mode. ``voiceover_path``/``script_path`` keep the legacy
    # single-file mapping for compatibility. ``script_mode`` selects the
    # canonical workflow: "single" = one global script drives the complete
    # concatenated timeline, "matched" = every voiceover needs its own script.
    voiceover_paths: list[str] = field(default_factory=list)
    script_paths: list[str] = field(default_factory=list)
    script_mode: str = "single"  # single = global | matched = individual
    # Phase 4: the global script is stored once, independently of the ordered
    # voiceover list. ``script_paths[0]`` remains the migration fallback for
    # older projects. The pause is inserted between units, never after the
    # final unit; ``final_pause`` below remains the visual outro after it.
    global_script_path: str = ""
    voiceover_pause: float = 0.7
    voiceover_order_mode: str = "natural"  # natural | mtime_oldest | mtime_newest | manual

    original_audio_mode: str = "original"  # mute | low | original (1.2.4: Original is the default)
    intro_audio_mode: str = "original"  # mute | low | original
    outro_audio_mode: str = "original"
    voiceover_volume: int = 100
    # Canonical music gain of the render that is currently being planned
    # (44 % ≈ +6 dB over the former 22 % linear gain; it stays below the 100 %
    # voiceover gain while the limiter and ducking remain the final safety net
    # in the mixed graph). The basic/Main-Video merge path keeps this shared
    # value; every YouTube job receives its own output-specific volume below.
    music_volume: int = MUSIC_VOLUME_PERCENT
    # Output-specific background music volume in percent, fully independent for
    # Long-Form and Shorts (44 % each by default). ``None`` means "not
    # configured": the shared ``music_volume`` of an existing project or API
    # caller is used as the migration fallback, so an old project never loses
    # its saved loudness. The volume applies through the complete video —
    # visual intro, spoken part and visual outro — because the music itself
    # plays from 0.000 s to the final video endpoint. Voiceover volume stays
    # independent, and a Short still plays only its own Shorts track.
    long_form_music_volume: int | None = None
    shorts_music_volume: int | None = None
    music_preset: str = "balanced"
    ducking_enabled: bool = True
    ducking_attack_ms: int = 25
    ducking_release_ms: int = 450
    # Main Video end padding == the Long-Form visual outro. One single tail
    # field on purpose: the legacy "Main Video End Padding" control and the new
    # explicit outro setting are the same timeline section, so they can never
    # stack into a duplicated visible ending. ``youtube_outputs`` writes the
    # collection-appropriate value here (Long-Form outro for landscape jobs,
    # Short outro for vertical jobs).
    final_pause: float = 1.0
    # User-facing visual-only section settings (see the module constants above).
    long_form_intro_seconds: float = LONG_FORM_INTRO_SECONDS
    long_form_outro_seconds: float = LONG_FORM_OUTRO_SECONDS
    short_intro_seconds: float = SHORT_INTRO_SECONDS
    short_outro_seconds: float = SHORT_OUTRO_SECONDS
    # Canonical render-time intro. Filled by ``long_form_settings()`` /
    # ``short_settings()`` for every YouTube job (and by the GUI for direct
    # renders); a raw ``ExportSettings()`` built by a legacy API caller keeps
    # the neutral 0.0 == "no visual-only intro", which preserves the historical
    # timeline of direct ``create_main`` callers.
    visual_intro_seconds: float = 0.0
    # Optional subtle Main Video opening effect: none | zoom_in | zoom_out.
    # It touches the opening visual portion only, always returns to a neutral
    # 1.0x frame, and never changes timeline, audio or subtitle timing.
    opening_effect: str = "none"
    short_video_mode: str = "hold"  # hold | loop
    # 1.3.0 smart duration fit: how the last selected clip reaches an exact
    # voiceover-derived target. ``cut`` keeps the proven 1.2.4 trimming
    # behavior (default); ``stretch`` slows only the final selected clip as
    # much as necessary, bounded by ``max_stretch_percent``. A required
    # stretch beyond the limit falls back to the normal cut/trim behavior —
    # never to Hold Last Frame.
    duration_fit_mode: str = "cut"  # cut | stretch
    max_stretch_percent: float = 10.0  # 5 | 10 | 15 | 20 | custom (1–50)
    # Canonical independent merge-duration controls. The values are playback
    # multipliers, so ``source_duration / duration_before_merge`` is the
    # timeline duration. Before Merge is intentionally active by default;
    # After Merge is a separate, disabled post-merge operation.
    duration_before_merge: float = 0.70
    duration_after_merge: float = 1.00
    duration_after_merge_enabled: bool = False
    # Deprecated compatibility input for projects/CLI callers from 1.3.0.
    # It is migrated to duration_before_merge by SettingsStore and is never
    # used as a second GUI setting. Keeping the field avoids breaking old JSON
    # and direct API callers while the canonical setting remains singular.
    video_speed: float = 1.0

    subtitle_enabled: bool = False
    subtitle_language: str = "German"  # German | English | Auto
    subtitle_style: str = "long_1"
    subtitle_animation: str = "static_phrase"  # Long-Form default: Static White Reveal
    subtitle_font: str = "modern_sans_bold"
    subtitle_position: str = "Center"
    subtitle_debug_overlay: bool = False
    subtitle_model: str = "small"
    allow_alignment_warnings: bool = False
    subtitle_ass_path: str = ""  # generated render-time artifact
    subtitle_fonts_dir: str = ""  # process-local legal bundled font directory

    watermark_enabled: bool = False
    watermark_path: str = ""
    watermark_position: str = "top_right"
    watermark_opacity: int = 80
    watermark_size: int = 12
    watermark_margin: int = 3
    watermark_scope: str = "both"  # main | outro | both
    outro_transition_enabled: bool = True

    # Optional Stage-2 Add Image section (legacy API name: Image Insertion).
    # It is always silent. The position aliases keep existing saved projects
    # usable. The former Quote/Flyer PDF artwork section was removed; its keys
    # are ignored when an older project JSON file is loaded.
    image_enabled: bool = False
    image_path: str = ""
    image_position: str = "after_intro"  # before_main/after_main; legacy aliases accepted
    image_duration: float = 4.0
    image_transition_type: str = "cross_dissolve"
    image_transition_duration: float = 1.0
    image_fit_mode: str = "fit"  # fit | fill | crop
    image_zoom: int = 100
    image_filter: str = "natural"

    # YouTube delivery selection. Long Form is the backwards-compatible
    # default; Shorts and the combined mode are orchestrated as independent
    # output jobs rather than as one stretched landscape timeline.
    export_mode: str = "long_form"  # long_form | shorts | long_form_and_shorts

    # Subtitle output is explicit. New projects default to With Subtitles:
    # burned-in subtitles (and the existing SRT/VTT sidecars) without an
    # additional clean video. With and Without is the opt-in dual variant.
    subtitle_output_mode: str = field(default=_SUBTITLE_DEFAULT)  # type: ignore[assignment]
    # A direct API caller that omits this field belongs to the legacy single
    # video contract; GUI/CLI selections pass an explicit new mode. This tiny
    # migration marker lets old API workflows keep their dual output while the
    # new YouTube selector defaults to With Subtitles.
    subtitle_output_mode_was_defaulted: bool = field(init=False, repr=False, compare=False, default=False)

    # Shorts have their own safe mobile subtitle profile. The long-form
    # controls above remain untouched when a project also creates Shorts.
    # ``short_subtitle_animation`` defaults to the clean phrase-level Short
    # animation (``subtitles.DEFAULT_SHORT_ANIMATION``); the former Word
    # Highlight default is no longer selectable for Shorts and saved values are
    # migrated (see ``subtitles.normalize_subtitle_animation``).
    short_subtitle_style: str = "short_1"
    short_subtitle_animation: str = "phrase_focus"
    short_subtitle_font: str = "inter"
    short_subtitle_position: str = "Bottom Center"

    # Process/render identity, intentionally not a user-facing control. The
    # Shorts orchestrator sets a unique value for every voiceover so even
    # duplicate audio paths cannot reuse another Short's Stage-1 cache result.
    render_variant_key: str = ""

    # Input-library configuration and explicit-order semantics. Empty
    # ``source_folders`` preserves the legacy single input folder field; a
    # populated list is the complete configured source set.
    source_folders: list[str] = field(default_factory=list)
    video_order_mode: str = "natural"  # natural | alphabetical | random | manual; legacy folder_alternating accepted
    # Legacy Input Root ("1 · Ordner" → Legacy Input Root). When Random order is
    # active, the first three selected clips are reserved from this folder
    # (themselves randomized), and clip 4+ returns to the normal full random
    # pool. An empty value keeps the historical unbiased shuffle untouched.
    legacy_input_root: str = ""


    # Render-time values filled by MainProjectEngine; they are harmless if
    # persisted and are recalculated before every Stage-1 render.
    program_duration: float = 0.0
    timeline_target_duration: float = 0.0
    # Stage-2 only: per-clip original-audio gains in composition order
    # (intro/image/main/outro). Filled by MainProjectEngine.add_outro().
    stage2_audio_modes: list[str] = field(default_factory=list)
    # Stage-2 only: per-section role names in composition order
    # ("intro"/"image"/"main"/"outro"). Filled by MainProjectEngine.add_outro().
    stage2_roles: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.subtitle_output_mode is _SUBTITLE_DEFAULT:
            self.subtitle_output_mode = "with_subtitles"
            self.subtitle_output_mode_was_defaulted = True


@dataclass(slots=True)
class ResolvedExport:
    width: int
    height: int
    fps: float
    fps_expr: str
    effective_durations: list[float]
    transitions: list[float]
    expected_duration: float
    encoder: str = "libx264"
    encoder_label: str = "CPU (libx264)"
    crf: int = 18
    preset: str = "slow"
    quality_label: str = "High Quality"
    warnings: list[str] = field(default_factory=list)

    @property
    def resolution_text(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(slots=True)
class ProgressEvent:
    percent: float
    out_time: float
    total_time: float
    elapsed: float
    remaining: float | None
    stage: str
    current_file: str = ""


@dataclass(slots=True)
class ValidationReport:
    ok: bool
    details: list[str]
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False


@dataclass(slots=True)
class AudioAssetInfo:
    path: Path
    duration: float
    sample_rate: int
    channels: int
    codec: str = ""


@dataclass(slots=True)
class WordTiming:
    text: str
    start: float
    end: float
    confidence: float = 1.0
    script_start: int = 0
    script_end: int = 0


@dataclass(slots=True)
class AlignmentResult:
    words: list[WordTiming]
    language: str
    method: str
    compatibility: float
    average_confidence: float
    warnings: list[str] = field(default_factory=list)
    # Hard acoustic section boundaries (the starts of later voiceover units).
    # Subtitle grouping may not merge across these, even when the wording and
    # measured font geometry would otherwise fit in one cue.
    hard_breaks: list[float] = field(default_factory=list)


@dataclass(slots=True)
class MainVideoResult:
    video: Path
    srt: Path | None
    vtt: Path | None
    report: ValidationReport
    alignment: AlignmentResult | None = None
    warnings: list[str] = field(default_factory=list)
    canonical_timeline: Path | None = None
    verification_frames: list[Path] = field(default_factory=list)
    # Optional internal quality evidence, classified separately from the render
    # result: PASS / DEGRADED / FAIL / SKIPPED. A FAIL never invalidates the
    # successfully rendered, probed and validated output video.
    verification_status: str = "SKIPPED"
    timings: dict[str, float | str | bool] = field(default_factory=dict)
    # 1.3.0: additional user-facing output WITHOUT burned-in subtitles. None
    # when no subtitles were generated; otherwise this file always exists.
    video_no_subtitles: Path | None = None


@dataclass(slots=True)
class CompleteWorkflowResult:
    main: MainVideoResult
    final_video: Path
    final_report: ValidationReport
    # 1.3.0: final composition rendered from the subtitle-free Main Video.
    final_video_no_subtitles: Path | None = None
    youtube_metadata: Path | None = None


@dataclass(slots=True)
class YoutubeExportResult:
    """Bundle returned by a Long-Form, Shorts, or combined export."""

    mode: str
    long_form: MainVideoResult | CompleteWorkflowResult | None = None
    shorts: list[MainVideoResult | CompleteWorkflowResult] = field(default_factory=list)

    @property
    def primary_output(self) -> Path:
        if self.long_form is not None:
            return self.long_form.video if isinstance(self.long_form, MainVideoResult) else self.long_form.final_video
        if self.shorts:
            item = self.shorts[0]
            return item.video if isinstance(item, MainVideoResult) else item.final_video
        raise ValueError("YouTube export produced no output.")

    @property
    def outputs(self) -> list[Path]:
        values: list[Path] = []
        items = ([self.long_form] if self.long_form is not None else []) + list(self.shorts)
        for item in items:
            if isinstance(item, MainVideoResult):
                values.append(item.video)
                if item.video_no_subtitles:
                    values.append(item.video_no_subtitles)
            else:
                values.append(item.final_video)
                if item.final_video_no_subtitles:
                    values.append(item.final_video_no_subtitles)
            main = item if isinstance(item, MainVideoResult) else item.main
            if main.srt:
                values.append(main.srt)
            if main.vtt:
                values.append(main.vtt)
        return values


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[ProgressEvent], None]
MediaSequence = Sequence[MediaInfo]
