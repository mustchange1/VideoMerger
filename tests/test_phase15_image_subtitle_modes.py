"""Focused regression coverage for Image Insertion and subtitle output modes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.errors import VideoMergerError
from app.video_merger.image_insertion import image_insertion_path
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import AudioInfo, ExportSettings, MediaInfo, ResolvedExport, ValidationReport
from app.video_merger.render_cache import stage1_fingerprint, stage2_fingerprint
from app.video_merger.settings_store import SettingsStore
from app.video_merger.subtitle_modes import (
    SUBTITLE_OUTPUT_BURNED_ONLY,
    SUBTITLE_OUTPUT_COMBINED,
    SUBTITLE_OUTPUT_WITHOUT,
)


def _media(path: Path, *, duration: float = 3.0, audio: bool = False) -> MediaInfo:
    return MediaInfo(
        path=path, duration=duration, width=1920, height=1080,
        effective_width=1920, effective_height=1080, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="16:9", source_duration=duration,
        audio=AudioInfo(present=audio, codec="aac" if audio else "", sample_rate=48000 if audio else 0,
                        channels=2 if audio else 0),
    )


def _image(path: Path, fit: str = "fit", zoom: int = 100, look: str = "natural") -> MediaInfo:
    return MediaInfo(
        path=path, duration=4.0, width=1200, height=800,
        effective_width=1200, effective_height=800, fps=30.0,
        fps_fraction="30/1", video_codec="image", pixel_format="yuv420p",
        sar="1:1", dar="", source_duration=4.0,
        is_image_insertion=True, image_fit_mode=fit, image_zoom=zoom, image_filter=look,
    )


def _resolved(count: int = 2) -> ResolvedExport:
    durations = [4.0] * count
    transitions = [0.5] * (count - 1)
    return ResolvedExport(
        1920, 1080, 30.0, "30", durations, transitions,
        sum(durations) - sum(transitions),
    )


def test_image_insertion_defaults_persist_and_unsupported_formats_are_rejected(tmp_path: Path) -> None:
    settings = ExportSettings()
    assert settings.image_enabled is False
    assert settings.image_position == "after_intro"
    assert settings.image_duration == 4.0
    assert settings.image_transition_type == "cross_dissolve"
    assert settings.image_transition_duration == 1.0
    assert settings.image_fit_mode == "fit"
    assert settings.image_zoom == 100
    assert settings.image_filter == "natural"
    assert settings.subtitle_output_mode == SUBTITLE_OUTPUT_COMBINED

    image = tmp_path / "poster.webp"
    image.write_bytes(b"fixture")
    settings = replace(
        settings, image_enabled=True, image_path=str(image), image_position="before_outro",
        image_duration=7.5, image_transition_type="film_dissolve",
        image_transition_duration=0.75, image_fit_mode="crop",
        image_zoom=125, image_filter="dark_editorial",
        subtitle_output_mode=SUBTITLE_OUTPUT_BURNED_ONLY,
    )
    path = tmp_path / "settings.json"
    SettingsStore(path).save(settings)
    loaded = SettingsStore(path).load()
    assert loaded.image_enabled is True
    assert loaded.image_path == str(image)
    assert loaded.image_position == "before_outro"
    assert loaded.image_duration == 7.5
    assert loaded.image_transition_type == "film_dissolve"
    assert loaded.image_transition_duration == 0.75
    assert loaded.image_fit_mode == "crop"
    assert loaded.image_zoom == 125
    assert loaded.image_filter == "dark_editorial"
    assert loaded.subtitle_output_mode == SUBTITLE_OUTPUT_BURNED_ONLY

    unsupported = tmp_path / "poster.bmp"
    unsupported.write_bytes(b"fixture")
    with pytest.raises(VideoMergerError, match="Nicht unterstütztes Format"):
        image_insertion_path(unsupported)


def test_image_graph_is_silent_aspect_safe_filtered_and_looped(tmp_path: Path) -> None:
    image = _image(tmp_path / "poster.jpg", fit="fit", zoom=125, look="cinematic")
    settings = ExportSettings(
        workflow_stage="outro", resolution="1920x1080", normalize_audio=False,
        image_fit_mode="fit", image_zoom=125, image_filter="cinematic",
        stage2_audio_modes=["mute", "original"],
    )
    built = FFmpegCommandBuilder("ffmpeg").build(
        [image, _media(tmp_path / "main.mp4")], settings, _resolved(), tmp_path / "out.mp4"
    )
    assert built.command[3:7] == ["-loop", "1", "-i", str(image.path)]
    assert "force_original_aspect_ratio=decrease" in built.filter_graph
    assert "eq=contrast=1.08:saturation=1.08" in built.filter_graph
    assert "anullsrc=r=48000:cl=stereo:d=4" in built.filter_graph
    assert "[0:a:0]" not in built.filter_graph
    assert "subtitles=" not in built.filter_graph


def test_image_fit_modes_and_cache_separation(tmp_path: Path) -> None:
    for fit, expression in (
        ("fit", "force_original_aspect_ratio=decrease"),
        ("fill", "force_original_aspect_ratio=increase"),
        ("crop", "crop=w=min(iw\\,ih*1920/1080)"),
    ):
        graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
            [_image(tmp_path / "poster.png", fit=fit)],
            ExportSettings(workflow_stage="outro", resolution="1920x1080", normalize_audio=False),
            ResolvedExport(1920, 1080, 30.0, "30", [4.0], [], 4.0),
        )
        assert expression in graph
        assert "setsar=1" in graph

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip")
    media = [_media(clip)]
    resolved = ResolvedExport(1920, 1080, 30.0, "30", [3.0], [], 3.0)
    first, _ = stage1_fingerprint(media, ExportSettings(), resolved)
    changed, _ = stage1_fingerprint(
        media,
        ExportSettings(image_enabled=True, image_path=str(tmp_path / "poster.png"), image_filter="film"),
        resolved,
    )
    # Stage 1 remains reusable when only the Stage-2 Add Image changes. Its
    # independent Stage-2 fingerprint below carries the invalidation instead.
    assert changed == first


def test_stage2_image_order_is_single_and_mute_for_every_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    intro, main, outro, image = (
        tmp_path / name for name in ("intro.mp4", "main.mp4", "outro.mp4", "image.jpeg")
    )
    for path in (intro, main, outro, image):
        path.write_bytes(b"fixture")
    engine = type("FakeEngine", (), {})()
    engine.ffprobe_path = Path("ffprobe")
    engine.analyzer = type("Analyzer", (), {})()
    engine.analyzer.probe_raw = lambda path: {
        "streams": [{"codec_type": "video", "width": 800, "height": 1200}]
    }
    captured: list[tuple[list[MediaInfo], ExportSettings]] = []
    engine.analyze = lambda paths, log=None: [_media(path) for path in paths]
    engine.make_plan = lambda media, settings, log=None: (
        captured.append((list(media), settings)),
        _resolved(len(media)),
    )[1]
    engine.export = lambda media, settings, resolved, output, **kwargs: ValidationReport(
        True, [], Path(output), resolved.expected_duration, 1920, 1080, 30.0, True, True
    )

    for has_intro in (False, True):
        for has_outro in (False, True):
            for position in ("after_intro", "before_outro", "before_main", "after_main"):
                settings = ExportSettings(
                    workflow_stage="outro", main_video_path=str(main),
                    intro_path=str(intro) if has_intro else "",
                    outro_path=str(outro) if has_outro else "",
                    image_enabled=True, image_path=str(image), image_position=position,
                )
                MainProjectEngine(engine).add_outro(settings, tmp_path)
                media, stage2 = captured[-1]
                names = [item.path.name for item in media]
                image_index = names.index("image.jpeg")
                expected_index = (
                    1 if has_intro else 0
                ) if position in {"after_intro", "before_main"} else (
                    len(names) - 2 if has_outro else len(names) - 1
                )
                assert image_index == expected_index
                assert names.count("image.jpeg") == 1
                assert sum(item.is_image_insertion for item in media) == 1
                assert stage2.stage2_roles[image_index] == "image"
                assert stage2.stage2_audio_modes[image_index] == "mute"


def test_add_image_position_aliases_and_content_fingerprint(tmp_path: Path) -> None:
    from app.video_merger.image_insertion import normalize_image_position

    assert normalize_image_position("After Intro") == "before_main"
    assert normalize_image_position("Before Main Video") == "before_main"
    assert normalize_image_position("Before Outro") == "after_main"
    assert normalize_image_position("After Main Video") == "after_main"

    image = tmp_path / "add-image.png"
    image.write_bytes(b"first image bytes")
    media = [_media(tmp_path / "main.mp4")]
    baseline, payload = stage2_fingerprint(
        media,
        ExportSettings(image_enabled=True, image_path=str(image)),
        ResolvedExport(1920, 1080, 30.0, "30", [3.0], [], 3.0),
    )
    assert payload["settings"]["image_path"]["sha256"]
    for changed in (
        replace(ExportSettings(image_enabled=True, image_path=str(image)), image_position="after_main"),
        replace(ExportSettings(image_enabled=True, image_path=str(image)), image_duration=6.0),
        replace(ExportSettings(image_enabled=True, image_path=str(image)), image_transition_type="film_dissolve"),
        replace(ExportSettings(image_enabled=True, image_path=str(image)), image_transition_duration=0.5),
        replace(ExportSettings(image_enabled=True, image_path=str(image)), image_fit_mode="fill"),
        replace(ExportSettings(image_enabled=True, image_path=str(image)), image_zoom=125),
        replace(ExportSettings(image_enabled=True, image_path=str(image)), image_filter="moody"),
        replace(ExportSettings(image_enabled=False, image_path=str(image)), image_enabled=False),
    ):
        changed_digest, _ = stage2_fingerprint(
            media, changed, ResolvedExport(1920, 1080, 30.0, "30", [3.0], [], 3.0)
        )
        assert changed_digest != baseline

    image.write_bytes(b"second image bytes")
    content_changed, _ = stage2_fingerprint(
        media,
        ExportSettings(image_enabled=True, image_path=str(image)),
        ResolvedExport(1920, 1080, 30.0, "30", [3.0], [], 3.0),
    )
    assert content_changed != baseline


def test_add_image_transition_is_local_to_image_boundaries(tmp_path: Path) -> None:
    def clip(name: str, image: bool = False) -> MediaInfo:
        return MediaInfo(
            path=tmp_path / name, duration=4.0, width=1920, height=1080,
            effective_width=1920, effective_height=1080, fps=30.0,
            fps_fraction="30/1", video_codec="image" if image else "h264",
            pixel_format="yuv420p", sar="1:1", dar="16:9", source_duration=4.0,
            is_image_insertion=image,
        )

    media = [clip("intro.mp4"), clip("add-image.png", True), clip("main.mp4"), clip("outro.mp4")]
    settings = ExportSettings(
        workflow_stage="outro", resolution="1920x1080", normalize_audio=False,
        transition_type="smooth_blur", image_transition_type="cross_dissolve",
        stage2_audio_modes=["original", "mute", "original", "original"],
    )
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
        media, settings, ResolvedExport(1920, 1080, 30.0, "30", [4.0] * 4, [1.0] * 3, 13.0)
    )
    # The global Smooth Blur remains at the unrelated Main/Outro boundary,
    # while the two Add Image boundaries use the shared Cross Dissolve family.
    assert "gblur=" in graph
    assert graph.count("xfade=transition=custom") == 3
    assert "[v0][v1]xfade=transition=custom" in graph
    assert "[vx2][v3]xfade=transition=custom" in graph


def test_add_image_before_main_stays_after_quote_flyer(tmp_path: Path) -> None:
    intro, main, quote, image = (
        tmp_path / name for name in ("intro.mp4", "main.mp4", "flyer.png", "add-image.jpg")
    )
    for path in (intro, main, quote, image):
        path.write_bytes(b"fixture")
    engine = type("FakeEngine", (), {})()
    engine.ffprobe_path = Path("ffprobe")
    engine.analyzer = type("Analyzer", (), {})()
    engine.analyzer.probe_raw = lambda path: {
        "streams": [{"codec_type": "video", "width": 800, "height": 1200}]
    }
    captured: list[list[MediaInfo]] = []
    engine.analyze = lambda paths, log=None: [_media(path) for path in paths]
    engine.make_plan = lambda media, settings, log=None: (
        captured.append(list(media)), _resolved(len(media))
    )[1]
    engine.export = lambda media, settings, resolved, output, **kwargs: ValidationReport(
        True, [], Path(output), resolved.expected_duration, 1920, 1080, 30.0, True, True
    )
    MainProjectEngine(engine).add_outro(
        ExportSettings(
            workflow_stage="outro", main_video_path=str(main), intro_path=str(intro),
            quote_enabled=True, quote_artwork_path=str(quote),
            image_enabled=True, image_path=str(image), image_position="before_main",
        ),
        tmp_path,
    )
    names = [item.path.name for item in captured[-1]]
    assert names == ["intro.mp4", "flyer.png", "add-image.jpg", "main.mp4"]
