"""Startup crash recovery for VM Automatic.

If VM Automatic closed (or crashed) while a job was RUNNING, the state file
still says RUNNING. On startup that job must be detected and recovered:

    RUNNING → INTERRUPTED → RETRY_PENDING  (while attempts remain)
    RUNNING → INTERRUPTED → FAILED         (attempts exhausted)

A SUCCEEDED job is never re-processed, and a job's recorded outputs are kept
exactly as they were. Recovery never marks an interrupted job successful.
"""
from __future__ import annotations

from typing import Callable

from .state import JobState


def recover_interrupted_jobs(store, max_attempts: int, log: Callable[[str], None] | None = None) -> list[str]:
    """Detect and transition interrupted jobs. Returns the recovered ids."""
    log = log or (lambda _message: None)
    recovered: list[str] = []
    for job in store.all_jobs():
        if job.state != JobState.RUNNING:
            continue
        job.record_state(JobState.INTERRUPTED, "App-Neustart erkannt")
        if job.attempts >= max_attempts:
            job.record_state(JobState.FAILED, "unterbrochen, keine Versuche mehr übrig")
            log(f"VM Automatic: Job {job.display_id} → FAILED (während RUNNING unterbrochen, Versuche aufgebraucht).")
        else:
            job.record_state(JobState.RETRY_PENDING, "unterbrochen, automatischer Retry geplant")
            log(f"VM Automatic: Job {job.display_id} → RETRY_PENDING (während RUNNING unterbrochen).")
        store.upsert(job)
        recovered.append(job.id)
    return recovered
