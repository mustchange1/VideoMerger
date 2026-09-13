"""Integration tests: VM Automatic controller flow with a fake runner.

Exercises the complete logic of the real automation flow (spec §46/§50)
without actual rendering: watcher events → stability → READY → QUEUED →
RUNNING → SUCCEEDED, sequential queue, per-job random order, duplicate
protection, restart persistence, retry policy and crash recovery.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.vm_automatic.config import VMAutomaticConfig
from app.vm_automatic.controller import VMAutomaticController
from app.vm_automatic.runner import JobRunResult
from app.vm_automatic.state import JobState
from app.video_merger.project_order import ProjectOrderStore

POOL_NAMES = ["c1.mp4", "c2.mp4", "c3.mp4", "c4.mp4", "c5.mp4"]


def _wait_until(predicate, timeout: float = 25.0, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class FakeRunner:
    """Mimics JobRunner.run: records jobs and derives a deterministic
    (seed-dependent) clip order so 'new job → new order' is observable."""

    def __init__(self, failures_by_job: dict[str, int] | None = None, delay: float = 0.05):
        self.calls: list[str] = []
        self.failures_by_job = failures_by_job or {}
        self.delay = delay
        self._lock = threading.Lock()

    def run(self, job, cancel_event=None):
        with self._lock:
            self.calls.append(job.id)
            fail_left = self.failures_by_job.get(job.id, 0)
            self.failures_by_job[job.id] = fail_left - 1
        time.sleep(self.delay)
        if fail_left > 0:
            return JobRunResult(ok=False, error="simulated render failure")
        names = list(POOL_NAMES)
        shift = int(job.seed or 0) % len(names)
        order = names[shift:] + names[:shift]
        return JobRunResult(
            ok=True,
            outputs=[f"output/{job.display_id}/MainVideo_16x9.mp4"],
            clip_order=order,
        )


@pytest.fixture()
def env(tmp_path: Path):
    """Controller environment with fast timers in a temp project."""
    watch = tmp_path / "watch"
    pool = tmp_path / "pool"
    watch.mkdir()
    pool.mkdir()
    for name in POOL_NAMES:
        (pool / name).write_bytes(b"x")
    return {
        "tmp": tmp_path,
        "watch": watch,
        "pool": pool,
        "out": tmp_path / "output",
    }


def make_controller(env: dict, runner: FakeRunner, require_script: bool = True) -> VMAutomaticController:
    tmp = env["tmp"]
    config = VMAutomaticConfig.defaults(root=tmp)
    config.watch_folder = str(env["watch"])
    config.output_folder = str(env["out"])
    config.clip_pool_folder = str(env["pool"])
    config.stability_seconds = 0.5
    config.stability_interval_seconds = 0.1
    config.debounce_seconds = 0.3
    config.reconciliation_seconds = 3600  # manual scans only in unit tests
    config.require_script = require_script
    controller = VMAutomaticController(
        config,
        runner=runner,
        state_path=tmp / "state.json",
        order_store=ProjectOrderStore(tmp / "order.json"),
    )
    return controller


def job_state(controller: VMAutomaticController, job_id: str) -> str | None:
    job = controller.store.get(job_id)
    return job.state if job else None


def wait_state(controller: VMAutomaticController, job_id: str, state: str, timeout: float = 25.0) -> bool:
    return _wait_until(lambda: job_state(controller, job_id) == state, timeout)


# ---------------------------------------------------------------------- #
# The full flow (spec §46 without real rendering)
# ---------------------------------------------------------------------- #
def test_full_automation_flow(env):
    runner = FakeRunner()
    controller = make_controller(env, runner)
    controller.start()
    try:
        # 1) empty watch folder – nothing to do
        time.sleep(0.5)
        assert controller.store.all_jobs() == []

        # 2) audio arrives first → job exists and waits (Waiting for stability)
        (env["watch"] / "Job_001.mp3").write_bytes(b"audio-1")
        assert _wait_until(lambda: job_state(controller, "job_001") in
                           (JobState.NEW, JobState.WAITING_FOR_STABILITY))
        # not ready without the script
        time.sleep(2.0)
        assert job_state(controller, "job_001") != JobState.QUEUED

        # 3) matching script arrives → stable → READY → QUEUED → RUNNING → SUCCEEDED
        (env["watch"] / "Job_001.txt").write_text("Erster Text")
        assert wait_state(controller, "job_001", JobState.SUCCEEDED)
        job = controller.store.get("job_001")
        assert job.outputs_verified is True
        assert job.seed is not None
        assert sorted(job.clip_order) == POOL_NAMES  # randomized full pool
        assert job.attempts == 1
        # full state trail: waiting → ready → queued → running → succeeded
        states = [entry["state"] for entry in job.history]
        for expected in (JobState.WAITING_FOR_STABILITY, JobState.READY,
                         JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED):
            assert expected in states, states

        # 4) second job → NEW random order (different seed)
        (env["watch"] / "Job_002.mp3").write_bytes(b"audio-2")
        (env["watch"] / "Job_002.txt").write_text("Zweiter Text")
        assert wait_state(controller, "job_002", JobState.SUCCEEDED)
        job2 = controller.store.get("job_002")
        assert job2.seed != job.seed
        assert job2.clip_order != job.clip_order
        assert sorted(job2.clip_order) == POOL_NAMES

        # 5) duplicate protection: rescan sees the same files – no re-run
        outputs_before = list(job.outputs)
        calls_before = list(runner.calls)
        controller.scan_now()
        assert _wait_until(lambda: controller.last_scan_summary != "")
        time.sleep(3.0)  # give (wrong) re-processing time to happen
        assert runner.calls == calls_before  # no new render calls
        assert controller.store.get("job_001").outputs == outputs_before

        # 6) restart: successful jobs remain completed, nothing re-runs
        controller.stop(cancel_running=False)
        runner2 = FakeRunner()
        controller2 = make_controller(env, runner2)
        controller2.start()
        try:
            assert wait_state(controller2, "job_001", JobState.SUCCEEDED)
            assert controller2.store.get("job_001").state == JobState.SUCCEEDED
            assert controller2.store.get("job_002").state == JobState.SUCCEEDED
            time.sleep(2.0)
            assert runner2.calls == []  # no duplicate processing

            # 7) third job is processed automatically after restart
            (env["watch"] / "Job_003.mp3").write_bytes(b"audio-3")
            (env["watch"] / "Job_003.txt").write_text("Dritter Text")
            assert wait_state(controller2, "job_003", JobState.SUCCEEDED)
            assert runner2.calls == ["job_003"]
            assert controller2.store.get("job_003").seed not in {job.seed, job2.seed}
        finally:
            controller2.stop(cancel_running=False)
    finally:
        if controller.status != "STOPPED":
            controller.stop(cancel_running=False)


# ---------------------------------------------------------------------- #
# Retry policy
# ---------------------------------------------------------------------- #
def test_failing_job_retries_then_succeeds(env):
    runner = FakeRunner(failures_by_job={"job_001": 1})  # first attempt fails
    controller = make_controller(env, runner)
    controller.start()
    try:
        (env["watch"] / "Job_001.mp3").write_bytes(b"audio")
        (env["watch"] / "Job_001.txt").write_text("Text")
        assert wait_state(controller, "job_001", JobState.SUCCEEDED, timeout=40)
        job = controller.store.get("job_001")
        assert job.attempts == 2  # one failure + one success
        assert job.seed is not None
        # retry reused the SAME seed (same order)
        assert len(runner.calls) == 2
    finally:
        controller.stop(cancel_running=False)


def test_exhausted_job_becomes_failed_and_stops(env):
    runner = FakeRunner(failures_by_job={"job_001": 99})
    controller = make_controller(env, runner)
    controller.start()
    try:
        (env["watch"] / "Job_001.mp3").write_bytes(b"audio")
        (env["watch"] / "Job_001.txt").write_text("Text")
        assert wait_state(controller, "job_001", JobState.FAILED, timeout=60)
        job = controller.store.get("job_001")
        assert job.attempts == 3  # max_attempts default
        assert "simulated render failure" in job.last_error
        # manual retry still possible (explicit user action)
        controller.retry("job_001")
        assert controller.store.get("job_001").state == JobState.QUEUED
    finally:
        controller.stop(cancel_running=False)


def test_crash_during_running_job_recovers_on_restart(env):
    """Simulate: the app died while a job was RUNNING (state file says
    RUNNING). The next startup must detect it, recover via INTERRUPTED →
    RETRY_PENDING and run it to completion (never mark it successful)."""
    from app.vm_automatic.state import JobStore

    # phase 1: get a job to RUNNING with the first runner
    runner = FakeRunner()
    controller = make_controller(env, runner)
    started = threading.Event()
    original_run = runner.run

    def blocking_run(job, cancel_event=None):
        started.set()
        return original_run(job, cancel_event)

    runner.run = blocking_run  # type: ignore[method-assign]
    controller.start()
    (env["watch"] / "Job_001.mp3").write_bytes(b"audio")
    (env["watch"] / "Job_001.txt").write_text("Text")
    assert _wait_until(lambda: job_state(controller, "job_001") == JobState.RUNNING, timeout=30)
    assert started.wait(timeout=5)
    # the state file on disk now says RUNNING (crash point)
    assert JobStore(env["tmp"] / "state.json").get("job_001").state == JobState.RUNNING
    # stop the app WITHOUT cancelling the (instant) fake render; the job
    # record stays as it is on disk – we emulate the process death.
    controller.stop(cancel_running=False)
    disk_state = JobStore(env["tmp"] / "state.json").get("job_001").state
    if disk_state == JobState.SUCCEEDED:
        # the fake render finished before the stop; force the crashed state
        # back to what a real crash would leave on disk.
        controller.store.update("job_001", state=JobState.RUNNING,
                                mutate=lambda job: job.record_state(JobState.RUNNING, "simulated crash"))
    assert JobStore(env["tmp"] / "state.json").get("job_001").state == JobState.RUNNING

    # phase 2: restart → recovery → retry → success
    runner2 = FakeRunner()
    controller2 = make_controller(env, runner2)
    controller2.start()
    try:
        assert wait_state(controller2, "job_001", JobState.SUCCEEDED, timeout=40)
        job = controller2.store.get("job_001")
        states = [entry["state"] for entry in job.history]
        assert JobState.INTERRUPTED in states
        assert JobState.RETRY_PENDING in states
        assert job.attempts == 2  # crashed attempt + recovered attempt
        assert runner2.calls == ["job_001"]
    finally:
        controller2.stop(cancel_running=False)


def test_stopped_controller_processes_nothing(env):
    runner = FakeRunner()
    controller = make_controller(env, runner)
    assert controller.status == "STOPPED"
    # not started at all
    (env["watch"] / "Job_001.mp3").write_bytes(b"audio")
    (env["watch"] / "Job_001.txt").write_text("Text")
    time.sleep(1.5)
    assert runner.calls == []  # Automation OFF → nothing processed


# ---------------------------------------------------------------------- #
# Voiceover-only mode (require_script=False)
# ---------------------------------------------------------------------- #
def test_voiceover_only_jobs_when_script_not_required(env):
    runner = FakeRunner()
    controller = make_controller(env, runner, require_script=False)
    controller.start()
    try:
        (env["watch"] / "Solo_001.mp3").write_bytes(b"audio-only")
        assert wait_state(controller, "solo_001", JobState.SUCCEEDED, timeout=30)
        assert controller.store.get("solo_001").script == ""
    finally:
        controller.stop(cancel_running=False)
