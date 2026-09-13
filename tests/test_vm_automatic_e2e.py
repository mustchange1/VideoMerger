"""REAL end-to-end automation flow (spec §46/§49/§50) using the EXISTING
VideoMerger render pipeline.

Requires FFmpeg (``ffmpeg_paths`` fixture; tests are marked ``e2e``).
The job is a voiceover-only job (``require_script=False``) so the full real
render path (VideoMerger engine + MainProjectEngine + FFmpeg) runs without
the downloaded ASR model – subtitles stay OFF, exactly as the plain
VideoMerger pipeline behaves for voiceover-only input. Subtitle rendering
itself is covered by the existing VideoMerger regression suite.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from app.vm_automatic.config import VMAutomaticConfig
from app.vm_automatic.controller import VMAutomaticController
from app.vm_automatic.state import JobState
from app.video_merger.models import ExportSettings
from app.video_merger.project_order import GeneratedOutputStore, ProjectOrderStore
from app.video_merger.settings_store import SettingsStore
from tests.conftest import make_clip

pytestmark = pytest.mark.e2e


def _wait_until(predicate, timeout: float = 180.0, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _job_state(controller, job_id: str) -> str | None:
    job = controller.store.get(job_id)
    return job.state if job else None


def _make_voiceover(ffmpeg: Path, path: Path, seconds: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=850:sample_rate=44100:duration={seconds}",
        "-c:a", "pcm_s16le", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


POOL_NAMES = [f"{index:02d}.mp4" for index in range(1, 6)]


@pytest.fixture()
def e2e_env(tmp_path: Path, ffmpeg_paths):
    # NOTE: total pool duration must exceed the voiceover duration – the
    # existing VideoMerger pipeline fits the video to the audio ("cut" mode).
    # 30 fps like the standard VideoMerger test geometry.
    ffmpeg, ffprobe = ffmpeg_paths
    pool = tmp_path / "pool"
    pool.mkdir()
    for name in POOL_NAMES:
        make_clip(ffmpeg, pool / name, size="320x180", fps=30, duration=1.0, color="red", audio_rate=48000)
    watch = tmp_path / "watch"
    watch.mkdir()
    out = tmp_path / "output"
    # a fast, explicit master configuration (kept in tmp – the real master
    # config in the repo is NEVER touched by VM Automatic)
    master = ExportSettings(
        aspect="16:9", resolution="320x180", transition_duration=0.1,
        encoding="CPU", quality_preset="fast", crf=30, preset="ultrafast",
        normalize_audio=False, final_pause=0.5,
    )
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.save(master)
    return {
        "ffmpeg": ffmpeg, "ffprobe": ffprobe, "tmp": tmp_path,
        "pool": pool, "watch": watch, "out": out,
        "settings_store": settings_store,
    }


def make_e2e_controller(env: dict) -> VMAutomaticController:
    tmp = env["tmp"]
    config = VMAutomaticConfig.defaults(root=tmp)
    config.watch_folder = str(env["watch"])
    config.output_folder = str(env["out"])
    config.clip_pool_folder = str(env["pool"])
    config.require_script = False  # voiceover-only real render
    config.stability_seconds = 1.5
    config.stability_interval_seconds = 0.3
    config.debounce_seconds = 0.5
    config.reconciliation_seconds = 3600
    controller = VMAutomaticController(
        config,
        settings_store=env["settings_store"],
        state_path=tmp / "state.json",
        order_store=ProjectOrderStore(tmp / "order.json"),
        output_store=GeneratedOutputStore(tmp / "generated_outputs.json"),
    )
    return controller


def test_real_automation_flow_with_existing_renderer(e2e_env):
    env = e2e_env
    controller = make_e2e_controller(env)
    controller.start()
    try:
        # --- job 1 ------------------------------------------------------ #
        _make_voiceover(env["ffmpeg"], env["watch"] / "E2E_A.wav")
        job_a = _wait_until(lambda: _job_state(controller, "e2e_a") == JobState.SUCCEEDED, timeout=240)
        assert job_a, controller.store.get("e2e_a")
        job = controller.store.get("e2e_a")
        assert job.outputs_verified
        assert len(job.outputs) >= 1
        main_video = env["out"] / "E2E_A" / "MainVideo_16x9.mp4"
        assert main_video.is_file() and main_video.stat().st_size > 0
        assert job.seed is not None
        assert len(job.clip_order) >= 2  # randomized order recorded
        assert sorted(job.clip_order) == POOL_NAMES

        # --- job 2: new random order ------------------------------------ #
        _make_voiceover(env["ffmpeg"], env["watch"] / "E2E_B.wav")
        assert _wait_until(lambda: _job_state(controller, "e2e_b") == JobState.SUCCEEDED, timeout=240)
        job_b = controller.store.get("e2e_b")
        assert job_b.seed != job.seed
        assert job_b.clip_order != job.clip_order
        assert (env["out"] / "E2E_B" / "MainVideo_16x9.mp4").is_file()

        # --- duplicate protection: rescan re-runs nothing ---------------- #
        calls_before = {
            "a": controller.store.get("e2e_a").attempts,
            "b": controller.store.get("e2e_b").attempts,
        }
        outputs_a_before = list(controller.store.get("e2e_a").outputs)
        controller.scan_now()
        assert _wait_until(lambda: controller.last_scan_summary != "")
        time.sleep(5.0)
        assert controller.store.get("e2e_a").attempts == calls_before["a"] == 1
        assert controller.store.get("e2e_b").attempts == calls_before["b"] == 1
        assert controller.store.get("e2e_a").outputs == outputs_a_before

        # --- restart: success persists, third job auto-processes --------- #
        controller.stop(cancel_running=False)
        controller2 = make_e2e_controller(env)
        controller2.start()
        try:
            assert controller2.store.get("e2e_a").state == JobState.SUCCEEDED
            assert controller2.store.get("e2e_b").state == JobState.SUCCEEDED
            _make_voiceover(env["ffmpeg"], env["watch"] / "E2E_C.wav")
            assert _wait_until(
                lambda: _job_state(controller2, "e2e_c") == JobState.SUCCEEDED, timeout=240
            )
            assert controller2.store.get("e2e_c").attempts == 1
            assert (env["out"] / "E2E_C" / "MainVideo_16x9.mp4").is_file()
            # still no re-run of the first jobs
            assert controller2.store.get("e2e_a").attempts == 1
            assert controller2.store.get("e2e_b").attempts == 1
        finally:
            controller2.stop(cancel_running=False)
    finally:
        if controller.status != "STOPPED":
            controller.stop(cancel_running=False)


def test_slow_copy_is_not_started_early(e2e_env):
    """Spec §49: a slowly copied file must not start the pipeline before it
    is fully copied and stable (COPYING → WAITING_FOR_STABILITY → STABLE →
    READY → QUEUED → RUNNING → SUCCEEDED)."""
    env = e2e_env
    controller = make_e2e_controller(env)
    controller.start()
    try:
        import sys

        # a valid WAV payload (≈ 2 s of tone), copied in slow chunks
        source = env["tmp"] / "E2E_D_source.wav"
        _make_voiceover(env["ffmpeg"], source, seconds=2.0)
        target = env["watch"] / "E2E_D.wav"
        target.write_bytes(b"")  # created while copying…
        copier = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\nfrom pathlib import Path\n"
             "src, target = Path(sys.argv[1]), Path(sys.argv[2])\n"
             "payload = src.read_bytes()\n"
             "chunk = max(1, len(payload) // 32)\n"
             "with target.open('wb') as h:\n"
             "    for offset in range(0, len(payload), chunk):\n"
             "        h.write(payload[offset:offset + chunk]); h.flush(); time.sleep(0.125)\n",
             str(source), str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # mid-copy: the job may exist but must NOT be running/succeeded yet
        time.sleep(2.0)
        assert _wait_until(lambda: controller.store.get("e2e_d") is not None)
        mid_state = _job_state(controller, "e2e_d")
        assert mid_state in {JobState.NEW, JobState.WAITING_FOR_STABILITY}, mid_state
        copier.wait(timeout=30)
        # only after copy + stability window does it run to success
        assert _wait_until(lambda: _job_state(controller, "e2e_d") == JobState.SUCCEEDED, timeout=180)
        job = controller.store.get("e2e_d")
        states = [entry["state"] for entry in job.history]
        assert JobState.WAITING_FOR_STABILITY in states
        assert (env["out"] / "E2E_D" / "MainVideo_16x9.mp4").is_file()
    finally:
        controller.stop(cancel_running=False)
