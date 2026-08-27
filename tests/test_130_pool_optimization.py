"""1.3.0 – Large Video Pool: safe optimizations only (no redesign).

The 1.2.4 pool behavior is already very good and stays authoritative. These
tests pin the 1.3.0 optimizations:

* ``compute_pool_status`` derives required/selected from ONE prefix pass
  (no second transition computation, no re-sorting, identical results);
* stretch/speed parameters reuse the same single pass;
* the existing media-probe caches mean repeated status updates never
  re-analyze files (stat-keyed cache behavior is asserted).
"""

from __future__ import annotations

import app.video_merger.video_pool as video_pool
from app.video_merger.media_analyzer import MediaAnalyzer
from app.video_merger.video_pool import compute_pool_status
from tests.conftest import fake_media


def _pool(count: int, duration: float = 2.0) -> list:
    return [fake_media(f"pool{index:03d}.mp4", duration=duration) for index in range(count)]


def test_pool_status_computes_prefixes_exactly_once(monkeypatch):
    media = _pool(200)
    calls = {"n": 0}
    original = video_pool.prefix_durations

    def counting(durations, transition_duration, fps):
        calls["n"] += 1
        return original(durations, transition_duration, fps)

    monkeypatch.setattr(video_pool, "prefix_durations", counting)
    status = compute_pool_status(
        media, 70.0, 0.5, 30.0, "hold",
        duration_fit_mode="stretch", max_stretch_percent=10.0,
        playback_rate=1.0,
    )
    assert calls["n"] == 1  # one O(n) pass serves required AND stretch mirror
    assert status.total == 200
    assert status.covered and status.mode == "exact"
    assert 0 < status.required < 200


def test_pool_status_results_are_unchanged_by_the_optimization():
    media = _pool(120, duration=2.5)
    status = compute_pool_status(media, 40.0, 0.5, 30.0, "hold")
    # Identical numbers to the documented 1.2.4 selection semantics.
    durations = [max(item.duration, max(0.12, 3.0 / 30.0)) for item in media]
    prefixes = video_pool.prefix_durations(durations, 0.5, 30.0)
    expected = next(i + 1 for i, value in enumerate(prefixes) if value >= 40.0 - 1e-6)
    assert status.required == expected == status.selected
    assert status.unused == 120 - expected


def test_pool_status_never_resorts_the_active_order():
    order = [f"{name}.mp4" for name in ("C", "A", "D", "B")]
    media = [fake_media(name, duration=2.0) for name in order]
    compute_pool_status(media, 3.0, 0.5, 30.0, "hold")
    assert [item.path.name for item in media] == order  # input order untouched


def test_repeated_status_updates_never_reprobe_media(tmp_path, monkeypatch):
    """GUI status refreshes are pure metadata math: no ffprobe subprocess is
    started for repeated pool-status updates (the analyzer cache is stat-keyed
    and the status computation never touches the files)."""
    import subprocess

    def forbidden(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("Pool-Status darf keine neue FFprobe-Analyse starten.")

    monkeypatch.setattr(subprocess, "run", forbidden)
    media = _pool(150)
    for _ in range(5):
        compute_pool_status(media, 60.0, 0.5, 30.0, "hold")


def test_media_analyzer_cache_is_stat_keyed_and_reused(tmp_path):
    """The persistent metadata cache: an unchanged file is never probed twice."""
    import json

    cache_file = tmp_path / "media_analysis.json"
    analyzer = MediaAnalyzer("ffprobe", cache_path=cache_file)
    entry = {
        "signature": "missing",
        "media": {
            "duration": 2.0, "width": 320, "height": 180,
            "effective_width": 320, "effective_height": 180,
            "fps": 30.0, "fps_fraction": "30/1", "video_codec": "h264",
            "pixel_format": "yuv420p", "sar": "1:1", "dar": "16:9",
            "rotation": 0, "audio": {"present": True, "codec": "aac",
                                     "sample_rate": 48000, "channels": 2,
                                     "channel_layout": "stereo"},
            "is_hdr": False, "color_primaries": "", "color_transfer": "",
            "color_space": "", "warnings": [], "source_duration": 0.0,
            "is_generated_quote": False,
        },
    }
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake")
    stat = path.stat()
    entry["signature"] = f"{stat.st_size}:{stat.st_mtime_ns}"
    cache_file.write_text(
        json.dumps({"schema": 2, "entries": {str(path): entry}}), encoding="utf-8"
    )
    analyzer = MediaAnalyzer("ffprobe", cache_path=cache_file)
    info = analyzer.analyze(path)  # must come from the cache, without ffprobe
    assert analyzer.last_cache_hit is True
    assert info.duration == 2.0
