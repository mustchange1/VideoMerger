from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence


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
    # Quote/Flyer artwork is a real, silent Stage-2 image input.
    is_quote_artwork: bool = False
    quote_fit_mode: str = "fit"  # fit | fill | crop
    # Independent Image Insertion is a different Stage-2 input.  Keeping a
    # distinct flag/settings payload prevents Quote/Flyer changes from being
    # silently applied to the user image feature.
    is_image_insertion: bool = False
    image_fit_mode: str = "fit"  # fit | fill | crop
    image_zoom: int = 100
    image_filter: str = "natural"

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
    transition_type: str = "cross_dissolve"
    transition_ease: str = "ease_in_out"
    transition_duration: float = 1.0
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
    music_path: str = ""
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
    # final unit; ``final_pause`` below remains Main Video end padding.
    global_script_path: str = ""
    voiceover_pause: float = 0.7
    voiceover_order_mode: str = "natural"  # natural | mtime_oldest | mtime_newest | manual

    original_audio_mode: str = "original"  # mute | low | original (1.2.4: Original is the default)
    intro_audio_mode: str = "original"  # mute | low | original
    outro_audio_mode: str = "original"
    voiceover_volume: int = 100
    # 44 % is approximately +6 dB over the former 22 % linear gain. It stays
    # below the 100 % voiceover gain while the limiter and ducking remain the
    # final safety net in the mixed graph.
    music_volume: int = 44
    music_preset: str = "balanced"
    ducking_enabled: bool = True
    ducking_attack_ms: int = 25
    ducking_release_ms: int = 450
    final_pause: float = 1.0
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
    subtitle_animation: str = "static_phrase"  # 1.2.4: Static Phrase is the default
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

    # Optional Quote/Flyer artwork between Intro and Main (Stage 2 only).
    # The legacy text fields were intentionally removed from the active model;
    # SettingsStore ignores them when loading older project JSON files.
    quote_enabled: bool = False
    quote_input_mode: str = "artwork"  # retained as a migration marker
    quote_artwork_path: str = ""
    quote_pdf_page: int = 1  # one-based page number for a multi-page PDF
    quote_artwork_fit_mode: str = "fit"  # fit | fill | crop
    quote_duration: float = 4.0  # seconds; new Flyer default

    # Independent optional Stage-2 Image Insertion. It is always silent and
    # never participates in Stage-1 selection, alignment, or cache keys.
    image_enabled: bool = False
    image_path: str = ""
    image_position: str = "after_intro"  # after_intro | before_outro
    image_duration: float = 4.0
    image_transition_duration: float = 1.0
    image_fit_mode: str = "fit"  # fit | fill | crop
    image_zoom: int = 100
    image_filter: str = "natural"

    # Subtitle output is explicit: the default emits all three user-facing
    # artifacts, Burned Only emits only the burned video, and Without emits a
    # clean video without creating an alignment/sidecar output bundle.
    subtitle_output_mode: str = "burned_and_sidecars"

    # Input-library configuration and explicit-order semantics. Empty
    # ``source_folders`` preserves the legacy single input folder field; a
    # populated list is the complete configured source set.
    source_folders: list[str] = field(default_factory=list)
    video_order_mode: str = "folder_alternating"  # folder_alternating | manual


    # Render-time values filled by MainProjectEngine; they are harmless if
    # persisted and are recalculated before every Stage-1 render.
    program_duration: float = 0.0
    timeline_target_duration: float = 0.0
    # Stage-2 only: per-clip original-audio gains in composition order
    # (intro/quote/main/outro). Filled by MainProjectEngine.add_outro().
    stage2_audio_modes: list[str] = field(default_factory=list)
    # Stage-2 only: per-section role names in composition order
    # ("intro"/"quote"/"main"/"outro"). Filled by MainProjectEngine.add_outro().
    stage2_roles: list[str] = field(default_factory=list)


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


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[ProgressEvent], None]
MediaSequence = Sequence[MediaInfo]
