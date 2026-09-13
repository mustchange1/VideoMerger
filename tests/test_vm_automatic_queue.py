"""Unit tests: VM Automatic queue – sequencing, duplicates, retries,
persistence and crash recovery."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.vm_automatic.queue import JobQueue
from app.vm_automatic.recovery import recover_interrupted_jobs
from app.vm_automatic.state import Job, JobState, JobStore


def make_job(job_id: str, state: str = JobState.READY, **kwargs) -> Job:
    job = Job(id=job_id, display_id=job_id.upper(), state=state, **kwargs)
    return job


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "state.json")


@pytest.fixture()
def q(store) -> JobQueue:
    return JobQueue(store, max_attempts=3, log=lambda _m: None)


# ---------------------------------------------------------------------- #
def test_sequential_jobs_in_natural_order(q, store):
    for job_id in ("job_010", "job_001", "job_002"):
        store.upsert(make_job(job_id))
    order = [job.id for job in q.queued_jobs()]
    assert order == ["job_001", "job_002", "job_010"]


def test_next_job_pops_oldest_and_marks_running(q, store):
    store.upsert(make_job("job_001"))
    store.upsert(make_job("job_002"))
    first = q.next_job()
    second = q.next_job()
    third = q.next_job()
    assert first.id == "job_001" and first.state == JobState.RUNNING
    assert first.attempts == 1
    assert second.id == "job_002"
    assert third is None
    # persisted state
    assert store.get("job_001").state == JobState.RUNNING


def test_duplicate_jobs_never_queued_twice(q, store):
    store.upsert(make_job("job_001"))
    store.upsert(make_job("job_001"))  # same deterministic identity
    assert len(store.all_jobs()) == 1
    assert q.next_job() is not None
    assert q.next_job() is None  # it is RUNNING now, not re-queued


def test_succeeded_job_is_never_processed_again(q, store):
    job = make_job("job_001", state=JobState.SUCCEEDED)
    store.upsert(job)
    assert q.queued_jobs() == []
    assert q.next_job() is None
    with pytest.raises(ValueError):
        q.retry("job_001")


def test_failure_below_limit_becomes_retry_pending(q, store):
    store.upsert(make_job("job_001"))
    job = q.next_job()
    assert job.state == JobState.RUNNING
    q.failure(job, "boom")
    refreshed = store.get("job_001")
    assert refreshed.state == JobState.RETRY_PENDING
    assert refreshed.attempts == 1
    assert "boom" in refreshed.last_error


def test_failure_exhausts_attempts_to_failed(q, store):
    store.upsert(make_job("job_001"))
    for attempt in range(3):
        job = q.next_job()
        assert job is not None
        q.failure(job, f"boom {attempt}")
        if attempt < 2:
            assert store.get("job_001").state == JobState.RETRY_PENDING
    final = store.get("job_001")
    assert final.state == JobState.FAILED
    assert final.attempts == 3
    # no automatic re-queue: queue is empty
    assert q.queued_jobs() == []


def test_manual_retry_allowed_for_failed_job(q, store):
    store.upsert(make_job("job_001"))
    for _ in range(3):
        q.failure(q.next_job(), "boom")
    assert store.get("job_001").state == JobState.FAILED
    retried = q.retry("job_001")
    assert retried.state == JobState.QUEUED
    # retry+reseed replaces the seed and clears the clip order
    job = q.retry("job_001", reseed=True, new_seed=12345)
    assert job.seed == 12345
    assert job.clip_order == []


def test_success_records_outputs_and_is_terminal(q, store):
    store.upsert(make_job("job_001"))
    job = q.next_job()
    q.success(job, ["out.mp4"], ["a.mp4", "b.mp4"])
    final = store.get("job_001")
    assert final.state == JobState.SUCCEEDED
    assert final.outputs == ["out.mp4"]
    assert final.clip_order == ["a.mp4", "b.mp4"]
    assert final.outputs_verified is True
    assert q.next_job() is None


def test_paused_queue_holds_jobs(q, store):
    store.upsert(make_job("job_001"))
    assert q.pause() is True
    assert q.next_job() is None
    assert q.resume() is True
    assert q.next_job() is not None


def test_persisted_states_survive_store_reload(store, tmp_path):
    q = JobQueue(store, max_attempts=3, log=lambda _m: None)
    store.upsert(make_job("job_001"))
    job = q.next_job()
    q.success(job, ["x.mp4"], ["a"])
    # a brand-new store instance on the same path sees the same state
    reloaded = JobStore(store.path)
    assert reloaded.get("job_001").state == JobState.SUCCEEDED
    assert reloaded.get("job_001").outputs == ["x.mp4"]


# ---------------------------------------------------------------------- #
# Crash recovery
# ---------------------------------------------------------------------- #
def test_crash_recovery_running_to_retry_pending(store):
    store.upsert(make_job("job_001", state=JobState.RUNNING, attempts=1))
    recovered = recover_interrupted_jobs(store, max_attempts=3, log=lambda _m: None)
    assert recovered == ["job_001"]
    job = store.get("job_001")
    assert job.state == JobState.RETRY_PENDING
    assert any(entry["state"] == JobState.INTERRUPTED for entry in job.history)


def test_crash_recovery_running_exhausted_to_failed(store):
    store.upsert(make_job("job_001", state=JobState.RUNNING, attempts=3))
    recover_interrupted_jobs(store, max_attempts=3, log=lambda _m: None)
    assert store.get("job_001").state == JobState.FAILED


def test_crash_recovery_never_touches_succeeded(store):
    store.upsert(make_job("job_001", state=JobState.SUCCEEDED))
    store.upsert(make_job("job_002", state=JobState.RUNNING, attempts=1))
    recovered = recover_interrupted_jobs(store, max_attempts=3, log=lambda _m: None)
    assert recovered == ["job_002"]
    assert store.get("job_001").state == JobState.SUCCEEDED


def test_crash_recovery_idempotent_on_second_startup(store):
    store.upsert(make_job("job_001", state=JobState.RUNNING, attempts=1))
    recover_interrupted_jobs(store, max_attempts=3, log=lambda _m: None)
    # second restart: nothing is RUNNING anymore
    recovered = recover_interrupted_jobs(store, max_attempts=3, log=lambda _m: None)
    assert recovered == []
    assert store.get("job_001").state == JobState.RETRY_PENDING
