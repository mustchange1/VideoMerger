from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path

from .alignment import LocalWordAligner, script_word_spans
from .engine import VideoMergerEngine
from .errors import VideoMergerError
from .font_manager import bundled_fonts_dir
from .models import (
    AlignmentResult,
    CompleteWorkflowResult,
    ExportSettings,
    LogCallback,
    MainVideoResult,
    MediaInfo,
    YoutubeExportResult,
    ProgressCallback,
    ProgressEvent,
    ValidationReport,
    WordTiming,
)
from .paths import project_root
from .project_assets import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    optional_path,
    probe_audio,
    read_script,
    require_asset,
)
from .image_insertion import (
    clamp_image_duration,
    clamp_image_zoom,
    image_insertion_path,
    normalize_image_filter,
    normalize_image_fit_mode,
    normalize_image_position,
    normalize_image_transition,
)
from .render_cache import (
    Stage1RenderCache,
    load_cached_alignment,
    stage1_fingerprint,
    stage2_fingerprint,
)
from .script_sections import script_section_path, split_global_script
from .subtitle_modes import (
    SUBTITLE_OUTPUT_COMBINED,
    SUBTITLE_OUTPUT_WITH,
    SUBTITLE_OUTPUT_WITHOUT,
    normalize_subtitle_output_mode,
    subtitle_clean_variant_requested,
    subtitle_render_requested,
    subtitle_sidecars_requested,
)
from .subtitle_verification import create_visual_verification_frames
from .subtitles import (
    build_cues,
    validate_subtitle_file,
    write_ass,
    write_canonical_timeline,
    write_srt,
    write_vtt,
)
from .target import choose_fps, parse_resolution, resolve_export
from .timeline import (
    after_merge_enabled,
    duration_after_merge_value,
    duration_before_merge_value,
    fit_media_to_duration,
)
from .validation import validate_output
from .video_pool import (
    VIDEO_ORDER_RANDOM,
    ShortsVideoPool,
    legacy_priority_prefix,
    media_source_folder,
    normalize_video_order_mode,
    order_media_for_video_order,
)
from .voiceover_order import (
    normalize_voiceover_order_mode,
    order_voiceover_paths,
    voiceover_order_indices,
)
from .youtube_metadata import generate_youtube_metadata_file
from .youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    NO_SCRIPT_SECTION,
    MainTimeline,
    ShortJob,
    build_short_jobs,
    long_form_settings,
    main_timeline,
    normalize_export_mode,
    short_settings,
    write_short_script_text,
)


def _raw_voiceover_paths(settings: ExportSettings) -> list[Path]:
    raw = list(settings.voiceover_paths)
    if not raw and settings.voiceover_path.strip():
        raw = [settings.voiceover_path]
    return [Path(value).expanduser().resolve() for value in raw if value.strip()]


def voiceover_paths(settings: ExportSettings) -> list[Path]:
    """Return the effective, deterministic Stage-1 voiceover order.

    ``manual`` preserves the persisted list exactly. All automatic modes are
    also applied headlessly so CLI and GUI exports cannot disagree about the
    acoustic timeline.
    """
    raw = _raw_voiceover_paths(settings)
    return order_voiceover_paths(raw, getattr(settings, "voiceover_order_mode", "natural"))


def ordered_voiceover_units(settings: ExportSettings) -> tuple[list[Path], list[Path | None]]:
    """Return ordered audio and (when matched) scripts as one paired sequence.

    Exact basename matches win, which preserves the existing GUI behavior even
    when a CLI/persisted project supplied the script list in a different order.
    Missing scripts stay as ``None`` so validation reports the affected audio
    unit instead of silently shifting the following scripts.
    """
    raw_units = _raw_voiceover_paths(settings)
    mode = normalize_voiceover_order_mode(getattr(settings, "voiceover_order_mode", "natural"))
    script_mode = "matched" if str(settings.script_mode).casefold() in {"matched", "individual"} else "single"
    indices = voiceover_order_indices(raw_units, mode)
    units = [raw_units[index] for index in indices]
    raw_scripts = script_paths(settings)
    if script_mode != "matched":
        return units, raw_scripts
    by_stem = {path.stem.casefold(): path for path in raw_scripts}
    ordered_scripts: list[Path | None] = []
    # A complete positional list remains compatible with the GUI's explicit
    # row assignments (the same permutation is applied to audio and scripts).
    # Partial lists use basename matching only: falling back by index would
    # silently assign the following script to a missing middle voiceover.
    positional = len(raw_scripts) == len(raw_units)
    for original_index, unit in zip(indices, units):
        matched = by_stem.get(unit.stem.casefold())
        if matched is not None:
            ordered_scripts.append(matched)
        elif positional and original_index < len(raw_scripts):
            ordered_scripts.append(raw_scripts[original_index])
        else:
            ordered_scripts.append(None)
    return units, ordered_scripts


def script_paths(settings: ExportSettings) -> list[Path]:
    raw = list(settings.script_paths)
    if not raw and settings.script_path.strip():
        raw = [settings.script_path]
    return [Path(value).expanduser().resolve() for value in raw if value.strip()]


def global_script_path(settings: ExportSettings) -> Path | None:
    """Resolve one authoritative global script without duplicating it per unit."""
    value = str(getattr(settings, "global_script_path", "") or "").strip()
    if value:
        return Path(value).expanduser().resolve()
    scripts = script_paths(settings)
    return scripts[0] if scripts else None


def voiceover_pause(settings: ExportSettings) -> float:
    """Return the bounded inter-voiceover silence in seconds."""
    try:
        return max(0.0, min(10.0, float(getattr(settings, "voiceover_pause", 0.7))))
    except (TypeError, ValueError):
        return 0.7


def voiceover_timeline_duration(durations: list[float], pause: float) -> float:
    """Return spoken audio plus exactly one gap between adjacent units."""
    return sum(max(0.0, float(value)) for value in durations) + max(0.0, float(pause)) * max(0, len(durations) - 1)



def _image_is_active(settings: ExportSettings) -> bool:
    return bool(
        getattr(settings, "image_enabled", False)
        and (getattr(settings, "image_path", "") or "").strip()
    )


def _validate_image_settings(settings: ExportSettings) -> None:
    if not getattr(settings, "image_enabled", False):
        return
    value = (getattr(settings, "image_path", "") or "").strip()
    if not value:
        raise VideoMergerError(
            "Include Image Insertion ist aktiviert, aber keine Bilddatei ausgewählt."
        )
    image_insertion_path(value)
    try:
        raw_duration = float(getattr(settings, "image_duration", 4.0))
    except (TypeError, ValueError) as exc:
        raise VideoMergerError("Ungültige Image-Insertion-Dauer; erlaubt sind 0.5–60.0 s.") from exc
    if not 0.5 <= raw_duration <= 60.0:
        raise VideoMergerError("Ungültige Image-Insertion-Dauer; erlaubt sind 0.5–60.0 s.")


def _image_position(settings: ExportSettings) -> str:
    return normalize_image_position(getattr(settings, "image_position", "after_intro"))


def _image_dimensions(engine: VideoMergerEngine, path: Path) -> tuple[int, int]:
    try:
        data = engine.analyzer.probe_raw(path)
        stream = next(item for item in data.get("streams", []) if item.get("codec_type") == "video")
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (StopIteration, TypeError, ValueError, KeyError, OSError) as exc:
        raise VideoMergerError(
            f"Image Insertion konnte nicht analysiert werden: {path.name}"
        ) from exc
    if width <= 0 or height <= 0:
        raise VideoMergerError(f"Image Insertion hat keine gültige Auflösung: {path.name}")
    return width, height

def _stage2_image_target(
    settings: ExportSettings, reference: MediaInfo | list[MediaInfo]
) -> tuple[int, int]:
    """Choose the actual output dimensions for a Stage-2 still image.

    The final target is still resolved by :mod:`target`; these dimensions only
    determine the canvas an uploaded image is fitted into. In particular, Auto
    must honor a portrait project even when the Main Video is landscape (and
    vice versa), rather than using the source video's shape as the target.
    When the complete Stage-2 sequence is available, use it so a 4K Intro or
    Outro also promotes an Auto project to a 4K canvas.
    """
    if str(settings.resolution or "").casefold() != "auto":
        return parse_resolution(settings.resolution)
    references = reference if isinstance(reference, list) else [reference]
    target_settings = replace(
        settings,
        workflow_stage="outro",
        timeline_target_duration=0.0,
    )
    resolved = resolve_export(references, target_settings)
    return resolved.width, resolved.height


def _offset_words(words: list[WordTiming], time_offset: float, char_offset: int) -> list[WordTiming]:
    return [
        WordTiming(
            text=word.text,
            start=word.start + time_offset,
            end=word.end + time_offset,
            confidence=word.confidence,
            script_start=word.script_start + char_offset,
            script_end=word.script_end + char_offset,
        )
        for word in words
    ]


def _concatenate_alignment(
    parts: list[tuple[AlignmentResult, float, int]],
    combined_script: str,
    languages: list[str],
) -> AlignmentResult:
    """Merge per-unit alignments into one canonical spoken timeline.

    ``parts`` are (alignment, time_offset, char_offset). Time offsets are the
    cumulative logical durations of previous units, including configured
    inter-unit silence; character offsets point into the combined script. The
    merged timeline is the single authoritative timing source for SRT/VTT/
    burn-in and for the video duration.
    """
    merged: list[WordTiming] = []
    compatibilities: list[float] = []
    confidences: list[float] = []
    warnings: list[str] = []
    methods: list[str] = []
    hard_breaks: list[float] = []
    for part_index, (alignment, time_offset, char_offset) in enumerate(parts):
        merged.extend(_offset_words(alignment.words, time_offset, char_offset))
        hard_breaks.extend(float(boundary) + time_offset for boundary in alignment.hard_breaks)
        if part_index > 0:
            hard_breaks.append(time_offset)
        compatibilities.append(alignment.compatibility)
        confidences.extend(word.confidence for word in alignment.words)
        warnings.extend(alignment.warnings)
        methods.append(alignment.method)
    average = sum(confidences) / max(1, len(confidences))
    return AlignmentResult(
        words=merged,
        language=languages[0] if languages else "auto",
        method=" + ".join(dict.fromkeys(methods)),
        compatibility=min(compatibilities) if compatibilities else 1.0,
        average_confidence=average,
        warnings=warnings,
        hard_breaks=sorted(set(hard_breaks)),
    )


def _aspect_token(aspect: str) -> str:
    return "9x16" if aspect == "9:16" else "16x9"


def _available_bundle(output_dir: Path, stem: str, suffixes: tuple[str, ...]) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        actual = stem if index == 1 else f"{stem}_{index}"
        paths = {suffix: output_dir / f"{actual}.{suffix}" for suffix in suffixes}
        if not any(path.exists() for path in paths.values()):
            return paths
        index += 1


def _available_dual_video_bundle(output_dir: Path, stem: str) -> tuple[Path, Path, Path]:
    """Reserve the primary (subtitled) + clean (no-subtitles) + metadata names.

    1.3.0: whenever subtitles are generated both user-facing variants must
    exist with deterministic, never-overwriting names.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        actual = stem if index == 1 else f"{stem}_{index}"
        primary = output_dir / f"{actual}.mp4"
        clean = output_dir / f"{actual}_no_subtitles.mp4"
        metadata = output_dir / f"{actual}_YouTube.txt"
        if not any(path.exists() for path in (primary, clean, metadata)):
            return primary, clean, metadata
        index += 1


def _subtitle_failure(stage: str, error: Exception | str) -> VideoMergerError:
    message = str(error)
    if message.startswith("SUBTITLE GENERATION FAILED"):
        return VideoMergerError(message)
    return VideoMergerError(f"SUBTITLE GENERATION FAILED [{stage}]: {message}")


def _seconds(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _offset_alignment(alignment: AlignmentResult, intro: float) -> AlignmentResult:
    """Shift a word timeline by the visual-only intro of the program.

    Alignment works on the voiceover clock (0 = first spoken sample). The final
    program starts with the configured visual intro, so every word and every
    hard break moves by exactly that amount: captions begin with the voiceover
    instead of at video time 0, and the visual intro/outro stay caption-free.
    Pure translation — no word is added, dropped, re-timed or re-aligned, so the
    acoustic word timing and all validation stay exactly as strict as before.
    """
    shift = max(0.0, float(intro or 0.0))
    if shift <= 1e-9:
        return alignment
    words = [
        replace(word, start=word.start + shift, end=word.end + shift)
        for word in alignment.words
    ]
    return replace(
        alignment,
        words=words,
        hard_breaks=[boundary + shift for boundary in alignment.hard_breaks],
    )


def _log_legacy_priority(
    media: list[MediaInfo], settings: ExportSettings, log: LogCallback,
) -> None:
    """Log the reserved Legacy Input Root opening clips of a Random sequence.

    One line, only for Random order and only when the preference actually
    reserved something, so the log stays readable and never claims a priority
    that a Natural/Alphabetical/Manual sequence does not have.
    """
    mode = normalize_video_order_mode(getattr(settings, "video_order_mode", "natural"))
    if mode != VIDEO_ORDER_RANDOM:
        return
    prefix = legacy_priority_prefix(media, getattr(settings, "legacy_input_root", ""))
    if not prefix:
        return
    log(
        "Legacy Input Root priority (Random): clips 1-"
        + str(len(prefix)) + " = " + ", ".join(item.path.name for item in prefix)
        + f" · remaining randomized pool starts at clip {len(prefix) + 1}"
    )


def _scale_alignment(alignment: AlignmentResult, speed: float) -> AlignmentResult:
    """Scale subtitle timing together with an explicit post-merge speed."""
    if abs(speed - 1.0) <= 1e-9:
        return alignment
    words = [
        replace(word, start=word.start / speed, end=word.end / speed)
        for word in alignment.words
    ]
    return replace(
        alignment,
        words=words,
        hard_breaks=[boundary / speed for boundary in alignment.hard_breaks],
    )


class MainProjectEngine:
    """Two-stage orchestration layered on the proven merge engine."""

    def __init__(
        self,
        engine: VideoMergerEngine,
        render_cache: Stage1RenderCache | None = None,
    ):
        self.engine = engine
        self.render_cache = render_cache or Stage1RenderCache()

    def _try_reuse_cached_main(
        self,
        fingerprint: str,
        resolved,
        subtitle_requested: bool,
        progress: ProgressCallback,
        log: LogCallback,
        cancel_event,
        subtitle_output_mode: str = SUBTITLE_OUTPUT_COMBINED,
    ) -> MainVideoResult | None:
        """Return a validated cached Stage-1 result, or ``None`` on any miss.

        Cache lookup is deliberately fail-closed: an unreadable manifest,
        missing artifact, invalid FFprobe result, or missing subtitle timeline
        becomes a normal fresh render rather than a silent reuse of bad data.
        """
        if cancel_event is not None and cancel_event.is_set():
            return None
        record = self.render_cache.load(fingerprint)
        if record is None:
            return None
        mode = normalize_subtitle_output_mode(subtitle_output_mode)
        record_mode = normalize_subtitle_output_mode(record.get("subtitle_output_mode"))
        if bool(record.get("subtitle_requested")) != bool(subtitle_requested) or record_mode != mode:
            log("Stage 1 cache MISS: subtitle output mode differs from the cached render.")
            return None
        sidecars_requested = subtitle_requested and subtitle_sidecars_requested(mode)
        clean_variant_requested = subtitle_requested and subtitle_clean_variant_requested(mode)
        self.render_cache.restore_sidecars(record)
        artifacts = self.render_cache.artifact_paths(record)
        video = artifacts.get("video")
        clean_video = artifacts.get("video_no_subtitles")
        if video is None or not video.is_file() or video.stat().st_size <= 0:
            log("Stage 1 cache MISS: cached Main Video is missing or empty.")
            return None
        if clean_variant_requested and (
            clean_video is None or not clean_video.is_file() or clean_video.stat().st_size <= 0
        ):
            log("Stage 1 cache MISS: cached clean Main Video is missing or empty.")
            return None
        if subtitle_requested and artifacts.get("canonical_timeline") is None:
            log("Stage 1 cache MISS: cached subtitle timeline is missing.")
            return None
        if sidecars_requested and any(
            artifacts.get(key) is None
            or not artifacts[key].is_file()
            or artifacts[key].stat().st_size <= 0
            for key in ("srt", "vtt")
        ):
            log("Stage 1 cache MISS: required subtitle sidecars could not be restored.")
            return None
        if sidecars_requested:
            try:
                validate_subtitle_file(artifacts["srt"], "srt")
                validate_subtitle_file(artifacts["vtt"], "vtt")
            except Exception as exc:
                log(f"Stage 1 cache MISS: cached subtitle sidecar validation failed: {exc}")
                return None

        report = validate_output(video, self.engine.ffprobe_path, resolved)
        if not report.ok:
            log("Stage 1 cache MISS: cached Main Video failed FFprobe validation.")
            return None
        if clean_variant_requested:
            clean_report = validate_output(clean_video, self.engine.ffprobe_path, resolved)
            if not clean_report.ok:
                log("Stage 1 cache MISS: cached clean Main Video failed FFprobe validation.")
                return None

        alignment = None
        timeline = artifacts.get("canonical_timeline")
        if subtitle_requested and timeline is not None:
            try:
                alignment = load_cached_alignment(timeline)
            except (OSError, ValueError, TypeError, KeyError):
                log("Stage 1 cache MISS: cached subtitle timeline is unreadable.")
                return None

        timings: dict[str, float | str | bool] = {
            "cache_hit": True,
            "render_reused": True,
            "ffmpeg_rendering_seconds": 0.0,
            "total_pipeline_seconds": 0.0,
        }
        log(f"Stage 1 cache HIT: reusing validated Main Video {video.name}; FFmpeg skipped.")
        progress(ProgressEvent(
            100.0,
            report.duration,
            report.duration,
            0.0,
            0.0,
            "Stage 1 – Reused Main Video",
            video.name,
        ))
        return MainVideoResult(
            video=video,
            srt=artifacts.get("srt") if sidecars_requested else None,
            vtt=artifacts.get("vtt") if sidecars_requested else None,
            report=report,
            alignment=alignment,
            warnings=[],
            canonical_timeline=timeline if subtitle_requested else None,
            verification_frames=[],
            timings=timings,
            video_no_subtitles=clean_video if clean_variant_requested else None,
        )

    def create_main(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        output_dir: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event=None,
        aligner: LocalWordAligner | None = None,
        reuse_cached: bool = False,
        video_order_rng=None,
        video_order_seed: int | None = None,
        order_already_applied: bool = False,
        output_stem: str | None = None,
        short_video_pool: ShortsVideoPool | None = None,
    ) -> MainVideoResult:
        total_started = time.perf_counter()
        timings: dict[str, float | str | bool] = {}
        if not media:
            raise VideoMergerError("Stage 1 benötigt mindestens einen Videoclip.")
        if not order_already_applied:
            media = order_media_for_video_order(
                media,
                getattr(settings, "video_order_mode", "natural"),
                rng=video_order_rng,
                seed=video_order_seed,
                legacy_root=getattr(settings, "legacy_input_root", ""),
            )
            # Only the call that really ordered the sequence reports the Legacy
            # Input Root priority; an orchestrated run logs it exactly once.
            _log_legacy_priority(media, settings, log)
        log(
            "Effective video order: "
            + " → ".join(item.path.name for item in media)
        )
        script_mode = "matched" if str(settings.script_mode).casefold() in {"matched", "individual"} else "single"
        units, unit_scripts = ordered_voiceover_units(settings)
        # A configured-but-missing voiceover is not a real acoustic timeline
        # unit. Drop it before probing and alignment so no phantom audio or
        # inter-unit pause is introduced; preserve every available file.
        available_units: list[Path] = []
        available_scripts: list[Path | None] = []
        missing_units: list[Path] = []
        for index, unit in enumerate(units):
            if unit.is_file():
                available_units.append(unit)
                if script_mode == "matched":
                    available_scripts.append(unit_scripts[index] if index < len(unit_scripts) else None)
            else:
                missing_units.append(unit)
        if missing_units:
            log(
                "Voiceover files skipped because they are unavailable (no audio or pause added): "
                + ", ".join(unit.name for unit in missing_units)
            )
        units = available_units
        if script_mode == "matched":
            unit_scripts = available_scripts
        music_path = optional_path(settings.music_path)
        watermark_path = optional_path(settings.watermark_path)

        subtitle_mode = normalize_subtitle_output_mode(
            getattr(settings, "subtitle_output_mode", SUBTITLE_OUTPUT_COMBINED)
        )
        if (
            subtitle_mode == SUBTITLE_OUTPUT_WITH
            and getattr(settings, "subtitle_output_mode_was_defaulted", False)
        ):
            subtitle_mode = normalize_subtitle_output_mode("burned_and_sidecars")
            log("Legacy direct API default retained: dual subtitle bundle.")
        # A supplied script is an explicit subtitle request unless the user
        # explicitly selected Without Subtitles. The latter still keeps the
        # voiceover as the duration/audio authority, but performs no alignment,
        # ASS burn, SRT, or VTT work.
        global_script = global_script_path(settings) if script_mode == "single" else None
        matched_script_paths = [path for path in unit_scripts if path is not None]
        source_requested = bool(settings.subtitle_enabled or global_script or matched_script_paths)
        subtitle_requested = subtitle_render_requested(subtitle_mode, source_requested)
        if subtitle_requested and not units:
            raise _subtitle_failure(
                "input validation",
                "Voiceover audio and the authoritative script.txt are both required.",
            )
        if subtitle_requested and script_mode == "single" and not global_script:
            raise _subtitle_failure(
                "script matching",
                "Single Global Script mode requires one script for the complete voiceover timeline.",
            )
        script_files: list[Path] = []
        if subtitle_requested:
            if script_mode == "single":
                try:
                    script_files = [require_asset(global_script, "Textskript", {".txt", ".text", ".md"})]
                except Exception as exc:
                    raise _subtitle_failure("script matching", exc) from exc
            else:
                # Individual script coverage is intentionally sparse. Missing
                # assignments are silent audio-only units, while a present but
                # invalid path is reported and omitted without shifting later
                # voiceover/script pairings.
                for path in unit_scripts:
                    if path is None:
                        continue
                    try:
                        script_files.append(require_asset(path, "Textskript", {".txt", ".text", ".md"}))
                    except Exception as exc:
                        log(f"Script skipped for partial matched coverage: {path}: {exc}")
        if subtitle_requested and units and (global_script or matched_script_paths) and not settings.subtitle_enabled:
            log(
                "Subtitles auto-enabled: Voiceover + Script are assigned; "
                f"output mode is {subtitle_mode}."
            )
        elif subtitle_mode == SUBTITLE_OUTPUT_WITHOUT and source_requested:
            log("Subtitle output mode Without Subtitles: no alignment, burn-in, SRT, or VTT will be generated.")

        audio_started = time.perf_counter()
        try:
            voice_assets = [
                probe_audio(self.engine.ffprobe_path, require_asset(unit, "Voiceover", AUDIO_EXTENSIONS))
                for unit in units
            ]
        except Exception as exc:
            if subtitle_requested:
                raise _subtitle_failure("voiceover validation", exc) from exc
            raise
        timings["voiceover_processing_seconds"] = time.perf_counter() - audio_started
        voice = voice_assets[0] if voice_assets else None
        # The voiceover sequence is the timing authority. Inter-unit silence
        # is real program audio, so it belongs in the Main target and all
        # cumulative alignment offsets; the existing final_pause remains the
        # separate end padding after the complete voiceover timeline.
        inter_voiceover_pause = voiceover_pause(settings)
        voice_total = voiceover_timeline_duration(
            [asset.duration for asset in voice_assets], inter_voiceover_pause
        )

        music_started = time.perf_counter()
        music = (
            probe_audio(self.engine.ffprobe_path, require_asset(music_path, "Background Music", AUDIO_EXTENSIONS))
            if music_path else None
        )
        timings["music_processing_seconds"] = time.perf_counter() - music_started
        if settings.watermark_enabled:
            require_asset(watermark_path, "Watermark", IMAGE_EXTENSIONS)

        fps, _fps_expr = choose_fps(media, settings.fps_choice)
        warnings: list[str] = []
        render_media = list(media)
        # 1.3.0 Global Video Speed: 0.50x–2.00x, 1.00x default. The voiceover
        # remains the timing authority — the target duration, subtitle
        # timeline, voiceover and music behavior never change; only the clip
        # playback rate (and therefore how much material is required).
        duration_before_merge = duration_before_merge_value(settings)
        if abs(duration_before_merge - 1.0) > 1e-6:
            log(
                f"Duration Before Merge: {duration_before_merge:.2f}x – "
                "jeder normale ausgewählte Clip wird vor der Timeline entsprechend angepasst."
            )
        duration_fit_mode = settings.duration_fit_mode if settings.duration_fit_mode in {"cut", "stretch"} else "cut"
        max_stretch = max(1.0, min(50.0, float(getattr(settings, "max_stretch_percent", 10.0) or 10.0)))
        timeline_plan: MainTimeline | None = None
        if voice_assets:
            # Canonical timeline of this render: [visual intro][voiceover +
            # normal video][visual outro]. One source of truth for the video
            # target, the clip/pool reservation, the audio program window, the
            # subtitle offset and the log lines below.
            timeline_plan = main_timeline(settings, voice_total)
            target = timeline_plan.target
            selection_media = media
            if short_video_pool is not None:
                # The pool owns only cross-Short consumption. The established
                # duration selector still decides the prefix for this Short;
                # this call merely removes that raw prefix from the shared
                # without-replacement cursor before the normal fit below.
                selection_media = short_video_pool.take_for_duration(
                    target,
                    settings.transition_duration,
                    fps,
                    settings.short_video_mode,
                    duration_fit_mode=duration_fit_mode,
                    max_stretch_percent=max_stretch,
                    playback_rate=duration_before_merge,
                )
                log(
                    f"Shorts without-replacement pool: assigned {len(selection_media)} clip(s); "
                    f"{short_video_pool.remaining_count} clip(s) remain before the next Short."
                )
            render_media, timing_warnings = fit_media_to_duration(
                selection_media, target, settings.transition_duration, fps, settings.short_video_mode,
                duration_fit_mode=duration_fit_mode,
                max_stretch_percent=max_stretch,
                playback_rate=duration_before_merge,
                # The effective project sequence was chosen above. Do not run
                # a second automatic folder shuffle after Required-Only
                # selection has begun; that could select a different prefix.
                folder_aware=False,
            )
            warnings.extend(timing_warnings)
            # Music and clip-original audio cover the visual intro and the
            # spoken timeline; the visual outro stays without them, exactly like
            # the historical end padding did.
            program_duration = timeline_plan.audio_program if timeline_plan else voice_total
        else:
            target = 0.0
            program_duration = 0.0

        render_settings = replace(
            settings,
            workflow_stage="main",
            program_duration=program_duration,
            timeline_target_duration=target,
            subtitle_enabled=subtitle_requested,
            subtitle_output_mode=subtitle_mode,
            # The Stage-1 export receives an already ordered/fitted sequence;
            # its explicit hand-off flag prevents a second order pass while
            # retaining the selected project mode on the render settings.
            subtitle_ass_path="",
            subtitle_fonts_dir=str(bundled_fonts_dir()),
            voiceover_paths=[str(asset.path) for asset in voice_assets],
            voiceover_path=str(voice.path) if voice else "",
            script_paths=(
                [str(path) for path in unit_scripts if path]
                if script_mode == "matched"
                else ([str(global_script)] if global_script else [])
            ),
            script_path=(
                str(unit_scripts[0]) if script_mode == "matched" and unit_scripts and unit_scripts[0]
                else (str(global_script) if global_script else "")
            ),
            global_script_path=str(global_script) if global_script else "",
            music_path=str(music.path) if music else "",
            watermark_path=str(watermark_path) if watermark_path else "",
        )
        resolved = self.engine.make_plan(render_media, render_settings, log)
        if not voice_assets:
            render_settings = replace(render_settings, program_duration=resolved.expected_duration)
        elif abs(resolved.expected_duration - target) > max(0.04, 1.0 / resolved.fps):
            warnings.append(
                f"Zielabweichung nach Frame-Rundung: {resolved.expected_duration - target:+.3f} s."
            )

        # The cache validates the final Stage-1 artifact, not the temporary
        # pre-post-merge master. Keep After Merge in Stage 1 while leaving the
        # clip-selection/timeline target above untouched.
        cache_resolved = resolved
        if after_merge_enabled(settings):
            after_speed = duration_after_merge_value(settings)
            cache_resolved = replace(
                resolved,
                expected_duration=resolved.expected_duration / after_speed,
            )
        stage1_digest, stage1_payload = stage1_fingerprint(
            render_media,
            settings,
            cache_resolved,
            voice_assets=voice_assets,
            script_files=script_files,
            subtitle_requested=subtitle_requested,
            music_asset=music,
            watermark_path=watermark_path,
        )
        if reuse_cached:
            cached_result = self._try_reuse_cached_main(
                stage1_digest,
                cache_resolved,
                subtitle_requested,
                progress,
                log,
                cancel_event,
                subtitle_output_mode=subtitle_mode,
            )
            if cached_result is not None:
                return cached_result

        # 1.3.0 Clean Output Directory: the user-facing folder receives only
        # useful artifacts (MainVideo.mp4, MainVideo_no_subtitles.mp4 when
        # subtitles exist, SRT, VTT). Internal evidence (verification PNGs,
        # canonical timeline JSON, staged ASS) lives under temp/ and never
        # clutters the Output folder.
        sidecars_requested = subtitle_requested and subtitle_sidecars_requested(subtitle_mode)
        clean_variant_requested = subtitle_requested and subtitle_clean_variant_requested(subtitle_mode)
        output_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = project_root() / "temp"
        if subtitle_requested:
            temp_dir.mkdir(parents=True, exist_ok=True)
        base_stem = output_stem or f"MainVideo_{_aspect_token(settings.aspect)}"
        name_index = 1
        while True:
            actual = base_stem if name_index == 1 else f"{base_stem}_{name_index}"
            output_video = output_dir / f"{actual}.mp4"
            # The clean master is user-facing only in With and Without mode;
            # With Subtitles keeps it internal and Without does not need it.
            output_video_clean = (
                output_dir / f"{actual}_no_subtitles.mp4"
                if clean_variant_requested
                else temp_dir / f".{actual}.clean_master_{uuid.uuid4().hex}.mp4"
            )
            srt_candidate = output_dir / f"{actual}.srt"
            vtt_candidate = output_dir / f"{actual}.vtt"
            reserved: list[Path] = [output_video]
            if clean_variant_requested:
                reserved.append(output_video_clean)
            if sidecars_requested:
                reserved += [srt_candidate, vtt_candidate]
            if not any(path.exists() for path in reserved):
                break
            name_index += 1
        srt_path: Path | None = None
        vtt_path: Path | None = None
        timeline_path: Path | None = None
        alignment = None
        ass_path: Path | None = None
        verification_frames: list[Path] = []
        if subtitle_requested:
            timeline_path = temp_dir / f"{output_video.stem}.subtitle_timeline.json"
            if sidecars_requested:
                srt_path, vtt_path = srt_candidate, vtt_candidate

        log("Stage 1 – Create Main Video")
        log("Aktive Clip-Reihenfolge: " + " → ".join(item.path.name for item in render_media))
        render_folders = [
            media_source_folder(item)
            for item in render_media
            if not item.is_image_insertion
        ]
        if len(set(render_folders)) > 1:
            log("Folder-aware alternation: consecutive clips use different source folders whenever an alternative remains.")
        if settings.short_video_mode == "loop" and len(render_media) > len(media):
            log("Full-Timeline Loop sequence: " + " → ".join(item.path.name for item in render_media))
        elif settings.short_video_mode == "hold":
            log("Short-video mode: Hold Last Frame (separate from Full-Timeline Loop).")
        log(f"Videomaterial: {sum(item.source_duration or item.duration for item in render_media):.3f} s")
        if voice_assets:
            if len(voice_assets) == 1:
                log(
                    f"Voiceover: {voice_total:.3f} s, {voice.sample_rate} Hz, {voice.channels} Kanal/Kanäle; "
                    f"Ziel Main Video: {resolved.expected_duration:.3f} s"
                )
            else:
                log(
                    "Voiceover: " + " → ".join(asset.path.name for asset in voice_assets)
                    + f" (gesamte Sprech-Timeline {voice_total:.3f} s, "
                    f"{inter_voiceover_pause:.2f} s Pause zwischen Einheiten); "
                    f"Ziel Main Video: {resolved.expected_duration:.3f} s"
                )
            if timeline_plan is not None:
                # One concise timeline block per job: visual sections, voiceover
                # start, spoken end and the resulting caption window.
                for timeline_line in timeline_plan.log_lines():
                    log(timeline_line)
            log(
                f"Script Mode: {'Multiple Matched Scripts' if script_mode == 'matched' else 'Single Global Script'}"
            )
        else:
            log("Voiceover: nicht zugewiesen; bestehender Video-Workflow bleibt aktiv.")
        if music:
            log(
                f"Music: {music.duration:.3f} s, {music.sample_rate} Hz; wird geloopt und auf "
                f"{render_settings.program_duration:.3f} s begrenzt."
            )
        else:
            log("Music: nicht zugewiesen.")

        try:
            if subtitle_requested:
                try:
                    aligner = aligner or LocalWordAligner(settings.subtitle_model)
                except Exception as exc:
                    raise _subtitle_failure("alignment engine", exc) from exc
                script_started = time.perf_counter()
                combined_script = ""
                combined_alignment: AlignmentResult | None = None
                try:
                    from .alignment import script_word_spans
                    if script_mode == "matched":
                        # Align every voiceover/script pair separately (each pair
                        # is independently cacheable), then concatenate the
                        # canonical word timelines with cumulative offsets.
                        parts: list[tuple[AlignmentResult, float, int]] = []
                        segments: list[str] = []
                        char_cursor = 0
                        time_cursor = 0.0
                        languages: list[str] = []
                        for unit_index, (asset, script_path_unit) in enumerate(zip(voice_assets, unit_scripts)):
                            segment = ""
                            unit_alignment = AlignmentResult(
                                words=[], language="auto", method="no script coverage",
                                compatibility=1.0, average_confidence=0.0,
                                warnings=[f"No script coverage for voiceover: {asset.path.name}"],
                            )
                            if script_path_unit is not None and script_path_unit.is_file():
                                try:
                                    segment = read_script(require_asset(
                                        script_path_unit, "Textskript", {".txt", ".text", ".md"}
                                    )).strip()
                                except VideoMergerError as exc:
                                    log(
                                        f"Script skipped for {asset.path.name}; audio remains authoritative: {exc}"
                                    )
                                if segment:
                                    try:
                                        alignment_kwargs = (
                                            {"fallback_end": asset.duration}
                                            if isinstance(aligner, LocalWordAligner) else {}
                                        )
                                        unit_alignment = aligner.align(
                                            segment, asset.path, settings.subtitle_language,
                                            **alignment_kwargs,
                                        )
                                    except Exception as exc:
                                        # ASR/alignment engine failures are
                                        # genuine global/system failures and are
                                        # not silently converted into captions.
                                        raise _subtitle_failure(
                                            "local ASR / word alignment", exc
                                        ) from exc
                            else:
                                log(f"No script assigned for {asset.path.name}; audio remains unsubtitled for this unit.")
                            # Keep one logical script slot per audio unit so
                            # later matched units retain their exact character
                            # offsets even when this unit has no script.
                            segments.append(segment)
                            languages.append(unit_alignment.language)
                            parts.append((unit_alignment, time_cursor, char_cursor))
                            char_cursor += len(segment)
                            if unit_index < len(voice_assets) - 1:
                                # The combined script uses exactly two newline
                                # characters between matched segments.
                                char_cursor += 2
                            time_cursor += asset.duration
                            if unit_index < len(voice_assets) - 1:
                                time_cursor += inter_voiceover_pause
                        combined_script = "\n\n".join(segments)
                        combined_alignment = _concatenate_alignment(parts, combined_script, languages)
                        log(
                            f"Scripts: {sum(bool(segment) for segment in segments)} of {len(segments)} matched Textskripte ("
                            + ", ".join(asset.path.name for asset in voice_assets)
                            + f"); gesamt {len(script_word_spans(combined_script))} Wörter"
                        )
                    else:
                        # Single global script: one authoritative text is
                        # mapped once onto the complete ordered acoustic
                        # timeline. LocalWordAligner keeps each unit's ASR
                        # transcription cacheable, then caches this composed
                        # mapping by unit order, durations and pause. It is
                        # never duplicated into per-voiceover script files.
                        script = read_script(require_asset(
                            global_script, "Textskript", {".txt", ".text", ".md"}
                        ))
                        combined_script = script
                        try:
                            if hasattr(aligner, "align_global"):
                                combined_alignment = aligner.align_global(
                                    script,
                                    [(asset.path, asset.duration) for asset in voice_assets],
                                    settings.subtitle_language,
                                    inter_voiceover_pause,
                                )
                            else:
                                # Compatibility hook for small injected test
                                # aligners and third-party implementations.
                                recognized_all: list = []
                                detected_languages: list[str] = []
                                pause_boundaries: list[float] = []
                                time_cursor = 0.0
                                for unit_index, asset in enumerate(voice_assets):
                                    recognized, detected = aligner.recognize(
                                        asset.path, settings.subtitle_language
                                    )
                                    recognized_all.extend(_offset_words(
                                        [WordTiming(
                                            text=word.text, start=word.start, end=word.end,
                                            confidence=word.confidence,
                                        ) for word in recognized],
                                        time_cursor, 0,
                                    ))
                                    detected_languages.append(detected)
                                    time_cursor += asset.duration
                                    if unit_index < len(voice_assets) - 1:
                                        time_cursor += inter_voiceover_pause
                                        if inter_voiceover_pause > 1e-9:
                                            pause_boundaries.append(time_cursor)
                                combined_alignment = aligner.align_from_recognized(
                                    script, recognized_all,
                                    detected_languages[0] if detected_languages else settings.subtitle_language,
                                )
                                combined_alignment.hard_breaks = sorted(
                                    set(combined_alignment.hard_breaks + pause_boundaries)
                                )
                        except Exception as exc:
                            raise _subtitle_failure(
                                "local ASR / word alignment", exc
                            ) from exc
                        log(
                            f"Script: {len(script_word_spans(script))} Wörter; ausgewählte Sprache: "
                            f"{settings.subtitle_language}; globales Alignment über "
                            f"{len(voice_assets)} Voiceover-Einheiten"
                        )
                    alignment = combined_alignment
                except VideoMergerError:
                    raise
                except Exception as exc:
                    raise _subtitle_failure("script loading", exc) from exc
                timings["script_processing_seconds"] = time.perf_counter() - script_started

                align_timings = dict(getattr(aligner, "last_timings", {}))
                timings["asr_seconds"] = (
                    _seconds(align_timings.get("model_loading_seconds"))
                    + _seconds(align_timings.get("transcription_seconds"))
                )
                timings["alignment_seconds"] = _seconds(align_timings.get("forced_mapping_seconds"))
                timings["alignment_cache_hit"] = bool(align_timings.get("cache_hit", False))
                timings["alignment_cache_level"] = str(align_timings.get("cache_level", "none"))
                log(
                    "Alignment cache: " + (
                        f"HIT ({timings['alignment_cache_level']}) – ASR not repeated"
                        if timings["alignment_cache_hit"] else "MISS – voiceover ASR executed once"
                    )
                )
                for warning in alignment.warnings:
                    warnings.append(warning)
                    log("WARNUNG: " + warning)
                # Compatibility and unmatched-word warnings describe local
                # caption gaps; they must never turn a usable audio render into
                # a global subtitle failure. Only genuinely invalid/system
                # errors raised by the ASR or file pipeline fail the workflow.
                if alignment.words and alignment.words[-1].start >= voice_total:
                    raise _subtitle_failure(
                        "word timeline validation",
                        "Der letzte Wortbeginn liegt außerhalb der Voiceover-Timeline.",
                    )

                # A post-merge speed change applies to the complete finished
                # program, including burned subtitles. Scale the authoritative
                # word timeline before writing SRT/VTT/ASS so sidecars and the
                # final video remain synchronized.
                subtitle_speed = duration_after_merge_value(settings) if after_merge_enabled(settings) else 1.0
                # Offset first (program clock), then scale: an explicit After
                # Merge speed applies to the complete finished program including
                # the visual intro, so captions keep their exact spoken interval.
                subtitle_intro = timeline_plan.intro if timeline_plan else 0.0
                alignment = _scale_alignment(
                    _offset_alignment(alignment, subtitle_intro), subtitle_speed
                )
                subtitle_program_end = (voice_total + subtitle_intro) / subtitle_speed
                subtitle_creation_started = time.perf_counter()
                try:
                    cues = build_cues(
                        combined_script, alignment, settings.subtitle_style, program_end=subtitle_program_end,
                        width=resolved.width, height=resolved.height, font_key=settings.subtitle_font,
                    )
                    timeline_path = temp_dir / f"{output_video.stem}.subtitle_timeline.json"
                    if sidecars_requested:
                        srt_path, vtt_path = srt_candidate, vtt_candidate
                        write_srt(cues, srt_path)
                        write_vtt(cues, vtt_path)
                        validate_subtitle_file(srt_path, "srt")
                        validate_subtitle_file(vtt_path, "vtt")
                    else:
                        # Burned Only intentionally has no SRT/VTT output;
                        # the canonical timeline remains a private cache input.
                        srt_path = vtt_path = None
                    write_canonical_timeline(combined_script, alignment, cues, timeline_path)
                    if cues and cues[-1].end > subtitle_program_end + 0.001:
                        raise VideoMergerError("Untertitel reichen in die Quiet Pause hinein.")
                    temp = project_root() / "temp"
                    temp.mkdir(parents=True, exist_ok=True)
                    ass_path = temp / f"{output_video.stem}_burn.ass"
                    write_ass(
                        combined_script, cues, ass_path, settings.subtitle_style,
                        settings.subtitle_position, resolved.width, resolved.height,
                        animation=settings.subtitle_animation, font_key=settings.subtitle_font,
                        debug_overlay=settings.subtitle_debug_overlay,
                    )
                    if not ass_path.is_file() or "[Events]" not in ass_path.read_text(encoding="utf-8-sig"):
                        raise VideoMergerError("ASS burn-in track could not be created.")
                    render_settings = replace(render_settings, subtitle_ass_path=str(ass_path))
                    timings["subtitle_creation_seconds"] = time.perf_counter() - subtitle_creation_started
                except Exception as exc:
                    raise _subtitle_failure("SRT/VTT/ASS timeline creation", exc) from exc

                log(
                    f"Subtitle Alignment: {len(alignment.words)} Wörter, Methode {alignment.method}, "
                    f"Kompatibilität {alignment.compatibility:.1%}, Confidence {alignment.average_confidence:.1%}"
                )
                subtitle_end = cues[-1].end if cues else 0.0
                log(
                    f"SRT/VTT validiert; Untertitel von {subtitle_intro:.3f} s (Voiceover-Start) "
                    f"bis {subtitle_end:.3f} s (Ende des gesprochenen Inhalts) – "
                    "kein Untertitel im Visual Intro oder Visual Outro."
                )

            for warning in warnings:
                log("WARNUNG: " + warning)
            render_started = time.perf_counter()
            clean_report: ValidationReport | None = None
            try:
                if subtitle_requested:
                    # 1.3.0 dual output: render the CLEAN master first (no
                    # burn-in in the graph), then burn the ASS into the
                    # primary variant in a dedicated libass pass. Both files
                    # share the same timeline, encoder settings and color
                    # tags; audio is stream-copied in the burn pass.
                    render_settings = replace(render_settings, subtitle_enabled=False)
                    clean_report = self.engine.export(
                        render_media, render_settings, resolved, output_video_clean,
                        progress=progress, log=log, cancel_event=cancel_event,
                        video_order_applied=True,
                    )
                    if not clean_report.ok:
                        raise VideoMergerError(
                            clean_report.message or "Das subtitle-freie MainVideo konnte nicht erstellt werden."
                        )
                    render_settings = replace(render_settings, subtitle_enabled=True)
                    burn_started = time.perf_counter()
                    try:
                        report = self.engine.burn_subtitles(
                            output_video_clean, ass_path, render_settings.subtitle_fonts_dir,
                            output_video, resolved, render_media,
                            progress=progress, log=log, cancel_event=cancel_event,
                        )
                    except Exception as exc:
                        raise _subtitle_failure("subtitle burn-in pass", exc) from exc
                    timings["subtitle_burn_seconds"] = time.perf_counter() - burn_started
                else:
                    report = self.engine.export(
                        render_media, render_settings, resolved, output_video,
                        progress=progress, log=log, cancel_event=cancel_event,
                        video_order_applied=True,
                    )
            except Exception as exc:
                if subtitle_requested:
                    raise _subtitle_failure("single-pass FFmpeg burn-in render", exc) from exc
                raise
            timings["ffmpeg_rendering_seconds"] = time.perf_counter() - render_started

            finalization_started = time.perf_counter()
            if subtitle_requested:
                try:
                    # Internal test evidence only (1.3.0 Clean Output): the
                    # verification frames live under temp/ — they never
                    # clutter the user-facing Output folder (explicitly
                    # allowed as internal evidence) and remain available for
                    # decoding checks after the render.
                    frame_paths = {
                        label: temp_dir / f"{output_video.stem}.subtitle_{label}.png"
                        for label in ("first", "middle", "final")
                    }
                    if alignment.words:
                        verification_frames = create_visual_verification_frames(
                            self.engine.ffmpeg_path, output_video, alignment, frame_paths,
                        )
                    else:
                        # An all-mismatch timeline is valid audio-only subtitle
                        # output. There is no spoken word at which a visual
                        # verification frame could be sampled, so do not turn
                        # the intentionally empty subtitle track into a render
                        # failure.
                        verification_frames = []
                        log("Visual subtitle verification skipped: no reliable matched words.")
                    required = [timeline_path, *verification_frames]
                    if sidecars_requested:
                        required = [srt_path, vtt_path, *required]
                    if not all(path and path.is_file() and path.stat().st_size > 0 for path in required):
                        raise VideoMergerError("Mindestens ein Subtitle-Ausgabeartefakt fehlt.")
                    sidecar_status = "SRT: PASS · VTT: PASS" if sidecars_requested else "SRT: not generated · VTT: not generated"
                    log(
                        "Subtitle Generation: PASS · Word-Level Alignment: PASS · "
                        + sidecar_status + " · Burned-In Subtitles: PASS"
                    )
                    log(
                        "Visual verification frames (decoded from final MP4, internal evidence): "
                        + ", ".join(path.name for path in verification_frames)
                    )
                    if sidecars_requested:
                        log(
                            "Subtitle output mode: With Burned-in Subtitles + SRT + VTT · "
                            + output_video.name + " (burned) + "
                            + output_video_clean.name + " (clean master)"
                        )
                    else:
                        log(
                            "Subtitle output mode: With Burned-in Subtitles only · "
                            + output_video.name + " (burned); no SRT/VTT files generated."
                        )
                except Exception as exc:
                    raise _subtitle_failure("first/middle/final visual verification", exc) from exc
            timings["finalization_seconds"] = time.perf_counter() - finalization_started
            timings["total_pipeline_seconds"] = time.perf_counter() - total_started
            for key in (
                "voiceover_processing_seconds", "music_processing_seconds", "asr_seconds",
                "alignment_seconds", "subtitle_creation_seconds", "ffmpeg_rendering_seconds",
                "subtitle_burn_seconds", "finalization_seconds", "total_pipeline_seconds",
            ):
                if key in timings:
                    log(f"PERFORMANCE {key}={float(timings[key]):.3f}")
            try:
                self.render_cache.save(
                    stage1_digest,
                    stage1_payload,
                    video=output_video,
                    video_no_subtitles=output_video_clean if clean_variant_requested else None,
                    srt=srt_path,
                    vtt=vtt_path,
                    canonical_timeline=timeline_path,
                    subtitle_requested=subtitle_requested,
                    subtitle_output_mode=subtitle_mode,
                )
                timings["render_cache_saved"] = True
                log(f"Stage 1 cache saved: {stage1_digest[:12]}…")
            except Exception as exc:
                # Rendering remains successful if the optional cache location
                # is unavailable; a later One-Click run will simply render.
                timings["render_cache_saved"] = False
                log(f"WARNUNG: Stage 1 cache konnte nicht gespeichert werden: {exc}")
            result = MainVideoResult(
                output_video, srt_path, vtt_path, report, alignment, warnings,
                canonical_timeline=timeline_path,
                verification_frames=verification_frames,
                timings=timings,
                video_no_subtitles=output_video_clean if clean_variant_requested else None,
            )
            result.timings["render_reused"] = False
            result.timings["cache_hit"] = False
            return result
        except Exception:
            # Never leave a captionless/partial bundle looking successful.
            output_video.unlink(missing_ok=True)
            output_video_clean.unlink(missing_ok=True)
            if srt_path is not None:
                srt_path.unlink(missing_ok=True)
            if vtt_path is not None:
                vtt_path.unlink(missing_ok=True)
            raise
        finally:
            # ASS and internal clean masters are process-local artifacts. Clean
            # them even when FFmpeg fails before creating the burned output.
            if ass_path:
                ass_path.unlink(missing_ok=True)
            if not clean_variant_requested:
                output_video_clean.unlink(missing_ok=True)

    def create_complete(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        output_dir: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event=None,
        aligner: LocalWordAligner | None = None,
        video_order_rng=None,
        video_order_seed: int | None = None,
        order_already_applied: bool = False,
    ) -> CompleteWorkflowResult | YoutubeExportResult:
        """Run the selected YouTube delivery mode through the complete workflow."""
        mode = normalize_export_mode(getattr(settings, "export_mode", EXPORT_MODE_LONG_FORM))
        if mode != EXPORT_MODE_LONG_FORM:
            return self.create_youtube_exports(
                media, settings, output_dir, progress=progress, log=log,
                cancel_event=cancel_event, aligner=aligner,
                video_order_rng=video_order_rng, video_order_seed=video_order_seed,
                order_already_applied=order_already_applied, complete=True,
            )
        return self._create_complete_single(
            media, settings, output_dir, progress=progress, log=log,
            cancel_event=cancel_event, aligner=aligner,
            video_order_rng=video_order_rng, video_order_seed=video_order_seed,
            order_already_applied=order_already_applied,
        )

    @staticmethod
    def _publish_youtube_sidecars(
        result: MainVideoResult | CompleteWorkflowResult,
        output_dir: Path,
        stem: str,
    ) -> None:
        """Move the internal Stage-1 SRT/VTT copies into the job bundle."""
        main = result if isinstance(result, MainVideoResult) else result.main
        for attribute in ("srt", "vtt"):
            source = getattr(main, attribute)
            if source is None or not source.is_file():
                continue
            target = output_dir / f"{stem}{source.suffix.lower()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            setattr(main, attribute, target)

    def _publish_short_script_text(
        self,
        result: MainVideoResult | CompleteWorkflowResult,
        job_settings: ExportSettings,
        log: LogCallback,
    ) -> None:
        """Write one ``<Short video name>.txt`` with that Short's own script text.

        Every Short automatically gets exactly one plain-text sidecar beside its
        video, containing the script text this Short uses: the derived section of
        a global script, its basename-matched individual script, or — for a
        single voiceover — the complete global script. The content is read back
        from the script that this job's render settings already resolved, so no
        additional transcription/ASR runs and the file can never contain text
        from another Short. The name follows the FINAL video (including a name
        that was bumped because the file already existed), which keeps the stable
        per-Short numbering identical for video and text.

        An explicit audio-only Short (its voiceover speaks no part of the global
        script) has no text to publish and gets no sidecar; a sidecar problem is
        logged and never turns a rendered Short into a failed job.
        """
        video = result.final_video if isinstance(result, CompleteWorkflowResult) else result.video
        try:
            target = write_short_script_text(video, global_script_path(job_settings))
        except Exception as exc:
            log(f"YouTube Short script text not written for {Path(video).name}: {exc}")
            return
        if target is None:
            log(f"YouTube Short {Path(video).name} speaks no script text; no .txt sidecar created.")
            return
        log(f"YouTube Short script text: {target}")

    def _short_script_sections(
        self,
        settings: ExportSettings,
        short_jobs: list[ShortJob],
        aligner: LocalWordAligner | None,
        log: LogCallback,
    ) -> dict[int, Path | str]:
        """Derive the global-script section that each Short's voiceover speaks.

        Multiple voiceovers plus ONE large global script is a single global
        source: the Long-Form uses the complete script across the complete
        timeline, while an individual Short may only caption the part its own
        voiceover actually says. The sections are derived acoustically from ONE
        global script mapping — the same ``align_global`` call (same units, same
        durations, same pause, same script text) that the Long-Form job uses, so
        the alignment cache turns the second request into a hit instead of
        repeating ASR — and never by aligning the complete global script against
        every Short.

        Returns ``{job index: section file}`` for a voiceover with a spoken
        section, ``{job index: NO_SCRIPT_SECTION}`` for a voiceover that speaks
        no part of the script, and omits every job that keeps its previous
        configuration. Omitting all jobs is also the deliberate fallback: the
        multiple individual scripts workflow never reaches this method, and a
        project whose sections cannot be derived behaves exactly as before
        instead of failing the export.
        """
        script_mode = (
            "matched" if str(settings.script_mode).casefold() in {"matched", "individual"} else "single"
        )
        script = global_script_path(settings) if script_mode == "single" else None
        if script is None:
            return {}
        subtitle_mode = normalize_subtitle_output_mode(
            getattr(settings, "subtitle_output_mode", SUBTITLE_OUTPUT_COMBINED)
        )
        if subtitle_mode == SUBTITLE_OUTPUT_WITHOUT:
            # Without Subtitles still renders no alignment, no burn-in, no SRT
            # and no VTT for the video: create_main decides that from the output
            # mode, not from the script assignment. The sections are derived
            # anyway because every Short must ship its own ``.txt`` script
            # sidecar — without them each Short would keep the COMPLETE global
            # script and its text file would claim words this Short never
            # speaks. This is one shared global mapping (the same cached
            # ``align_global`` call a combined Long-Form run makes), never one
            # alignment per Short, and it stays fail-soft below when no
            # alignment engine is available.
            log(
                "Subtitle output mode Without Subtitles: no captions are rendered, but the global "
                "script is mapped once so every Short still receives its own script text file."
            )
        units, _unit_scripts = ordered_voiceover_units(settings)
        # Sections exist per real acoustic unit. A configured-but-missing
        # voiceover is not part of the timeline (create_main drops it as well)
        # and therefore keeps its previous configuration.
        available: list[Path] = [unit for unit in units if unit.is_file()]
        if len(available) < 2:
            # One voiceover speaks the complete script, so its section is the
            # global script itself and there is nothing to derive.
            return {}
        notes: list[str] = []
        try:
            text = read_script(require_asset(script, "Textskript", {".txt", ".text", ".md"}))
            durations = [probe_audio(self.engine.ffprobe_path, unit).duration for unit in available]
            pause = voiceover_pause(settings)
            aligner = aligner or LocalWordAligner(settings.subtitle_model)
            if not hasattr(aligner, "align_global"):
                raise VideoMergerError("the alignment engine maps no global script")
            alignment = aligner.align_global(
                text, list(zip(available, durations)), settings.subtitle_language, pause,
            )
            # Units that share one voiceover file share one section: the same
            # audio can only speak one part of the script, however many output
            # jobs reference it.
            keys = [str(unit) for unit in available]
            sections = split_global_script(
                text, alignment.words, durations, pause, unit_keys=keys,
            )
            positions: dict[str, int] = {}
            for index, key in enumerate(keys):
                positions.setdefault(key, index)
            result: dict[int, Path | str] = {}
            summary: list[str] = []
            for job_index, job in enumerate(short_jobs):
                voice = Path(job.voiceover_path).expanduser().resolve()
                position = positions.get(str(voice))
                if position is None:
                    # Not part of the acoustic timeline: keep its configuration.
                    continue
                section = sections[position].strip()
                if not section:
                    result[job_index] = NO_SCRIPT_SECTION
                    summary.append(f"{job.output_name}=no spoken script section")
                    notes.append(
                        f"YouTube Short {job.output_name}: {voice.name} speaks no part of the global "
                        "script; this Short stays without subtitles instead of showing text it never says."
                    )
                    continue
                result[job_index] = script_section_path(section, f"Short_{job.output_name}")
                summary.append(f"{job.output_name}={len(script_word_spans(section))} word(s)")
            if not result:
                return {}
            for note in notes:
                log(note)
            log(
                f"Global script sections for Shorts ({len(available)} voiceover units, "
                f"{len(script_word_spans(text))} script words, complete script stays with the Long-Form): "
                + ", ".join(summary)
            )
            return result
        except Exception as exc:
            log(
                f"Global script sections for Shorts unavailable ({exc}); "
                "every Short keeps the complete global script."
            )
            return {}

    def create_youtube_exports(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        output_dir: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event=None,
        aligner: LocalWordAligner | None = None,
        video_order_rng=None,
        video_order_seed: int | None = None,
        order_already_applied: bool = False,
        *,
        complete: bool = False,
    ) -> YoutubeExportResult:
        """Render Long-Form and/or one isolated Short per voiceover.

        The selected export mode alone decides which jobs this single action
        creates: Long-Form only, one Short per voiceover, or BOTH from the same
        run. The GUI's Create Main Video button, One-Click and the CLI share
        exactly these semantics, and every job renders exactly once.

        Every Short calls the regular Stage-1/Stage-2 pipeline with a one-item
        acoustic timeline. A shared :class:`ShortsVideoPool` assigns the next
        required raw prefix without replacement; it is reset only after the
        complete source pool is consumed. This deliberately preserves
        intro/outro, Add Image, music, original audio,
        transitions, chunking and the established FFmpeg command builder
        instead of maintaining a second Shorts renderer.
        """
        mode = normalize_export_mode(getattr(settings, "export_mode", EXPORT_MODE_LONG_FORM))
        # Resolve the project order exactly once for the complete generation
        # run. In particular, Random must not be re-seeded/re-shuffled for each
        # Short: the shared pool below consumes this one effective sequence.
        effective_media = list(media)
        if not order_already_applied:
            effective_media = order_media_for_video_order(
                effective_media,
                getattr(settings, "video_order_mode", "natural"),
                rng=video_order_rng,
                seed=video_order_seed,
                legacy_root=getattr(settings, "legacy_input_root", ""),
            )
        _log_legacy_priority(effective_media, settings, log)
        short_video_pool = (
            ShortsVideoPool(effective_media) if mode != EXPORT_MODE_LONG_FORM else None
        )
        if mode == EXPORT_MODE_LONG_FORM:
            jobs = [("long", long_form_settings(settings), output_dir / "LongForm", "YouTube_LongForm")]
        else:
            jobs = []
            if mode == EXPORT_MODE_COMBINED:
                jobs.append(("long", long_form_settings(settings), output_dir / "LongForm", "YouTube_LongForm"))
            short_jobs = build_short_jobs(settings)
            if not short_jobs:
                raise VideoMergerError("YouTube Shorts benötigen mindestens ein Voiceover.")
            # One global script stays one global source: the Long-Form receives
            # the complete text, each Short only the section its own voiceover
            # speaks. Derived once here, before the per-job settings are built.
            script_sections = self._short_script_sections(settings, short_jobs, aligner, log)
            jobs.extend(
                (
                    "short",
                    short_settings(settings, job, script_sections.get(index)),
                    output_dir / "Shorts",
                    job.output_name,
                )
                for index, job in enumerate(short_jobs)
            )

        # When FFprobe is available, reserve each Short's raw prefix before the
        # first render starts. This makes the no-replacement assignment visible
        # at the orchestration boundary as well as inside create_main, while
        # probe_audio's existing cache avoids doing duplicate media analysis.
        # If a test/extension engine cannot probe here, create_main consumes the
        # same shared pool lazily after its normal audio validation.
        planned_short_media: dict[str, list[MediaInfo]] = {}
        runtime_short_pool = short_video_pool
        short_entries = [entry for entry in jobs if entry[0] == "short"]
        if short_entries and effective_media and hasattr(self.engine, "ffprobe_path"):
            planning_pool = ShortsVideoPool(effective_media)
            try:
                planning_fps, _planning_fps_expr = choose_fps(
                    effective_media, short_entries[0][1].fps_choice
                )
                planning_rate = duration_before_merge_value(short_entries[0][1])
                planning_fit_mode = (
                    short_entries[0][1].duration_fit_mode
                    if short_entries[0][1].duration_fit_mode in {"cut", "stretch"}
                    else "cut"
                )
                planning_stretch = max(
                    1.0,
                    min(50.0, float(getattr(short_entries[0][1], "max_stretch_percent", 10.0) or 10.0)),
                )
                for _kind, short_job_settings, _job_dir, short_stem in short_entries:
                    voice_path = Path(short_job_settings.voiceover_path).expanduser().resolve()
                    voice_asset = probe_audio(self.engine.ffprobe_path, voice_path)
                    planned_short_media[short_stem] = planning_pool.take_for_duration(
                        # Reserve intro + spoken timeline + outro for this Short,
                        # so the without-replacement pool never hands the next
                        # Short material that this one still needs.
                        main_timeline(short_job_settings, voice_asset.duration).target,
                        short_job_settings.transition_duration,
                        planning_fps,
                        short_job_settings.short_video_mode,
                        duration_fit_mode=planning_fit_mode,
                        max_stretch_percent=planning_stretch,
                        playback_rate=planning_rate,
                    )
                runtime_short_pool = None
                log(
                    "Shorts without-replacement pool planned before rendering: "
                    + ", ".join(f"{stem}={len(value)} clip(s)" for stem, value in planned_short_media.items())
                )
            except Exception as exc:
                # Preserve the normal create_main validation/error path. A
                # failed preflight must not partially consume the live pool.
                planned_short_media.clear()
                log(f"Shorts pool preflight deferred to Stage 1: {exc}")

        output = YoutubeExportResult(mode)
        total = len(jobs)
        for job_index, (kind, job_settings, job_dir, stem) in enumerate(jobs):
            if cancel_event is not None and cancel_event.is_set():
                raise VideoMergerError("YouTube export wurde abgebrochen.")
            job_dir.mkdir(parents=True, exist_ok=True)
            log(
                ("YouTube Long-Form" if kind == "long" else f"YouTube Short {stem}")
                + f" – independent job {job_index + 1}/{total}; output={job_dir / (stem + '.mp4')}"
            )

            def job_progress(event: ProgressEvent, *, offset=job_index) -> None:
                progress(ProgressEvent(
                    percent=(offset + max(0.0, min(100.0, event.percent) / 100.0)) * 100.0 / total,
                    out_time=event.out_time, total_time=event.total_time,
                    elapsed=event.elapsed, remaining=event.remaining,
                    stage=f"YouTube {kind.title()} {offset + 1}/{total} – {event.stage}",
                    current_file=event.current_file,
                ))

            if kind == "short":
                reserved = planned_short_media.get(stem)
                if reserved is not None:
                    job_media, job_pool = reserved, None
                else:
                    # Defensive only, and unreachable while the preflight above
                    # reserves every Short: an unreserved Short must consume the
                    # shared without-replacement cursor. Handing it the complete
                    # pool with no cursor would give every Short the same leading
                    # clips and silently break the no-replacement contract.
                    job_media = effective_media
                    job_pool = runtime_short_pool if runtime_short_pool is not None else short_video_pool
            else:
                job_media, job_pool = effective_media, None
            if complete:
                result = self._create_complete_single(
                    job_media, job_settings, job_dir,
                    progress=job_progress, log=log, cancel_event=cancel_event,
                    aligner=aligner, video_order_rng=video_order_rng,
                    video_order_seed=video_order_seed,
                    # ``effective_media`` is already the single project order;
                    # only the shared Shorts pool changes the per-job subset.
                    order_already_applied=True,
                    output_stem=stem,
                    short_video_pool=job_pool,
                )
            else:
                result = self.create_main(
                    job_media, job_settings, job_dir,
                    progress=job_progress, log=log, cancel_event=cancel_event,
                    aligner=aligner, reuse_cached=True,
                    video_order_rng=video_order_rng, video_order_seed=video_order_seed,
                    order_already_applied=True, output_stem=stem,
                    short_video_pool=job_pool,
                )
            if complete:
                final_stem = result.final_video.stem if isinstance(result, CompleteWorkflowResult) else stem
                self._publish_youtube_sidecars(result, job_dir, final_stem)
            if kind == "short":
                # Automatic, always: one script text file per Short, named like
                # the video that was just produced.
                self._publish_short_script_text(result, job_settings, log)
            if kind == "long":
                output.long_form = result
            else:
                output.shorts.append(result)
        progress(ProgressEvent(100.0, 0.0, 0.0, 0.0, 0.0, "YouTube Export – Complete", str(output.primary_output)))
        return output

    def _create_complete_single(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        output_dir: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event=None,
        aligner: LocalWordAligner | None = None,
        video_order_rng=None,
        video_order_seed: int | None = None,
        order_already_applied: bool = False,
        output_stem: str | None = None,
        short_video_pool: ShortsVideoPool | None = None,
    ) -> CompleteWorkflowResult:
        """Execute actual Stage 1, then hand its exact MP4 to existing Stage 2.

        1.3.0: the primary output of the one-click workflow is always the
        FINAL video. When subtitles were generated, a second final variant
        WITHOUT burned-in subtitles is composed from the clean Main Video,
        and the YouTube metadata file is created from the authoritative
        voiceover transcript.
        Eine aktivierte Add-Image-Datei ist ein gültiger Grund für Stage 2,
        auch ohne Intro und ohne Outro.
        """
        _validate_image_settings(settings)
        image_active = _image_is_active(settings)
        if not optional_path(settings.intro_path) and not optional_path(settings.outro_path) and not image_active:
            raise VideoMergerError("One-Click benötigt eine zugewiesene Intro-, Image- und/oder Outro-Datei.")

        # Same subtitle-expectation rule as create_main: voiceover + script
        # always produce subtitles (a checked box alone does not when no
        # voiceover exists). Decides whether a third (no-subtitle) pass runs.
        unit_probe = list(settings.voiceover_paths) or (
            [settings.voiceover_path] if settings.voiceover_path.strip() else []
        )
        script_probe = list(settings.script_paths) or (
            [settings.script_path] if settings.script_path.strip() else []
        )
        if str(settings.script_mode).casefold() not in {"matched", "individual"}:
            script_probe = [str(global_script_path(settings))] if global_script_path(settings) else []
        subtitle_mode = normalize_subtitle_output_mode(
            getattr(settings, "subtitle_output_mode", SUBTITLE_OUTPUT_COMBINED)
        )
        if (
            subtitle_mode == SUBTITLE_OUTPUT_WITH
            and getattr(settings, "subtitle_output_mode_was_defaulted", False)
        ):
            subtitle_mode = normalize_subtitle_output_mode("burned_and_sidecars")
            log("Legacy direct API default retained: dual subtitle bundle.")
        subtitle_expected = subtitle_render_requested(
            subtitle_mode, bool(settings.subtitle_enabled or (unit_probe and script_probe))
        )
        # Combined mode has a user-facing clean variant and therefore a third
        # progress lane for its second Stage-2 composition. Burned Only and
        # Without Subtitles each need exactly one Stage-2 pass.
        parts = 3 if subtitle_expected and subtitle_clean_variant_requested(subtitle_mode) else 2

        def stage_progress(part: int, parts: int, event: ProgressEvent) -> None:
            span = 100.0 / parts
            base = (part - 1) * span
            progress(ProgressEvent(
                percent=base + max(0.0, min(100.0, event.percent)) * span / 100.0,
                out_time=event.out_time, total_time=event.total_time,
                elapsed=event.elapsed, remaining=event.remaining,
                stage=f"One-Click {part}/{parts} – {event.stage}", current_file=event.current_file,
            ))

        log("ONE-CLICK COMPLETE WORKFLOW – START")
        # In a multi-output run the Stage-1 master is an internal per-job
        # artifact. The final user-facing output remains exactly LongForm/
        # YouTube_LongForm.mp4 or Shorts/001.mp4.
        stage1_dir = (
            output_dir
            if output_stem is None
            else project_root() / "temp" / "youtube_stage1" / output_stem
        )
        stage1_dir.mkdir(parents=True, exist_ok=True)
        main = self.create_main(
            media, settings, stage1_dir,
            progress=lambda event: stage_progress(1, parts, event), log=log,
            cancel_event=cancel_event, aligner=aligner, reuse_cached=True,
            video_order_rng=video_order_rng, video_order_seed=video_order_seed,
            order_already_applied=order_already_applied,
            output_stem=output_stem,
            short_video_pool=short_video_pool,
        )
        if not main.video.is_file() or not main.report.ok:
            raise VideoMergerError("One-Click Stage 1 lieferte keine validierte MainVideo-Datei.")
        actual_main = main.video.resolve()
        log(f"actual MainVideo input = {actual_main}")
        stage2_settings = replace(settings, main_video_path=str(actual_main))
        log(f"Actual Stage 1 input used by Stage 2: {actual_main}")
        # Reserve a paired final bundle only when With and Without Subtitles
        # was selected. With Subtitles and Without Subtitles produce one final
        # video and never leave a misleading *_no_subtitles.mp4 sibling.
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle_stem = output_stem or f"FinalVideo_{_aspect_token(settings.aspect)}"
        if subtitle_expected and subtitle_clean_variant_requested(subtitle_mode):
            final_primary, final_clean, metadata_path = _available_dual_video_bundle(
                output_dir, bundle_stem
            )
        else:
            single_bundle = _available_bundle(
                output_dir, bundle_stem, ("mp4", "YouTube.txt")
            )
            final_primary = single_bundle["mp4"]
            final_clean = output_dir / f".{final_primary.stem}.unused_no_subtitles.mp4"
            metadata_path = single_bundle["YouTube.txt"]
        final_video, final_report = self.add_outro(
            stage2_settings, output_dir,
            progress=lambda event: stage_progress(2, parts, event), log=log,
            cancel_event=cancel_event, output_path=final_primary,
        )
        if not final_video.is_file() or not final_report.ok:
            raise VideoMergerError("One-Click Stage 2 lieferte keine validierte FinalVideo-Datei.")
        final_clean_video: Path | None = None
        if main.video_no_subtitles is not None:
            # Second Stage-2 pass with the CLEAN main video → FinalVideo
            # without burned-in subtitles (subtitles remain available as the
            # SRT/VTT sidecar files).
            clean_settings = replace(stage2_settings, main_video_path=str(main.video_no_subtitles.resolve()))
            log(f"Clean-variant Stage 2 input: {clean_settings.main_video_path}")
            final_clean_video, clean_report = self.add_outro(
                clean_settings, output_dir,
                progress=lambda event: stage_progress(3, parts, event), log=log,
                cancel_event=cancel_event, output_path=final_clean,
            )
            if not final_clean_video.is_file() or not clean_report.ok:
                raise VideoMergerError("One-Click Stage 2 (no-subtitle variant) lieferte keine validierte FinalVideo-Datei.")
            log("Dual final output: " + final_video.name + " (burned subtitles, primary) + " + final_clean_video.name)

        # 1.3.0 Automatic YouTube title + description from the authoritative
        # voiceover transcript/script. Local + free + unlimited; a metadata
        # problem NEVER blocks the video — it is reported clearly instead.
        youtube_metadata_path: Path | None = None
        try:
            transcript = ""
            if main.canonical_timeline is not None and main.canonical_timeline.is_file():
                payload = json.loads(main.canonical_timeline.read_text(encoding="utf-8"))
                transcript = str(payload.get("authoritative_script", ""))
            if transcript.strip():
                youtube_metadata_path = generate_youtube_metadata_file(
                    transcript, metadata_path,
                    language_preference=settings.subtitle_language, log=log,
                )
            else:
                log(
                    "YouTube metadata: kein autoritatives Voiceover-Transkript vorhanden – "
                    "es werden keine Metadaten erfunden und keine Datei geschrieben."
                )
        except Exception as exc:
            youtube_metadata_path = None
            log(f"YOUTUBE METADATA GENERATION FAILED: {exc} – das fertige Video ist davon unberührt.")

        progress(ProgressEvent(100.0, final_report.duration, final_report.duration, 0.0, 0.0, "One-Click – Complete", final_video.name))
        log("ONE-CLICK COMPLETE WORKFLOW – PASS")
        return CompleteWorkflowResult(
            main, final_video, final_report,
            final_video_no_subtitles=final_clean_video,
            youtube_metadata=youtube_metadata_path,
        )

    def add_outro(
        self,
        settings: ExportSettings,
        output_dir: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event=None,
        output_path: Path | None = None,
    ) -> tuple[Path, ValidationReport]:
        """Stage 2: compose Intro → optional Add Image → MainVideo → optional Add Image → Outro.

        The Intro and the Outro are independent media. Neither receives the
        main application voiceover, the generated main background music or the
        subtitle track; each keeps only its own original audio according to its
        independent Mute/Low/Original setting. The selected transition and
        duration apply between every adjacent section.
        """
        main_path = require_asset(optional_path(settings.main_video_path), "MainVideo", {".mp4", ".mov", ".mkv"})
        intro_path = optional_path(settings.intro_path)
        outro_path = optional_path(settings.outro_path)
        if intro_path:
            require_asset(intro_path, "Intro", {".mp4", ".mov", ".mkv", ".m4v"})
        if outro_path:
            require_asset(outro_path, "Outro", {".mp4", ".mov", ".mkv", ".m4v"})
        _validate_image_settings(settings)
        image_active = _image_is_active(settings)
        if not intro_path and not outro_path:
            if not image_active:
                raise VideoMergerError("Stage 2 benötigt mindestens ein Intro-, Image- oder Outro-Video.")
        ordered_paths = [path for path in (intro_path, main_path, outro_path) if path]
        media = list(self.engine.analyze(ordered_paths, log))
        stage2_resolution = settings.resolution

        # Add Image (legacy name: Image Insertion) has its own persisted
        # framing/look controls. Insert exactly one occurrence at the requested
        # semantic boundary. The image is never sent through the Stage-1 media
        # list.
        image_position: int | None = None
        if image_active:
            if str(settings.resolution or "").casefold() == "auto":
                image_target_width, image_target_height = _stage2_image_target(settings, media)
                stage2_resolution = f"{image_target_width}x{image_target_height}"
            image_path = image_insertion_path(settings.image_path)
            image_width, image_height = _image_dimensions(self.engine, image_path)
            image_duration = clamp_image_duration(settings.image_duration)
            # Resolve the actual MainVideo reference by role rather than by a
            # positional index; the reference supplies only cadence metadata.
            image_reference = next(
                item for item in media
                if item.path == main_path
                and not item.is_image_insertion
            )
            image_item = MediaInfo(
                path=image_path,
                duration=image_duration,
                width=image_width,
                height=image_height,
                fps=image_reference.fps,
                effective_width=image_width,
                effective_height=image_height,
                fps_fraction=image_reference.fps_fraction,
                video_codec="image",
                pixel_format="yuv420p",
                sar="1:1",
                dar="",
                source_duration=image_duration,
                is_image_insertion=True,
                image_fit_mode=normalize_image_fit_mode(settings.image_fit_mode),
                image_zoom=clamp_image_zoom(settings.image_zoom),
                image_filter=normalize_image_filter(settings.image_filter),
                image_transition_type=normalize_image_transition(
                    getattr(settings, "image_transition_type", "cross_dissolve")
                ),
            )
            main_index = next(
                index for index, item in enumerate(media)
                if item.path == main_path
                and not item.is_image_insertion
            )
            if _image_position(settings) == "before_main":
                # Before Main is semantic, rather than merely "after Intro":
                # the image stays immediately before the Main Video.
                image_position = main_index
            else:
                # After Main is likewise immediately after Main, before an
                # optional Outro. With no Outro this is the final section.
                image_position = main_index + 1
            media.insert(image_position, image_item)
            log(
                f"Add Image aktiv (legacy Image Insertion): {image_path.name}, Position "
                f"{_image_position(settings)}, Dauer {image_duration:.3f} s, "
                f"Transition {image_item.image_transition_type}, "
                f"Fit {image_item.image_fit_mode}, Zoom {image_item.image_zoom} %, "
                f"Filter {image_item.image_filter}; Audio: stumm"
            )

        # Per-section original-audio gain and role in composition order. Add
        # Image is an explicit mute slot; no application voiceover, music, or
        # original audio can attach to the still image.
        audio_modes: list[str] = []
        stage2_roles: list[str] = []
        for item in media:
            if item.is_image_insertion:
                stage2_roles.append("image")
                audio_modes.append("mute")
            elif intro_path and item.path == intro_path and "intro" not in stage2_roles:
                stage2_roles.append("intro")
                audio_modes.append(settings.intro_audio_mode)
            elif outro_path and item.path == outro_path and "outro" not in stage2_roles:
                stage2_roles.append("outro")
                audio_modes.append(settings.outro_audio_mode)
            else:
                stage2_roles.append("main")
                audio_modes.append("original")

        outro_settings = replace(
            settings,
            workflow_stage="outro",
            resolution=stage2_resolution,
            subtitle_enabled=False,
            subtitle_ass_path="",
            subtitle_fonts_dir="",
            voiceover_paths=[],
            voiceover_path="",
            script_paths=[],
            script_path="",
            music_path="",
            original_audio_mode="original",
            stage2_audio_modes=audio_modes,
            stage2_roles=stage2_roles,
            transition_duration=(
                settings.transition_duration if settings.outro_transition_enabled else 0.0
            ),
        )
        resolved = self.engine.make_plan(media, outro_settings, log)
        # The existing "Use transition into Outro" switch remains
        # authoritative for the section chain.
        if image_position is not None:
            # Keep the selected transition family, but let the image have
            # its own persisted duration request. Clamp at each boundary
            # so short sections never create duplicate/overlapping
            # dissolves, hard cuts, black frames, or timeline gaps.
            try:
                image_transition = max(
                    0.0, min(5.0, float(getattr(settings, "image_transition_duration", 1.0)))
                )
            except (TypeError, ValueError):
                image_transition = 1.0
            for boundary in (image_position - 1, image_position):
                if 0 <= boundary < len(resolved.transitions):
                    left = resolved.effective_durations[boundary]
                    right = resolved.effective_durations[boundary + 1]
                    if image_transition <= 0.0:
                        resolved.transitions[boundary] = 0.0
                    else:
                        resolved.transitions[boundary] = max(
                            0.01, min(image_transition, left * 0.45, right * 0.45)
                        )
            resolved.expected_duration = max(
                0.0, sum(resolved.effective_durations) - sum(resolved.transitions)
            )
        stage2_digest, _stage2_payload = stage2_fingerprint(media, settings, resolved)
        log(f"Stage 2 composition fingerprint: {stage2_digest}")
        if output_path is not None:
            output = Path(output_path).expanduser().resolve()
        else:
            output = _available_bundle(
                output_dir, f"FinalVideo_{_aspect_token(settings.aspect)}", ("mp4",)
            )["mp4"]
        if intro_path:
            log("Stage 2 – Add Intro / Main / Outro")
            log(
                f"Intro: {media[0].duration:.3f} s; audio available: "
                f"{'yes' if media[0].audio.present else 'no'}"
            )
        else:
            log("Stage 2 – Add Outro")
        log(
            f"Outro: {media[-1].duration:.3f} s; audio available: "
            f"{'yes' if media[-1].audio.present else 'no'}"
        )
        log("Intro/Outro receive no application voiceover, no background music, and no subtitles.")
        log(f"Intro original audio mode: {settings.intro_audio_mode}")
        log(f"Outro original audio mode: {settings.outro_audio_mode}")
        report = self.engine.export(
            media, outro_settings, resolved, output,
            progress=progress, log=log, cancel_event=cancel_event,
        )
        return output, report
