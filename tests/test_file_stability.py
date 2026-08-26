from __future__ import annotations

import pytest

from app.video_merger.file_stability import wait_for_files_stable


def test_stable_nonempty_file_passes(tmp_path):
    path = tmp_path / "stable video.mp4"
    path.write_bytes(b"complete")
    messages: list[str] = []
    wait_for_files_stable([path], log=messages.append, interval=0.01, timeout=1)
    assert any("Dateistabilität: OK" in message for message in messages)


def test_empty_or_partial_file_is_rejected(tmp_path):
    path = tmp_path / "still-copying.mp4"
    path.touch()
    with pytest.raises(Exception, match="leer oder noch nicht vollständig"):
        wait_for_files_stable([path], interval=0.01, timeout=0.1)
