from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .command_builder import FFmpegCommandBuilder
from .errors import ExportCancelled, ExportError, ValidationError, VideoMergerError
from .file_stability import wait_for_files_stable
from .hardware import resolve_encoder
from .media_analyzer import MediaAnalyzer
from .models import ExportSettings, LogCallback, MediaInfo, ProgressCallback, ResolvedExport, ValidationReport
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
    ) -> ValidationReport:
        export_started = time.perf_counter()
        self.last_timings = {}
        cancel_event = cancel_event or threading.Event()
        preflight_started = time.perf_counter()
        self.preflight(log)
        self.last_timings["preflight_seconds"] = time.perf_counter() - preflight_started
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
            # Diagnostic copy only. FFmpeg receives the graph directly through
            # -filter_complex and never reads this file.
            graph_path.write_text(built.filter_graph, encoding="utf-8", newline="\n")
            try:
                render_started = time.perf_counter()
                self._execute(
                    built.command, media, resolved, progress, log, cancel_event,
                    transition_label(settings.transition_type),
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
                        transition_label(settings.transition_type),
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

    def _execute(
        self,
        command: list[str],
        media: list[MediaInfo],
        resolved: ResolvedExport,
        progress_callback: ProgressCallback,
        log: LogCallback,
        cancel_event: threading.Event,
        transition_name: str,
    ) -> None:
        rendered_command = format_command_for_log(command)
        if os.name == "nt" and len(rendered_command) > 30_000:
            raise ExportError(
                "Der direkte FFmpeg-Befehl überschreitet mit diesem sehr großen Projekt das sichere Windows-Limit. "
                "Bitte das Projekt in kleinere Teile aufteilen."
            )
        log("Starte FFmpeg (Argumentliste ohne Shell; Unicode-Pfade bleiben atomar).")
        log("Rendering command:\n" + rendered_command)
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
