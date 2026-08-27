"""1.3.0 – One-Click outputs, dual subtitle variants, clean Output directory and
automatic local YouTube metadata (German + English).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import ExportSettings
from app.video_merger.youtube_metadata import (
    YouTubeMetadata, build_metadata, detect_language, generate_youtube_metadata_file,
    try_ollama_polish,
)
from tests.conftest import make_clip


def _voice(ffmpeg: Path, path: Path, duration: float) -> None:
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"sine=f=880:r=48000:d={duration}", "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True, timeout=120,
    )


DE_SCRIPT = (
    "Warum Stille heute so selten geworden ist. "
    "Wir füllen jede Lücke mit Lärm, mit Bildern, mit Aufgaben. "
    "Doch in der Stille entsteht der Raum, in dem Gedanken überhaupt erst wachsen können. "
    "Vielleicht ist die wahre Aufmerksamkeit kein Können, sondern ein Verzicht. "
    "Wer eine Minute lang nichts tut, verliert nichts – er findet etwas."
)
EN_SCRIPT = (
    "Why silence has become so rare today. "
    "We fill every gap with noise, with images, with tasks. "
    "Yet silence is the very space in which thoughts can grow. "
    "Perhaps true attention is not a skill but a renunciation. "
    "Whoever does nothing for one minute loses nothing and finds something."
)


# --------------------------------------------------------------------------- #
# One-Click: dual outputs + clean Output directory (real renders)
# --------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_one_click_produces_dual_outputs_clean_directory_and_local_metadata(
    ffmpeg_paths, tmp_path
):
    ffmpeg, ffprobe = ffmpeg_paths
    clips = [tmp_path / "blue.mp4", tmp_path / "red.mp4"]
    for clip, color in zip(clips, ("blue", "red")):
        make_clip(ffmpeg, clip, size="320x180", duration=1.4, color=color, audio_rate=None)
    outro = tmp_path / "outro.mp4"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=green:s=320x180:r=30:d=0.8",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(outro)],
        check=True, capture_output=True, timeout=120,
    )
    voice = tmp_path / "voice.wav"
    _voice(ffmpeg, voice, 1.6)
    script = tmp_path / "script.txt"
    script.write_text("One click renders pool, voiceover and final video.", encoding="utf-8")
    timings = [
        ("One", .08, .20), ("click", .22, .34), ("renders", .36, .50),
        ("pool,", .52, .66), ("voiceover", .68, .90), ("and", .92, 1.02),
        ("final", 1.04, 1.18), ("video.", 1.20, 1.34),
    ]

    def recognize(_path, _language):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    aligner = LocalWordAligner("outputs-e2e", recognize, cache_dir=tmp_path / "cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(clips)
    settings = ExportSettings(
        resolution="320x180", encoding="CPU", preset="fast", crf=28,
        normalize_audio=False, voiceover_path=str(voice), script_path=str(script),
        subtitle_enabled=True, subtitle_language="English", final_pause=0.5,
        outro_path=str(outro), transition_duration=0.4,
    )
    output = tmp_path / "Output"
    result = MainProjectEngine(engine).create_complete(media, settings, output, aligner=aligner)

    # The primary one-click output is the FINAL video (with burned subtitles).
    assert result.final_video.is_file() and result.final_report.ok
    assert result.final_video.name == "FinalVideo_16x9.mp4"
    # Both additional variants exist and belong to the same bundle.
    assert result.final_video_no_subtitles is not None
    assert result.final_video_no_subtitles.name == "FinalVideo_16x9_no_subtitles.mp4"
    assert result.main.video_no_subtitles is not None
    assert result.main.video_no_subtitles.name == "MainVideo_16x9_no_subtitles.mp4"
    assert result.youtube_metadata is not None
    assert result.youtube_metadata.name == "FinalVideo_16x9_YouTube.txt"

    # CLEAN OUTPUT DIRECTORY: exactly the useful user-facing files.
    names = sorted(path.name for path in output.iterdir())
    assert names == [
        "FinalVideo_16x9.mp4",
        "FinalVideo_16x9_YouTube.txt",
        "FinalVideo_16x9_no_subtitles.mp4",
        "MainVideo_16x9.mp4",
        "MainVideo_16x9.srt",
        "MainVideo_16x9.vtt",
        "MainVideo_16x9_no_subtitles.mp4",
    ]
    # No debug/verification PNGs, no timeline JSON in the Output folder.
    assert not list(output.glob("*.png"))
    assert not list(output.glob("*.json"))
    # Internal evidence lives under temp/ instead.
    temp_timeline = Path(result.main.canonical_timeline)
    assert temp_timeline.is_file() and "temp" in temp_timeline.parts
    assert all("temp" in Path(frame).parts for frame in result.main.verification_frames)

    # The subtitled final is the primary: it is the longer-standing variant and
    # actually carries caption glyphs (checked via the SRT sidecar + file size
    # sanity — the subtitle burn re-encodes through libass).
    srt_text = result.main.srt.read_text(encoding="utf-8")
    assert "voiceover" in srt_text
    assert result.final_video.stat().st_size > 1000


@pytest.mark.e2e
def test_explicit_main_video_render_creates_dual_variants(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "a.mp4"
    make_clip(ffmpeg, clip, size="320x180", duration=2.6, color="navy", audio_rate=None)
    voice = tmp_path / "voice.wav"
    _voice(ffmpeg, voice, 1.8)
    script = tmp_path / "script.txt"
    script.write_text("Explicit main render keeps both subtitle variants.", encoding="utf-8")
    timings = [
        ("Explicit", .08, .26), ("main", .28, .40), ("render", .42, .60),
        ("keeps", .62, .78), ("both", .80, .96), ("subtitle", .98, 1.24),
        ("variants.", 1.26, 1.52),
    ]

    def recognize(_path, _language):
        return [RecognizedWord(word, start, end, .99) for word, start, end in timings], "en"

    aligner = LocalWordAligner("explicit-main", recognize, cache_dir=tmp_path / "cache")
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    settings = ExportSettings(
        resolution="320x180", encoding="CPU", preset="fast", crf=28,
        normalize_audio=False, voiceover_path=str(voice), script_path=str(script),
        subtitle_enabled=True, subtitle_language="English", final_pause=0.4,
    )
    output = tmp_path / "Out"
    result = MainProjectEngine(engine).create_main(media, settings, output, aligner=aligner)
    assert result.report.ok
    assert result.video.name == "MainVideo_16x9.mp4"
    assert result.video_no_subtitles is not None and result.video_no_subtitles.is_file()
    assert result.srt is not None and result.srt.is_file()
    assert result.vtt is not None and result.vtt.is_file()
    assert sorted(path.name for path in output.iterdir()) == [
        "MainVideo_16x9.mp4", "MainVideo_16x9.srt",
        "MainVideo_16x9.vtt", "MainVideo_16x9_no_subtitles.mp4",
    ]


@pytest.mark.e2e
def test_one_click_without_subtitles_renders_single_final_and_no_fake_metadata(
    ffmpeg_paths, tmp_path
):
    """No voiceover/script → no subtitles → one final file, and NO metadata is
    invented (there is no authoritative transcript)."""
    ffmpeg, ffprobe = ffmpeg_paths
    clip = tmp_path / "a.mp4"
    make_clip(ffmpeg, clip, size="320x180", duration=1.2, color="orange", audio_rate=None)
    outro = tmp_path / "outro.mp4"
    make_clip(ffmpeg, outro, size="320x180", duration=0.8, color="purple", audio_rate=None)
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze([clip])
    settings = ExportSettings(
        resolution="320x180", encoding="CPU", preset="fast", crf=28,
        normalize_audio=False, outro_path=str(outro), transition_duration=0.3,
    )
    output = tmp_path / "Output"
    result = MainProjectEngine(engine).create_complete(media, settings, output)
    assert result.final_video.is_file() and result.final_report.ok
    assert result.final_video_no_subtitles is None
    assert result.youtube_metadata is None
    assert sorted(path.name for path in output.iterdir()) == [
        "FinalVideo_16x9.mp4", "MainVideo_16x9.mp4",
    ]


# --------------------------------------------------------------------------- #
# Local, free, unlimited YouTube metadata
# --------------------------------------------------------------------------- #


def test_german_transcript_produces_german_metadata_from_transcript_only():
    language = detect_language(DE_SCRIPT, "Auto")
    assert language == "German"
    metadata = build_metadata(DE_SCRIPT, "German")
    assert metadata.language == "German"
    assert 15 <= len(metadata.title) <= 100
    assert "Stille" in metadata.title  # title comes from the transcript itself
    assert "Worum es in diesem Video geht:" in metadata.description
    assert "folge dem Kanal" in metadata.description  # natural German CTA
    rendered = metadata.render()
    assert rendered.startswith("TITLE: ")
    assert "DESCRIPTION:\n" in rendered
    assert rendered.rstrip().endswith("LANGUAGE: German")
    # Accuracy: summary lines are verbatim transcript sentences.
    for line in metadata.description.splitlines():
        if line.startswith("– ") and len(line) > 12:
            assert line[2:] in DE_SCRIPT


def test_english_transcript_produces_english_metadata():
    assert detect_language(EN_SCRIPT, "Auto") == "English"
    metadata = build_metadata(EN_SCRIPT, "English")
    assert metadata.language == "English"
    assert "silence" in metadata.title or "Silence" in metadata.title
    assert "What this video is about:" in metadata.description
    assert "follow the channel" in metadata.description
    assert "Worum es" not in metadata.description  # never the wrong language


def test_explicit_language_preference_wins():
    assert detect_language(DE_SCRIPT, "English") == "English"
    assert detect_language(EN_SCRIPT, "German") == "German"


def test_themes_are_extracted_verbatim_key_phrases():
    metadata = build_metadata(DE_SCRIPT, "German")
    themes = [line[2:] for line in metadata.description.splitlines()
              if line.startswith("· ")]
    assert 2 <= len(themes) <= 6
    for phrase in themes:
        assert phrase in DE_SCRIPT  # extracted, never invented


def test_short_transcript_raises_clear_error(tmp_path):
    with pytest.raises(Exception):
        build_metadata("kurz.", "German")  # no usable sentence → clear failure


def test_generation_without_ollama_is_deterministic_and_free(tmp_path, monkeypatch):
    """No Ollama present → the deterministic local draft is used; no network
    call is required (unreachable endpoint), nothing is invented."""
    def _fail(*_args, **_kwargs):
        raise OSError("no network in this test")

    monkeypatch.setattr("app.video_merger.youtube_metadata._ollama_models", _fail)
    logs: list[str] = []
    path = generate_youtube_metadata_file(
        DE_SCRIPT, tmp_path / "meta.txt", "Auto", log=logs.append, use_ollama=True,
    )
    assert path.is_file()
    assert any("Ollama nicht verfügbar" in line for line in logs)
    assert "TITLE: " in path.read_text(encoding="utf-8")


def test_ollama_polish_is_validated_and_can_be_rejected(monkeypatch):
    metadata = build_metadata(DE_SCRIPT, "German")
    # Invalid JSON answer → polished=None → deterministic draft stays.
    monkeypatch.setattr(
        "app.video_merger.youtube_metadata._ollama_models", lambda *a, **k: ["llama3.1"],
    )
    monkeypatch.setattr(
        "app.video_merger.youtube_metadata._ollama_generate",
        lambda *a, **k: "I refuse to answer in JSON",
    )
    assert try_ollama_polish(metadata, DE_SCRIPT) is None
    # Valid JSON answer → polished, tagged with the local model.
    good = '{"title": "Warum Stille heute so selten geworden ist", "description": "%s"}' % (
        "Eine ruhige Reflexion über Stille und Aufmerksamkeit. " * 3
    )
    monkeypatch.setattr(
        "app.video_merger.youtube_metadata._ollama_generate", lambda *a, **k: good,
    )
    polished = try_ollama_polish(metadata, DE_SCRIPT)
    assert polished is not None
    assert polished.generator.startswith("local-extractor+ollama")
    assert "llama3.1" in polished.generator
    # …but a too-short description is rejected as well:
    bad = '{"title": "Ok title here", "description": "zu kurz"}'
    monkeypatch.setattr(
        "app.video_merger.youtube_metadata._ollama_generate", lambda *a, **k: bad,
    )
    assert try_ollama_polish(metadata, DE_SCRIPT) is None


def test_metadata_file_format_matches_specification(tmp_path):
    path = generate_youtube_metadata_file(
        EN_SCRIPT, tmp_path / "FinalVideo_16x9_YouTube.txt", "Auto",
        log=lambda _m: None, use_ollama=False,
    )
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("TITLE: ")
    assert any(line == "DESCRIPTION:" for line in lines)
    assert lines[-1] == "LANGUAGE: English"
    metadata = YouTubeMetadata(
        title="t" * 30, description="d" * 200, language="German",
        generator="local-extractor",
    )
    assert metadata.render().count("LANGUAGE: German") == 1


def test_no_paid_or_cloud_api_is_referenced():
    """FREE + LOCAL + UNLIMITED: no paid/cloud API client anywhere in the module."""
    source = Path("app/video_merger/youtube_metadata.py").read_text(encoding="utf-8")
    for forbidden in ("import openai", "from openai", "import anthropic",
                      "from anthropic", "import google.generativeai",
                      "api.openai.com", "api.anthropic.com",
                      "generativelanguage.googleapis.com",
                      "api_key", "apiKey", "API_KEY", "SECRET"):
        assert forbidden.lower() not in source.lower(), forbidden
    assert "127.0.0.1:11434" in source  # only the optional local Ollama endpoint
