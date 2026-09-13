"""Job execution: VM Automatic → existing VideoMerger pipeline.

This module is the ONLY place where rendering happens, and it does not
implement any rendering itself. It reuses the existing VideoMerger building
blocks exactly as the GUI does (see ``gui/workers.py``):

* ``VideoMergerEngine``  – FFmpeg engine (preflight, analyze, export)
* ``MainProjectEngine``  – create_main / create_complete workflows
* ``discover_videos``    – the existing clip-pool discovery rules
* ``ProjectOrderStore``  – the existing persisted active clip order
* ``GeneratedOutputStore`` – the existing "never reuse outputs as inputs" store
* ``SettingsStore``      – the master VideoMerger configuration (READ-ONLY)

The automation layer may change ONLY the job-specific input (voiceover +
script) and the job-local randomized clip order. Every other setting comes
from the untouched master configuration.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from ..video_merger.engine import VideoMergerEngine
from ..video_merger.errors import VideoMergerError
from ..video_merger.main_project import MainProjectEngine
from ..video_merger.output_manager import sanitize_filename
from ..video_merger.paths import locate_ffmpeg
from ..video_merger.project_assets import optional_path
from ..video_merger.project_order import GeneratedOutputStore, ProjectOrderStore
from ..video_merger.settings_store import SettingsStore
from .config import (
    OUTPUTS_AUTO, OUTPUTS_BOTH, OUTPUTS_LONG, OUTPUTS_SHORTS,
    WORKFLOW_COMPLETE, WORKFLOW_MAIN, VMAutomaticConfig,
)
from .randomization import JobClipOrder, randomize_pool, resolve_eligible_pool
from .state import Job


@dataclass(slots=True)
class JobRunResult:
    ok: bool
    outputs: list[str] = field(default_factory=list)
    clip_order: list[str] = field(default_factory=list)
    error: str = ""
    workflow: str = ""
    aspects: list[str] = field(default_factory=list)


def _aspect_label(aspect: str) -> str:
    return "Shorts" if aspect == "9:16" else "Long-Form"


def resolve_workflow(master, vm_config: VMAutomaticConfig) -> str:
    """auto → one-click when the master config has Intro/Outro/Quote, else Main."""
    if vm_config.workflow == WORKFLOW_MAIN:
        return WORKFLOW_MAIN
    if vm_config.workflow == WORKFLOW_COMPLETE:
        return WORKFLOW_COMPLETE
    if optional_path(master.intro_path) or optional_path(master.outro_path):
        return WORKFLOW_COMPLETE
    if bool(master.quote_enabled) and (master.quote_text or "").strip():
        return WORKFLOW_COMPLETE
    return WORKFLOW_MAIN


def resolve_aspects(master, vm_config: VMAutomaticConfig) -> list[str]:
    master_aspect = master.aspect if master.aspect in {"16:9", "9:16"} else "16:9"
    if vm_config.outputs == OUTPUTS_AUTO:
        return [master_aspect]
    if vm_config.outputs == OUTPUTS_LONG:
        return ["16:9"]
    if vm_config.outputs == OUTPUTS_SHORTS:
        return ["9:16"]
    if vm_config.outputs == OUTPUTS_BOTH:
        ordered = ["16:9", "9:16"]
        return ordered
    return [master_aspect]


def verify_outputs(paths: list[Path], log: Callable[[str], None], interval: float = 0.7) -> list[str]:
    """Spec §28: outputs must exist, be non-zero, readable and stable."""
    problems: list[str] = []
    first = []
    for path in paths:
        if path is None:
            continue
        if not path.is_file():
            problems.append(f"Ausgabe fehlt: {path.name}")
            continue
        stat = path.stat()
        if stat.st_size <= 0:
            problems.append(f"Ausgabe ist leer: {path.name}")
            continue
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            problems.append(f"Ausgabe nicht lesbar: {path.name}: {exc}")
            continue
        first.append((path, stat.st_size, stat.st_mtime_ns))
    if not problems and first:
        time.sleep(interval)
        for path, size, mtime in first:
            try:
                stat = path.stat()
            except OSError:
                problems.append(f"Ausgabe verschwunden: {path.name}")
                continue
            if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                problems.append(f"Ausgabe ist nicht stabil: {path.name}")
    if problems:
        raise VideoMergerError("VM Automatic Output-Validierung fehlgeschlagen: " + "; ".join(problems))
    log("VM Automatic: Output-Validierung OK (existiert, nicht leer, lesbar, stabil).")
    return [str(path) for path in paths if path is not None]


class JobRunner:
    """Executes one job through the EXISTING VideoMerger workflow."""

    def __init__(
        self,
        vm_config: VMAutomaticConfig,
        settings_store: SettingsStore | None = None,
        order_store: ProjectOrderStore | None = None,
        output_store: "GeneratedOutputStore | None" = None,
        log: Callable[[str], None] | None = None,
    ):
        self.vm_config = vm_config
        self.settings_store = settings_store or SettingsStore()
        self.order_store = order_store or ProjectOrderStore()
        self.log = log or (lambda _message: None)
        self._output_store = output_store  # lazy default in _generated_paths()

    def _generated_paths(self) -> set[str]:
        if self._output_store is None:
            self._output_store = GeneratedOutputStore()
        return self._output_store.paths()

    # ------------------------------------------------------------------ #
    def run(self, job: Job, cancel_event: threading.Event | None = None) -> JobRunResult:
        """Run one complete job (all configured aspects) end to end."""
        cancel_event = cancel_event or threading.Event()
        log = self.log
        # 1) Job inputs must still exist (they may have been deleted).
        audio_path = Path(job.audio_path) if job.audio_path else None
        script_path = Path(job.script_path) if job.script_path else None
        if audio_path is None or not audio_path.is_file():
            return JobRunResult(False, error=f"Voiceover-Datei fehlt: {job.audio or 'nicht vergeben'}")
        if script_path is not None and not script_path.is_file():
            return JobRunResult(False, error=f"Script-Datei fehlt: {job.script or 'nicht vergeben'}")

        # 2) Master configuration (read-only – never saved back).
        master = self.settings_store.load()
        workflow = resolve_workflow(master, self.vm_config)
        aspects = resolve_aspects(master, self.vm_config)
        if job.seed is None:
            job.seed = _fresh_seed()
        log(f"VM Automatic: Job {job.display_id} – Workflow: {workflow} · Outputs: {aspects} · Seed {job.seed}")

        ffmpeg, ffprobe = locate_ffmpeg()
        engine = VideoMergerEngine(ffmpeg, ffprobe)
        engine.preflight(log)

        # 3) Eligible clip pool (existing VideoMerger rules, unchanged).
        pool = resolve_eligible_pool(
            self.vm_config.resolved_clip_pool_folder(),
            self.order_store,
            self._generated_paths(),
        )
        if not pool:
            return JobRunResult(False, error="Keine geeigneten Videoclips im Clip-Pool-Ordner gefunden.")

        # 4) Job-local randomized clip order (seed-replayable).
        clip_order: JobClipOrder = randomize_pool(pool, job.seed, log=log)
        log(
            f"VM Automatic: Randomizing {clip_order.pool_size} eligible clips "
            f"(Seed {clip_order.seed}): " + " → ".join(clip_order.names)
        )
        media = engine.analyze(list(clip_order.paths), log)

        # 5) Job-local execution configuration: master + job input ONLY.
        job_settings = replace(
            master,
            voiceover_paths=[str(audio_path)],
            voiceover_path=str(audio_path),
            script_paths=[str(script_path)] if script_path is not None else [],
            script_path=str(script_path) if script_path is not None else "",
        )

        project = MainProjectEngine(engine)
        out_root = self.vm_config.resolved_output_folder()
        all_outputs: list[str] = []
        for aspect in aspects:
            if cancel_event.is_set():
                return JobRunResult(False, error="Abgebrochen vom Benutzer.", workflow=workflow, aspects=aspects)
            log(f"VM Automatic: Starting {_aspect_label(aspect)}")
            out_dir = out_root / sanitize_filename(job.display_id) if self.vm_config.job_subfolders else out_root
            aspect_settings = replace(job_settings, aspect=aspect)
            if workflow == WORKFLOW_COMPLETE:
                result = project.create_complete(
                    media, aspect_settings, out_dir,
                    log=log, cancel_event=cancel_event,
                )
                produced = [
                    result.main.video, result.main.video_no_subtitles,
                    result.main.srt, result.main.vtt,
                    result.final_video, result.final_video_no_subtitles,
                    result.youtube_metadata,
                ]
            else:
                result = project.create_main(
                    media, aspect_settings, out_dir,
                    log=log, cancel_event=cancel_event,
                )
                produced = [result.video, result.video_no_subtitles, result.srt, result.vtt]
            if not result.report.ok:
                return JobRunResult(
                    False, error="VideoMerger-Validierung negativ: " + " ".join(result.report.details)[:2000],
                    workflow=workflow, aspects=aspects, clip_order=list(clip_order.names),
                )
            paths = [path for path in produced if path is not None]
            for path in paths:
                self._output_store.add(path)  # never let outputs become inputs
            all_outputs.extend(verify_outputs(paths, log))
            log(f"VM Automatic: {_aspect_label(aspect)} fertig: {', '.join(p.name for p in paths)}")

        return JobRunResult(
            True,
            outputs=all_outputs,
            clip_order=list(clip_order.names),
            workflow=workflow,
            aspects=aspects,
        )


def _fresh_seed() -> int:
    import secrets

    return secrets.randbits(32)
