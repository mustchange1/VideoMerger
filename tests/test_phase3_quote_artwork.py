"""Phase 3: uploaded Quote artwork, fit modes, PDF pages and silence guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.errors import VideoMergerError
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import (
    AudioInfo,
    ExportSettings,
    MediaInfo,
    ResolvedExport,
    ValidationReport,
)
from app.video_merger.quote_artwork import (
    PreparedQuoteArtwork,
    pdf_page_count,
    prepare_quote_artwork,
    quote_artwork_path,
)


def _media(path: str, width: int, height: int, duration: float = 3.0) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=width, height=height,
        effective_width=width, effective_height=height, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="", audio=AudioInfo(),
    )


def _artwork(path: str, mode: str) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=2.0, width=400, height=200,
        effective_width=400, effective_height=200, fps=30.0,
        fps_fraction="30/1", video_codec="image", pixel_format="rgb",
        sar="1:1", dar="", source_duration=2.0,
        is_quote_artwork=True, quote_fit_mode=mode,
    )


def _resolved(count: int) -> ResolvedExport:
    durations = [2.0] + [3.0] * (count - 1)
    transitions = [0.5] * (count - 1)
    return ResolvedExport(
        width=1920, height=1080, fps=30.0, fps_expr="30",
        effective_durations=durations, transitions=transitions,
        expected_duration=sum(durations) - sum(transitions),
    )


@pytest.mark.parametrize("suffix", [".png", ".jpg", ".jpeg", ".webp"])
def test_supported_raster_artwork_extensions_are_validated(tmp_path, suffix):
    path = tmp_path / f"art{suffix}"
    path.write_bytes(b"not decoded here")
    prepared = prepare_quote_artwork(
        path, 1, 1920, 1080, tmp_path / "temp",
        lambda _path: (400, 200),
    )
    assert prepared == PreparedQuoteArtwork(path.resolve(), 400, 200, path.resolve(), None)


def test_artwork_fit_modes_have_aspect_safe_graphs_and_a_looped_input():
    main = _media("main.mp4", 1920, 1080)
    for mode in ("fit", "fill", "crop"):
        artwork = _artwork("art.png", mode)
        settings = ExportSettings(
            workflow_stage="outro", resolution="1920x1080",
            transition_duration=1.0, quote_enabled=True,
            quote_input_mode="artwork", quote_artwork_path="art.png",
            quote_artwork_fit_mode=mode, stage2_audio_modes=["mute", "original"],
            normalize_audio=False,
        )
        built = FFmpegCommandBuilder("ffmpeg").build(
            [artwork, main], settings, _resolved(2), Path("out.mp4"),
        )
        assert built.command[3:7] == ["-loop", "1", "-i", "art.png"]
        assert "[0:v:0]" in built.filter_graph
        assert "format=yuv420p" in built.filter_graph
        if mode == "fit":
            assert "force_original_aspect_ratio=decrease" in built.filter_graph
            assert "pad=w=1920:h=1080" in built.filter_graph
        elif mode == "fill":
            assert "force_original_aspect_ratio=increase" in built.filter_graph
            assert "crop=1920:1080" in built.filter_graph
        else:
            assert "crop=w=min(iw\\,ih*1920/1080)" in built.filter_graph


@pytest.mark.parametrize("suffix", [".png", ".jpg", ".jpeg", ".webp"])
def test_stage2_places_uploaded_artwork_between_intro_and_main(tmp_path, monkeypatch, suffix):
    intro = tmp_path / "intro.mp4"
    main = tmp_path / "main.mp4"
    artwork = tmp_path / f"art{suffix}"
    for path in (intro, main, artwork):
        path.write_bytes(b"fixture")

    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    monkeypatch.setattr(engine, "analyze", lambda paths, log=None: [_media(str(path), 1920, 1080) for path in paths])
    monkeypatch.setattr(
        engine.analyzer, "probe_raw",
        lambda path: {"streams": [{"codec_type": "video", "width": 400, "height": 200}]},
    )
    captured = {}
    monkeypatch.setattr(
        engine, "make_plan",
        lambda media, settings, log=None: (
            captured.setdefault("media", list(media)),
            ResolvedExport(
                1920, 1080, 30.0, "30", [item.duration for item in media],
                [0.5] * (len(media) - 1), sum(item.duration for item in media) - 0.5 * (len(media) - 1),
            ),
        )[1],
    )
    monkeypatch.setattr(
        engine, "export",
        lambda media, settings, resolved, output, **kwargs: ValidationReport(
            ok=True, details=[], path=Path(output), duration=resolved.expected_duration,
            width=1920, height=1080, fps=30.0, has_video=True, has_audio=True,
        ),
    )
    settings = ExportSettings(
        workflow_stage="outro", intro_path=str(intro), main_video_path=str(main),
        quote_enabled=True, quote_input_mode="artwork", quote_artwork_path=str(artwork),
        quote_artwork_fit_mode="fill", quote_duration=2.0,
        resolution="1920x1080", transition_duration=1.0,
    )
    output, report = MainProjectEngine(engine).add_outro(settings, tmp_path)
    assert report.ok
    assert not output.is_file()  # export is intentionally a sentinel
    assert [item.path.name for item in captured["media"]] == ["intro.mp4", artwork.name, "main.mp4"]
    quote = captured["media"][1]
    assert quote.is_quote_artwork and quote.quote_fit_mode == "fill"
    assert quote.audio.present is False and quote.duration == 2.0
    assert settings.quote_transition_duration == 0.0


def test_one_click_gate_accepts_artwork_without_text(tmp_path, monkeypatch):
    engine = VideoMergerEngine("fake-ffmpeg", "fake-ffprobe")
    project = MainProjectEngine(engine)
    artwork = tmp_path / "art.png"
    artwork.write_bytes(b"fixture")
    sentinel = RuntimeError("ARTWORK GATE PASSED")
    monkeypatch.setattr(project, "create_main", lambda *args, **kwargs: (_ for _ in ()).throw(sentinel))
    settings = ExportSettings(
        quote_enabled=True, quote_input_mode="artwork",
        quote_artwork_path=str(artwork), quote_artwork_fit_mode="fit",
    )
    with pytest.raises(RuntimeError, match="ARTWORK GATE PASSED"):
        project.create_complete([], settings, tmp_path)


def test_quote_defaults_keep_text_mode_disabled_and_fit():
    settings = ExportSettings()
    assert settings.quote_enabled is False
    assert settings.quote_input_mode == "text"
    assert settings.quote_artwork_path == ""
    assert settings.quote_pdf_page == 1
    assert settings.quote_artwork_fit_mode == "fit"
    assert settings.quote_duration == 2.0


def test_uploaded_artwork_is_silent_even_when_other_sections_have_audio():
    artwork = _artwork("art.webp", "fit")
    main = _media("main.mp4", 1920, 1080)
    main.audio = AudioInfo(present=True, codec="aac", sample_rate=48000, channels=2)
    settings = ExportSettings(
        workflow_stage="outro", resolution="1920x1080", transition_duration=1.0,
        quote_enabled=True, quote_input_mode="artwork", quote_artwork_path="art.webp",
        stage2_audio_modes=["mute", "original"], normalize_audio=False,
    )
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(
        [artwork, main], settings, _resolved(2),
    )
    # Artwork gets a generated silence branch and never references an image
    # audio stream. The main clip's original audio branch remains independent.
    assert "anullsrc=r=48000:cl=stereo:d=2" in graph
    assert "[0:a:0]" not in graph
    assert "[1:a:0]" in graph


def test_pdf_page_selection_rasterizes_the_requested_page_when_supported(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "pages.pdf"
    document = fitz.open()
    for label in ("page one", "page two"):
        page = document.new_page(width=600, height=400)
        page.insert_text((72, 100), label)
    document.save(str(pdf))
    document.close()

    assert pdf_page_count(pdf) == 2
    prepared = prepare_quote_artwork(
        pdf, 2, 1920, 1080, tmp_path / "temp", lambda _path: (1, 1),
    )
    assert prepared.pdf_page == 2
    assert prepared.source_path == pdf.resolve()
    assert prepared.path.suffix == ".png"
    assert prepared.width > 0 and prepared.height > 0 and prepared.path.is_file()
    with pytest.raises(VideoMergerError, match="existiert nicht"):
        prepare_quote_artwork(pdf, 3, 1920, 1080, tmp_path / "temp", lambda _path: (1, 1))


def test_pdf_path_with_wrong_extension_is_rejected(tmp_path):
    path = tmp_path / "art.svg"
    path.write_text("<svg/>", encoding="utf-8")
    with pytest.raises(VideoMergerError, match="Nicht unterstütztes Format"):
        quote_artwork_path(path)


def test_pdf_raster_cleanup_never_removes_the_source(tmp_path):
    fitz = pytest.importorskip("fitz")
    from app.video_merger.quote_artwork import cleanup_prepared_quote_artwork

    pdf = tmp_path / "art.pdf"
    document = fitz.open()
    document.new_page(width=600, height=400)
    document.save(str(pdf))
    document.close()

    prepared = prepare_quote_artwork(
        pdf, 1, 3840, 2160, tmp_path / "temp", lambda _path: (1, 1),
    )
    assert prepared.path.is_file()
    cleanup_prepared_quote_artwork(prepared)
    assert not prepared.path.exists()
    assert pdf.is_file()


def test_auto_pdf_quality_target_follows_the_selected_portrait_output():
    from app.video_merger.main_project import _quote_artwork_target

    reference = _media("main.mp4", 1920, 1080)
    settings = ExportSettings(aspect="9:16", resolution="Auto")
    assert _quote_artwork_target(settings, reference) == (1080, 1920)


def test_all_uploaded_quote_settings_round_trip_through_settings_store(tmp_path):
    from app.video_merger.settings_store import SettingsStore

    settings = ExportSettings(
        quote_enabled=True,
        quote_input_mode="artwork",
        quote_artwork_path=str(tmp_path / "flyer.PDF"),
        quote_pdf_page=7,
        quote_artwork_fit_mode="crop",
        quote_duration=4.5,
        quote_zoom_percent=8.0,
        quote_transition_duration=0.35,
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
    assert loaded.quote_zoom_percent == 8.0
    assert loaded.quote_transition_duration == 0.35
