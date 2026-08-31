"""Focused tests for the artwork-only Quote/Flyer Stage-2 workflow."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.errors import VideoMergerError
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import AudioInfo, ExportSettings, MediaInfo, ResolvedExport, ValidationReport
from app.video_merger.quote_artwork import (
    PreparedQuoteArtwork,
    pdf_page_count,
    prepare_quote_artwork,
    quote_artwork_path,
)
from app.video_merger.render_cache import stage1_fingerprint
from app.video_merger.settings_store import SettingsStore


ARTWORK_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"]


def _media(path: str, width: int = 1920, height: int = 1080, duration: float = 3.0, audio: bool = False) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=width, height=height,
        effective_width=width, effective_height=height, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="16:9", source_duration=duration,
        audio=AudioInfo(present=audio, codec="aac" if audio else "", sample_rate=48000 if audio else 0,
                        channels=2 if audio else 0),
    )


def _artwork(path: str, fit: str = "fit", width: int = 400, height: int = 200) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=2.0, width=width, height=height,
        effective_width=width, effective_height=height, fps=30.0,
        fps_fraction="30/1", video_codec="image", pixel_format="rgb",
        sar="1:1", dar="", source_duration=2.0,
        is_quote_artwork=True, quote_fit_mode=fit,
    )


def _resolved(count: int, width: int = 1920, height: int = 1080) -> ResolvedExport:
    durations = [2.0] + [3.0] * (count - 1)
    transitions = [0.5] * (count - 1)
    return ResolvedExport(
        width=width, height=height, fps=30.0, fps_expr="30",
        effective_durations=durations, transitions=transitions,
        expected_duration=sum(durations) - sum(transitions),
    )


@pytest.mark.parametrize("suffix", ARTWORK_EXTENSIONS)
def test_supported_artwork_extensions_are_accepted(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"flyer{suffix}"
    path.write_bytes(b"fixture")
    prepared = prepare_quote_artwork(
        path, 1, 1920, 1080, tmp_path / "temp", lambda _path: (1200, 800)
    )
    assert prepared == PreparedQuoteArtwork(path.resolve(), 1200, 800, path.resolve(), None)


def test_missing_unsupported_and_unreadable_artwork_are_clear_errors(tmp_path: Path) -> None:
    with pytest.raises(VideoMergerError, match="fehlt oder ist keine lesbare Datei"):
        quote_artwork_path(tmp_path / "missing.png")
    unsupported = tmp_path / "flyer.svg"
    unsupported.write_text("<svg/>", encoding="utf-8")
    with pytest.raises(VideoMergerError, match="Nicht unterstütztes Format"):
        quote_artwork_path(unsupported)
    unreadable = tmp_path / "flyer.png"
    unreadable.write_bytes(b"broken")
    with pytest.raises(VideoMergerError, match="konnte nicht analysiert werden"):
        prepare_quote_artwork(
            unreadable, 1, 1920, 1080, tmp_path / "temp",
            lambda _path: (_ for _ in ()).throw(VideoMergerError("probe failed")),
        )


@pytest.mark.parametrize(
    ("fit", "required"),
    [
        ("fit", "force_original_aspect_ratio=decrease"),
        ("fill", "force_original_aspect_ratio=increase"),
        ("crop", "crop=w=min(iw\\,ih*1920/1080)"),
    ],
)
def test_fit_fill_crop_preserve_aspect_and_loop_the_artwork(fit: str, required: str) -> None:
    settings = ExportSettings(
        workflow_stage="outro", resolution="1920x1080", transition_type="cross_dissolve",
        transition_duration=1.0, quote_enabled=True, quote_input_mode="artwork",
        quote_artwork_path="flyer.png", quote_artwork_fit_mode=fit,
        stage2_audio_modes=["mute", "original"], normalize_audio=False,
    )
    built = FFmpegCommandBuilder("ffmpeg").build(
        [_artwork("flyer.png", fit), _media("main.mp4")], settings,
        _resolved(2), Path("out.mp4"),
    )
    assert built.command[3:7] == ["-loop", "1", "-i", "flyer.png"]
    assert "[0:v:0]" in built.filter_graph
    assert required in built.filter_graph
    assert "drawtext=" not in built.filter_graph
    assert "[0:a:0]" not in built.filter_graph
    assert "anullsrc=r=48000:cl=stereo:d=2" in built.filter_graph


@pytest.mark.parametrize(("width", "height"), [(1920, 1080), (3840, 2160), (1080, 1920), (2160, 3840)])
def test_artwork_graph_supports_landscape_portrait_1080p_and_4k(width: int, height: int) -> None:
    settings = ExportSettings(
        workflow_stage="outro", resolution=f"{width}x{height}", quote_artwork_fit_mode="fit",
        normalize_audio=False,
    )
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
        [_artwork("flyer.webp"), _media("main.mp4")], settings,
        _resolved(2, width, height),
    )
    assert f"scale=w={width}:h={height}" in graph
    assert f"pad=w={width}:h={height}" in graph
    assert "setsar=1" in graph


def test_stage2_sequence_inserts_artwork_between_intro_and_main_and_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    intro = tmp_path / "intro.mp4"
    main = tmp_path / "main.mp4"
    artwork = tmp_path / "flyer.jpg"
    for path in (intro, main, artwork):
        path.write_bytes(b"fixture")
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    monkeypatch.setattr(engine, "analyze", lambda paths, log=None: [_media(str(path), audio=True) for path in paths])
    monkeypatch.setattr(engine.analyzer, "probe_raw", lambda path: {
        "streams": [{"codec_type": "video", "width": 1200, "height": 800}]
    })
    captured: dict[str, object] = {}
    monkeypatch.setattr(engine, "make_plan", lambda media, settings, log=None: (
        captured.setdefault("media", list(media)),
        ResolvedExport(
            1920, 1080, 30.0, "30", [item.duration for item in media],
            [0.5] * (len(media) - 1), sum(item.duration for item in media) - 0.5 * (len(media) - 1),
        ),
    )[1])
    monkeypatch.setattr(engine, "export", lambda media, settings, resolved, output, **kwargs: ValidationReport(
        ok=True, details=[], path=Path(output), duration=resolved.expected_duration,
        width=1920, height=1080, fps=30.0, has_video=True, has_audio=True,
    ))
    settings = ExportSettings(
        workflow_stage="outro", intro_path=str(intro), main_video_path=str(main),
        quote_enabled=True, quote_input_mode="artwork", quote_artwork_path=str(artwork),
        quote_artwork_fit_mode="fill", quote_duration=2.0,
        resolution="1920x1080", transition_duration=1.0,
    )
    output, report = MainProjectEngine(engine).add_outro(settings, tmp_path)
    assert report.ok
    assert not output.is_file()
    media = captured["media"]
    assert [item.path.name for item in media] == ["intro.mp4", "flyer.jpg", "main.mp4"]
    quote = media[1]
    assert quote.is_quote_artwork and quote.quote_fit_mode == "fill"
    assert quote.audio.present is False
    assert quote.duration == 2.0
    # The renderer receives an explicit mute slot for the artwork section.
    assert settings.quote_input_mode == "artwork"


def test_quote_disabled_keeps_intro_main_outro_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [tmp_path / name for name in ("intro.mp4", "main.mp4", "outro.mp4")]
    for path in paths:
        path.write_bytes(b"fixture")
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    monkeypatch.setattr(engine, "analyze", lambda paths, log=None: [_media(str(path)) for path in paths])
    captured: dict[str, object] = {}
    monkeypatch.setattr(engine, "make_plan", lambda media, settings, log=None: (
        captured.setdefault("media", list(media)),
        ResolvedExport(1920, 1080, 30.0, "30", [3.0] * len(media), [0.5] * (len(media) - 1),
                       3.0 * len(media) - 0.5 * (len(media) - 1)),
    )[1])
    monkeypatch.setattr(engine, "export", lambda media, settings, resolved, output, **kwargs: ValidationReport(
        ok=True, details=[], path=Path(output), duration=resolved.expected_duration,
        width=1920, height=1080, fps=30.0, has_video=True, has_audio=True,
    ))
    settings = ExportSettings(
        workflow_stage="outro", intro_path=str(paths[0]), main_video_path=str(paths[1]),
        outro_path=str(paths[2]), quote_enabled=False,
    )
    MainProjectEngine(engine).add_outro(settings, tmp_path)
    media = captured["media"]
    assert [item.path.name for item in media] == ["intro.mp4", "main.mp4", "outro.mp4"]
    assert not any(item.is_quote_artwork for item in media)


def test_one_click_artwork_gate_does_not_require_text_or_intermediate_quote_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artwork = tmp_path / "flyer.png"
    artwork.write_bytes(b"fixture")
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    project = MainProjectEngine(engine)
    sentinel = RuntimeError("STAGE 1 STARTED")
    monkeypatch.setattr(project, "create_main", lambda *args, **kwargs: (_ for _ in ()).throw(sentinel))
    settings = ExportSettings(
        quote_enabled=True, quote_input_mode="artwork", quote_artwork_path=str(artwork)
    )
    with pytest.raises(RuntimeError, match="STAGE 1 STARTED"):
        project.create_complete([], settings, tmp_path)


def test_quote_enabled_without_artwork_is_a_clear_error_before_stage1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = MainProjectEngine(VideoMergerEngine("fake-ffmpeg", "fake-ffprobe"))
    monkeypatch.setattr(project, "create_main", lambda *args, **kwargs: pytest.fail("Stage 1 must not start"))
    with pytest.raises(VideoMergerError, match="keine Artwork-Datei"):
        project.create_complete([], ExportSettings(quote_enabled=True), tmp_path)


def test_quote_duration_is_shared_by_image_and_pdf_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artwork = tmp_path / "flyer.webp"
    artwork.write_bytes(b"fixture")
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    monkeypatch.setattr(engine, "analyze", lambda paths, log=None: [_media(str(path)) for path in paths])
    monkeypatch.setattr(engine.analyzer, "probe_raw", lambda path: {
        "streams": [{"codec_type": "video", "width": 800, "height": 600}]
    })
    captured: dict[str, object] = {}
    monkeypatch.setattr(engine, "make_plan", lambda media, settings, log=None: (
        captured.setdefault("media", list(media)),
        ResolvedExport(1920, 1080, 30.0, "30", [item.duration for item in media],
                       [0.5] * (len(media) - 1), sum(item.duration for item in media) - 0.5 * (len(media) - 1)),
    )[1])
    monkeypatch.setattr(engine, "export", lambda media, settings, resolved, output, **kwargs: ValidationReport(
        ok=True, details=[], path=Path(output), duration=resolved.expected_duration,
        width=1920, height=1080, fps=30.0, has_video=True, has_audio=True,
    ))
    settings = ExportSettings(
        workflow_stage="outro", main_video_path=str(tmp_path / "main.mp4"),
        quote_enabled=True, quote_artwork_path=str(artwork), quote_duration=4.5,
    )
    Path(settings.main_video_path).write_bytes(b"fixture")
    MainProjectEngine(engine).add_outro(settings, tmp_path)
    assert captured["media"][0].duration == pytest.approx(4.5)


def test_pdf_page_count_selection_and_invalid_pdf_when_pymupdf_is_available(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "pages.pdf"
    document = fitz.open()
    for label in ("page one", "page two"):
        page = document.new_page(width=600, height=400)
        page.insert_text((72, 100), label)
    document.save(str(pdf))
    document.close()
    assert pdf_page_count(pdf) == 2
    prepared = prepare_quote_artwork(pdf, 2, 1920, 1080, tmp_path / "temp", lambda _path: (1, 1))
    assert prepared.pdf_page == 2
    assert prepared.path.suffix == ".png" and prepared.path.is_file()
    prepared.path.unlink()
    with pytest.raises(VideoMergerError, match="existiert nicht"):
        prepare_quote_artwork(pdf, 3, 1920, 1080, tmp_path / "temp", lambda _path: (1, 1))

    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf")
    with pytest.raises(VideoMergerError, match="PDF-Quote-Artwork konnte nicht gelesen werden"):
        pdf_page_count(corrupt)


def test_pdf_raster_cleanup_never_removes_source(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    from app.video_merger.quote_artwork import cleanup_prepared_quote_artwork

    pdf = tmp_path / "flyer.pdf"
    document = fitz.open()
    document.new_page(width=600, height=400)
    document.save(str(pdf))
    document.close()
    prepared = prepare_quote_artwork(pdf, 1, 3840, 2160, tmp_path / "temp", lambda _path: (1, 1))
    cleanup_prepared_quote_artwork(prepared)
    assert not prepared.path.exists()
    assert pdf.is_file()


def test_uploaded_settings_persist_and_legacy_text_projects_load_safely(tmp_path: Path) -> None:
    settings = ExportSettings(
        quote_enabled=True, quote_input_mode="artwork", quote_artwork_path=str(tmp_path / "flyer.PDF"),
        quote_pdf_page=7, quote_artwork_fit_mode="crop", quote_duration=4.5,
    )
    path = tmp_path / "settings.json"
    SettingsStore(path).save(settings)
    loaded = SettingsStore(path).load()
    assert loaded.quote_enabled is True
    assert loaded.quote_input_mode == "artwork"
    assert loaded.quote_artwork_path == settings.quote_artwork_path
    assert loaded.quote_pdf_page == 7
    assert loaded.quote_artwork_fit_mode == "crop"
    assert loaded.quote_duration == 4.5

    path.write_text(json.dumps({
        "quote_enabled": True, "quote_text": "old project text",
        "quote_attribution": "old author", "quote_duration": 3.0,
    }), encoding="utf-8")
    legacy = SettingsStore(path).load()
    assert legacy.quote_enabled is True
    assert legacy.quote_artwork_path == ""
    assert legacy.quote_duration == 3.0


def test_quote_only_changes_do_not_invalidate_stage1_cache(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    voice = tmp_path / "voice.wav"
    script = tmp_path / "script.txt"
    for path in (clip, voice, script):
        path.write_bytes(b"fixture")
    media = [_media(str(clip))]
    settings = ExportSettings(
        resolution="1920x1080", encoding="CPU", quality_preset="custom", crf=24,
        preset="fast", voiceover_paths=[str(voice)], script_paths=[str(script)],
        subtitle_enabled=True,
    )
    resolved = _resolved(1)
    first, _ = stage1_fingerprint(media, settings, resolved)
    changed = replace(
        settings, quote_enabled=True, quote_input_mode="artwork",
        quote_artwork_path=str(tmp_path / "flyer.pdf"), quote_pdf_page=3,
        quote_artwork_fit_mode="crop", quote_duration=4.0,
    )
    second, _ = stage1_fingerprint(media, changed, resolved)
    assert second == first
