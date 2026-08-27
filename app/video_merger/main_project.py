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
from .subtitle_verification import create_visual_verification_frames
from .subtitles import (
    build_cues, validate_subtitle_file, write_ass, write_canonical_timeline, write_srt, write_vtt,
)
from .target import choose_fps
from .timeline import fit_media_to_duration
from .youtube_metadata import generate_youtube_metadata_file


def voiceover_paths(settings: ExportSettings) -> list[Path]:
    """Return the ordered voiceover units for Stage 1.

    The 1.2.3 list is authoritative; the legacy single ``voiceover_path`` is
    accepted for compatibility with CLI calls and persisted older projects.
    """
    raw = list(settings.voiceover_paths)
    if not raw and settings.voiceover_path.strip():
        raw = [settings.voiceover_path]
    return [Path(value).expanduser().resolve() for value in raw if value.strip()]


def script_paths(settings: ExportSettings) -> list[Path]:
    raw = list(settings.script_paths)
    if not raw and settings.script_path.strip():
        raw = [settings.script_path]
    return [Path(value).expanduser().resolve() for value in raw if value.strip()]


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
    cumulative probed durations of the previous units; character offsets point
    into the combined script. The merged timeline is the single authoritative
    timing source for SRT/VTT/burn-in and for the video duration.
    """
    merged: list[WordTiming] = []
    compatibilities: list[float] = []
    confidences: list[float] = []
    warnings: list[str] = []
    methods: list[str] = []
    for alignment, time_offset, char_offset in parts:
        merged.extend(_offset_words(alignment.words, time_offset, char_offset))
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

    def __init__(self, engine: VideoMergerEngine):
        self.engine = engine

    def create_main(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        output_dir: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event=None,
        aligner: LocalWordAligner | None = None,
    ) -> MainVideoResult:
        total_started = time.perf_counter()
        timings: dict[str, float | str | bool] = {}
        if not media:
            raise VideoMergerError("Stage 1 benötigt mindestens einen Videoclip.")
        units = voiceover_paths(settings)
        unit_scripts = script_paths(settings)
        music_path = optional_path(settings.music_path)
        watermark_path = optional_path(settings.watermark_path)
        script_mode = settings.script_mode if settings.script_mode in {"single", "matched"} else "single"

        # A supplied script is an explicit subtitle request. When voiceover and
        # script are assigned, subtitles are automatically enabled so the real
        # user workflow cannot silently produce a captionless video merely
        # because a checkbox remained unchecked.
        global_script = unit_scripts[0] if (script_mode == "single" and unit_scripts) else None
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
                if index >= len(unit_scripts):
                    raise _subtitle_failure(
                        "script matching",
                        f"Missing script for voiceover:\n{unit.name}",
                    )
        if subtitle_requested and script_mode == "single" and not global_script:
            raise _subtitle_failure(
                "script matching",
                "Single Global Script mode requires one script for the complete voiceover timeline.",
            )
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
        voice_total = sum(asset.duration for asset in voice_assets)

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
                    f"Ziel Main Video: {resolved.expected_duration:.3f} s inkl. {settings.final_pause:.1f} s Pause"
                )
            else:
                log(
                    "Voiceover: " + " → ".join(asset.path.name for asset in voice_assets)
                    + f" (gesamte Sprech-Timeline {voice_total:.3f} s); "
                    f"Ziel Main Video: {resolved.expected_duration:.3f} s inkl. {settings.final_pause:.1f} s Pause"
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
                        for asset, script_path_unit in zip(voice_assets, unit_scripts):
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
                            char_cursor += len(segment) + (2 if segments and segment else 0)
                            time_cursor += asset.duration
                        combined_script = "\n\n".join(segments)
                        combined_alignment = _concatenate_alignment(parts, combined_script, languages)
                        log(
                            f"Scripts: {len(segments)} matched Textskripte ("
                            + ", ".join(asset.path.name for asset in voice_assets)
                            + f"); gesamt {len(script_word_spans(combined_script))} Wörter"
                        )
                    else:
                        # Single global script: transcribe every unit once and
                        # force the authoritative script onto the concatenated
                        # acoustic timeline. Units remain cached individually.
                        script = read_script(require_asset(
                            global_script, "Textskript", {".txt", ".text", ".md"}
                        ))
                        combined_script = script
                        recognized_all: list = []
                        detected_languages: list[str] = []
                        time_cursor = 0.0
                        for asset in voice_assets:
                            try:
                                recognized, detected = aligner.recognize(
                                    asset.path, settings.subtitle_language
                                )
                            except Exception as exc:
                                raise _subtitle_failure(
                                    "local ASR / word alignment", exc
                                ) from exc
                            offset_words = _offset_words(
                                [WordTiming(
                                    text=word.text, start=word.start, end=word.end,
                                    confidence=word.confidence,
                                ) for word in recognized],
                                time_cursor, 0,
                            )
                            recognized_all.extend(offset_words)
                            detected_languages.append(detected)
                            time_cursor += asset.duration
                        try:
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
                            f"{settings.subtitle_language}"
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
            result = MainVideoResult(
                output_video, srt_path, vtt_path, report, alignment, warnings,
                canonical_timeline=timeline_path,
                verification_frames=verification_frames,
                timings=timings,
                video_no_subtitles=output_video_clean if subtitle_requested else None,
            )
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
        1.2.4: Eine aktivierte Quote-Karte (mit Text) ist ein gültiger
        Grund für Stage 2, auch ohne Intro und ohne Outro.
        """
        quote_active = bool(settings.quote_enabled and (settings.quote_text or "").strip())
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
            cancel_event=cancel_event, aligner=aligner,
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
        if not intro_path and not outro_path:
            if not (settings.quote_enabled and (settings.quote_text or "").strip()):
                raise VideoMergerError("Stage 2 benötigt mindestens ein Intro- oder Outro-Video.")
        ordered_paths = [path for path in (intro_path, main_path, outro_path) if path]
        media = list(self.engine.analyze(ordered_paths, log))

        # 1.2.4 Quote Card: optional synthetic section between Intro and
        # MainVideo (Intro → [Quote] → MainVideo → Outro). The card is
        # generated entirely inside the FFmpeg filter graph (color source +
        # vignette + drawtext), is silent, and never enters the subtitle,
        # voiceover or music timeline of the main program.
        quote_position: int | None = None
        if settings.quote_enabled:
            quote_text = (settings.quote_text or "").strip()
            if not quote_text:
                raise VideoMergerError("Die Quote-Karte ist aktiv, aber der Quote-Text ist leer.")
            quote_duration = float(settings.quote_duration)
            if not (0.5 - 1e-9 <= quote_duration <= 5.0 + 1e-9):
                raise VideoMergerError(
                    f"Ungültige Quote-Dauer: {settings.quote_duration}; erlaubt: 0.5–5.0 s"
                )
            quote_index = 1 if intro_path else 0
            reference = media[quote_index]
            # width/height = 0 marks a generated section: no source pixels,
            # resolution-aware layout is applied inside the filter graph.
            quote_item = MediaInfo(
                path=Path("<generated:quote-card>"),
                duration=float(settings.quote_duration),
                width=0,
                height=0,
                fps=reference.fps,
                effective_width=0,
                effective_height=0,
                fps_fraction=reference.fps_fraction,
                video_codec="generated",
                pixel_format="yuv420p",
                sar="1:1",
                dar=reference.dar,
                source_duration=float(settings.quote_duration),
                is_generated_quote=True,
            )
            media.insert(quote_index, quote_item)
            quote_position = quote_index
            log(
                f"Quote Card aktiv: {quote_duration:.1f} s, Stil {settings.quote_style}, "
                f"Font {settings.quote_font}, "
                f"Position zwischen {'Intro und MainVideo' if intro_path else '(Start) und MainVideo'}; Audio: stumm"
            )

        # Per-clip original-audio gain in composition order (intro/quote/main/outro).
        audio_modes: list[str] = []
        if intro_path:
            audio_modes.append(settings.intro_audio_mode)
        if quote_position is not None:
            audio_modes.append("mute")  # the generated Quote Card is silent
        audio_modes.append("original")  # the generated Main Video keeps its mix
        if outro_path:
            audio_modes.append(settings.outro_audio_mode)

        outro_settings = replace(
            settings,
            workflow_stage="outro",
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
            # The Quote Card must join the section chain with the same
            # transition system, so transitions stay active when it is on.
            transition_duration=(
                settings.transition_duration
                if (settings.outro_transition_enabled or quote_position is not None) else 0.0
            ),
        )
        resolved = self.engine.make_plan(media, outro_settings, log)
        # 1.3.0: optional dedicated transition duration around the quote card
        # (0.0 = the global transition duration applies). The clamp mirrors
        # target.safe_transition_durations so the boundaries around the card
        # can never exceed 45 % of either neighboring section.
        quote_transition = float(getattr(settings, "quote_transition_duration", 0.0) or 0.0)
        if quote_position is not None and quote_transition > 0.01:
            for boundary in (quote_position - 1, quote_position):
                if 0 <= boundary < len(resolved.transitions):
                    left = resolved.effective_durations[boundary]
                    right = resolved.effective_durations[boundary + 1]
                    value = max(0.01, round(min(quote_transition, left * 0.45, right * 0.45), 6))
                    resolved.transitions[boundary] = value
            resolved.expected_duration = max(
                0.0, sum(resolved.effective_durations) - sum(resolved.transitions)
            )
            log(f"Quote transition duration: {quote_transition:.2f} s (nur um die Quote-Karte).")
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
