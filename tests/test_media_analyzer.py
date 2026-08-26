from pathlib import Path

import pytest

from app.video_merger.media_analyzer import MediaAnalyzer
from tests.conftest import make_clip


@pytest.mark.e2e
def test_ffprobe_metadata_and_audio(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    path = tmp_path / "Clip ä ö ü.mp4"
    make_clip(ffmpeg, path, size="160x90", fps=25, duration=0.6, audio_rate=44100)
    info = MediaAnalyzer(ffprobe).analyze(path)
    assert (info.effective_width, info.effective_height) == (160, 90)
    assert info.fps == pytest.approx(25, abs=0.05)
    assert info.audio.present
    assert info.audio.sample_rate == 44100
    assert info.video_codec == "h264"
    assert not info.is_hdr


def test_rotation_metadata_swaps_effective_dimensions(monkeypatch, tmp_path):
    analyzer = MediaAnalyzer("ffprobe")
    raw = {
        "format": {"duration": "2.0"},
        "streams": [{
            "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
            "avg_frame_rate": "30/1", "pix_fmt": "yuv420p", "sample_aspect_ratio": "1:1",
            "side_data_list": [{"rotation": -90}],
        }],
    }
    monkeypatch.setattr(analyzer, "probe_raw", lambda _path: raw)
    info = analyzer.analyze(tmp_path / "rotated.mp4")
    assert info.rotation == 270
    assert (info.effective_width, info.effective_height) == (1080, 1920)


def test_hdr_is_detected_transparently(monkeypatch, tmp_path):
    analyzer = MediaAnalyzer("ffprobe")
    raw = {
        "format": {"duration": "1.0"},
        "streams": [{
            "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
            "avg_frame_rate": "25/1", "pix_fmt": "yuv420p10le", "sample_aspect_ratio": "1:1",
            "color_primaries": "bt2020", "color_transfer": "smpte2084",
        }],
    }
    monkeypatch.setattr(analyzer, "probe_raw", lambda _path: raw)
    info = analyzer.analyze(tmp_path / "hdr.mov")
    assert info.is_hdr
    assert info.warnings


def test_corrupt_file_has_clear_error(ffmpeg_paths, tmp_path):
    _, ffprobe = ffmpeg_paths
    path = tmp_path / "kaputt.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(Exception, match="Ungültige/beschädigte"):
        MediaAnalyzer(ffprobe).analyze(path)
