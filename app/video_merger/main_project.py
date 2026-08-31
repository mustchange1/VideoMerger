from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from .alignment import LocalWordAligner
from .engine import VideoMergerEngine
from .errors import VideoMergerError
from .models import (
    AlignmentResult, CompleteWorkflowResult, ExportSettings, LogCallback, MainVideoResult,
    MediaInfo, ProgressCallback, ProgressEvent, ValidationReport, WordTiming,
)
from .font_manager import bundled_fonts_dir
from .paths import project_root
from .project_assets import (
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, optional_path, probe_audio, read_script, require_asset,
)
from .quote_artwork import (
    cleanup_prepared_quote_artwork, prepare_quote_artwork, quote_artwork_path,
)
from .render_cache import Stage1RenderCache, load_cached_alignment, stage1_fingerprint
from .subtitle_verification import create_visual_verification_frames
from .subtitles import (
    build_cues, validate_subtitle_file, write_ass, write_canonical_timeline, write_srt, write_vtt,
)
from .target import choose_fps, parse_resolution, resolve_export
from .timeline import fit_media_to_duration
from .validation import validate_output
from .youtube_metadata import generate_youtube_metadata_file
from .voiceover_order import (
    normalize_voiceover_order_mode,
    order_voiceover_paths,
    voiceover_order_indices,
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
    for original_index, unit in zip(indices, units):
        matched = by_stem.get(unit.stem.casefold())
        if matched is not None:
            ordered_scripts.append(matched)
        elif original_index < len(raw_scripts):
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


def _quote_is_active(settings: ExportSettings) -> bool:
    """Return whether the optional artwork section has been requested."""
    return bool(
        settings.quote_enabled
        and (getattr(settings, "quote_artwork_path", "") or "").strip()
    )


def _validate_quote_artwork_settings(settings: ExportSettings) -> None:
    """Validate the source path before any Stage-1 work begins.

    This keeps an invalid Stage-2-only choice from needlessly invalidating or
    rerendering the Main Video cache. PDF parsing/rasterization remains lazy
    until Stage 2, when the selected page is actually needed.
    """
    if not settings.quote_enabled:
        return
    value = (getattr(settings, "quote_artwork_path", "") or "").strip()
    if not value:
        raise VideoMergerError(
            "Include Quote / Flyer ist aktiviert, aber keine Artwork-Datei ausgewählt."
        )
    quote_artwork_path(value)


def _quote_fit_mode(settings: ExportSettings) -> str:
    value = str(getattr(settings, "quote_artwork_fit_mode", "fit") or "fit").strip().casefold()
    return value if value in {"fit", "fill", "crop"} else "fit"


def _quote_artwork_target(
    settings: ExportSettings, reference: MediaInfo | list[MediaInfo]
) -> tuple[int, int]:
    """Choose the actual output dimensions for PDF rasterization.

    The final target is still resolved by :mod:`target`; these dimensions only
    determine how much detail a vector PDF receives before FFmpeg fits it. In
    particular, Auto must honor a portrait project even when the Main Video is
    landscape (and vice versa), rather than using the source video's shape as
    the raster target.  When the complete Stage-2 sequence is available, use
    it so a 4K Intro or Outro also promotes an Auto project to a 4K raster.
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
        hard_breaks=hard_breaks,
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
        if bool(record.get("subtitle_requested")) != bool(subtitle_requested):
            log("Stage 1 cache MISS: subtitle mode differs from the cached render.")
            return None
        self.render_cache.restore_sidecars(record)
        artifacts = self.render_cache.artifact_paths(record)
        video = artifacts.get("video")
        clean_video = artifacts.get("video_no_subtitles")
        if video is None or not video.is_file() or video.stat().st_size <= 0:
            log("Stage 1 cache MISS: cached Main Video is missing or empty.")
            return None
        if subtitle_requested and (
            clean_video is None or not clean_video.is_file() or clean_video.stat().st_size <= 0
        ):
            log("Stage 1 cache MISS: cached clean Main Video is missing or empty.")
            return None
        if subtitle_requested and any(
            artifacts.get(key) is None
            or not artifacts[key].is_file()
            or artifacts[key].stat().st_size <= 0
            for key in ("srt", "vtt", "canonical_timeline")
        ):
            log("Stage 1 cache MISS: required subtitle sidecars could not be restored.")
            return None
        if subtitle_requested:
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
        if subtitle_requested:
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
            srt=artifacts.get("srt") if subtitle_requested else None,
            vtt=artifacts.get("vtt") if subtitle_requested else None,
            report=report,
            alignment=alignment,
            warnings=[],
            canonical_timeline=timeline if subtitle_requested else None,
            verification_frames=[],
            timings=timings,
            video_no_subtitles=clean_video if subtitle_requested else None,
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
    ) -> MainVideoResult:
        total_started = time.perf_counter()
        timings: dict[str, float | str | bool] = {}
        if not media:
            raise VideoMergerError("Stage 1 benötigt mindestens einen Videoclip.")
        units, unit_scripts = ordered_voiceover_units(settings)
        music_path = optional_path(settings.music_path)
        watermark_path = optional_path(settings.watermark_path)
        script_mode = "matched" if str(settings.script_mode).casefold() in {"matched", "individual"} else "single"

        # A supplied script is an explicit subtitle request. When voiceover and
        # script are assigned, subtitles are automatically enabled so the real
        # user workflow cannot silently produce a captionless video merely
        # because a checkbox remained unchecked.
        global_script = global_script_path(settings) if script_mode == "single" else None
        subtitle_requested = bool(settings.subtitle_enabled or global_script or unit_scripts)
        if subtitle_requested and not units:
            raise _subtitle_failure(
                "input validation",
                "Voiceover audio and the authoritative script.txt are both required.",
            )
        if subtitle_requested and script_mode == "matched":
            # Every spoken unit must have its own script; never fall back to a
            # captionless render or to an accidentally mismatched global text.
            for index, unit in enumerate(units):
                if index >= len(unit_scripts) or unit_scripts[index] is None:
                    raise _subtitle_failure(
                        "script matching",
                        f"Missing script for voiceover:\n{unit.name}",
                    )
        if subtitle_requested and script_mode == "single" and not global_script:
            raise _subtitle_failure(
                "script matching",
                "Single Global Script mode requires one script for the complete voiceover timeline.",
            )
        script_files: list[Path] = []
        if subtitle_requested:
            candidates = [global_script] if script_mode == "single" else unit_scripts
            try:
                script_files = [
                    require_asset(path, "Textskript", {".txt", ".text", ".md"})
                    for path in candidates if path is not None
                ]
            except Exception as exc:
                raise _subtitle_failure("script matching", exc) from exc
        if units and (global_script or unit_scripts) and not settings.subtitle_enabled:
            subtitle_requested = True
            log("Subtitles auto-enabled: Voiceover + Script are assigned; SRT, VTT and burn-in are mandatory.")

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
        video_speed = max(0.5, min(2.0, float(getattr(settings, "video_speed", 1.0) or 1.0)))
        if abs(video_speed - 1.0) > 1e-6:
            log(f"Global Video Speed: {video_speed:.2f}x – Voiceover, Untertitel und Musik bleiben unverändert.")
        duration_fit_mode = settings.duration_fit_mode if settings.duration_fit_mode in {"cut", "stretch"} else "cut"
        max_stretch = max(1.0, min(50.0, float(getattr(settings, "max_stretch_percent", 10.0) or 10.0)))
        if voice_assets:
            target = voice_total + max(0.0, settings.final_pause)
            render_media, timing_warnings = fit_media_to_duration(
                media, target, settings.transition_duration, fps, settings.short_video_mode,
                duration_fit_mode=duration_fit_mode,
                max_stretch_percent=max_stretch,
                playback_rate=video_speed,
            )
            warnings.extend(timing_warnings)
            program_duration = voice_total
        else:
            target = 0.0
            program_duration = 0.0

        render_settings = replace(
            settings,
            workflow_stage="main",
            program_duration=program_duration,
            timeline_target_duration=target,
            subtitle_enabled=subtitle_requested,
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

        stage1_digest, stage1_payload = stage1_fingerprint(
            render_media,
            settings,
            resolved,
            voice_assets=voice_assets,
            script_files=script_files,
            subtitle_requested=subtitle_requested,
            music_asset=music,
            watermark_path=watermark_path,
        )
        if reuse_cached:
            cached_result = self._try_reuse_cached_main(
                stage1_digest,
                resolved,
                subtitle_requested,
                progress,
                log,
                cancel_event,
            )
            if cached_result is not None:
                return cached_result

        # 1.3.0 Clean Output Directory: the user-facing folder receives only
        # useful artifacts (MainVideo.mp4, MainVideo_no_subtitles.mp4 when
        # subtitles exist, SRT, VTT). Internal evidence (verification PNGs,
        # canonical timeline JSON, staged ASS) lives under temp/ and never
        # clutters the Output folder.
        output_dir.mkdir(parents=True, exist_ok=True)
        base_stem = f"MainVideo_{_aspect_token(settings.aspect)}"
        name_index = 1
        while True:
            actual = base_stem if name_index == 1 else f"{base_stem}_{name_index}"
            output_video = output_dir / f"{actual}.mp4"                    # primary (burned subtitles)
            output_video_clean = output_dir / f"{actual}_no_subtitles.mp4"  # additional clean variant
            srt_candidate = output_dir / f"{actual}.srt"
            vtt_candidate = output_dir / f"{actual}.vtt"
            reserved: list[Path] = [output_video, output_video_clean]
            if subtitle_requested:
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
        temp_dir = project_root() / "temp"
        if subtitle_requested:
            temp_dir.mkdir(parents=True, exist_ok=True)
            srt_path, vtt_path = srt_candidate, vtt_candidate
            timeline_path = temp_dir / f"{output_video.stem}.subtitle_timeline.json"

        log("Stage 1 – Create Main Video")
        log("Aktive Clip-Reihenfolge: " + " → ".join(item.path.name for item in render_media))
        if settings.short_video_mode == "loop" and len(render_media) > len(media):
            log("Full-Timeline Loop sequence: " + " → ".join(item.path.name for item in render_media))
        elif settings.short_video_mode == "hold":
            log("Short-video mode: Hold Last Frame (separate from Full-Timeline Loop).")
        log(f"Videomaterial: {sum(item.source_duration or item.duration for item in render_media):.3f} s")
        if voice_assets:
            if len(voice_assets) == 1:
                log(
                    f"Voiceover: {voice_total:.3f} s, {voice.sample_rate} Hz, {voice.channels} Kanal/Kanäle; "
                    f"Ziel Main Video: {resolved.expected_duration:.3f} s inkl. {settings.final_pause:.1f} s End-Padding"
                )
            else:
                log(
                    "Voiceover: " + " → ".join(asset.path.name for asset in voice_assets)
                    + f" (gesamte Sprech-Timeline {voice_total:.3f} s, "
                    f"{inter_voiceover_pause:.2f} s Pause zwischen Einheiten); "
                    f"Ziel Main Video: {resolved.expected_duration:.3f} s inkl. {settings.final_pause:.1f} s End-Padding"
                )
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
                            script_unit = read_script(require_asset(
                                script_path_unit, "Textskript", {".txt", ".text", ".md"}
                            ))
                            segment = script_unit.strip()
                            if segment:
                                segments.append(segment)
                            try:
                                unit_alignment = aligner.align(
                                    segment, asset.path, settings.subtitle_language
                                )
                            except Exception as exc:
                                raise _subtitle_failure(
                                    "local ASR / word alignment", exc
                                ) from exc
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
                            f"Scripts: {len(segments)} matched Textskripte ("
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
                                combined_alignment = aligner.align_from_recognized(
                                    script, recognized_all,
                                    detected_languages[0] if detected_languages else settings.subtitle_language,
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
                if alignment.compatibility < 0.35:
                    raise _subtitle_failure(
                        "script/voiceover compatibility",
                        "Skript und Voiceover unterscheiden sich zu stark. Bitte Zuordnung korrigieren.",
                    )
                confirmation_needed = (
                    alignment.compatibility < 0.72
                    or (
                        alignment.compatibility < 0.90
                        and any("could not be confidently" in warning for warning in alignment.warnings)
                    )
                )
                if confirmation_needed and not settings.allow_alignment_warnings:
                    raise _subtitle_failure(
                        "alignment confidence",
                        "Some words could not be confidently aligned. Prüfen Sie Voiceover/Script oder "
                        "aktivieren Sie 'Continue After Alignment Warning' bewusst.",
                    )
                if alignment.words[-1].start >= voice_total:
                    raise _subtitle_failure(
                        "word timeline validation",
                        "Der letzte Wortbeginn liegt außerhalb der Voiceover-Timeline.",
                    )

                subtitle_creation_started = time.perf_counter()
                try:
                    cues = build_cues(
                        combined_script, alignment, settings.subtitle_style, program_end=voice_total,
                        width=resolved.width, height=resolved.height, font_key=settings.subtitle_font,
                    )
                    srt_path, vtt_path = srt_candidate, vtt_candidate
                    timeline_path = temp_dir / f"{output_video.stem}.subtitle_timeline.json"
                    write_srt(cues, srt_path)
                    write_vtt(cues, vtt_path)
                    write_canonical_timeline(combined_script, alignment, cues, timeline_path)
                    validate_subtitle_file(srt_path, "srt")
                    validate_subtitle_file(vtt_path, "vtt")
                    if cues[-1].end > voice_total + 0.001:
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
                    if not ass_path.is_file() or "Dialogue:" not in ass_path.read_text(encoding="utf-8-sig"):
                        raise VideoMergerError("ASS burn-in track contains no subtitle events.")
                    render_settings = replace(render_settings, subtitle_ass_path=str(ass_path))
                    timings["subtitle_creation_seconds"] = time.perf_counter() - subtitle_creation_started
                except Exception as exc:
                    raise _subtitle_failure("SRT/VTT/ASS timeline creation", exc) from exc

                log(
                    f"Subtitle Alignment: {len(alignment.words)} Wörter, Methode {alignment.method}, "
                    f"Kompatibilität {alignment.compatibility:.1%}, Confidence {alignment.average_confidence:.1%}"
                )
                log(f"SRT/VTT validiert; Untertitel enden bei {cues[-1].end:.3f} s vor der Quiet Pause.")

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
                    verification_frames = create_visual_verification_frames(
                        self.engine.ffmpeg_path, output_video, alignment, frame_paths,
                    )
                    required = [srt_path, vtt_path, timeline_path, *verification_frames]
                    if not all(path and path.is_file() and path.stat().st_size > 0 for path in required):
                        raise VideoMergerError("Mindestens ein Subtitle-Ausgabeartefakt fehlt.")
                    log(
                        "Subtitle Generation: PASS · Word-Level Alignment: PASS · SRT: PASS · VTT: PASS · "
                        "Burned-In Subtitles: PASS"
                    )
                    log(
                        "Visual verification frames (decoded from final MP4, internal evidence): "
                        + ", ".join(path.name for path in verification_frames)
                    )
                    log(
                        "Dual subtitle output: " + output_video.name + " (burned, primary) + "
                        + output_video_clean.name + " (no subtitles)"
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
                    video_no_subtitles=output_video_clean if subtitle_requested else None,
                    srt=srt_path,
                    vtt=vtt_path,
                    canonical_timeline=timeline_path,
                    subtitle_requested=subtitle_requested,
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
                video_no_subtitles=output_video_clean if subtitle_requested else None,
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
            if ass_path and output_video.exists():
                ass_path.unlink(missing_ok=True)

    def create_complete(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        output_dir: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event=None,
        aligner: LocalWordAligner | None = None,
    ) -> CompleteWorkflowResult:
        """Execute actual Stage 1, then hand its exact MP4 to existing Stage 2.

        1.3.0: the primary output of the one-click workflow is always the
        FINAL video. When subtitles were generated, a second final variant
        WITHOUT burned-in subtitles is composed from the clean Main Video,
        and the YouTube metadata file is created from the authoritative
        voiceover transcript.
        Eine aktivierte Quote-/Flyer-Datei ist ein gültiger Grund für Stage 2,
        auch ohne Intro und ohne Outro.
        """
        _validate_quote_artwork_settings(settings)
        quote_active = _quote_is_active(settings)
        if not optional_path(settings.intro_path) and not optional_path(settings.outro_path) and not quote_active:
            raise VideoMergerError("One-Click benötigt eine zugewiesene Intro- und/oder Outro-Datei.")

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
        subtitle_expected = bool(settings.subtitle_enabled or (unit_probe and script_probe))
        parts = 3 if subtitle_expected else 2

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
        main = self.create_main(
            media, settings, output_dir,
            progress=lambda event: stage_progress(1, parts, event), log=log,
            cancel_event=cancel_event, aligner=aligner, reuse_cached=True,
        )
        if not main.video.is_file() or not main.report.ok:
            raise VideoMergerError("One-Click Stage 1 lieferte keine validierte MainVideo-Datei.")
        actual_main = main.video.resolve()
        log(f"actual MainVideo input = {actual_main}")
        stage2_settings = replace(settings, main_video_path=str(actual_main))
        log(f"Actual Stage 1 input used by Stage 2: {actual_main}")
        # 1.3.0: reserve BOTH final names up front so the subtitled primary
        # and the no-subtitles variant belong to the same bundle index.
        output_dir.mkdir(parents=True, exist_ok=True)
        final_primary, final_clean, metadata_path = _available_dual_video_bundle(
            output_dir, f"FinalVideo_{_aspect_token(settings.aspect)}"
        )
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
        """Stage 2: compose Intro (optional) → MainVideo → Outro (optional).

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
        _validate_quote_artwork_settings(settings)
        quote_artwork_value = (getattr(settings, "quote_artwork_path", "") or "").strip()
        quote_active = _quote_is_active(settings)
        if not intro_path and not outro_path:
            if not quote_active:
                raise VideoMergerError("Stage 2 benötigt mindestens ein Intro- oder Outro-Video.")
        ordered_paths = [path for path in (intro_path, main_path, outro_path) if path]
        media = list(self.engine.analyze(ordered_paths, log))
        # PDF pages are rasterized only for this synchronous Stage-2 export.
        # Keep the source PDF untouched and remove the derived PNG in every
        # normal success/failure path after FFmpeg no longer needs it.
        prepared_quote_artwork = None
        stage2_resolution = settings.resolution

        # Quote / Flyer: optional artwork section between Intro and Main Video.
        # The selected image/PDF page uses the same duration and transition
        # pipeline as every other Stage-2 section.
        quote_position: int | None = None
        if settings.quote_enabled:
            quote_duration = float(settings.quote_duration)
            if not (0.5 - 1e-9 <= quote_duration <= 5.0 + 1e-9):
                raise VideoMergerError(
                    f"Ungültige Quote-Dauer: {settings.quote_duration}; erlaubt: 0.5–5.0 s"
                )
            quote_index = 1 if intro_path else 0
            # The main video is the stable quality reference, regardless of an
            # optional Intro. It is already in ``media`` at index 1/0.
            main_reference = media[1 if intro_path else 0]
            reference = main_reference
            # Uploaded PNG/JPG/JPEG/WEBP or one selected PDF page becomes
            # a real, silent Stage-2 image input. The source file is never
            # copied into the normal Output folder.
            target_width, target_height = _quote_artwork_target(settings, media)
            # An uploaded poster must be fitted into the project's target;
            # its source pixel count must not unexpectedly promote an Auto
            # video project to a different export resolution.
            if str(settings.resolution or "").casefold() == "auto":
                stage2_resolution = f"{target_width}x{target_height}"

            def image_dimensions(path: Path) -> tuple[int, int]:
                try:
                    data = self.engine.analyzer.probe_raw(path)
                    stream = next(
                        item for item in data.get("streams", [])
                        if item.get("codec_type") == "video"
                    )
                    width = int(stream.get("width") or 0)
                    height = int(stream.get("height") or 0)
                except (StopIteration, TypeError, ValueError, KeyError) as exc:
                    raise VideoMergerError(
                        f"Quote-Artwork konnte nicht analysiert werden: {path.name}"
                    ) from exc
                if width <= 0 or height <= 0:
                    raise VideoMergerError(f"Quote-Artwork hat keine gültige Auflösung: {path.name}")
                return width, height

            prepared = prepare_quote_artwork(
                quote_artwork_value,
                int(getattr(settings, "quote_pdf_page", 1) or 1),
                target_width,
                target_height,
                project_root() / "temp",
                image_dimensions,
            )
            if prepared.pdf_page is not None:
                prepared_quote_artwork = prepared
            quote_item = MediaInfo(
                path=prepared.path,
                duration=quote_duration,
                width=prepared.width,
                height=prepared.height,
                fps=reference.fps,
                effective_width=prepared.width,
                effective_height=prepared.height,
                fps_fraction=reference.fps_fraction,
                video_codec="image",
                pixel_format="yuv420p",
                sar="1:1",
                dar="",
                source_duration=quote_duration,
                is_quote_artwork=True,
                quote_fit_mode=_quote_fit_mode(settings),
            )
            log(
                f"Quote Artwork aktiv: {prepared.source_path.name}"
                + (f", PDF-Seite {prepared.pdf_page}" if prepared.pdf_page else "")
                + f", Fit-Modus {quote_item.quote_fit_mode}; Audio: stumm"
            )
            media.insert(quote_index, quote_item)
            quote_position = quote_index
            log(
                f"Position zwischen {'Intro und MainVideo' if intro_path else '(Start) und MainVideo'}"
            )

        # Per-clip original-audio gain and role in composition order
        # (intro/quote/main/outro). Quote is explicitly silent.
        audio_modes: list[str] = []
        stage2_roles: list[str] = []
        if intro_path:
            audio_modes.append(settings.intro_audio_mode)
            stage2_roles.append("intro")
        if quote_position is not None:
            audio_modes.append("mute")
            stage2_roles.append("quote")
        audio_modes.append("original")  # the generated Main Video keeps its mix
        stage2_roles.append("main")
        if outro_path:
            audio_modes.append(settings.outro_audio_mode)
            stage2_roles.append("outro")

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
            # The Quote/Flyer must join the section chain with the same
            # transition system, so transitions stay active when it is on.
            transition_duration=(
                settings.transition_duration
                if (settings.outro_transition_enabled or quote_position is not None) else 0.0
            ),
        )
        try:
            resolved = self.engine.make_plan(media, outro_settings, log)
            # The existing "Use transition into Outro" switch remains
            # authoritative.  A Quote/Flyer needs transitions on its own two
            # boundaries, but must not silently re-enable Main → Outro.
            if quote_position is not None and outro_path and not settings.outro_transition_enabled:
                resolved.transitions[-1] = 0.0
                log("Übergang zum Outro deaktiviert; Quote-Übergänge bleiben aktiv.")
            if quote_position is not None:
                resolved.expected_duration = max(
                    0.0, sum(resolved.effective_durations) - sum(resolved.transitions)
                )
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
        finally:
            cleanup_prepared_quote_artwork(prepared_quote_artwork)
