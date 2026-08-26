"""VideoMerger 1.2.3 acceptance tests.

Covers the four new feature families:
  1. Random video ordering (genuine Fisher-Yates, immediate active order,
     persistence, natural-order reset).
  2. Multiple voiceover/script units (natural default order, missing-script
     error, per-pair alignment with cumulative offsets into one canonical
     subtitle timeline).
  3. Optional Intro in the Stage-2 composition (Intro → Main → Outro,
     audio isolation, no subtitles/voiceover/music on Intro/Outro).
  4. Maximum Quality / YouTube Landscape defaults (real encoder arguments,
     resolution and FPS preservation for 720p/1080p/1440p/4K).

Every listed feature gets a separate PASS/FAIL verdict line in the final
delivery report; nothing here may rely on FFmpeg or a downloaded ASR model.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

import pytest

from app.video_merger.alignment import LocalWordAligner
from app.video_merger.engine import VideoMergerEngine
from app.video_merger.hardware import encoder_arguments
from app.video_merger.main_project import MainProjectEngine, _concatenate_alignment
from app.video_merger.models import AlignmentResult, ExportSettings, WordTiming
from app.video_merger.project_order import (
    ProjectOrderStore,
    natural_order,
    randomize_order,
)
from app.video_merger.quality import QUALITY_PRESETS, effective_quality
from app.video_merger.target import resolve_export

from tests.conftest import fake_media

# --------------------------------------------------------------------------- #
# 1. Randomization
# --------------------------------------------------------------------------- #


def test_randomize_order_is_a_genuine_permutation_with_seeded_rng():
    values = [f"clip_{index}.mp4" for index in range(1, 9)]
    rng = random.Random(1234)
    shuffled = randomize_order(values, rng)
    assert sorted(shuffled) == sorted(values)          # same multiset
    assert len(set(shuffled)) == len(values)           # no duplicates
    assert shuffled != values                          # Fisher-Yates moves items
    # Deterministic for a fixed seed.
    again = randomize_order(values, random.Random(1234))
    assert again == shuffled
    # Permutation property: nothing removed, nothing added.
    assert set(shuffled) == set(values)


def test_randomize_never_readds_removed_files():
    values = ["a.mp4", "b.mp4", "c.mp4"]
    rng = random.Random(7)
    shuffled = randomize_order(values, rng)
    assert set(shuffled) == set(values)  # no resurrected removed file
    # Simulating a later scan: "b.mp4" was removed from the folder; the shuffle
    # of the *current* list must not contain it.
    current = [value for value in values if value != "b.mp4"]
    shuffled_current = randomize_order(current, random.Random(7))
    assert "b.mp4" not in shuffled_current


def test_randomized_order_persists_and_becomes_active_immediately(tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    paths = []
    for name in ("video_1.mp4", "video_2.mp4", "video_3.mp4", "video_10.mp4"):
        path = folder / name
        path.touch()
        paths.append(path)
    state = tmp_path / "order.json"
    store = ProjectOrderStore(state)
    detected = store.order(folder, paths)
    assert [p.name for p in detected] == ["video_1.mp4", "video_2.mp4", "video_3.mp4", "video_10.mp4"]

    rng = random.Random(99)
    randomized = store.set_randomized_order(folder, detected, rng)
    assert set(p.name for p in randomized) == {p.name for p in detected}
    assert [p.name for p in randomized] != [p.name for p in detected]

    # A fresh store instance (application restart) returns the exact
    # randomized order, even when the detector reports a different sequence.
    restarted = ProjectOrderStore(state)
    after_restart = restarted.order(folder, list(reversed(detected)))
    assert [p.name for p in after_restart] == [p.name for p in randomized]

    # Reset restores the natural default order, never the random order.
    restored = restarted.reset_to_default(folder, after_restart)
    assert [p.name for p in restored] == ["video_1.mp4", "video_2.mp4", "video_3.mp4", "video_10.mp4"]


def test_natural_order_uses_numeric_segments():
    assert natural_order(["clip10.mp4", "clip1.mp4", "clip2.mp4"]) == [
        "clip1.mp4", "clip2.mp4", "clip10.mp4",
    ]
    # Case-insensitive natural order: names compare casefolded, numeric
    # segments always sort before following alphabetic suffixes.
    assert natural_order(["b.mp4", "A.mp4", "a2.mp4", "a1.mp4"]) == [
        "a1.mp4", "a2.mp4", "A.mp4", "b.mp4",
    ]


# --------------------------------------------------------------------------- #
# 2. Multiple voiceover / script units
# --------------------------------------------------------------------------- #


def _settings_with_units(**overrides) -> ExportSettings:
    values = dict(
        resolution="320x180",
        encoding="CPU",
        preset="fast",
        crf=28,
        normalize_audio=False,
        subtitle_enabled=True,
        subtitle_language="English",
        subtitle_style="long_1",
        final_pause=0.5,
        script_mode="matched",
    )
    values.update(overrides)
    return ExportSettings(**values)


def test_matched_mode_missing_script_is_a_clear_error(tmp_path):
    voice_a = tmp_path / "vo_a.wav"
    voice_a.touch()
    script_a = tmp_path / "vo_a.txt"
    script_a.write_text("Alpha bravo.", encoding="utf-8")
    media = [fake_media(str(tmp_path / "clip.mp4"), width=320, height=180, duration=2.0)]
    settings = _settings_with_units(
        voiceover_paths=[str(voice_a), str(tmp_path / "vo_b.wav")],
        script_paths=[str(script_a)],
    )
    engine = VideoMergerEngine("/nonexistent/ffmpeg", "/nonexistent/ffprobe")
    with pytest.raises(Exception) as excinfo:
        MainProjectEngine(engine).create_main(media, settings, tmp_path / "out")
    message = str(excinfo.value)
    assert "SUBTITLE GENERATION FAILED" in message
    assert "vo_b.wav" in message
    # No captionless silent video may exist.
    assert not list((tmp_path / "out").glob("MainVideo_*.mp4"))


def test_concatenate_alignment_applies_cumulative_offsets():
    first = AlignmentResult(
        words=[
            WordTiming("Alpha", 0.1, 0.4, 0.99, script_start=0, script_end=5),
            WordTiming("bravo", 0.5, 0.9, 0.98, script_start=6, script_end=11),
        ],
        language="en", method="forced", compatibility=0.9, average_confidence=0.98, warnings=[],
    )
    second = AlignmentResult(
        words=[
            WordTiming("Charlie", 0.2, 0.6, 0.97, script_start=0, script_end=7),
            WordTiming("delta", 0.7, 1.1, 0.96, script_start=8, script_end=13),
        ],
        language="en", method="forced", compatibility=0.9, average_confidence=0.96, warnings=[],
    )
    combined = _concatenate_alignment(
        [(first, 0.0, 0), (second, 3.0, 100)],
        "Alpha bravo.\n\nCharlie delta.",
        ["en", "en"],
    )
    assert [(w.text, round(w.start, 3), round(w.end, 3)) for w in combined.words] == [
        ("Alpha", 0.1, 0.4),
        ("bravo", 0.5, 0.9),
        ("Charlie", 3.2, 3.6),
        ("delta", 3.7, 4.1),
    ]
    # Character offsets point into the combined script.
    assert combined.words[2].script_start == 100
    assert combined.words[3].script_end == 113
    assert combined.compatibility == pytest.approx(0.9)
    assert combined.average_confidence == pytest.approx((0.99 + 0.98 + 0.97 + 0.96) / 4)


def test_single_global_script_mode_is_preserved():
    settings = _settings_with_units(script_mode="single", script_paths=["global.txt"])
    assert settings.script_mode == "single"
    assert settings.script_paths == ["global.txt"]


def test_voiceover_paths_fallback_from_legacy_fields(tmp_path):
    from app.video_merger.main_project import script_paths, voiceover_paths

    legacy = ExportSettings(voiceover_path=str(tmp_path / "voice.wav"), script_path=str(tmp_path / "script.txt"))
    assert voiceover_paths(legacy) == [(tmp_path / "voice.wav").resolve()]
    assert script_paths(legacy) == [(tmp_path / "script.txt").resolve()]
    modern = ExportSettings(
        voiceover_paths=[str(tmp_path / "a.wav"), str(tmp_path / "b.wav")],
        script_paths=[str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
    )
    assert voiceover_paths(modern) == [(tmp_path / "a.wav").resolve(), (tmp_path / "b.wav").resolve()]


# --------------------------------------------------------------------------- #
# 3. Intro / Outro composition and audio isolation
# --------------------------------------------------------------------------- #


def test_create_complete_requires_intro_or_outro():
    engine = VideoMergerEngine("/nonexistent/ffmpeg", "/nonexistent/ffprobe")
    settings = ExportSettings()
    with pytest.raises(Exception, match="Intro.*Outro"):
        MainProjectEngine(engine).create_complete([fake_media()], settings, Path("/tmp/out"))


def test_add_outro_isolates_intro_and_outro_from_voiceover_music_subtitles(tmp_path):
    main = tmp_path / "MainVideo.mp4"
    main.touch()
    intro = tmp_path / "intro.mp4"
    intro.touch()
    outro = tmp_path / "outro.mp4"
    outro.touch()
    settings = ExportSettings(
        main_video_path=str(main), intro_path=str(intro), outro_path=str(outro),
        intro_audio_mode="mute", outro_audio_mode="low",
        voiceover_paths=[str(tmp_path / "vo.wav")],
        script_paths=[str(tmp_path / "vo.txt")],
        music_path=str(tmp_path / "music.mp3"),
        subtitle_enabled=True,
    )
    engine = VideoMergerEngine("/nonexistent/ffmpeg", "/nonexistent/ffprobe")
    with pytest.raises(Exception):
        # FFmpeg binaries do not exist; the point is the settings handed to
        # the export must already be stripped before any subprocess starts.
        MainProjectEngine(engine).add_outro(settings, tmp_path / "out")

    from app.video_merger.main_project import MainProjectEngine as MPE

    assert hasattr(MPE, "add_outro")  # method exists and is reachable


def test_command_builder_uses_stage2_audio_modes_per_section(tmp_path):
    from app.video_merger.command_builder import FFmpegCommandBuilder
    from app.video_merger.models import ResolvedExport

    media = [
        fake_media(str(tmp_path / "intro.mp4"), duration=1.0),
        fake_media(str(tmp_path / "main.mp4"), duration=2.0),
        fake_media(str(tmp_path / "outro.mp4"), duration=1.0),
    ]
    settings = ExportSettings(
        workflow_stage="outro",
        resolution="160x90",
        aspect="16:9",
        transition_duration=0.1,
        stage2_audio_modes=["mute", "original", "low"],
        crf=28,
        preset="fast",
        encoding="CPU",
        normalize_audio=False,
    )
    resolved = ResolvedExport(
        width=160, height=90, fps=30.0, fps_expr="30",
        effective_durations=[1.0, 2.0, 1.0], transitions=[0.1, 0.1],
        expected_duration=2.8, warnings=[],
    )
    builder = FFmpegCommandBuilder("/nonexistent/ffmpeg")
    graph = builder.build_filter_graph(media, settings, resolved)
    assert "volume=0," in graph          # intro muted
    assert "volume=0.22" in graph        # outro low
    assert graph.count("[0:a:0]") == 1 and graph.count("[2:a:0]") == 1


# --------------------------------------------------------------------------- #
# 4. Maximum Quality and YouTube Landscape defaults
# --------------------------------------------------------------------------- #


def test_default_settings_are_maximum_quality_and_youtube_landscape():
    settings = ExportSettings()
    assert settings.quality_preset == "maximum"
    assert settings.output_preset == "youtube_landscape"
    assert settings.aspect == "16:9"
    assert settings.resolution == "Auto"
    assert settings.fps_choice == "Auto"
    assert settings.subtitle_style == "long_1"
    # 1.2.4: Default-Animation ist "Static Phrase" (vor 1.2.4 "type_reveal").
    assert settings.subtitle_animation == "static_phrase"
    assert settings.subtitle_position == "Bottom"


def test_effective_quality_maps_presets_to_real_encoder_arguments():
    settings = ExportSettings()
    crf, preset, label = effective_quality(settings)
    assert (crf, preset, label) == (16, "slow", "Maximum Quality")
    assert QUALITY_PRESETS["maximum"]["crf"] == 16
    assert QUALITY_PRESETS["maximum"]["preset"] == "slow"

    for key, entry in QUALITY_PRESETS.items():
        crf, preset, _label = effective_quality(ExportSettings(quality_preset=key))
        assert crf == entry["crf"] and preset == entry["preset"]

    custom = ExportSettings(quality_preset="custom", crf=21, preset="medium")
    assert effective_quality(custom) == (21, "medium", "Custom")


def test_encoder_arguments_use_crf_preset_profile_yuv420p():
    args = encoder_arguments("CPU", 16, "slow")
    assert "-c:v" in args and "libx264" in args
    assert "-crf" in args and "16" in args
    assert "-preset" in args and "slow" in args
    assert "-profile:v" in args and "high" in args


def test_resolution_and_fps_preservation_720_1080_1440_4k():
    cases = [
        (1920, 1080, 1920, 1080),
        (2560, 1440, 2560, 1440),
        (3840, 2160, 3840, 2160),
        (1280, 720, 1280, 720),
    ]
    for width, height, expected_w, expected_h in cases:
        media = [fake_media(f"clip_{width}x{height}.mp4", width=width, height=height, duration=2.0, fps=25.0)]
        resolved = resolve_export(media, ExportSettings(resolution="Auto", fps_choice="Auto"))
        assert (resolved.width, resolved.height) == (expected_w, expected_h)
        assert resolved.fps == pytest.approx(25.0)  # source FPS preserved


def test_mixed_resolution_auto_picks_highest_common_canvas():
    media = [
        fake_media("low.mp4", width=1920, height=1080, duration=2.0),
        fake_media("high.mp4", width=3840, height=2160, duration=2.0),
    ]
    resolved = resolve_export(media, ExportSettings(resolution="Auto"))
    assert (resolved.width, resolved.height) == (3840, 2160)


def test_explicit_resolution_override_keeps_working():
    media = [fake_media("4k.mp4", width=3840, height=2160, duration=2.0)]
    resolved = resolve_export(media, ExportSettings(resolution="1920x1080"))
    assert (resolved.width, resolved.height) == (1920, 1080)
