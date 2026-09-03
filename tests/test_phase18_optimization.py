"""Regression coverage for shared Shorts consumption, fair ordering, and alignment fallbacks."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.video_merger.alignment import LocalWordAligner, RecognizedWord, script_word_spans
from app.video_merger.models import AudioInfo, ExportSettings, MainVideoResult, MediaInfo, ValidationReport
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.subtitle_presets import get_preset
from app.video_merger.subtitles import build_cues, validate_cues
from app.video_merger.video_pool import ShortsVideoPool, order_media_for_video_order
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_SHORTS,
    build_short_jobs,
    long_form_settings,
    short_settings,
)


def _media(path: str, duration: float = 2.0) -> MediaInfo:
    return MediaInfo(
        path=Path(path), duration=duration, width=1920, height=1080,
        effective_width=1920, effective_height=1080, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="", audio=AudioInfo(),
    )


def test_shared_shorts_pool_consumes_distinct_prefixes_before_reuse():
    pool = ShortsVideoPool([_media(f"/pool/clip_{index}.mp4") for index in range(4)])

    first = pool.take_for_duration(1.0, 0.0, 30.0, "hold")
    second = pool.take_for_duration(3.0, 0.0, 30.0, "hold")
    third = pool.take_for_duration(3.0, 0.0, 30.0, "hold")
    fourth = pool.take_for_duration(1.0, 0.0, 30.0, "hold")

    assert [item.path.name for item in first] == ["clip_0.mp4"]
    assert [item.path.name for item in second] == ["clip_1.mp4", "clip_2.mp4"]
    assert [item.path.name for item in third] == ["clip_3.mp4"]
    assert [item.path.name for item in fourth] == ["clip_0.mp4"]
    assert pool.rounds_completed == 1


def test_random_order_is_seedable_permutation_and_first_position_is_not_folder_weighted():
    media = [
        _media(f"/pool/A/a_{index}.mp4") for index in range(9)
    ] + [_media("/pool/B/b_0.mp4")]
    ordered = order_media_for_video_order(media, "random", seed=17)
    repeated = order_media_for_video_order(media, "random", seed=17)

    assert [item.path for item in ordered] == [item.path for item in repeated]
    assert {item.path for item in ordered} == {item.path for item in media}

    # The first item is sampled from the complete eligible clip pool, not from
    # an equal-weight folder choice. A small seeded sample catches the former
    # implementation's strong short-folder bias without asserting a sequence.
    first_folder_counts = {"A": 0, "B": 0}
    for seed in range(200):
        first = order_media_for_video_order(media, "random", seed=seed)[0]
        first_folder_counts[first.path.parent.name] += 1
    assert first_folder_counts["A"] > 150
    assert first_folder_counts["B"] < 50


def test_shorts_jobs_share_pool_and_use_separate_mobile_subtitle_profile(tmp_path):
    voices = []
    for index in range(3):
        path = tmp_path / f"voice_{index}.wav"
        path.write_bytes(b"audio")
        voices.append(path)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        short_subtitle_font="inter",
    )
    jobs = build_short_jobs(settings)
    assert short_settings(settings, jobs[0]).subtitle_font == "inter"
    assert short_settings(settings, jobs[0]).aspect == "9:16"
    assert short_settings(settings, jobs[0]).subtitle_position == "Bottom Center"
    assert get_preset("long_1").collection == "long"
    assert get_preset("short_1").collection == "short"
    assert ExportSettings().subtitle_animation == "static_phrase"
    assert ExportSettings().short_subtitle_font == "inter"

    settings = ExportSettings(
        subtitle_style="long_3", subtitle_animation="outline_highlight",
        short_subtitle_style="short_4", short_subtitle_animation="color_change",
    )
    long_settings = long_form_settings(settings)
    short_profile = short_settings(settings, jobs[0])
    assert (long_settings.aspect, long_settings.subtitle_style, long_settings.subtitle_animation) == (
        "16:9", "long_3", "outline_highlight",
    )
    assert (short_profile.aspect, short_profile.subtitle_style, short_profile.subtitle_animation) == (
        "9:16", "short_4", "color_change",
    )


def test_create_youtube_exports_assigns_distinct_prefixes_before_pool_reuse(tmp_path):
    class ProbeEngine:
        ffprobe_path = tmp_path / "ffprobe"

    project = MainProjectEngine(ProbeEngine())
    media = [_media(f"/pool/clip_{index}.mp4") for index in range(3)]
    voices = []
    for index in range(3):
        path = tmp_path / f"voice_{index}.wav"
        path.write_bytes(b"audio")
        voices.append(path)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        subtitle_enabled=False,
        final_pause=0.0,
    )
    seen_media = []

    def fake_create_main(job_media, job_settings, output_dir, **kwargs):
        seen_media.append([item.path for item in job_media])
        path = Path(output_dir) / f"{kwargs['output_stem']}.mp4"
        report = ValidationReport(
            True, [], path, duration=1.0, width=1080, height=1920,
            fps=30.0, has_video=True,
        )
        return MainVideoResult(path, None, None, report)

    project.create_main = fake_create_main  # type: ignore[method-assign]
    with patch(
        "app.video_merger.main_project.probe_audio",
        side_effect=lambda _ffprobe, path: SimpleNamespace(duration=1.0),
    ):
        project.create_youtube_exports(media, settings, tmp_path / "output")
    assert seen_media == [
        [Path("/pool/clip_0.mp4")],
        [Path("/pool/clip_1.mp4")],
        [Path("/pool/clip_2.mp4")],
    ]


def test_create_youtube_exports_passes_one_shared_pool_to_every_short(tmp_path):
    project = MainProjectEngine(object())
    voices = []
    for index in range(3):
        path = tmp_path / f"voice_{index}.wav"
        path.write_bytes(b"audio")
        voices.append(path)
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=[str(path) for path in voices],
        subtitle_enabled=False,
    )
    pools = []

    def fake_create_main(media, job_settings, output_dir, **kwargs):
        pools.append(kwargs.get("short_video_pool"))
        path = Path(output_dir) / f"{kwargs['output_stem']}.mp4"
        report = ValidationReport(
            True, [], path, duration=1.0, width=1080, height=1920,
            fps=30.0, has_video=True,
        )
        return MainVideoResult(path, None, None, report)

    project.create_main = fake_create_main  # type: ignore[method-assign]
    # The fake does not need FFprobe or video files; this checks orchestration
    # state handoff while ShortsVideoPool itself is covered above.
    project.create_youtube_exports([], settings, tmp_path / "output")
    assert len(pools) == 3
    assert pools[0] is not None
    assert pools[0] is pools[1] is pools[2]


def test_alignment_without_acoustic_words_stays_inside_known_program_duration(tmp_path):
    script = "One two three four five six"
    result = LocalWordAligner("tiny", lambda _path, _language: ([], "en")).align(
        script, tmp_path / "voice.wav", "English", fallback_end=0.6,
    )
    assert [word.text for word in result.words] == [
        token for token, _start, _end in script_word_spans(script)
    ]
    assert all(0.0 <= word.start < word.end <= 0.6 for word in result.words)


def test_alignment_retains_script_coverage_and_copies_available_times(tmp_path):
    script = "Alpha Bravo Gamma"
    aligner = LocalWordAligner(
        "tiny",
        lambda _path, _language: (
            [
                RecognizedWord("Alpha", 0.10, 0.32, 0.95),
                RecognizedWord("Gamma", 0.80, 1.06, 0.90),
            ],
            "en",
        ),
    )
    result = aligner.align(script, tmp_path / "voice.wav", "English")

    assert [word.text for word in result.words] == [token for token, _a, _b in script_word_spans(script)]
    assert result.words[0].start == pytest.approx(0.10)
    assert result.words[0].end == pytest.approx(0.32)
    assert result.words[2].start == pytest.approx(0.80)
    assert result.words[2].end == pytest.approx(1.06)
    assert result.words[1].start == pytest.approx(0.32)
    assert result.words[1].end == pytest.approx(0.56)
    assert result.words[1].confidence == 0.0

    cues = build_cues(script, result, "long_1", program_end=2.0)
    validate_cues(cues, len(result.words))
    assert sum(len(cue.words) for cue in cues) == len(script_word_spans(script))
    assert "retained" in " ".join(result.warnings)
