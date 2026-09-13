"""The persistent, sequential job queue of VM Automatic.

Rules implemented here:

* ONE JOB AT A TIME (sequential; no parallel rendering);
* duplicate protection – job identity is the normalized stem, a job is only
  ever queued through its single store record (terminal states are never
  re-queued);
* deterministic promotion order – natural order of the job id
  (001 → 002 → 003, never 001 → 010 → 002);
* limited automatic retries (``max_attempts``) – a job that exceeds the
  limit becomes FAILED (needs attention) and is never retried on its own;
* manual retry is always possible from the GUI (Retry / Retry+Randomize).
"""
from __future__ import annotations

import threading
from typing import Callable

from ..video_merger.project_order import natural_sort_key
from .state import Job, JobState, utc_now_iso


class JobQueue:
    def __init__(
        self,
        store,
        max_attempts: int = 3,
        log: Callable[[str], None] | None = None,
        initially_paused: bool = False,
    ):
        self.store = store
        self.max_attempts = max(1, int(max_attempts))
        self.log = log or (lambda _message: None)
        self._lock = threading.RLock()
        self._paused = bool(initially_paused)

    # ------------------------------------------------------------------ #
    def pause(self) -> bool:
        with self._lock:
            if self._paused:
                return False
            self._paused = True
            self.log("VM Automatic: Queue pausiert (keine neuen Jobs, laufender Job läuft zu Ende).")
            return True

    def resume(self) -> bool:
        with self._lock:
            if not self._paused:
                return False
            self._paused = False
            self.log("VM Automatic: Queue fortgesetzt.")
            return True

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    # ------------------------------------------------------------------ #
    def queued_jobs(self) -> list[Job]:
        """All queueable jobs in promotion order (deterministic)."""
        with self._lock:
            jobs = [job for job in self.store.all_jobs() if job.state in JobState.QUEUEABLE]
            jobs.sort(key=lambda job: (job.queued_at or job.detected_at, natural_sort_key(job.id)))
            return jobs

    def next_job(self) -> Job | None:
        """Pop the oldest queueable job and mark it RUNNING.

        Returns None when paused or empty. The state transition is persisted
        BEFORE the render starts, so a crash mid-render is recoverable
        (RUNNING on disk at startup → INTERRUPTED → retry).
        """
        with self._lock:
            if self._paused:
                return None
            jobs = [job for job in self.store.all_jobs() if job.state in JobState.QUEUEABLE]
            if not jobs:
                return None
            jobs.sort(key=lambda job: (job.queued_at or job.detected_at, natural_sort_key(job.id)))
            job = jobs[0]
            # fresh seed for every job; retries reuse the stored seed (the
            # GUI "Retry + Randomize Again" is the only way to reseed)
            if job.seed is None:
                import secrets

                job.seed = int(secrets.randbits(32))
            job.attempts += 1
            job.render_started_at = utc_now_iso()
            job.record_state(JobState.RUNNING, f"attempt {job.attempts}/{self.max_attempts}")
            self.store.upsert(job)
            self.log(f"VM Automatic: Job {job.display_id} → RUNNING (Versuch {job.attempts}/{self.max_attempts}).")
            return job

    # ------------------------------------------------------------------ #
    def mark_ready(self, job: Job) -> None:
        if job.state == JobState.READY:
            return
        job.ready_at = utc_now_iso()
        job.record_state(JobState.READY, "alle Dateien stabil")
        self.store.upsert(job)
        self.log(f"VM Automatic: Job {job.display_id} – Job ready (Dateien stabil).")

    def mark_queued(self, job: Job) -> None:
        if job.state == JobState.QUEUED:
            return
        job.queued_at = utc_now_iso()
        job.record_state(JobState.QUEUED)
        self.store.upsert(job)
        self.log(f"VM Automatic: Job {job.display_id} → QUEUED.")

    def success(self, job: Job, outputs: list[str], clip_order: list[str]) -> Job:
        with self._lock:
            job.finished_at = utc_now_iso()
            job.outputs = list(outputs)
            job.outputs_verified = True
            job.clip_order = list(clip_order)
            job.last_error = ""
            job.record_state(JobState.SUCCEEDED, "Output verifiziert")
            self.store.upsert(job)
            self.log(f"VM Automatic: Job {job.display_id} – Job completed successfully.")
            return job

    def failure(self, job: Job, error: str) -> Job:
        """Record one failed attempt; auto-retry only while attempts remain."""
        with self._lock:
            job.last_error = str(error)[:4000]
            job.finished_at = utc_now_iso()
            if job.attempts >= self.max_attempts:
                job.record_state(JobState.FAILED, f"alle {job.attempts} Versuche fehlgeschlagen")
                self.log(
                    f"VM Automatic: Job {job.display_id} → FAILED nach {job.attempts} Versuchen: "
                    + str(error)[:500]
                )
            else:
                job.record_state(JobState.RETRY_PENDING, f"Versuch {job.attempts} fehlgeschlagen")
                self.log(
                    f"VM Automatic: Job {job.display_id} → RETRY_PENDING "
                    f"(Versuch {job.attempts}/{self.max_attempts}): " + str(error)[:500]
                )
            self.store.upsert(job)
            return job

    # ------------------------------------------------------------------ #
    def retry(self, job_id: str, reseed: bool = False, new_seed: int | None = None) -> Job:
        """Manual retry (GUI). Always allowed; counts as an attempt.

        ``reseed=True`` generates a fresh clip order (Retry + Randomize
        Again); by default the stored seed/order is reused.
        """
        with self._lock:
            job = self.store.get(job_id)
            if job is None:
                raise KeyError(f"Unbekannter Job: {job_id}")
            if job.state == JobState.SUCCEEDED:
                raise ValueError("Ein SUCCEEDED Job wird niemals erneut verarbeitet (Duplikatschutz).")
            if job.state == JobState.RUNNING:
                raise ValueError("Job läuft gerade – Retry ist nicht möglich.")
            if reseed:
                if new_seed is None:
                    import secrets

                    new_seed = secrets.randbits(32)
                job.seed = int(new_seed)
                job.clip_order = []
            job.record_state(JobState.QUEUED, "manueller Retry" + (" (neue Zufallsreihenfolge)" if reseed else ""))
            job.queued_at = utc_now_iso()
            self.store.upsert(job)
            self.log(f"VM Automatic: Job {job.display_id} → QUEUED (manueller Retry).")
            return job
