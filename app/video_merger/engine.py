from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import replace
from pathlib import Path

from .chunked_render import (
    SAFE_COMMAND_TARGET,
    WINDOWS_COMMAND_LIMIT,
    ChunkingError,
    plan_segments,
)
from .command_builder import BuiltCommand, FFmpegCommandBuilder
from .errors import ExportCancelled, ExportError, ValidationError, VideoMergerError
from .file_stability import wait_for_files_stable
from .hardware import encoder_arguments, resolve_encoder
from .media_analyzer import MediaAnalyzer
from .models import (
    ExportSettings, LogCallback, MediaInfo, ProgressCallback, ProgressEvent,
    ResolvedExport, ValidationReport,
)
from .paths import project_root
from .platform_utils import format_command_for_log, hidden_process_flags, safe_subprocess_env
from .progress import ProgressTracker
from .quality import effective_quality
from .target import resolve_export
from .transition_effects import transition_label
from .validation import validate_output


class VideoMergerEngine:
    def __init__(self, ffmpeg_path: Path | str, ffprobe_path: Path | str):
        self.ffmpeg_path = Path(ffmpeg_path).resolve()
        self.ffprobe_path = Path(ffprobe_path).resolve()
        self.analyzer = MediaAnalyzer(self.ffprobe_path)
        self.builder = FFmpegCommandBuilder(self.ffmpeg_path)
        self._active_process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._preflight_complete = False
        self._ffmpeg_version = ""
        self._ffprobe_version = ""
        self.last_filter_graph = ""
        # 1.3.0: the composition (main render) graph of the last export() call;
        # last_filter_graph may afterwards hold the subtitle burn-in graph.
        self.last_render_graph = ""
        self.last_timings: dict[str, float] = {}

    def _version_line(self, executable: Path, name: str) -> str:
        try:
            completed = subprocess.run(
                [str(executable), "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=hidden_process_flags(),
                env=safe_subprocess_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExportError(f"{name} konnte nicht ausgeführt werden: {executable}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ExportError(f"{name} -version ist fehlgeschlagen: {detail}")
        lines = (completed.stdout or completed.stderr).splitlines()
        return lines[0].strip() if lines else f"{name}: Version unbekannt"

    def preflight(self, log: LogCallback = lambda _message: None) -> None:
        """Verify the exact binaries and direct -filter_complex support."""
        if not self.ffmpeg_path.is_file() or not self.ffprobe_path.is_file():
            raise ExportError("Die ausgewählten lokalen FFmpeg-/FFprobe-Dateien existieren nicht.")
        if not self._ffmpeg_version:
            self._ffmpeg_version = self._version_line(self.ffmpeg_path, "FFmpeg")
        if not self._ffprobe_version:
            self._ffprobe_version = self._version_line(self.ffprobe_path, "FFprobe")
        log("FFmpeg executable:\n" + str(self.ffmpeg_path))
        log("FFmpeg version:\n" + self._ffmpeg_version)
        log("FFprobe executable:\n" + str(self.ffprobe_path))
        log("FFprobe version:\n" + self._ffprobe_version)
        if self._preflight_complete:
            log("FFmpeg direct -filter_complex preflight: bereits erfolgreich geprüft.")
            return
        smoke_graph = "[0:v]null[vout]"
        smoke_command = [
            str(self.ffmpeg_path), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=16x16:r=1:d=0.05",
            "-filter_complex", smoke_graph,
            "-map", "[vout]", "-frames:v", "1", "-f", "null", "-",
        ]
        log("FFmpeg preflight command:\n" + format_command_for_log(smoke_command))
        try:
            completed = subprocess.run(
                smoke_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=hidden_process_flags(),
                env=safe_subprocess_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExportError(f"FFmpeg-Preflight konnte nicht ausgeführt werden: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ExportError(
                "Das ausgewählte FFmpeg kann den erforderlichen direkten -filter_complex-Test nicht ausführen.\n"
                f"Executable: {self.ffmpeg_path}\n{detail}"
            )
        self._preflight_complete = True
        log("FFmpeg direct -filter_complex preflight: OK")

    def analyze(self, paths: list[Path], log: LogCallback = lambda _m: None) -> list[MediaInfo]:
        wait_for_files_stable(paths, log=log)
        return self.analyzer.analyze_many(paths, log)

    def make_plan(self, media: list[MediaInfo], settings: ExportSettings, log: LogCallback = lambda _m: None) -> ResolvedExport:
        hdr_files = [item.path.name for item in media if item.is_hdr]
        if hdr_files and not settings.allow_hdr_unsafe:
            names = ", ".join(hdr_files[:4])
            raise VideoMergerError(
                "HDR-Material erkannt (" + names + "). Der Export wurde zum Schutz vor falschen Farben blockiert. "
                "Version 1.2.1 ist für SDR/BT.709 ausgelegt."
            )
        resolved = resolve_export(media, settings)
        encoder, label, encoder_warnings = resolve_encoder(self.ffmpeg_path, settings.encoding)
        resolved.encoder = encoder
        resolved.encoder_label = label
        # The quality preset is authoritative: it always maps to real encoder
        # arguments. "custom" preserves the explicit low-level CRF/preset.
        resolved.crf, resolved.preset, resolved.quality_label = effective_quality(settings)
        resolved.warnings.extend(encoder_warnings)
        for warning in resolved.warnings:
            log(f"WARNUNG: {warning}")
        return resolved

    def export(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        resolved: ResolvedExport,
        output_path: Path,
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event: threading.Event | None = None,
        working_directory: Path | str | None = None,
        allow_chunking: bool = True,
    ) -> ValidationReport:
        export_started = time.perf_counter()
        self.last_timings = {}
        cancel_event = cancel_event or threading.Event()
        preflight_started = time.perf_counter()
        self.preflight(log)
        self.last_timings["preflight_seconds"] = time.perf_counter() - preflight_started
        # 1.3.0: FFmpeg runs with cwd = project root by default. The
        # filtergraph's file references (staged ASS, fonts dir, quote font)
        # are then plain relative ASCII paths — the root-cause Windows fix
        # for drive colons/backslashes/spaces/umlauts in filter values.
        # Every input/output path is absolute, so the cwd never changes what
        # is read or written.
        workdir = Path(working_directory) if working_directory else project_root()
        output_path = output_path.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = project_root() / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        graph_path = temp_dir / f"diagnostic_filtergraph_{uuid.uuid4().hex}.txt"
        log(f"Eingabedateien: {len(media)} (persistente aktive Projekt-Reihenfolge)")
        log("Tatsächliche Render-Reihenfolge: " + " → ".join(item.path.name for item in media))
        log(f"Ziel: {settings.aspect}, {resolved.resolution_text}, {resolved.fps:g} fps")
        log(
            f"Übergang: {transition_label(settings.transition_type)}, "
            f"Kurve: {settings.transition_ease}, angefordert {settings.transition_duration:.2f} s"
        )
        if resolved.transitions:
            log("Effektive Übergänge: " + ", ".join(f"{value:.3f} s" for value in resolved.transitions))
        log(
            f"Encoding: {resolved.encoder_label}; Qualität: {resolved.quality_label} "
            f"(CRF/CQ {resolved.crf}, Preset {resolved.preset})"
        )
        log(f"Audio: AAC-LC, 48 kHz, Stereo; Normalisierung: {'AN' if settings.normalize_audio else 'AUS'}")
        log(f"Ausgabe: {output_path}")
        export_succeeded = False
        try:
            build_started = time.perf_counter()
            built = self.builder.build(media, settings, resolved, output_path)
            self.last_timings["command_build_seconds"] = time.perf_counter() - build_started
            self.last_filter_graph = built.filter_graph
            self.last_render_graph = built.filter_graph
            command_length = len(format_command_for_log(built.command))
            log(f"FFmpeg command length: {command_length} characters")
            if settings.workflow_stage == "main" and settings.subtitle_enabled:
                if not settings.subtitle_ass_path or not Path(settings.subtitle_ass_path).is_file():
                    raise ExportError(
                        "SUBTITLE GENERATION FAILED [burn-in preparation]: ASS subtitle file is missing."
                    )
                if "subtitles=filename=" not in built.filter_graph:
                    raise ExportError(
                        "SUBTITLE GENERATION FAILED [FFmpeg filter graph]: burned-in subtitle filter is missing."
                    )
                log("Burned-In Subtitle Guard: ASS file present and subtitles filter included in the one-pass graph.")
            if allow_chunking and self._chunking_required(built.command):
                log(
                    f"FFmpeg command is above the conservative {SAFE_COMMAND_TARGET:,}-character "
                    f"target ({command_length:,}); automatic Chunked Rendering enabled."
                )
                chunk_report = self._export_chunked(
                    media, settings, resolved, output_path, built.command,
                    progress, log, cancel_event, workdir,
                )
                self.last_timings["engine_total_seconds"] = time.perf_counter() - export_started
                export_succeeded = True
                return chunk_report
            # Diagnostic copy only. FFmpeg receives the graph directly through
            # -filter_complex and never reads this file.
            graph_path.write_text(built.filter_graph, encoding="utf-8", newline="\n")
            try:
                render_started = time.perf_counter()
                self._execute(
                    built.command, media, resolved, progress, log, cancel_event,
                    transition_label(settings.transition_type), workdir,
                )
            except ExportError as first_error:
                if isinstance(first_error, ExportCancelled):
                    raise
                if settings.encoding == "Auto" and resolved.encoder != "libx264":
                    log(f"WARNUNG: Hardware-Encode fehlgeschlagen ({first_error}). CPU-Fallback wird gestartet.")
                    output_path.unlink(missing_ok=True)
                    resolved.encoder = "libx264"
                    resolved.encoder_label = "CPU (libx264)"
                    built = self.builder.build(media, settings, resolved, output_path)
                    graph_path.write_text(built.filter_graph, encoding="utf-8", newline="\n")
                    self._execute(
                        built.command, media, resolved, progress, log, cancel_event,
                        transition_label(settings.transition_type), workdir,
                    )
                else:
                    raise
            self.last_timings["ffmpeg_rendering_seconds"] = time.perf_counter() - render_started
            if cancel_event.is_set():
                raise ExportCancelled("Export wurde abgebrochen.")
            log("Validiere Ausgabedatei mit FFprobe …")
            validation_started = time.perf_counter()
            report = validate_output(output_path, self.ffprobe_path, resolved)
            self.last_timings["output_validation_seconds"] = time.perf_counter() - validation_started
            for detail in report.details:
                log("Validierung: " + detail)
            if not report.ok:
                raise ValidationError("Export failed validation. " + " ".join(report.details))
            if settings.workflow_stage == "main" and settings.subtitle_enabled:
                report.details.append(
                    "Burned-in subtitle filter executed in the single final FFmpeg encode."
                )
                log("Burned-In Subtitles: PASS – Filter im finalen MP4-Encode ausgeführt.")
            self.last_timings["engine_total_seconds"] = time.perf_counter() - export_started
            log("Export completed successfully.")
            export_succeeded = True
            return report
        except Exception:
            if output_path.exists():
                try:
                    output_path.unlink()
                    log("Unvollständige/ungültige Ausgabedatei wurde entfernt.")
                except OSError as cleanup_error:
                    log(f"WARNUNG: Unvollständige Ausgabe konnte nicht entfernt werden: {cleanup_error}")
            raise
        finally:
            if export_succeeded or cancel_event.is_set():
                graph_path.unlink(missing_ok=True)
            elif graph_path.exists():
                log(f"Diagnose-Filtergraph wurde nach dem Fehler aufbewahrt: {graph_path}")

    def burn_subtitles(
        self,
        clean_video: Path,
        ass_path: Path,
        fonts_dir: str,
        output_path: Path,
        resolved: ResolvedExport,
        media: list[MediaInfo],
        progress: ProgressCallback = lambda _event: None,
        log: LogCallback = lambda _message: None,
        cancel_event: threading.Event | None = None,
    ) -> ValidationReport:
        """1.3.0: burn an existing ASS into an already rendered clean video.

        The primary (subtitled) output is produced from the clean master with
        one lightweight re-encode — video passes through the real libass
        ``subtitles`` filter, audio is stream-copied (no generation loss), the
        same encoder arguments and color tags as the main pipeline are used.
        This is the second half of the two-file subtitle output (clean +
        burned) and keeps the burned variant bit-anchored to the clean render.
        """
        cancel_event = cancel_event or threading.Event()
        self.preflight(log)
        if not Path(ass_path).is_file():
            raise ExportError(
                "SUBTITLE GENERATION FAILED [burn-in preparation]: ASS subtitle file is missing."
            )
        clean_video = Path(clean_video).expanduser().resolve()
        output_path = Path(output_path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        from .filter_escape import filter_file_value

        anchor = project_root()
        filename_value = filter_file_value(str(ass_path), anchor)
        fonts_value = filter_file_value(fonts_dir, anchor) if fonts_dir else ""
        fonts_option = f":fontsdir={fonts_value}" if fonts_value else ""
        graph = (
            f"[0:v:0]subtitles=filename={filename_value}{fonts_option}:charenc=UTF-8[vsub]"
        )
        self.last_filter_graph = graph
        if "subtitles=filename=" not in graph:
            raise ExportError(
                "SUBTITLE GENERATION FAILED [FFmpeg filter graph]: burned-in subtitle filter is missing."
            )
        log("Burned-In Subtitle Guard: ASS file present and subtitles filter included in the burn pass.")
        command = [
            str(self.ffmpeg_path), "-hide_banner", "-y",
            "-i", str(clean_video),
            "-filter_complex", graph,
            "-map", "[vsub]", "-map", "0:a:0?",
            *encoder_arguments(resolved.encoder, resolved.crf, resolved.preset),
            "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-metadata:s:v:0", "rotate=0",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-max_muxing_queue_size", "4096",
            "-progress", "pipe:1", "-nostats",
            str(output_path),
        ]
        export_succeeded = False
        try:
            self._execute(
                command, media, resolved, progress, log, cancel_event,
                "Subtitle Burn-In", anchor,
            )
            if cancel_event.is_set():
                raise ExportCancelled("Export wurde vom Benutzer abgebrochen.")
            log("Validiere Untertitel-Ausgabedatei mit FFprobe …")
            report = validate_output(output_path, self.ffprobe_path, resolved)
            for detail in report.details:
                log("Validierung: " + detail)
            if not report.ok:
                raise ValidationError("Export failed validation. " + " ".join(report.details))
            report.details.append(
                "Burned-in subtitle filter executed in the dedicated subtitle encode."
            )
            log("Burned-In Subtitles: PASS – Filter im Untertitel-Encode ausgeführt.")
            export_succeeded = True
            return report
        finally:
            if not export_succeeded and output_path.exists():
                try:
                    output_path.unlink()
                    log("Unvollständige/ungültige Untertitel-Ausgabe wurde entfernt.")
                except OSError as cleanup_error:
                    log(f"WARNUNG: Ungültige Untertitel-Ausgabe konnte nicht entfernt werden: {cleanup_error}")

    @staticmethod
    def _is_windows() -> bool:
        return os.name == "nt"

    def _chunking_required(self, command: list[str]) -> bool:
        """Return whether a Windows command needs the conservative chunk path."""
        return self._is_windows() and len(format_command_for_log(command)) > SAFE_COMMAND_TARGET

    @staticmethod
    def _concat_manifest_line(path: Path) -> str:
        # ffconcat's single-quoted path form is UTF-8 safe. Escape an embedded
        # apostrophe using the syntax understood by its parser and normalize
        # Windows separators without changing the actual file path.
        value = str(path.expanduser().resolve()).replace("\\", "/")
        value = value.replace("'", "'\\''")
        return f"file '{value}'"

    def _export_chunked(
        self,
        media: list[MediaInfo],
        settings: ExportSettings,
        resolved: ResolvedExport,
        output_path: Path,
        oversized_command: list[str],
        progress: ProgressCallback,
        log: LogCallback,
        cancel_event: threading.Event,
        workdir: Path,
    ) -> ValidationReport:
        """Render an oversized timeline as large, transition-aware segments.

        The ordinary builder remains the source of truth. Intermediate segment
        commands contain one overlap clip so the preceding segment renders the
        complete transition; the next segment trims that overlap prefix. Final
        assembly uses the concat demuxer with stream copy, so it introduces no
        second video/audio encode and no visible or audible seam.
        """
        from .chunked_render import ChunkPlan

        chunk_root = project_root() / "temp" / f"chunked_{uuid.uuid4().hex}"
        chunk_root.mkdir(parents=True, exist_ok=True)
        durations = list(resolved.effective_durations)
        transitions = list(resolved.transitions)
        full_duration = sum(durations) - sum(transitions)
        if full_duration <= 0:
            raise ExportError("Chunked Rendering: die visuelle Timeline ist leer.")
        subtitle_active = settings.workflow_stage == "main" and settings.subtitle_enabled
        if subtitle_active and (
            not settings.subtitle_ass_path or not Path(settings.subtitle_ass_path).is_file()
        ):
            raise ExportError(
                "SUBTITLE GENERATION FAILED [burn-in preparation]: ASS subtitle file is missing."
            )

        def segment_inputs(
            plan: ChunkPlan, destination: Path,
        ) -> tuple[list[MediaInfo], ExportSettings, ResolvedExport, BuiltCommand]:
            segment_media = media[plan.media_start:plan.media_stop]
            segment_resolved = replace(
                resolved,
                effective_durations=durations[plan.media_start:plan.media_stop],
                transitions=transitions[plan.media_start:plan.media_stop - 1],
                expected_duration=plan.duration,
            )
            # Subtitle events are authored on the complete timeline and are
            # burned only once after assembly. This prevents per-segment clock
            # resets and keeps the clean/no-subtitle output authoritative.
            segment_settings = replace(
                settings,
                program_duration=plan.duration,
                timeline_target_duration=plan.duration,
                subtitle_enabled=False,
                subtitle_ass_path="",
                stage2_audio_modes=settings.stage2_audio_modes[
                    plan.media_start:plan.media_stop
                ],
                stage2_roles=settings.stage2_roles[plan.media_start:plan.media_stop],
            )
            audio_start = plan.logical_start if settings.workflow_stage == "main" else plan.video_window_start
            built = self.builder.build(
                segment_media,
                segment_settings,
                segment_resolved,
                destination,
                video_window_start=plan.video_window_start,
                audio_window_start=audio_start,
                window_duration=plan.duration,
            )
            return segment_media, segment_settings, segment_resolved, built

        probe_destination = chunk_root / "chunk_probe.mp4"

        def fits(
            media_start: int,
            media_stop: int,
            logical_start: float,
            logical_end: float,
            video_window_start: float,
            duration: float,
        ) -> bool:
            plan = ChunkPlan(
                number=0,
                media_start=media_start,
                media_stop=media_stop,
                logical_start=logical_start,
                logical_end=logical_end,
                duration=duration,
                video_window_start=video_window_start,
                audio_window_start=logical_start,
            )
            try:
                _segment_media, _segment_settings, _segment_resolved, built = segment_inputs(
                    plan, probe_destination
                )
                return len(format_command_for_log(built.command)) <= SAFE_COMMAND_TARGET
            except (OSError, ValueError, TypeError):
                return False

        try:
            if cancel_event.is_set():
                raise ExportCancelled("Export wurde vom Benutzer abgebrochen.")
            try:
                plans = plan_segments(durations, transitions, fits)
            except ChunkingError as exc:
                raise ExportError(f"Chunked Rendering konnte keinen sicheren Segmentplan erstellen: {exc}") from exc
            log(
                f"Chunked Rendering: {len(plans)} große Segmente geplant "
                f"(Ziel je Befehl {SAFE_COMMAND_TARGET:,} Zeichen; ursprünglicher Befehl "
                f"{len(format_command_for_log(oversized_command)):,} Zeichen)."
            )
            def render_segments() -> list[Path]:
                rendered_paths: list[Path] = []
                for index, plan in enumerate(plans, start=1):
                    if cancel_event.is_set():
                        raise ExportCancelled("Export wurde vom Benutzer abgebrochen.")
                    segment_path = chunk_root / f"segment_{index:04d}.mp4"
                    segment_media, _segment_settings, segment_resolved, built = segment_inputs(plan, segment_path)
                    command_length = len(format_command_for_log(built.command))
                    if command_length > WINDOWS_COMMAND_LIMIT:
                        raise ExportError(
                            f"Chunked Rendering erzeugte trotz Planung einen unsicheren Segmentbefehl "
                            f"({command_length:,} Zeichen)."
                        )
                    log(
                        f"Chunk {index}/{len(plans)}: Clips {plan.media_start + 1}–{plan.media_stop}, "
                        f"Timeline {plan.logical_start:.3f}–{plan.logical_end:.3f} s, "
                        f"Befehl {command_length:,} Zeichen"
                    )

                    def segment_progress(event: ProgressEvent, current=plan, chunk_number=index) -> None:
                        fraction = current.duration / max(full_duration, 1e-9)
                        base = current.logical_start / max(full_duration, 1e-9)
                        progress(replace(
                            event,
                            percent=max(0.0, min(90.0, (base + event.percent / 100.0 * fraction) * 90.0)),
                            total_time=full_duration,
                            stage=f"Chunk {chunk_number}/{len(plans)} – {event.stage}",
                        ))

                    self._execute(
                        built.command, segment_media, segment_resolved, segment_progress, log,
                        cancel_event, transition_label(settings.transition_type), workdir,
                    )
                    segment_report = validate_output(segment_path, self.ffprobe_path, segment_resolved)
                    if not segment_report.ok:
                        raise ValidationError(
                            f"Chunk {index}/{len(plans)} failed validation: "
                            + " ".join(segment_report.details)
                        )
                    rendered_paths.append(segment_path)
                return rendered_paths

            try:
                segment_paths = render_segments()
            except ExportError as first_error:
                if isinstance(first_error, ExportCancelled):
                    raise
                if settings.encoding != "Auto" or resolved.encoder == "libx264":
                    raise
                log(f"WARNUNG: Hardware-Encode fehlgeschlagen ({first_error}). CPU-Fallback wird gestartet.")
                resolved.encoder = "libx264"
                resolved.encoder_label = "CPU (libx264)"
                for path in chunk_root.glob("segment_*.mp4"):
                    path.unlink(missing_ok=True)
                segment_paths = render_segments()

            assembled_clean = output_path if not subtitle_active else chunk_root / "assembled_clean.mp4"
            manifest = chunk_root / "segments.ffconcat"
            manifest.write_text(
                "ffconcat version 1.0\n" + "\n".join(
                    self._concat_manifest_line(path) for path in segment_paths
                ) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            assembly_command = [
                str(self.ffmpeg_path), "-hide_banner", "-y",
                "-f", "concat", "-safe", "0", "-i", str(manifest),
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-movflags", "+faststart", "-max_muxing_queue_size", "4096",
                "-progress", "pipe:1", "-nostats", str(assembled_clean),
            ]
            assembly_length = len(format_command_for_log(assembly_command))
            if assembly_length > WINDOWS_COMMAND_LIMIT:
                raise ExportError(
                    f"Chunked Rendering assembly command is unexpectedly unsafe ({assembly_length:,} Zeichen)."
                )
            log(
                f"Chunk assembly: {len(segment_paths)} Segmente ohne Re-Encode, "
                f"Befehl {assembly_length:,} Zeichen."
            )

            def assembly_progress(event: ProgressEvent) -> None:
                progress(replace(
                    event,
                    percent=90.0 + max(0.0, min(100.0, event.percent)) * 0.1,
                    total_time=resolved.expected_duration,
                    stage=f"Chunk assembly – {event.stage}",
                ))

            self._execute(
                assembly_command, media, resolved, assembly_progress, log,
                cancel_event, transition_label(settings.transition_type), workdir,
            )
            assembled_report = validate_output(assembled_clean, self.ffprobe_path, resolved)
            if not assembled_report.ok:
                raise ValidationError(
                    "Chunk assembly failed validation. " + " ".join(assembled_report.details)
                )
            if subtitle_active:
                log("Chunked Rendering: burn subtitles once on the assembled clean master.")
                return self.burn_subtitles(
                    assembled_clean,
                    Path(settings.subtitle_ass_path),
                    settings.subtitle_fonts_dir,
                    output_path,
                    resolved,
                    media,
                    progress=progress,
                    log=log,
                    cancel_event=cancel_event,
                )
            return assembled_report
        except Exception:
            # Keep failed chunked exports from leaving a stale user-facing
            # master behind, including when this helper is exercised directly
            # rather than through export()'s outer cleanup guard.
            output_path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(chunk_root, ignore_errors=True)

    def _execute(
        self,
        command: list[str],
        media: list[MediaInfo],
        resolved: ResolvedExport,
        progress_callback: ProgressCallback,
        log: LogCallback,
        cancel_event: threading.Event,
        transition_name: str,
        working_directory: Path | str | None = None,
    ) -> None:
        rendered_command = format_command_for_log(command)
        if self._is_windows() and len(rendered_command) > WINDOWS_COMMAND_LIMIT:
            raise ExportError(
                "Der direkte FFmpeg-Befehl überschreitet mit diesem sehr großen Projekt das sichere Windows-Limit. "
                "Bitte das Projekt in kleinere Teile aufteilen."
            )
        log("Starte FFmpeg (Argumentliste ohne Shell; Unicode-Pfade bleiben atomar).")
        log("Rendering command:\n" + rendered_command)
        if working_directory is not None:
            log(f"FFmpeg working directory: {working_directory}")
        tracker = ProgressTracker(media, resolved, transition_name)
        stderr_tail: deque[str] = deque(maxlen=80)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=hidden_process_flags(),
                env=safe_subprocess_env(),
                cwd=str(working_directory) if working_directory is not None else None,
            )
        except OSError as exc:
            raise ExportError(f"FFmpeg konnte nicht gestartet werden: {exc}") from exc
        with self._process_lock:
            self._active_process = process

        def consume_stderr() -> None:
            assert process.stderr is not None
            for raw_line in process.stderr:
                line = raw_line.rstrip()
                if line:
                    stderr_tail.append(line)
                    log("FFmpeg: " + line)

        stderr_thread = threading.Thread(target=consume_stderr, name="ffmpeg-stderr", daemon=True)
        stderr_thread.start()
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                if cancel_event.is_set():
                    process.terminate()
                    break
                line = raw_line.strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        out_time = int(value) / 1_000_000.0
                        progress_callback(tracker.event(out_time))
                    except ValueError:
                        pass
            if cancel_event.is_set() and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            return_code = process.wait()
            stderr_thread.join(timeout=3)
        finally:
            with self._process_lock:
                self._active_process = None
        if cancel_event.is_set():
            raise ExportCancelled("Export wurde vom Benutzer abgebrochen.")
        if return_code != 0:
            detail = "\n".join(stderr_tail)
            raise ExportError(f"FFmpeg-Fehler (Exit-Code {return_code}):\n{detail}")
        progress_callback(tracker.completed())

    def cancel(self) -> None:
        with self._process_lock:
            process = self._active_process
        if process and process.poll() is None:
            process.terminate()
