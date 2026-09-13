"""The VM Automatic controller – headless, Qt-independent automation core.

Architecture (per specification §56):

    VM AUTOMATIC
      |
      +-- Watcher (events)     +-- Stability    +-- Queue
      +-- Reconciliation scan  +-- Pairing/Jobs +-- Recovery
      |
      v
    Existing VideoMerger  (JobRunner -> MainProjectEngine -> render)

Threading model:
* supervisor thread – drains debounced filesystem events, feeds the
  stability tracker, schedules the periodic reconciliation scan, promotes
  READY jobs to QUEUED.
* worker thread     – executes ONE job at a time via the JobRunner.
* GUI actions       – run on the UI thread; all shared state mutations are
  guarded by a single controller lock (coarse, simple, correct).

Idle cost: the supervisor sleeps on an Event (no busy loop); the only
periodic work is the reconciliation scan (one directory listing + stat per
file, no hashing).
"""
from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..video_merger.project_order import GeneratedOutputStore, ProjectOrderStore
from ..video_merger.settings_store import SettingsStore
from .config import VMAutomaticConfig
from .identity import is_audio_name, is_ignored_name, is_script_name, normalize_stem
from .logging_setup import TeeLogger
from .pairing import scan_watch_folder
from .queue import JobQueue
from .recovery import recover_interrupted_jobs
from .runner import JobRunner
from .state import Job, JobState, JobStore, utc_now_iso
from .stability import FileStability
from .watcher import create_watcher

STATUS_STOPPED = "STOPPED"
STATUS_WATCHING = "WATCHING"
STATUS_STOPPING = "STOPPING"


class VMAutomaticController:
    """Owns the watcher, job store, queue and the render worker."""

    def __init__(
        self,
        vm_config: VMAutomaticConfig | None = None,
        *,
        runner: JobRunner | None = None,
        order_store: ProjectOrderStore | None = None,
        settings_store: SettingsStore | None = None,
        state_path: Path | str | None = None,
        output_store: GeneratedOutputStore | None = None,
        logger: "logging.Logger | None" = None,
        log_path: Path | None = None,
    ):
        self.vm_config = vm_config or VMAutomaticConfig.load()
        self.vm_config.validate()
        self.store = JobStore(state_path)
        self.order_store = order_store or ProjectOrderStore()
        self.settings_store = settings_store or SettingsStore()
        self._output_store = output_store or GeneratedOutputStore()

        from .logging_setup import configure_vm_logger

        self._logger, self._default_log_path = configure_vm_logger()
        self._listeners: list[Callable[[str], None]] = []
        self.log = TeeLogger(self._logger, self._listeners)
        self.log.log_path = log_path or self._default_log_path

        self.queue = JobQueue(
            self.store,
            max_attempts=self.vm_config.max_attempts,
            log=self.log,
            initially_paused=bool(self.vm_config.paused),
        )
        self._last_promote = 0.0
        self.stability = FileStability(
            stability_seconds=self.vm_config.stability_seconds,
            sample_interval=self.vm_config.stability_interval_seconds,
        )
        self.runner = runner or JobRunner(
            self.vm_config,
            self.settings_store,
            self.order_store,
            output_store=self._output_store,
            log=self.log,
        )

        self._lock = threading.RLock()
        self._events: "queue.Queue[tuple[str, Path]]" = queue.Queue()
        self._debounce: dict[str, float] = {}
        self._stable_sigs: dict[str, dict[str, tuple[int, int]]] = {}
        # (job_id, role) → True while a file is still being tracked for
        # stability; re-observed on every supervisor tick (stat-only, no
        # hashing) so a file that finishes copying also becomes stable
        # without any further filesystem events.
        self._pending: dict[tuple[str, str], bool] = {}
        self._watcher = None
        self._supervisor: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._dispatch = threading.Event()
        self._next_reconcile: float | None = None
        self._cancel = threading.Event()
        self._inflight: Job | None = None
        self.status = STATUS_STOPPED
        self.last_reconcile_at: str = ""
        self.last_scan_summary = ""

    # ------------------------------------------------------------------ #
    # Listeners / notification
    # ------------------------------------------------------------------ #
    def add_listener(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def _notify(self, payload: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(payload)
            except Exception:  # a broken listener must never kill automation
                pass

    def _log(self, message: str) -> None:
        self.log(message)
        self._notify({"type": "log", "message": message})

    # ------------------------------------------------------------------ #
    # Startup order (spec §7): load state → immediate reconciliation scan →
    # start watcher → process ready queue.
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        with self._lock:
            if self._supervisor and self._supervisor.is_alive():
                return
            self._stop.clear()
            self._cancel.clear()
            watch = self.vm_config.resolved_watch_folder()
            watch.mkdir(parents=True, exist_ok=True)
            self.vm_config.resolved_output_folder().mkdir(parents=True, exist_ok=True)

            # 1) recover interrupted jobs (persisted state is already loaded)
            recovered = recover_interrupted_jobs(self.store, self.vm_config.max_attempts, log=self._log)
            if recovered:
                self._log(f"VM Automatic: Wiederherstellung: {len(recovered)} unterbrochener Job(s) erkannt.")

            # 2) immediate reconciliation scan (do NOT wait for the 5-minute scan)
            self._reconcile("startup")

            # 3) start the event-driven filesystem watcher
            self._watcher = create_watcher(watch, self._on_fs_event,
                                           poll_interval=self.vm_config.polling_fallback_seconds,
                                           log=self._log)
            self._watcher.start()

            # 4) process the ready queue (supervisor promotes, worker executes)
            self._next_reconcile = time.monotonic() + self.vm_config.reconciliation_seconds
            self._worker = threading.Thread(target=self._worker_loop, name="vm-auto-worker", daemon=True)
            self._worker.start()
            self._supervisor = threading.Thread(target=self._supervisor_loop, name="vm-auto-supervisor", daemon=True)
            self._supervisor.start()
            self.status = STATUS_WATCHING
            self._notify({"type": "status", "status": self.status})
            self._log(
                f"VM Automatic: WATCHING – Watch-Ordner {watch} · Reconcile alle "
                f"{int(self.vm_config.reconciliation_seconds)} s · Stability {self.vm_config.stability_seconds:g} s."
            )

    def stop(self, cancel_running: bool = True) -> None:
        with self._lock:
            if self.status == STATUS_STOPPED:
                return
            self.status = STATUS_STOPPING
            self._notify({"type": "status", "status": self.status})
            self._stop.set()
            if self._watcher is not None:
                try:
                    self._watcher.stop()
                except Exception:
                    pass
                self._watcher = None
            if cancel_running:
                self._cancel.set()
            if self._supervisor is not None:
                self._supervisor.join(timeout=10)
                self._supervisor = None
            if self._worker is not None:
                self._worker.join(timeout=15)
                if self._worker.is_alive():
                    self._log("VM Automatic: Render läuft noch; Zustand bleibt RUNNING (Crash-Recovery greift beim Neustart).")
                self._worker = None
            self.status = STATUS_STOPPED
            self._notify({"type": "status", "status": self.status})
            self._log("VM Automatic: STOPPED – kein Weiterverarbeiten (Automation ist vollständig aus).")

    # ------------------------------------------------------------------ #
    # GUI-facing actions
    # ------------------------------------------------------------------ #
    def scan_now(self) -> None:
        self._events.put(("scan", Path(".")))
        self._dispatch.set()

    def pause_queue(self) -> bool:
        changed = self.queue.pause()
        self.vm_config.paused = self.queue.paused
        self._notify({"type": "queue", "paused": self.queue.paused})
        return changed

    def resume_queue(self) -> bool:
        changed = self.queue.resume()
        self.vm_config.paused = self.queue.paused
        self._dispatch.set()
        self._notify({"type": "queue", "paused": self.queue.paused})
        return changed

    def retry(self, job_id: str, reseed: bool = False) -> Job:
        new_seed = secrets.randbits(32) if reseed else None
        job = self.queue.retry(job_id, reseed=reseed, new_seed=new_seed)
        self._dispatch.set()
        self._notify({"type": "job", "job_id": job_id})
        return job

    def update_config(self, changes: dict[str, Any]) -> None:
        """Persist VM Automatic-specific settings changes (never VideoMerger's)."""
        for key, value in changes.items():
            if not hasattr(self.vm_config, key):
                raise AttributeError(f"VM-Automatic-Config-Feld unbekannt: {key}")
            setattr(self.vm_config, key, value)
        self.vm_config.validate()
        self.vm_config.save()
        self.queue.max_attempts = max(1, int(self.vm_config.max_attempts))
        self.stability.stability_seconds = self.vm_config.stability_seconds
        self.stability.sample_interval = self.vm_config.stability_interval_seconds
        if self.status == STATUS_WATCHING:
            self._next_reconcile = time.monotonic() + self.vm_config.reconciliation_seconds
        self._log("VM Automatic: Einstellungen aktualisiert und gespeichert.")
        self._notify({"type": "config"})

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def _on_fs_event(self, kind: str, path: Path) -> None:
        self._events.put((kind, path))
        self._dispatch.set()

    # ------------------------------------------------------------------ #
    # Supervisor
    # ------------------------------------------------------------------ #
    def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                # blocking wait wakes the idle loop up for new events
                item = self._events.get(timeout=0.4)
            except queue.Empty:
                item = None
            if item is not None:
                kind, path = item
                if kind == "scan":
                    self._reconcile("manual")
                else:
                    self._register_event(kind, path)
            self.tick()

    def tick(self) -> None:
        """One supervisor iteration (also called directly from tests)."""
        # drain everything already queued
        while True:
            try:
                kind, path = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "scan":
                self._reconcile("manual")
            else:
                self._register_event(kind, path)
        # debounce expiry
        now = time.monotonic()
        due = [key for key, deadline in list(self._debounce.items()) if deadline <= now]
        if due:
            for key in due:
                self._debounce.pop(key, None)
            self._process_candidates([Path(key) for key in due])
        # complete stability tracking for files that stopped changing
        # (no further watcher events arrive for them)
        self._observe_pending()
        # reconciliation schedule
        if self._next_reconcile is not None and now >= self._next_reconcile:
            self._reconcile("periodic")
        # promotion (throttled; cheap for small job counts)
        if now - self._last_promote >= 2.0:
            self._last_promote = now
            self._promote_ready()

    def _register_event(self, kind: str, path: Path) -> None:
        try:
            key = str(path.expanduser().resolve())
        except OSError:
            return
        if not path.is_file():
            return
        watch = str(self.vm_config.resolved_watch_folder())
        if not (key == watch or key.startswith(watch + os.sep)):
            return
        self._debounce[key] = time.monotonic() + self.vm_config.debounce_seconds
        self._dispatch.set()

    def _process_candidates(self, paths: list[Path]) -> None:
        handled = []
        for path in paths:
            try:
                if not path.is_file():
                    self.stability.forget(path)
                    continue
                if is_ignored_name(path.name, self.vm_config.extra_ignore_markers):
                    continue
                handled.append(path)
            except OSError:
                continue
        for path in handled:
            self._observe_file(path)

    def _observe_pending(self) -> None:
        """Re-observe files whose stability is still pending (cheap stat check)."""
        with self._lock:
            for job_id, role in list(self._pending):
                job = self.store.get(job_id)
                if job is None or job.is_terminal or job.state == JobState.RUNNING:
                    self._pending.pop((job_id, role), None)
                    continue
                name = job.audio if role == "audio" else job.script
                if not name:
                    self._pending.pop((job_id, role), None)
                    continue
                path = self.vm_config.resolved_watch_folder() / name
                if not path.is_file():
                    self._pending.pop((job_id, role), None)
                    continue
                result = self.stability.check(path)
                if result.stable:
                    self._pending.pop((job_id, role), None)
                    self._stable_sigs.setdefault(job_id, {})[role] = (result.size, 0)
                    self._log(f"VM Automatic: Datei stabil: {name} ({result.size} Bytes).")
                    if self._job_is_complete(job) and job.state in {
                        JobState.NEW, JobState.WAITING_FOR_STABILITY, JobState.READY,
                    }:
                        self.queue.mark_ready(job)
                        self._notify({"type": "job", "job_id": job.id})
                elif job.state in {JobState.READY, JobState.QUEUED}:
                    # input changed again → back to waiting, keep tracking
                    self.stability.forget(path)
                    job = self._set_state(job_id, JobState.WAITING_FOR_STABILITY)
                    self._log(
                        f"VM Automatic: Job {job.display_id} – {name} ändert sich erneut, "
                        "Stabilität wird geprüft (WAITING_FOR_STABILITY)."
                    )
                    self._notify({"type": "job", "job_id": job.id})
                # otherwise: still tracking – checked again on the next tick

    # ------------------------------------------------------------------ #
    # Job bookkeeping
    # ------------------------------------------------------------------ #
    def _ensure_job(self, path: Path) -> Job | None:
        """Create/update the job record for one inbox file (caller holds the lock)."""
        name = path.name
        if is_ignored_name(name, self.vm_config.extra_ignore_markers):
            return None
        stem = normalize_stem(name)
        if not stem:
            return None
        job = self.store.get(stem)
        if job is None:
            job = Job(id=stem, display_id=path.stem)
            if is_audio_name(name):
                job.audio = name
                job.audio_path = str(path)
            elif is_script_name(name):
                job.script = name
                job.script_path = str(path)
            job.record_state(JobState.NEW)
            self.store.upsert(job)
            self._log(f"Detected new job: {job.display_id}")
        else:
            changes: dict[str, Any] = {}
            if is_audio_name(name) and not job.audio:
                changes["audio"] = name
                changes["audio_path"] = str(path)
            elif is_script_name(name) and not job.script:
                changes["script"] = name
                changes["script_path"] = str(path)
            if changes:
                job = self.store.update(job.id, **changes)
        return job

    def _observe_file(self, path: Path) -> None:
        """Stability observation + job state update for one file."""
        with self._lock:
            job = self._ensure_job(path)
            if job is None:
                return
            result = self.stability.check(path)
            role = "audio" if is_audio_name(path.name) else "script"
            if result.stable:
                self._pending.pop((job.id, role), None)
                self._stable_sigs.setdefault(job.id, {})[role] = (result.size, 0)
                self._log(f"VM Automatic: Datei stabil: {path.name} ({result.size} Bytes).")
                if is_audio_name(path.name) and not job.audio:
                    job = self.store.update(job.id, audio=path.name, audio_path=str(path))
                elif is_script_name(path.name) and not job.script:
                    job = self.store.update(job.id, script=path.name, script_path=str(path))
                if self._job_is_complete(job):
                    if job.state in {JobState.NEW, JobState.WAITING_FOR_STABILITY, JobState.READY}:
                        self.queue.mark_ready(job)
                        self._notify({"type": "job", "job_id": job.id})
                    return
                if job.state in {JobState.NEW, JobState.READY}:
                    missing = "Script" if job.audio else "Audio"
                    job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)
                    self._log(f"VM Automatic: Warten auf {missing} für Job {job.display_id} …")
                elif job.state == JobState.QUEUED:
                    # a new required part arrived after queuing: re-stabilize
                    self.stability.forget(path)
                    job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)
                    self._log(f"VM Automatic: Job {job.display_id} – neue Eingabe, Stabilität wird geprüft.")
            else:
                if job.state in {JobState.READY, JobState.QUEUED}:
                    self.stability.forget(path)
                    job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)
                    self._log(f"VM Automatic: {path.name} ändert sich erneut – Job {job.display_id} wartet (Waiting for stability).")
                elif job.state == JobState.NEW:
                    job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)
                self._pending[(job.id, role)] = True
                if job.state == JobState.WAITING_FOR_STABILITY:
                    self._log(f"VM Automatic: Waiting for stability – {path.name} ({result.reason}).")
            self._notify({"type": "job", "job_id": job.id})

    # ------------------------------------------------------------------ #
    # Reconciliation scan (safety scan; default every 300 s)
    # ------------------------------------------------------------------ #
    def _reconcile(self, reason: str) -> None:
        with self._lock:
            watch = self.vm_config.resolved_watch_folder()
            started = time.perf_counter()
            pairings = scan_watch_folder(watch, tuple(self.vm_config.extra_ignore_markers))
            seen: set[str] = set()
            for stem, pairing in pairings.items():
                seen.add(stem)
                if pairing.audio_conflicts or pairing.script_conflicts:
                    extra = [p.name for p in (*pairing.audio_conflicts, *pairing.script_conflicts)]
                    self._log(f"VM Automatic: WARNUNG Job {pairing.display_id}: zusätzliche gleichnamige Datei(en) ignoriert: {', '.join(extra)}")
                job = self.store.get(stem)
                if job is None:
                    job = Job(id=stem, display_id=pairing.display_id)
                    job.audio = pairing.audio.name if pairing.audio else ""
                    job.script = pairing.script.name if pairing.script else ""
                    job.audio_path = str(pairing.audio) if pairing.audio else ""
                    job.script_path = str(pairing.script) if pairing.script else ""
                    job.record_state(JobState.NEW)
                    self.store.upsert(job)
                    self._log(f"Detected new job: {job.display_id} (Reconciliation)")
                # keep recorded paths current + observe stability
                for role, path in (("audio", pairing.audio), ("script", pairing.script)):
                    if path is None:
                        continue
                    if not path.is_file():
                        continue
                    if job.state in JobState.TERMINAL:
                        continue
                    if not (job.audio_path or job.script_path):
                        # job was created incomplete; fill in the discovered part
                        if role == "audio" and not job.audio:
                            job = self.store.update(job.id, audio=path.name, audio_path=str(path))
                        elif role == "script" and not job.script:
                            job = self.store.update(job.id, script=path.name, script_path=str(path))
                    elif job.state == JobState.RUNNING:
                        continue  # never disturb a running render
                    elif job.state == JobState.QUEUED:
                        sig = self._signature(path)
                        known = self._stable_sigs.get(job.id, {}).get(role)
                        if known and sig and sig[0] != known[0]:
                            self.stability.forget(path)
                            job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)
                            self._log(f"VM Automatic: Eingabe von Job {job.display_id} wurde geändert – neu stabilisiert.")
                        continue
                    self._observe_file_unlocked(job, path)
            # jobs with no visible files anymore (user deleted inputs)
            for job in self.store.all_jobs():
                if job.id in seen or job.is_terminal:
                    continue
                if job.state in {JobState.QUEUED, JobState.READY, JobState.RETRY_PENDING}:
                    job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)
                    self._log(f"VM Automatic: Job {job.display_id} – Eingabedateien fehlen, warte (WAITING_FOR_STABILITY).")
            self.last_reconcile_at = utc_now_iso()
            elapsed = (time.perf_counter() - started) * 1000.0
            self.last_scan_summary = f"{len(pairings)} Job-Kandidat(en) in {elapsed:.0f} ms ({reason})"
            self._log(f"VM Automatic: Reconciliation-Scan ({reason}): {self.last_scan_summary}.")
            self._promote_ready()
            self._notify({"type": "reconcile", "summary": self.last_scan_summary})

    def _signature(self, path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def _job_is_complete(self, job: Job) -> bool:
        """A job has all inputs it needs (script optional via config)."""
        if not job.audio:
            return False
        if job.script or not self.vm_config.require_script:
            return True
        return False

    def _set_state(self, job_id: str, state: str, note: str = "") -> Job:
        """Persist a state transition (recorded in the job history)."""
        return self.store.update(job_id, mutate=lambda job: job.record_state(state, note))

    def _observe_file_unlocked(self, job: Job, path: Path) -> None:
        result = self.stability.check(path)
        role = "audio" if is_audio_name(path.name) else "script"
        if result.stable:
            self._pending.pop((job.id, role), None)
            self._stable_sigs.setdefault(job.id, {})[role] = (result.size, 0)
            if self._job_is_complete(job) and job.state in {JobState.NEW, JobState.WAITING_FOR_STABILITY}:
                self.queue.mark_ready(job)
                self._notify({"type": "job", "job_id": job.id})
        else:
            self._pending[(job.id, role)] = True
            if job.state in {JobState.READY, JobState.QUEUED}:
                self.stability.forget(path)
                job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)
                self._log(f"VM Automatic: Job {job.display_id} – {path.name} nicht stabil, neu gewartet.")
            elif job.state == JobState.NEW:
                job = self._set_state(job.id, JobState.WAITING_FOR_STABILITY)

    def _promote_ready(self) -> None:
        for job in self.store.all_jobs():
            if job.state == JobState.READY and not self.queue.paused:
                self.queue.mark_queued(job)
                self._notify({"type": "job", "job_id": job.id})
            elif job.state == JobState.RETRY_PENDING and not self.queue.paused:
                # automatic retry: only while attempts remain
                if job.attempts < self.vm_config.max_attempts:
                    job = self._set_state(job.id, JobState.QUEUED, "automatischer Retry")
                    self._log(f"VM Automatic: Job {job.display_id} → QUEUED (automatischer Retry).")
                    self._notify({"type": "job", "job_id": job.id})

    # ------------------------------------------------------------------ #
    # Worker (one job at a time)
    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            # Hold the controller lock while popping so the worker and the
            # supervisor can never update the same job record concurrently
            # (the JSON store is read-modify-write per operation).
            with self._lock:
                job = self.queue.next_job()
            if job is None:
                self._stop.wait(1.0)
                continue
            self._inflight = job
            self._cancel.clear()
            self._notify({"type": "job_running", "job_id": job.id})
            from .runner import JobRunResult

            try:
                result = self.runner.run(job, cancel_event=self._cancel)
            except Exception as exc:  # never kill the worker
                result = JobRunResult(ok=False, error=f"{type(exc).__name__}: {exc}")
            if not isinstance(result, JobRunResult):
                result = JobRunResult(ok=False, error="Runner ohne Ergebnis zurückgekehrt.")
            if self._cancel.is_set():
                # user stopped the app mid-render: keep state RUNNING so the
                # next startup performs crash recovery (INTERRUPTED → retry).
                self._log(f"VM Automatic: Job {job.display_id} – abgebrochen; Zustand bleibt RUNNING (Recovery beim Neustart).")
            elif result.ok:
                self.queue.success(job, result.outputs, result.clip_order)
                self._archive_inputs(job)
            else:
                self.queue.failure(job, result.error)
            self._inflight = None
            self._notify({"type": "job_done", "job_id": job.id})

    def _archive_inputs(self, job: Job) -> None:
        if not self.vm_config.archive_inputs:
            return
        archive = self.vm_config.resolved_archive_folder()
        try:
            archive.mkdir(parents=True, exist_ok=True)
            moved = []
            for name in (job.audio, job.script):
                source = self.vm_config.resolved_watch_folder() / name
                if name and source.is_file():
                    target = archive / f"{job.display_id}_{name}"
                    suffix = 1
                    while target.exists():
                        suffix += 1
                        target = archive / f"{job.display_id}_{suffix}_{name}"
                    source.rename(target)
                    moved.append(name)
            if moved:
                job = self.store.update(job.id, archive_path=str(archive))
                self._log(f"VM Automatic: Job {job.display_id} – Eingaben archiviert: {', '.join(moved)}")
        except OSError as exc:
            self._log(f"VM Automatic: WARNUNG – Archivierung fehlgeschlagen (Dateien bleiben im Watch-Ordner): {exc}")

    # ------------------------------------------------------------------ #
    # Introspection (GUI)
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        jobs = self.store.all_jobs()
        current = self._inflight
        succeeded = [job for job in jobs if job.state == JobState.SUCCEEDED]
        return {
            "status": self.status,
            "watcher_backend": getattr(self._watcher, "backend", "none"),
            "paused": self.queue.paused,
            "watch_folder": str(self.vm_config.resolved_watch_folder()),
            "output_folder": str(self.vm_config.resolved_output_folder()),
            "clip_pool_folder": str(self.vm_config.resolved_clip_pool_folder()),
            "reconciliation_seconds": self.vm_config.reconciliation_seconds,
            "stability_seconds": self.vm_config.stability_seconds,
            "max_attempts": self.vm_config.max_attempts,
            "jobs": jobs,
            "current_job": current.id if current else None,
            "last_successful_job": max(succeeded, key=lambda j: j.finished_at or "").id if succeeded else None,
            "last_reconcile_at": self.last_reconcile_at,
            "last_scan_summary": self.last_scan_summary,
        }
