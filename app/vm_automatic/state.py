"""Persistent job state for VM Automatic (atomic JSON store).

State survives application restarts. Every transition is appended to a
bounded per-job history. The store file is written atomically (temp file +
replace) following the repository convention (see ProjectOrderStore).

States:
    NEW                 – job detected, files not yet fully recorded
    WAITING_FOR_STABILITY – files present, still proving size/mtime stability
    READY               – complete package, stable, waiting for the queue
    QUEUED              – accepted into the sequential queue
    RUNNING             – the existing VideoMerger pipeline is processing it
    SUCCEEDED           – rendered AND output-verified (terminal)
    FAILED              – failed after all allowed attempts (terminal, needs attention)
    RETRY_PENDING       – failed once, eligible for an automatic retry
    INTERRUPTED         – was RUNNING when the app closed/crashed (startup recovery)
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..video_merger.paths import project_root

SCHEMA_VERSION = 1
_HISTORY_LIMIT = 200


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- #
# States
# ---------------------------------------------------------------------- #
class JobState:
    NEW = "NEW"
    WAITING_FOR_STABILITY = "WAITING_FOR_STABILITY"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRY_PENDING = "RETRY_PENDING"
    INTERRUPTED = "INTERRUPTED"

    ALL = (
        NEW, WAITING_FOR_STABILITY, READY, QUEUED, RUNNING,
        SUCCEEDED, FAILED, RETRY_PENDING, INTERRUPTED,
    )
    TERMINAL = (SUCCEEDED, FAILED)
    # States in which a job may (re)enter the queue.
    QUEUEABLE = (READY, QUEUED, RETRY_PENDING)


def default_state_path() -> Path:
    return project_root() / "config" / "vm_automatic" / "state.json"


# ---------------------------------------------------------------------- #
# Job
# ---------------------------------------------------------------------- #
@dataclass(slots=True)
class Job:
    id: str
    display_id: str = ""
    audio: str = ""                 # file name (relative to the watch folder)
    script: str = ""                # file name; empty = no script yet
    audio_path: str = ""
    script_path: str = ""
    state: str = JobState.NEW
    detected_at: str = ""
    stable_at: str = ""
    ready_at: str = ""
    queued_at: str = ""
    render_started_at: str = ""
    finished_at: str = ""
    last_attempt_at: str = ""
    seed: int | None = None         # randomization seed (per job; retries reuse it)
    clip_order: list[str] = field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 3
    outputs: list[str] = field(default_factory=list)
    outputs_verified: bool = False
    last_error: str = ""
    archive_path: str = ""
    history: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.display_id:
            self.display_id = self.id
        if not self.detected_at:
            self.detected_at = utc_now_iso()

    # ------------------------------------------------------------------ #
    def record_state(self, state: str, note: str = "") -> None:
        self.state = state
        entry: dict[str, str] = {"ts": utc_now_iso(), "state": state}
        if note:
            entry["note"] = note
        self.history.append(entry)
        if len(self.history) > _HISTORY_LIMIT:
            del self.history[: len(self.history) - _HISTORY_LIMIT]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "display_id": self.display_id,
            "audio": self.audio, "script": self.script,
            "audio_path": self.audio_path, "script_path": self.script_path,
            "state": self.state,
            "detected_at": self.detected_at, "stable_at": self.stable_at,
            "ready_at": self.ready_at, "queued_at": self.queued_at,
            "render_started_at": self.render_started_at, "finished_at": self.finished_at,
            "last_attempt_at": self.last_attempt_at,
            "seed": self.seed, "clip_order": list(self.clip_order),
            "attempts": self.attempts, "max_attempts": self.max_attempts,
            "outputs": list(self.outputs), "outputs_verified": self.outputs_verified,
            "last_error": self.last_error, "archive_path": self.archive_path,
            "history": list(self.history[-_HISTORY_LIMIT:]),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        known = {
            "id", "display_id", "audio", "script", "audio_path", "script_path",
            "state", "detected_at", "stable_at", "ready_at", "queued_at",
            "render_started_at", "finished_at", "last_attempt_at",
            "seed", "clip_order", "attempts", "max_attempts",
            "outputs", "outputs_verified", "last_error", "archive_path", "history",
        }
        values = {key: value for key, value in data.items() if key in known}
        values.setdefault("id", str(values.get("display_id", "")))
        if values.get("seed") is not None:
            try:
                values["seed"] = int(values["seed"])
            except (TypeError, ValueError):
                values["seed"] = None
        return cls(**values)

    # ------------------------------------------------------------------ #
    @property
    def is_terminal(self) -> bool:
        return self.state in JobState.TERMINAL

    @property
    def needs_attention(self) -> bool:
        return self.state in {JobState.FAILED, JobState.INTERRUPTED}


# ---------------------------------------------------------------------- #
# Store
# ---------------------------------------------------------------------- #
class JobStore:
    """Thread-safe, atomic JSON job store keyed by deterministic job id."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or default_state_path())
        self._lock = threading.RLock()

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            # Corrupt state file: keep a broken copy for inspection, start clean.
            try:
                self.path.replace(self.path.with_suffix(f".corrupt-{os.getpid()}"))
            except OSError:
                pass
            return {}
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, raw in jobs.items():
            if isinstance(raw, dict) and raw.get("id"):
                result[str(key)] = raw
        return result

    def _write(self, jobs: dict[str, Job]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "jobs": {job.id: job.to_dict() for job in jobs.values()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    # ------------------------------------------------------------------ #
    def all_jobs(self) -> list[Job]:
        with self._lock:
            return [Job.from_dict(raw) for raw in self._read().values()]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            raw = self._read().get(job_id)
            return Job.from_dict(raw) if raw else None

    def upsert(self, job: Job) -> Job:
        """Create or replace a job record and persist atomically."""
        with self._lock:
            jobs = {key: Job.from_dict(raw) for key, raw in self._read().items()}
            jobs[job.id] = job
            self._write(jobs)
            return job

    def update(self, job_id: str, mutate: Callable[[Job], None] | None = None, **changes: Any) -> Job:
        """Apply field changes to an existing job and persist atomically.

        ``mutate`` may perform state transitions (``job.record_state``).
        Raises KeyError when the job does not exist.
        """
        with self._lock:
            jobs = {key: Job.from_dict(raw) for key, raw in self._read().items()}
            if job_id not in jobs:
                raise KeyError(f"Unbekannter Job: {job_id}")
            job = jobs[job_id]
            for key, value in changes.items():
                if not hasattr(job, key):
                    raise AttributeError(f"Job-Feld unbekannt: {key}")
                setattr(job, key, value)
            if mutate is not None:
                mutate(job)
            jobs[job_id] = job
            self._write(jobs)
            return job

    def delete(self, job_id: str) -> bool:
        with self._lock:
            jobs = {key: Job.from_dict(raw) for key, raw in self._read().items()}
            if job_id not in jobs:
                return False
            del jobs[job_id]
            self._write(jobs)
            return True
