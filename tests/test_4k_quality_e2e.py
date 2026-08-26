from __future__ import annotations

import pytest

from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from tests.conftest import make_clip


@pytest.mark.e2e
def test_real_4k_source_is_not_silently_downscaled(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    source = tmp_path / "4k_source.mp4"
    make_clip(ffmpeg, source, size="3840x2160", duration=.2, color="navy", audio_rate=None)
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([source])
    settings = ExportSettings(
        aspect="16:9", resolution="Auto", encoding="CPU", preset="fast",
        crf=28, normalize_audio=False,
    )
    result = MainProjectEngine(engine).create_main(media, settings, tmp_path / "out")
    assert result.report.ok
    assert (result.report.width, result.report.height) == (3840, 2160)
    assert result.report.fps == pytest.approx(30, abs=.1)
