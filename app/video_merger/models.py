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
    # 1.2.4: synthetic section rendered from filters only (the generated
    # Quote Card). Such items never receive an ``-i`` input and are rendered
    # silently; they can never enter the subtitle/voiceover/music timeline.
    is_generated_quote: bool = False

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
    transition_type: str = "smooth_blur"
    transition_ease: str = "ease_in_out"
    transition_duration: float = 0.5
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

    # 1.2.3 multiple voiceover/script units. The ordered lists are the
    # authoritative project state; ``voiceover_path``/``script_path`` keep the
    # legacy single-file mapping for compatibility and are derived from the
    # lists by the GUI. ``script_mode`` selects the canonical workflow:
    # "single" = one global script drives the whole concatenated timeline,
    # "matched" = every voiceover needs its own matching script file.
    voiceover_paths: list[str] = field(default_factory=list)
    script_paths: list[str] = field(default_factory=list)
    script_mode: str = "single"  # single | matched

    original_audio_mode: str = "original"  # mute | low | original (1.2.4: Original is the default)
    intro_audio_mode: str = "original"  # mute | low | original
    outro_audio_mode: str = "original"
    voiceover_volume: int = 100
    music_volume: int = 22
    music_preset: str = "quiet"
    ducking_enabled: bool = True
    ducking_attack_ms: int = 25
    ducking_release_ms: int = 450
    final_pause: float = 1.0
    short_video_mode: str = "hold"  # hold | loop

    subtitle_enabled: bool = False
    subtitle_language: str = "German"  # German | English | Auto
    subtitle_style: str = "long_1"
    subtitle_animation: str = "static_phrase"  # 1.2.4: Static Phrase is the default
    subtitle_font: str = "modern_sans_bold"
    subtitle_position: str = "Bottom"
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

    # 1.2.4 optional Quote Card between Intro and Main (Stage 2 only). The
    # section is generated in FFmpeg (dark card + drawtext), is always
    # silent and never receives voiceover, music or subtitles.
    quote_enabled: bool = False
    quote_text: str = ""
    quote_attribution: str = ""
    quote_duration: float = 2.0  # seconds (1.0–3.0 in the GUI)
    quote_font: str = "inter"

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


@dataclass(slots=True)
class CompleteWorkflowResult:
    main: MainVideoResult
    final_video: Path
    final_report: ValidationReport


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[ProgressEvent], None]
MediaSequence = Sequence[MediaInfo]
