"""Unit tests: VM Automatic output validation (spec §28).

A job becomes successful only when the expected outputs exist, are
non-zero, readable and stable – never merely because a render started.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.vm_automatic.runner import verify_outputs
from app.video_merger.errors import VideoMergerError


def test_success_verification_passes_for_good_files(tmp_path):
    good = tmp_path / "FinalVideo_16x9.mp4"
    good.write_bytes(b"video-data" * 100)
    srt = tmp_path / "FinalVideo_16x9.srt"
    srt.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    logs: list[str] = []
    outputs = verify_outputs([good, srt], logs.append, interval=0.1)
    assert outputs == [str(good), str(srt)]
    assert any("Output-Validierung OK" in line for line in logs)


def test_missing_output_fails(tmp_path):
    good = tmp_path / "FinalVideo_16x9.mp4"
    good.write_bytes(b"data")
    with pytest.raises(VideoMergerError) as excinfo:
        verify_outputs([good, tmp_path / "missing.srt"], lambda _m: None, interval=0.05)
    assert "feHLT" not in str(excinfo.value)
    assert "fehlt" in str(excinfo.value)


def test_zero_byte_output_fails(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(VideoMergerError) as excinfo:
        verify_outputs([empty], lambda _m: None, interval=0.05)
    assert "leer" in str(excinfo.value)


def test_unstable_output_fails(tmp_path):
    changing = tmp_path / "unstable.mp4"
    changing.write_bytes(b"part1")

    def modify_later():
        time.sleep(0.15)
        changing.write_bytes(b"part1-CHANGED")

    thread = threading.Thread(target=modify_later, daemon=True)
    thread.start()
    with pytest.raises(VideoMergerError) as excinfo:
        verify_outputs([changing], lambda _m: None, interval=0.3)
    thread.join(timeout=2)
    assert "stabil" in str(excinfo.value)


def test_none_paths_are_skipped(tmp_path):
    good = tmp_path / "video.mp4"
    good.write_bytes(b"payload")
    outputs = verify_outputs([good, None], lambda _m: None, interval=0.05)
    assert outputs == [str(good)]
