from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ..discovery import discover_videos
from ..engine import VideoMergerEngine
from ..main_project import MainProjectEngine
from ..models import ExportSettings
from ..output_manager import make_output_path
from ..paths import locate_ffmpeg
from ..project_order import GeneratedOutputStore, ProjectOrderStore
from ..video_pool import order_media_for_video_order


class ProcessingWorker(QObject):
    log = Signal(str)
    progress = Signal(object)
    analysis_ready = Signal(object, object)
    finished = Signal(str, object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, mode: str, input_folder: Path, output_folder: Path, settings: ExportSettings):
        super().__init__()
        self.mode = mode
        self.input_folder = input_folder
        self.output_folder = output_folder
        self.settings = settings
        self.cancel_event = threading.Event()
        self.engine: VideoMergerEngine | None = None

    @Slot()
    def run(self) -> None:
        try:
            ffmpeg, ffprobe = locate_ffmpeg()
            self.engine = VideoMergerEngine(ffmpeg, ffprobe)
            self.engine.preflight(self.log.emit)
            output_store = GeneratedOutputStore()
            if self.mode == "outro":
                output, report = MainProjectEngine(self.engine).add_outro(
                    self.settings, self.output_folder,
                    progress=self.progress.emit, log=self.log.emit,
                    cancel_event=self.cancel_event,
                )
                output_store.add(output)
                self.finished.emit(str(output), report)
                return
            configured_sources = list(getattr(self.settings, "source_folders", []) or [])
            discovery_input = configured_sources if configured_sources else self.input_folder
            paths = discover_videos(
                discovery_input,
                order_store=ProjectOrderStore(),
                excluded_paths=output_store.paths(),
            )
            self.log.emit(
                f"Input files found in {len(configured_sources) or 1} configured source folder(s): {len(paths)}"
            )
            self.log.emit("Discovered persisted order: " + " → ".join(path.name for path in paths))
            analysis_started = time.perf_counter()
            media = self.engine.analyze(paths, self.log.emit)
            analysis_seconds = time.perf_counter() - analysis_started
            self.log.emit(f"PERFORMANCE video_analysis_seconds={analysis_seconds:.3f}")
            # Apply the selected project order once, before pool sizing and
            # Required-Only selection. The same ordered MediaInfo list is sent
            # to the preview and Stage 1, so Random cannot preview one sequence
            # and render another.
            media = order_media_for_video_order(
                media, getattr(self.settings, "video_order_mode", "natural"),
            )
            self.log.emit("Effective preview/export order: " + " → ".join(item.path.name for item in media))
            export_media = media
            # Every worker branch receives an already effective list. Main and
            # complete retain the original mode for their Stage-1 cache
            # identity; basic/merge/preview use the explicit export hand-off
            # flag below to prevent a second order pass.
            export_settings = self.settings
            if self.mode in {"main", "complete"}:
                # Emit the existing visual analysis first; Stage 1 then computes
                # its voiceover-driven trimmed/extended plan without changing
                # the captured active order.
                initial_plan = self.engine.make_plan(media, self.settings, self.log.emit)
                self.analysis_ready.emit(media, initial_plan)
                project = MainProjectEngine(self.engine)
                # All three YouTube modes use the batch orchestrator, including
                # Long-Form alone, so its output is consistently placed under
                # Output/LongForm. Basic/Preview remain the legacy single-file
                # workflow below.
                result = project.create_youtube_exports(
                    media, self.settings, self.output_folder,
                    progress=self.progress.emit, log=self.log.emit,
                    cancel_event=self.cancel_event,
                    order_already_applied=True,
                    complete=self.mode == "complete",
                )
                for path in result.outputs:
                    output_store.add(path)
                self.finished.emit(str(result.primary_output), result)
                return
            if self.mode == "preview":
                if len(media) < 2:
                    raise ValueError("Für eine Übergangsvorschau werden mindestens zwei Clips benötigt.")
                export_media = [replace(item, duration=min(item.duration, 3.0)) for item in media[:2]]
                export_settings = replace(self.settings, output_name=f"preview_transition_{datetime.now():%Y-%m-%d_%H-%M-%S}")
                self.log.emit("Preview: Die ersten maximal 3 Sekunden der ersten zwei Clips werden verwendet.")
            resolved = self.engine.make_plan(export_media, export_settings, self.log.emit)
            self.analysis_ready.emit(media, resolved)
            if self.mode == "analyze":
                self.finished.emit("", None)
                return
            output_path = make_output_path(
                self.output_folder, export_settings.aspect, export_settings.output_name
            )
            report = self.engine.export(
                export_media,
                export_settings,
                resolved,
                output_path,
                progress=self.progress.emit,
                log=self.log.emit,
                cancel_event=self.cancel_event,
                video_order_applied=True,
            )
            output_store.add(output_path)
            self.finished.emit(str(output_path), report)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))

    @Slot()
    def cancel(self) -> None:
        self.cancel_event.set()
        if self.engine:
            self.engine.cancel()
