"""Phase 26 – one explicit speech/subtitle language for the whole pipeline.

The selector offers exactly two languages, ``Deutsch`` (the default) and
``English``, and ONE canonical value derived from it drives every stage:

    GUI/CLI/project file -> ExportSettings.subtitle_language
        -> faster-whisper (forced ``language="de"`` / ``"en"``)
        -> word-level timestamps -> script alignment -> cue segmentation
        -> SRT/VTT/ASS + burned captions -> transcript -> metadata
        -> alignment cache identity and the Stage-1 render fingerprint

What this module proves:

1. **Canonical model** – every accepted spelling (``de``, ``Deutsch``,
   ``en``, ``English``, ``Auto``, …) resolves to one canonical value, German is
   the default, and an unusable value fails closed instead of silently becoming
   auto-detection.

2. **The language really reaches the ASR** – the shared test recognizers in the
   older suites ignore their language argument, so nothing used to pin this
   down. Here the recognizer records the code it was handed: ``de`` for
   Deutsch, ``en`` for English, ``None`` for Auto, from ``align``,
   ``align_global`` and ``recognize`` alike.

3. **Cache isolation** – the same audio analysed as German and as English never
   shares a transcription/alignment cache entry, the language is part of the
   Stage-1 render fingerprint, and the historical German value is stored
   unchanged so an untouched project keeps its exact identity.

4. **Persistence** – save/load round-trip, a legacy file without the key, a
   legacy file storing ``Auto`` and the shipped example configuration all load.

5. **Real renders** – an English Long-Form and an English grouped Short are
   rendered by the real pipeline with real FFmpeg, and the produced media is
   measured: several naturally segmented cues, no single enormous caption,
   first/middle/final spoken sections aligned to the audio, burned glyphs on
   screen, sensible SRT/VTT timestamps, cumulative timing across a group
   boundary, one transcript and one music timeline.

6. **German control** – the same measurement for a German fixture, so the
   default path is proven unchanged rather than assumed unchanged.

7. **Wrong-language safety** – when the spoken content cannot be aligned to the
   supplied script at all (what a language drift looks like), the render fails
   closed with an explicit language diagnostic instead of publishing
   interpolated guesswork, and ``allow_alignment_warnings`` is the documented
   escape hatch that keeps the historical behaviour reachable.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.video_merger.alignment import (
    _CACHE_SCHEMA,
    LocalWordAligner,
    RecognizedWord,
    _json_digest,
    script_word_spans,
)
from app.video_merger.diagnostics import run_project_diagnostics
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.errors import VideoMergerError
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import (
    DEFAULT_SUBTITLE_LANGUAGE,
    LANGUAGE_MISMATCH_COMPATIBILITY,
    LONG_FORM_INTRO_SECONDS,
    SUBTITLE_LANGUAGE_AUTO,
    SUBTITLE_LANGUAGE_CHOICES,
    SUBTITLE_LANGUAGE_ENGLISH,
    SUBTITLE_LANGUAGE_GERMAN,
    SUBTITLE_LANGUAGE_LABELS,
    ExportSettings,
    normalize_subtitle_language,
    subtitle_language_code,
    subtitle_language_label,
)
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from app.video_merger.render_cache import _SUBTITLE_SETTING_FIELDS, build_stage1_payload
from app.video_merger.settings_store import SettingsStore
from app.video_merger.youtube_outputs import EXPORT_MODE_LONG_FORM, EXPORT_MODE_SHORTS
from tests.conftest import make_clip

# --------------------------------------------------------------------------
# Fixtures. Representative of the philosophical narration this feature was
# requested for - a rhetorical question, appositions, contractions, an em dash
# inside a sentence, several sentences - but a test fixture, never a copy of a
# production file.
# --------------------------------------------------------------------------

ENGLISH_SCRIPT_A = (
    "Have you ever questioned the nature of everything you see around you? "
    "Ancient philosophy speaks of Maya, the profound illusion that shapes our "
    "daily experience. It isn't that the physical world doesn't exist, but that "
    "we misinterpret its true nature\u2014taking the transient projection for "
    "the ultimate reality."
)

ENGLISH_SCRIPT_B = (
    "Maya acts like a subtle veil drawn over human perception. Our minds "
    "constantly categorize, label, and cling to fleeting moments, creating the "
    "sensation of deep separation."
)

GERMAN_SCRIPT = (
    "Hast du dich jemals gefragt, was die Natur von allem um dich herum "
    "wirklich ist? Die alte Philosophie spricht von Maya, der tiefgr\u00fcndigen "
    "Illusion, die unsere t\u00e4gliche Erfahrung pr\u00e4gt. Es ist nicht so, dass "
    "die physikalische Welt nicht existiert, sondern dass wir ihre wahre Natur "
    "missverstehen."
)

SAMPLE_RATE = 48000
SECONDS_PER_WORD = 0.14
PAUSE = 0.4
SHORT_INTRO = 0.7
SHORT_OUTRO = 0.7
MUSIC_HZ = 260
RENDER_BUDGET_SECONDS = 180.0
CAPTION_BRIGHTNESS = 175


def _run(command, timeout: int = 300) -> bytes:
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        stdin=subprocess.DEVNULL, check=False,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _words(text: str) -> list[str]:
    return [token for token, _start, _end in script_word_spans(text)]


def _spoken_duration(text: str) -> float:
    """A calm narration: one word slot per word plus a small tail."""
    return round(len(_words(text)) * SECONDS_PER_WORD + 0.3, 3)


def _tone(ffmpeg: Path, path: Path, frequency: int, seconds: float) -> Path:
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"sine=frequency={frequency}:sample_rate={SAMPLE_RATE}:duration={seconds}",
        "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", str(path),
    ])
    return path


def _recognizer(tokens: list[str], captured: list, duration: float, detected: str):
    """Deterministic word timing; records the language code the ASR received."""

    def recognize(_path, language):
        captured.append(language)
        usable = max(0.2, duration - 0.15)
        step = usable / max(1, len(tokens))
        words = [
            RecognizedWord(
                token,
                round(0.05 + index * step, 3),
                round(min(duration - 0.02, 0.05 + index * step + step * 0.8), 3),
                0.95,
            )
            for index, token in enumerate(tokens)
        ]
        return words, detected

    return recognize


def _aligner(tokens: list[str], captured: list, duration: float, detected: str):
    return LocalWordAligner(
        "phase26", _recognizer(tokens, captured, duration, detected), use_cache=False,
    )


def _srt_seconds(stamp: str) -> float:
    hours, minutes, rest = stamp.split(":")
    seconds, _, millis = rest.partition(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis or 0) / 1000


def _srt_cues(path: Path) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        stamp = next((line for line in lines if " --> " in line), None)
        if stamp is None:
            continue
        start, end = stamp.split(" --> ")
        text = " ".join(lines[lines.index(stamp) + 1:])
        cues.append((_srt_seconds(start), _srt_seconds(end), text))
    assert cues, f"{path.name} contains no cue"
    return cues


def _bright_pixels(ffmpeg: Path, path: Path, at: float) -> int:
    """Count pixels brighter than the navy background: burned caption glyphs."""
    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{at}", "-i", str(path),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ])
    assert raw, f"no frame decoded at {at:.3f} s"
    return sum(1 for value in raw if value >= CAPTION_BRIGHTNESS)


def _strength(ffmpeg: Path, path: Path, start: float, seconds: float, frequency: float) -> float:
    """Sinusoidal correlation at a known frequency: which track is audible."""
    import array
    import math

    raw = _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{start}", "-i", str(path),
        "-t", f"{seconds}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
    ])
    values = array.array("f")
    values.frombytes(raw)
    if not values:
        return 0.0
    sine = cosine = 0.0
    for index, value in enumerate(values):
        angle = 2.0 * math.pi * frequency * index / SAMPLE_RATE
        sine += value * math.sin(angle)
        cosine += value * math.cos(angle)
    return math.hypot(sine, cosine) / len(values)


def _probe_duration(ffprobe: Path, path: Path) -> float:
    payload = json.loads(_run([
        ffprobe, "-v", "error", "-print_format", "json", "-show_format", str(path),
    ]).decode("utf-8"))
    return float(payload["format"]["duration"])


# ==========================================================================
# 1. The canonical language model
# ==========================================================================

def test_german_is_the_default_and_stays_the_historical_value():
    """§4/§15: doing nothing behaves exactly like before this feature."""
    assert ExportSettings().subtitle_language == "German"
    assert DEFAULT_SUBTITLE_LANGUAGE == SUBTITLE_LANGUAGE_GERMAN == "German"
    assert subtitle_language_code(ExportSettings().subtitle_language) == "de"
    assert subtitle_language_label(ExportSettings().subtitle_language) == "Deutsch"


def test_the_two_user_facing_choices_are_deutsch_and_english():
    """§1: exactly two choices, in this order, with these labels."""
    assert SUBTITLE_LANGUAGE_CHOICES == (SUBTITLE_LANGUAGE_GERMAN, SUBTITLE_LANGUAGE_ENGLISH)
    assert [SUBTITLE_LANGUAGE_LABELS[item] for item in SUBTITLE_LANGUAGE_CHOICES] == [
        "Deutsch", "English",
    ]
    assert SUBTITLE_LANGUAGE_AUTO not in SUBTITLE_LANGUAGE_CHOICES


@pytest.mark.parametrize("value, expected", [
    ("German", SUBTITLE_LANGUAGE_GERMAN),
    ("deutsch", SUBTITLE_LANGUAGE_GERMAN),
    ("Deutsch", SUBTITLE_LANGUAGE_GERMAN),
    ("de", SUBTITLE_LANGUAGE_GERMAN),
    ("DE", SUBTITLE_LANGUAGE_GERMAN),
    ("  de  ", SUBTITLE_LANGUAGE_GERMAN),
    ("deu", SUBTITLE_LANGUAGE_GERMAN),
    ("german", SUBTITLE_LANGUAGE_GERMAN),
    ("English", SUBTITLE_LANGUAGE_ENGLISH),
    ("english", SUBTITLE_LANGUAGE_ENGLISH),
    ("en", SUBTITLE_LANGUAGE_ENGLISH),
    ("EN", SUBTITLE_LANGUAGE_ENGLISH),
    ("englisch", SUBTITLE_LANGUAGE_ENGLISH),
    ("eng", SUBTITLE_LANGUAGE_ENGLISH),
    ("Auto", SUBTITLE_LANGUAGE_AUTO),
    ("auto", SUBTITLE_LANGUAGE_AUTO),
    ("automatic", SUBTITLE_LANGUAGE_AUTO),
])
def test_every_accepted_spelling_resolves_to_one_canonical_value(value, expected):
    assert normalize_subtitle_language(value) == expected


@pytest.mark.parametrize("value, code", [
    ("German", "de"), ("de", "de"), ("Deutsch", "de"),
    ("English", "en"), ("en", "en"), ("Auto", None),
])
def test_the_canonical_value_maps_to_the_whisper_language_code(value, code):
    assert subtitle_language_code(value) == code


@pytest.mark.parametrize("value", ["", None, "   "])
def test_a_missing_language_falls_back_to_german(value):
    """§11: hand-edited and old configurations keep loading."""
    assert normalize_subtitle_language(value) == SUBTITLE_LANGUAGE_GERMAN


@pytest.mark.parametrize("value", ["French", "fr", "Deutschland", "Klingon", "Germanisch"])
def test_an_unusable_language_fails_closed_instead_of_drifting_to_auto(value):
    """§8/§21: an unknown value must never silently become auto-detection."""
    with pytest.raises(VideoMergerError, match="Unbekannte Untertitelsprache"):
        normalize_subtitle_language(value)
    with pytest.raises(VideoMergerError):
        subtitle_language_code(value)


# ==========================================================================
# 2. The selected language really reaches the ASR
# ==========================================================================

def test_align_forces_the_selected_language_onto_the_asr(ffmpeg_paths, tmp_path):
    """§5/§8: explicit selection takes precedence over automatic detection."""
    ffmpeg = ffmpeg_paths[0]
    audio = _tone(ffmpeg, tmp_path / "voice.wav", 700, 1.0)
    for language, expected_code in (("German", "de"), ("English", "en"), ("Auto", None)):
        captured: list = []
        aligner = _aligner(_words("alpha bravo charly"), captured, 1.0, expected_code or "auto")
        aligner.align("alpha bravo charly", audio, language, fallback_end=1.0)
        assert captured == [expected_code], (
            f"{language} must force {expected_code!r} onto faster-whisper, got {captured}"
        )


def test_align_global_and_recognize_force_the_same_language(ffmpeg_paths, tmp_path):
    """§5/§14: the multi-voiceover and transcription entry points agree."""
    ffmpeg = ffmpeg_paths[0]
    audio = _tone(ffmpeg, tmp_path / "voice.wav", 700, 1.2)
    for language, expected_code in (("German", "de"), ("English", "en")):
        captured: list = []
        aligner = _aligner(_words("alpha bravo charly delta"), captured, 1.2, expected_code)
        aligner.align_global(
            "alpha bravo charly delta", [(audio, 1.2)], language, PAUSE,
        )
        assert captured == [expected_code]

        captured.clear()
        aligner = _aligner(_words("alpha bravo charly delta"), captured, 1.2, expected_code)
        aligner.recognize(audio, language)
        assert captured == [expected_code]


def test_the_aligner_rejects_an_unknown_language_from_every_entry_point(ffmpeg_paths, tmp_path):
    ffmpeg = ffmpeg_paths[0]
    audio = _tone(ffmpeg, tmp_path / "voice.wav", 700, 0.6)
    aligner = _aligner(_words("alpha bravo"), [], 0.6, "de")
    for call in (
        lambda: aligner.align("alpha bravo", audio, "French"),
        lambda: aligner.align_global("alpha bravo", [(audio, 0.6)], "French", PAUSE),
        lambda: aligner.recognize(audio, "French"),
    ):
        with pytest.raises(VideoMergerError, match="Unbekannte Untertitelsprache"):
            call()


# ==========================================================================
# 3. Cache identity (§9)
# ==========================================================================

def test_german_and_english_analysis_never_share_a_cache_entry(ffmpeg_paths, tmp_path):
    """§9: the same audio analysed as de and as en must not reuse each other.

    An injected recognizer deliberately disables the aligner cache (synthetic
    output must never contaminate the production cache), so isolation is proven
    against a live cache using exactly the keys the pipeline builds.
    """
    ffmpeg = ffmpeg_paths[0]
    audio = _tone(ffmpeg, tmp_path / "voice.wav", 700, 1.2)
    script = "alpha bravo charly delta"
    aligner = LocalWordAligner("phase26", cache_dir=tmp_path / "alignment-cache")
    assert aligner.use_cache is True, "a cache test needs the cache enabled"
    audio_sha = aligner._audio_fingerprint(audio)

    def keys(language_code: str) -> tuple[str, str]:
        transcription = _json_digest({
            "schema": _CACHE_SCHEMA, "audio_sha256": audio_sha,
            "model": aligner.model_name, "language": language_code,
        })
        alignment = _json_digest({
            "transcription": transcription,
            "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
            "fallback_end": 1.2,
        })
        return transcription, alignment

    german_transcription, german_alignment = keys(subtitle_language_code("German"))
    english_transcription, english_alignment = keys(subtitle_language_code("English"))
    assert german_transcription != english_transcription
    assert german_alignment != english_alignment

    cached = {
        "detected_language": "de",
        "words": [
            asdict(RecognizedWord(word, round(index * 0.25, 3), round(index * 0.25 + 0.2, 3), 0.95))
            for index, word in enumerate(_words(script))
        ],
    }
    aligner._cache_write("transcriptions", german_transcription, cached)
    aligner._cache_write("alignments", german_alignment, {"words": cached["words"]})

    # The mechanism works: the German entry round-trips …
    assert aligner._cache_read("transcriptions", german_transcription) is not None
    assert aligner._cache_read("alignments", german_alignment) is not None
    # … and the English lookup of the identical audio cannot see it.
    assert aligner._cache_read("transcriptions", english_transcription) is None
    assert aligner._cache_read("alignments", english_alignment) is None
    assert len(list((tmp_path / "alignment-cache").glob("transcriptions/*"))) == 1


def test_the_language_is_part_of_the_stage1_render_fingerprint():
    """§9: a language change must invalidate a cached render."""
    assert "subtitle_language" in _SUBTITLE_SETTING_FIELDS
    assert "subtitle_model" in _SUBTITLE_SETTING_FIELDS

    resolved = SimpleNamespace(
        width=1920, height=1080, fps=30.0, fps_expr="30", effective_durations=[2.0],
        transitions=[0.0], expected_duration=2.0, encoder="libx264",
        encoder_label="CPU", crf=18, preset="slow", quality_label="maximum",
    )
    base = ExportSettings(subtitle_enabled=True, subtitle_language="German")
    english = ExportSettings(subtitle_enabled=True, subtitle_language="English")
    german_payload = build_stage1_payload([], base, resolved, subtitle_requested=True)
    english_payload = build_stage1_payload([], english, resolved, subtitle_requested=True)
    assert german_payload["settings"]["subtitle_language"] == "German"
    assert english_payload["settings"]["subtitle_language"] == "English"
    assert json.dumps(german_payload, sort_keys=True) != json.dumps(english_payload, sort_keys=True)


def test_the_historical_german_value_is_stored_unchanged():
    """§15: the canonical representation was not renamed, so existing
    fingerprints, project files and cache keys keep their identity."""
    assert ExportSettings().subtitle_language == "German"
    assert normalize_subtitle_language("German") == "German"
    # The field itself is never rewritten by normalization; only readers resolve
    # it, which is what keeps a German project byte-identical.
    assert ExportSettings(subtitle_language=normalize_subtitle_language("de")).subtitle_language == "German"
    assert ExportSettings(subtitle_language=normalize_subtitle_language("Deutsch")).subtitle_language == "German"


# ==========================================================================
# 4. Persistence (§11)
# ==========================================================================

def test_settings_round_trip_keeps_the_selected_language(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    for language in ("English", "German"):
        store.save(ExportSettings(subtitle_language=language))
        assert store.load().subtitle_language == language


def test_a_legacy_file_without_the_language_key_loads_as_german(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"subtitle_enabled": True, "aspect": "16:9"}), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.subtitle_language == SUBTITLE_LANGUAGE_GERMAN
    assert loaded.aspect == "16:9"


def test_a_legacy_file_storing_auto_still_loads_without_being_rewritten(tmp_path):
    """§8: automatic detection stays supported for existing projects."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"subtitle_language": "Auto"}), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.subtitle_language == SUBTITLE_LANGUAGE_AUTO
    assert subtitle_language_code(loaded.subtitle_language) is None


def test_the_shipped_example_configuration_still_loads():
    example = Path(__file__).resolve().parents[1] / "config" / "example_settings.json"
    assert example.is_file(), example
    loaded = SettingsStore(example).load()
    assert normalize_subtitle_language(loaded.subtitle_language) == SUBTITLE_LANGUAGE_GERMAN


# ==========================================================================
# 5. CLI (§12)
# ==========================================================================

def _cli_settings(monkeypatch, argv: list[str]) -> ExportSettings:
    """Run app.cli.main() with the render orchestration stubbed out."""
    from app import cli

    captured: dict = {}

    class _Engine:
        def __init__(self, *_args, **_kwargs):
            pass

        def preflight(self, *_args, **_kwargs):
            return None

        def analyze(self, inputs, *_args, **_kwargs):
            return []

    class _Project:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_youtube_exports(self, _media, settings, _output, **_kwargs):
            captured["settings"] = settings
            return SimpleNamespace(primary_output=Path("out/video.mp4"))

    class _Store:
        def __init__(self, *_args, **_kwargs):
            pass

        def paths(self):
            return set()

        def add(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(cli, "locate_ffmpeg", lambda: (Path("ffmpeg"), Path("ffprobe")))
    monkeypatch.setattr(cli, "VideoMergerEngine", _Engine)
    monkeypatch.setattr(cli, "MainProjectEngine", _Project)
    monkeypatch.setattr(cli, "GeneratedOutputStore", _Store)
    monkeypatch.setattr(cli, "ProjectOrderStore", _Store)
    monkeypatch.setattr(cli, "discover_videos", lambda *_a, **_k: [Path("clip.mp4")])
    monkeypatch.setattr(cli, "order_media_for_video_order", lambda media, *_a, **_k: media)
    monkeypatch.setattr("builtins.print", lambda *_a, **_k: None)
    import sys

    monkeypatch.setattr(sys, "argv", ["app.cli"] + argv)
    assert cli.main() == 0
    return captured["settings"]


@pytest.mark.parametrize("flag, expected", [
    ("de", SUBTITLE_LANGUAGE_GERMAN),
    ("en", SUBTITLE_LANGUAGE_ENGLISH),
    ("German", SUBTITLE_LANGUAGE_GERMAN),
    ("English", SUBTITLE_LANGUAGE_ENGLISH),
    ("Auto", SUBTITLE_LANGUAGE_AUTO),
])
def test_the_cli_accepts_the_language_codes_and_the_legacy_spellings(monkeypatch, tmp_path, flag, expected):
    settings = _cli_settings(monkeypatch, [
        "--stage", "main", "--input", str(tmp_path), "--subtitles",
        "--language", flag, "--output", str(tmp_path / "out.mp4"),
    ])
    assert settings.subtitle_language == expected
    assert subtitle_language_code(settings.subtitle_language) == subtitle_language_code(expected)


def test_the_cli_defaults_to_german_and_rejects_an_unknown_language(monkeypatch, tmp_path):
    settings = _cli_settings(monkeypatch, [
        "--stage", "main", "--input", str(tmp_path), "--output", str(tmp_path / "out.mp4"),
    ])
    assert settings.subtitle_language == SUBTITLE_LANGUAGE_GERMAN

    import sys

    monkeypatch.setattr(sys, "argv", [
        "app.cli", "--stage", "main", "--input", str(tmp_path),
        "--language", "French", "--output", str(tmp_path / "out.mp4"),
    ])
    from app import cli

    with pytest.raises(SystemExit):
        cli.main()


# ==========================================================================
# 6. Diagnostics (§13)
# ==========================================================================

@pytest.mark.parametrize("language, label, code, forced", [
    ("German", "Deutsch", "de", "forced onto faster-whisper"),
    ("English", "English", "en", "forced onto faster-whisper"),
    ("en", "English", "en", "forced onto faster-whisper"),
    ("Auto", "Auto", "auto-detect", "detected by faster-whisper"),
])
def test_diagnostics_report_language_asr_code_and_alignment_reference(language, label, code, forced):
    items = run_project_diagnostics(ExportSettings(subtitle_enabled=True, subtitle_language=language))
    item = next(entry for entry in items if entry.name == "Subtitle Language")
    assert item.ok is True
    assert f"Subtitle Language: {label}" in item.detail
    assert f"ASR Language: {code}" in item.detail
    assert forced in item.detail
    assert "Alignment Reference: supplied script" in item.detail


def test_diagnostics_report_an_unusable_language_as_a_failed_item():
    items = run_project_diagnostics(ExportSettings(subtitle_enabled=True, subtitle_language="French"))
    item = next(entry for entry in items if entry.name == "Subtitle Language")
    assert item.ok is False
    assert "Unbekannte Untertitelsprache" in item.detail


# ==========================================================================
# 7. GUI (§10) - PySide6 cannot load in a headless sandbox, so the widget
#    wiring is pinned at source level, exactly like the other GUI contracts.
# ==========================================================================

def _gui_source() -> str:
    path = Path(__file__).resolve().parents[1] / "app" / "video_merger" / "gui" / "main_window.py"
    return path.read_text(encoding="utf-8")


def test_gui_offers_exactly_the_two_choices_with_canonical_item_data():
    source = _gui_source()
    # The old three-item list with English-only labels is gone.
    assert 'addItems(["German", "English", "Auto"])' not in source
    # The two choices come from the canonical model, and the stored value is the
    # item data rather than the visible label.
    assert "for language in SUBTITLE_LANGUAGE_CHOICES:" in source
    assert "addItem(SUBTITLE_LANGUAGE_LABELS[language], language)" in source
    assert "subtitle_language=str(self.subtitle_language_combo.currentData())" in source
    assert "subtitle_language_combo.currentText()" not in source
    assert 'QLabel("Speech Language")' in source


def test_gui_loader_never_silently_rewrites_a_stored_language():
    source = _gui_source()
    assert "def _load_subtitle_language(self, stored: object) -> None:" in source
    assert "self._load_subtitle_language(self.saved.subtitle_language)" in source
    # A legacy "Auto" project keeps its value: the entry is added on demand.
    assert "self.subtitle_language_combo.addItem(SUBTITLE_LANGUAGE_LABELS[language], language)" in source
    assert "except VideoMergerError:" in source


# ==========================================================================
# Real renders
# ==========================================================================

def _long_form_settings(language: str, voice: Path, script: Path, *, allow_warnings: bool = False):
    return ExportSettings(
        export_mode=EXPORT_MODE_LONG_FORM,
        aspect="16:9", resolution="160x90",
        voiceover_path=str(voice), voiceover_paths=[str(voice)],
        script_path=str(script), script_mode="single", script_paths=[str(script)],
        subtitle_enabled=True, subtitle_output_mode="with_subtitles",
        subtitle_language=language, subtitle_style="long_1",
        subtitle_animation="static_phrase", subtitle_position="Center",
        subtitle_font="modern_sans_bold",
        voiceover_order_mode="list", voiceover_pause=PAUSE, final_pause=0.0,
        original_audio_mode="mute", normalize_audio=False, ducking_enabled=False,
        video_order_mode="natural", workflow_stage="main", encoding="CPU",
        quality_preset="custom", crf=32, preset="ultrafast",
        allow_alignment_warnings=allow_warnings,
    )


def _render_long_form(tmp_path, ffmpeg, ffprobe, *, script_text, language, asr_tokens,
                      detected, allow_warnings=False, clips=3, logs=None):
    voice_path = tmp_path / "voice.wav"
    duration = _spoken_duration(script_text)
    _tone(ffmpeg, voice_path, 700, duration)
    script_path = tmp_path / "script.txt"
    script_path.write_text(script_text, encoding="utf-8")
    folder = tmp_path / "clips"
    folder.mkdir(exist_ok=True)
    media_paths = []
    for index in range(clips):
        clip = folder / f"clip_{index}.mp4"
        needed = (duration + 2 * LONG_FORM_INTRO_SECONDS) / clips + 0.8
        make_clip(ffmpeg, clip, size="160x90", duration=needed, color="navy", audio_rate=None)
        media_paths.append(clip)

    captured: list = []
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(media_paths, lambda _message: None)
    settings = _long_form_settings(language, voice_path, script_path, allow_warnings=allow_warnings)
    project = MainProjectEngine(engine)
    started = time.perf_counter()
    result = project.create_main(
        media, settings, tmp_path / "output",
        aligner=_aligner(asr_tokens, captured, duration, detected),
        log=logs.append if logs is not None else lambda _message: None,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < RENDER_BUDGET_SECONDS, f"render took {elapsed:.1f} s"
    return SimpleNamespace(result=result, captured=captured, duration=duration,
                           settings=settings, script_text=script_text)


def test_english_long_form_renders_synchronised_english_subtitles(ffmpeg_paths, tmp_path):
    """§17/§20/§26: measured English subtitle timing on a real render."""
    ffmpeg, ffprobe = ffmpeg_paths
    script_words = _words(ENGLISH_SCRIPT_A)
    run = _render_long_form(
        tmp_path, ffmpeg, ffprobe, script_text=ENGLISH_SCRIPT_A, language="English",
        asr_tokens=script_words, detected="en",
    )

    # The selected language was forced onto the ASR.
    assert run.captured == ["en"]

    video = Path(run.result.video)
    assert video.is_file()
    # The MainVideo path carries no visual Long-Form intro/outro, so the
    # timeline is the voiceover itself: the captions start with the first
    # spoken word and end with the last one.
    spoken = run.duration
    total = _probe_duration(ffprobe, video)
    assert total == pytest.approx(spoken, abs=0.12)

    srt = video.with_suffix(".srt")
    vtt = video.with_suffix(".vtt")
    assert srt.is_file() and vtt.is_file(), "one MP4 plus SRT/VTT sidecars"
    cues = _srt_cues(srt)

    # --- naturally segmented, never one enormous block --------------------
    assert len(cues) >= 4, f"expected several cues, got {len(cues)}"
    longest = max(cue[1] - cue[0] for cue in cues)
    assert longest < 0.5 * spoken, (
        f"one caption covers {longest:.2f} s of {spoken:.2f} s spoken audio"
    )

    # --- first / middle / final spoken sections follow the audio ----------
    speech_start = cues[0][0]
    assert speech_start == pytest.approx(0.05, abs=0.10), "captions start with the speech"
    assert cues[-1][1] == pytest.approx(spoken, abs=0.20), "the last caption ends the speech"
    assert cues[-1][1] <= total + 0.05
    middle_cue = cues[len(cues) // 2]
    assert middle_cue[0] == pytest.approx(spoken / 2, abs=0.9), "cues spread over the speech"

    # --- the caption text is the supplied script, in order ----------------
    caption_words = " ".join(cue[2] for cue in cues).split()
    assert len(caption_words) == len(script_words)
    assert [re.sub(r"[^\w']", "", word).casefold() for word in caption_words] == [
        re.sub(r"[^\w']", "", word).casefold() for word in script_words
    ]

    # --- timestamps are sensible: monotone, non-overlapping, inside the video
    for (start_a, end_a, _a), (start_b, _end_b, _b) in pairwise(cues):
        assert end_a > start_a
        assert start_b >= end_a, "cues must not overlap"
    assert cues[0][0] >= 0.0, "no caption before the first spoken word"
    assert cues[-1][1] <= total + 0.05, "no caption past the end of the video"

    # --- burned captions are really on screen -----------------------------
    assert _bright_pixels(ffmpeg, video, speech_start + 0.3) > 20
    assert _bright_pixels(ffmpeg, video, 0.0) == 0, "nothing burned before the first cue"


def test_german_long_form_control_is_unchanged(ffmpeg_paths, tmp_path):
    """§18: the default language still produces the historical result."""
    ffmpeg, ffprobe = ffmpeg_paths
    german_words = _words(GERMAN_SCRIPT)
    run = _render_long_form(
        tmp_path, ffmpeg, ffprobe, script_text=GERMAN_SCRIPT, language="German",
        asr_tokens=german_words, detected="de",
    )
    assert run.captured == ["de"], "Deutsch must force de onto the ASR"

    video = Path(run.result.video)
    cues = _srt_cues(video.with_suffix(".srt"))
    assert len(cues) >= 4
    assert max(cue[1] - cue[0] for cue in cues) < 0.5 * run.duration
    caption_words = " ".join(cue[2] for cue in cues).split()
    assert [re.sub(r"[^\w']", "", w).casefold() for w in caption_words] == [
        re.sub(r"[^\w']", "", w).casefold() for w in german_words
    ]
    # German umlauts survive into the burned captions and the sidecar.
    assert any("\u00e4" in cue[2] or "\u00fc" in cue[2] for cue in cues), cues
    # Same timeline as the English run: this path has no visual intro.
    assert cues[0][0] == pytest.approx(0.05, abs=0.10)
    assert _bright_pixels(ffmpeg, video, cues[0][0] + 0.3) > 20


def test_an_unalignable_script_fails_closed_instead_of_publishing_guesswork(ffmpeg_paths, tmp_path):
    """§19/§21: what a language drift looks like must not render silently.

    The audio is English and the script is English, but the recognition returns
    German wording - the measured consequence of forcing the wrong language.
    Almost nothing matches lexically, so every caption timestamp would be
    interpolated guesswork.
    """
    ffmpeg, ffprobe = ffmpeg_paths
    with pytest.raises(VideoMergerError) as excinfo:
        _render_long_form(
            tmp_path, ffmpeg, ffprobe, script_text=ENGLISH_SCRIPT_A, language="English",
            asr_tokens=_words(GERMAN_SCRIPT), detected="de",
        )
    message = str(excinfo.value)
    assert message.startswith("SUBTITLE GENERATION FAILED [language / script alignment]"), message
    assert "English" in message, "the failure must name the selected language"
    # Fail closed: no video, no sidecar, nothing that looks like a success.
    output = tmp_path / "output"
    assert not list(output.glob("*.mp4"))
    assert not list(output.glob("*.srt"))
    assert not list(output.glob("*.vtt"))


def test_the_mismatch_is_far_below_the_fail_closed_floor(ffmpeg_paths, tmp_path):
    """The floor is a wide margin, not a tightrope: a correct English run
    measures ~1.0, a noisy but correct one still ~0.9, a language drift ~0.05."""
    from app.video_merger.alignment import LocalWordAligner as _Aligner

    duration = _spoken_duration(ENGLISH_SCRIPT_A)
    correct = _Aligner(
        "phase26", _recognizer(_words(ENGLISH_SCRIPT_A), [], duration, "en"), use_cache=False,
    ).align(ENGLISH_SCRIPT_A, Path("voice.wav"), "English", fallback_end=duration)
    drifted = _Aligner(
        "phase26", _recognizer(_words(GERMAN_SCRIPT), [], duration, "de"), use_cache=False,
    ).align(ENGLISH_SCRIPT_A, Path("voice.wav"), "English", fallback_end=duration)

    assert correct.compatibility > 0.95
    assert drifted.compatibility < LANGUAGE_MISMATCH_COMPATIBILITY
    assert LANGUAGE_MISMATCH_COMPATIBILITY <= 0.20


def test_allow_alignment_warnings_is_the_documented_escape_hatch(ffmpeg_paths, tmp_path):
    """§21: the historical behaviour stays reachable through the existing
    setting, which now actually means something."""
    ffmpeg, ffprobe = ffmpeg_paths
    run = _render_long_form(
        tmp_path, ffmpeg, ffprobe, script_text=ENGLISH_SCRIPT_A, language="English",
        asr_tokens=_words(GERMAN_SCRIPT), detected="de", allow_warnings=True,
    )
    video = Path(run.result.video)
    assert video.is_file(), "with the escape hatch the render completes"
    cues = _srt_cues(video.with_suffix(".srt"))
    # The complete script is still captioned, and the uncertainty is explicit
    # in the alignment result rather than hidden.
    assert len(" ".join(cue[2] for cue in cues).split()) == len(_words(ENGLISH_SCRIPT_A))


def test_a_german_mismatch_keeps_the_historical_render_behaviour(ffmpeg_paths, tmp_path):
    """German is the byte-compatible baseline: a lexical mismatch stays a
    reported warning and never turns fatal, exactly as before this phase.

    The render below has a compatibility of ~0.04 (English wording recognised
    against a German script), which is what the fail-closed floor rejects for an
    explicit non-default selection - and what German has always rendered.
    """
    ffmpeg, ffprobe = ffmpeg_paths
    logs: list[str] = []
    run = _render_long_form(
        tmp_path, ffmpeg, ffprobe, script_text=GERMAN_SCRIPT, language="German",
        asr_tokens=_words(ENGLISH_SCRIPT_A), detected="en", logs=logs,
    )
    video = Path(run.result.video)
    assert video.is_file(), "the historical German path still renders"
    assert run.captured == ["de"]

    # The mismatch is reported for every language, not hidden.
    language_lines = [line for line in logs if line.startswith("Speech language:")]
    assert len(language_lines) == 1
    assert "Speech language: Deutsch · ASR language: de (forced)" in language_lines[0]
    assert "Alignment reference: supplied script" in language_lines[0]
    reported = float(re.search(r"compatibility (\d+[.,]\d+)%", language_lines[0]).group(1).replace(",", "."))
    assert reported < LANGUAGE_MISMATCH_COMPATIBILITY * 100

    # The authoritative script is still captioned in full.
    cues = _srt_cues(video.with_suffix(".srt"))
    assert len(" ".join(cue[2] for cue in cues).split()) == len(_words(GERMAN_SCRIPT))


def _shorts_settings(language: str, voices: list[Path], scripts: list[Path],
                     music: Path | None, groups: list[list[str]]):
    return ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        aspect="9:16", resolution="90x160",
        voiceover_paths=[str(item) for item in voices], voiceover_path=str(voices[0]),
        script_mode="matched", script_paths=[str(item) for item in scripts],
        script_path=str(scripts[0]),
        subtitle_enabled=True, subtitle_output_mode="with_subtitles",
        subtitle_language=language, short_subtitle_style="short_2",
        short_subtitle_animation="phrase_focus", short_subtitle_position="Top Center",
        subtitle_style="long_1", subtitle_animation="static_phrase",
        voiceover_order_mode="list", voiceover_pause=PAUSE,
        short_intro_seconds=SHORT_INTRO, short_outro_seconds=SHORT_OUTRO,
        final_pause=0.0, original_audio_mode="mute", normalize_audio=False,
        ducking_enabled=False, video_order_mode="natural", workflow_stage="main",
        encoding="CPU", quality_preset="custom", crf=32, preset="ultrafast",
        short_music_path=str(music) if music is not None else "", shorts_music_volume=44,
        short_script_groups=groups,
        music_path="",
    )


def test_grouped_english_short_keeps_one_continuous_english_timeline(ffmpeg_paths, tmp_path):
    """§14/§25 Test C: English Script A + English Script B stay ONE Short."""
    ffmpeg, ffprobe = ffmpeg_paths
    words_a, words_b = _words(ENGLISH_SCRIPT_A), _words(ENGLISH_SCRIPT_B)
    duration_a, duration_b = _spoken_duration(ENGLISH_SCRIPT_A), _spoken_duration(ENGLISH_SCRIPT_B)

    voice_a = _tone(ffmpeg, tmp_path / "voice_a.wav", 700, duration_a)
    voice_b = _tone(ffmpeg, tmp_path / "voice_b.wav", 900, duration_b)
    script_a = tmp_path / "script_a.txt"
    script_b = tmp_path / "script_b.txt"
    script_a.write_text(ENGLISH_SCRIPT_A, encoding="utf-8")
    script_b.write_text(ENGLISH_SCRIPT_B, encoding="utf-8")
    music = _tone(ffmpeg, tmp_path / "music.wav", MUSIC_HZ, 0.6)

    folder = tmp_path / "clips"
    folder.mkdir(exist_ok=True)
    media_paths = []
    for index in range(12):
        clip = folder / f"clip_{index}.mp4"
        make_clip(ffmpeg, clip, size="90x160", duration=1.5, color="navy", audio_rate=None)
        media_paths.append(clip)

    settings = _shorts_settings(
        "English", [voice_a, voice_b], [script_a, script_b], music,
        groups=[[str(voice_a), str(voice_b)]],
    )

    # One recognizer per member, each recording the language it was forced to.
    captured: list = []
    tokens = {"voice_a.wav": (words_a, duration_a), "voice_b.wav": (words_b, duration_b)}

    def recognize(path, language):
        captured.append((Path(path).name, language))
        words, duration = tokens[Path(path).name]
        step = (duration - 0.15) / max(1, len(words))
        return [
            RecognizedWord(word, round(0.05 + i * step, 3),
                           round(min(duration - 0.02, 0.05 + i * step + step * 0.8), 3), 0.95)
            for i, word in enumerate(words)
        ], "en"

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(media_paths, lambda _message: None)
    project = MainProjectEngine(engine)
    started = time.perf_counter()
    result = project.create_youtube_exports(
        media, settings, tmp_path / "output",
        aligner=LocalWordAligner("phase26", recognize, use_cache=False), log=lambda _m: None,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < RENDER_BUDGET_SECONDS, f"render took {elapsed:.1f} s"

    # --- ONE Short, ONE MP4, no Long-Form ---------------------------------
    assert result.long_form is None
    assert len(result.shorts) == 1
    short = result.shorts[0]
    video = Path(short.video)
    assert video.name == "001-002.mp4", video.name
    total = SHORT_INTRO + duration_a + PAUSE + duration_b + SHORT_OUTRO
    assert _probe_duration(ffprobe, video) == pytest.approx(total, abs=0.12)
    assert not (tmp_path / "output" / "LongForm").exists()

    # --- the selected language reached the ASR for BOTH members -----------
    assert captured == [("voice_a.wav", "en"), ("voice_b.wav", "en")], captured

    # --- ONE transcript: Script A then Script B ---------------------------
    transcript = video.with_suffix(".txt")
    assert transcript.is_file()
    text = transcript.read_text(encoding="utf-8")
    assert text.strip() == f"{ENGLISH_SCRIPT_A.strip()}\n\n{ENGLISH_SCRIPT_B.strip()}"

    # --- ONE continuous subtitle timeline with cumulative timestamps ------
    cues = _srt_cues(video.with_suffix(".srt"))
    assert len(cues) >= 6, f"expected natural segmentation, got {len(cues)}"
    member_b_speech_start = SHORT_INTRO + duration_a + PAUSE + 0.05
    later = [cue for cue in cues if cue[0] >= member_b_speech_start - 0.2]
    assert later, "Script B must be captioned after the accumulated offset"
    assert later[0][0] == pytest.approx(member_b_speech_start, abs=0.35), (
        f"Script B starts at {later[0][0]:.3f}, expected ~{member_b_speech_start:.3f}"
    )
    earlier = [cue for cue in cues if cue[0] < member_b_speech_start - 0.2]
    assert earlier and earlier[0][0] == pytest.approx(SHORT_INTRO + 0.05, abs=0.3)
    # No caption resets to zero at the group boundary and none is enormous.
    assert min(cue[0] for cue in cues) >= SHORT_INTRO - 0.05
    assert max(cue[1] - cue[0] for cue in cues) < 0.5 * (duration_a + duration_b)
    # Both members' wording is present, in order.
    caption_words = [re.sub(r"[^\w']", "", w).casefold()
                     for w in " ".join(cue[2] for cue in cues).split()]
    expected = [re.sub(r"[^\w']", "", w).casefold() for w in words_a + words_b]
    assert caption_words == expected

    # --- ONE music timeline covering the complete grouped Short -----------
    for probe_at in (0.05, SHORT_INTRO + 0.3, member_b_speech_start + 0.3, total - 0.2):
        assert _strength(ffmpeg, video, probe_at, 0.12, MUSIC_HZ) > 0.005, (
            f"the Shorts music stops at {probe_at:.3f} s"
        )

    # --- burned captions are on screen while speaking ---------------------
    assert _bright_pixels(ffmpeg, video, SHORT_INTRO + 0.3) > 20
    assert _bright_pixels(ffmpeg, video, member_b_speech_start + 0.3) > 20


def test_ungrouped_english_shorts_stay_separate_with_the_same_language(ffmpeg_paths, tmp_path):
    """§14: without a group the two English scripts are two Shorts, each with
    its own timeline starting after its own intro - and both forced to en."""
    ffmpeg, ffprobe = ffmpeg_paths
    words_a, words_b = _words(ENGLISH_SCRIPT_A), _words(ENGLISH_SCRIPT_B)
    duration_a, duration_b = _spoken_duration(ENGLISH_SCRIPT_A), _spoken_duration(ENGLISH_SCRIPT_B)

    voice_a = _tone(ffmpeg, tmp_path / "voice_a.wav", 700, duration_a)
    voice_b = _tone(ffmpeg, tmp_path / "voice_b.wav", 900, duration_b)
    script_a = tmp_path / "script_a.txt"
    script_b = tmp_path / "script_b.txt"
    script_a.write_text(ENGLISH_SCRIPT_A, encoding="utf-8")
    script_b.write_text(ENGLISH_SCRIPT_B, encoding="utf-8")

    folder = tmp_path / "clips"
    folder.mkdir(exist_ok=True)
    media_paths = []
    for index in range(10):
        clip = folder / f"clip_{index}.mp4"
        make_clip(ffmpeg, clip, size="90x160", duration=1.5, color="navy", audio_rate=None)
        media_paths.append(clip)

    # Deliberately without a Shorts music bed: a looped 0.6 s track drives the
    # FFmpeg 6.0 ``-stream_loop -1`` deadlock documented in
    # ``command_builder.music_outro_loop`` at exactly these Short durations.
    # Measured here to be language independent (the German control stalls the
    # same way, English without music renders both Shorts in 2.8 s), so it is
    # unrelated to this phase. The grouped test above keeps the music bed and
    # passes, which is where the single music timeline is asserted.
    settings = _shorts_settings("English", [voice_a, voice_b], [script_a, script_b], None, groups=[])
    captured: list = []
    tokens = {"voice_a.wav": (words_a, duration_a), "voice_b.wav": (words_b, duration_b)}

    def recognize(path, language):
        captured.append((Path(path).name, language))
        words, duration = tokens[Path(path).name]
        step = (duration - 0.15) / max(1, len(words))
        return [
            RecognizedWord(word, round(0.05 + i * step, 3),
                           round(min(duration - 0.02, 0.05 + i * step + step * 0.8), 3), 0.95)
            for i, word in enumerate(words)
        ], "en"

    engine = VideoMergerEngine(ffmpeg, ffprobe)
    media = engine.analyze(media_paths, lambda _message: None)
    result = MainProjectEngine(engine).create_youtube_exports(
        media, settings, tmp_path / "output",
        aligner=LocalWordAligner("phase26", recognize, use_cache=False), log=lambda _m: None,
    )
    assert result.long_form is None
    assert len(result.shorts) == 2
    names = sorted(Path(short.video).name for short in result.shorts)
    assert names == ["001.mp4", "002.mp4"]
    assert captured == [("voice_a.wav", "en"), ("voice_b.wav", "en")]

    first, second = sorted((Path(short.video) for short in result.shorts), key=lambda p: p.name)
    assert _probe_duration(ffprobe, first) == pytest.approx(SHORT_INTRO + duration_a + SHORT_OUTRO, abs=0.12)
    assert _probe_duration(ffprobe, second) == pytest.approx(SHORT_INTRO + duration_b + SHORT_OUTRO, abs=0.12)
    # Each Short captions only its own script, starting after its own intro.
    cues_first = _srt_cues(first.with_suffix(".srt"))
    cues_second = _srt_cues(second.with_suffix(".srt"))
    assert cues_first[0][0] == pytest.approx(SHORT_INTRO + 0.05, abs=0.3)
    assert cues_second[0][0] == pytest.approx(SHORT_INTRO + 0.05, abs=0.3)
    assert [re.sub(r"[^\w']", "", w).casefold() for w in " ".join(c[2] for c in cues_second).split()] == [
        re.sub(r"[^\w']", "", w).casefold() for w in words_b
    ]
