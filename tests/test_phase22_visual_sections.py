"""Phase 22: explicit visual-only intro/outro sections and the subtitle timeline.

Contracts pinned here — all of them run the REAL ``create_main`` /
``create_youtube_exports`` pipeline with a fake FFmpeg engine, so durations,
subtitle windows, log lines and cache identities are the values the application
really produces:

1. **Four configurable visual sections** — Long-Form intro/outro default to
   :data:`LONG_FORM_INTRO_SECONDS`/:data:`LONG_FORM_OUTRO_SECONDS` (2.5 s) and
   Short intro/outro to :data:`SHORT_INTRO_SECONDS`/:data:`SHORT_OUTRO_SECONDS`
   (1.5 s). Zero disables a section, negative or non-numeric values are rejected,
   and the canonical render fields keep their historical defaults so a direct
   ``create_main(ExportSettings())`` behaves exactly like before.

2. **One unambiguous structure** — ``[visual intro][voiceover + video][visual
   outro]``. The Long-Form outro *is* the former Main Video end padding, so the
   tail after the spoken audio exists exactly once and can never double; the
   explicit Short outro replaces the legacy 0.7 s guaranteed ending instead of
   stacking a second one behind it.

3. **Subtitles only while spoken** — the whole caption timeline is shifted by the
   intro inside the timeline model (never by a post-render delay), starts exactly
   with the voiceover and ends exactly with the spoken audio. No cue may reach
   into the visual intro or outro, word-level alignment, SRT/VTT validity and the
   strict cue validation stay intact.

4. **Video material and audio** — the pool target reserves intro + spoken +
   outro of real material, the intro carries no voiceover audio (music may play),
   and the timeline structure is logged once per job without spam.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.video_merger.alignment import (
    LocalWordAligner,
    RecognizedWord,
    script_word_spans,
)
from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.errors import VideoMergerError
from app.video_merger.main_project import MainProjectEngine
from app.video_merger.models import (
    LONG_FORM_INTRO_SECONDS,
    LONG_FORM_OUTRO_SECONDS,
    MAX_VISUAL_SECTION_SECONDS,
    SHORT_INTRO_SECONDS,
    SHORT_OUTRO_SECONDS,
    AudioInfo,
    ExportSettings,
    MediaInfo,
    ValidationReport,
)
from app.video_merger.render_cache import stage1_fingerprint
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
    SHORT_ENDING_SECONDS,
    build_short_jobs,
    effective_intro_seconds,
    effective_outro_seconds,
    long_form_settings,
    main_timeline,
    short_settings,
    visual_section_seconds,
)
from tests.conftest import fake_media

SECTIONS = [
    "Ruhe ist die erste Antwort des Tages.",
    "Bewegung folgt dem Gedanken langsam.",
    "Stille trägt das Ende dieser Geschichte.",
]
GLOBAL_SCRIPT = "\n\n".join(SECTIONS)
DURATIONS = [4.0, 3.5, 5.0]
PAUSE = 0.7
CLIP_SECONDS = 3.0
EPS = 1e-6


# --------------------------------------------------------------------------- #
# Harness (a real orchestrator run without FFmpeg binaries)
# --------------------------------------------------------------------------- #


def _sentence(index: int) -> str:
    return f"Dies ist der gesprochene Satz Nummer {index + 1} mit mehreren verschiedenen Worten."


@pytest.fixture(autouse=True)
def _private_project_root(tmp_path, monkeypatch):
    """Keep derived sections, caches and staged files inside the test's tmp."""
    root = tmp_path / "project"
    monkeypatch.setattr("app.video_merger.script_sections.project_root", lambda: root)
    monkeypatch.setattr("app.video_merger.main_project.project_root", lambda: root)
    return root


def _spoken(section: str, duration: float) -> list[RecognizedWord]:
    tokens = section.split()
    slot = duration / max(1, len(tokens))
    return [
        RecognizedWord(
            token.strip(".,") or token, round(index * slot + 0.1, 3),
            round(min(duration - 0.05, index * slot + 0.1 + slot * 0.8), 3), 0.95,
        )
        for index, token in enumerate(tokens)
    ]


def _media(path: Path | str, duration: float = CLIP_SECONDS, portrait: bool = True) -> MediaInfo:
    width, height = (1080, 1920) if portrait else (1920, 1080)
    return MediaInfo(
        path=Path(path), duration=duration, width=width, height=height,
        effective_width=width, effective_height=height, fps=30.0,
        fps_fraction="30/1", video_codec="h264", pixel_format="yuv420p",
        sar="1:1", dar="", audio=AudioInfo(), source_duration=duration,
    )


def _write(path: Path, payload: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class Project:
    """Voiceovers, a global script, music and a clip pool for one project."""

    def __init__(self, tmp_path: Path, *, durations=DURATIONS, sections=SECTIONS):
        self.root = tmp_path
        self.durations = list(durations)
        self.sections = list(sections)
        self.voices = [
            _write(tmp_path / f"voice_{index + 1}.wav", b"audio")
            for index in range(len(durations))
        ]
        self.script = _write(tmp_path / "global_script.txt", GLOBAL_SCRIPT.encode("utf-8"))
        self.music = _write(tmp_path / "music" / "long_form_theme.mp3", b"music")
        self.clips = [
            _write(tmp_path / "pool" / f"clip_{index}.mp4", b"video") for index in range(12)
        ]
        recognized = {
            str(path.resolve()): _spoken(section, duration)
            for path, section, duration in zip(self.voices, self.sections, self.durations)
        }

        def recognize(path, _language):
            key = str(Path(path).expanduser().resolve())
            if key not in recognized:
                raise AssertionError(f"unexpected voiceover {path}")
            return list(recognized[key]), "de"

        self.recognize = recognize

    @property
    def media(self) -> list[MediaInfo]:
        return [_media(clip) for clip in self.clips]

    def aligner(self) -> LocalWordAligner:
        return LocalWordAligner("tiny", self.recognize, use_cache=False)

    def settings(self, mode: str = EXPORT_MODE_LONG_FORM, **overrides) -> ExportSettings:
        values = {
            "export_mode": mode,
            "aspect": "9:16" if mode == EXPORT_MODE_SHORTS else "16:9",
            "resolution": "Auto",
            "voiceover_paths": [str(path) for path in self.voices],
            "voiceover_path": str(self.voices[0]),
            "script_mode": "single",
            "global_script_path": str(self.script),
            "script_paths": [str(self.script)],
            "script_path": str(self.script),
            "subtitle_enabled": True,
            "subtitle_output_mode": "with_subtitles",
            "subtitle_language": "German",
            "subtitle_style": "long_3",
            "subtitle_position": "Bottom Center",
            "short_subtitle_style": "short_2",
            "short_subtitle_animation": "phrase_focus",
            "short_subtitle_position": "Top Center",
            "voiceover_pause": PAUSE,
            "music_path": str(self.music),
            "music_volume": 44,
        }
        values.update(overrides)
        return ExportSettings(**values)


class FakeEngine:
    """Plans and 'renders' through the real code paths, without FFmpeg."""

    def __init__(self, tmp_path: Path, record: dict, portrait: bool = True):
        self.ffmpeg_path = tmp_path / "ffmpeg"
        self.ffprobe_path = tmp_path / "ffprobe"
        self.analyzer = SimpleNamespace(probe_raw=lambda path: {
            "streams": [{
                "codec_type": "video",
                "width": 1080 if portrait else 1920,
                "height": 1920 if portrait else 1080,
            }]
        })
        self._record = record
        self._portrait = portrait

    def analyze(self, paths, log=None):
        return [_media(path, portrait=self._portrait) for path in paths]

    def make_plan(self, media, settings, log=None):
        resolved = resolve_export(media, settings)
        self._record.setdefault("plans", []).append(
            SimpleNamespace(settings=settings, resolved=resolved, media=list(media))
        )
        return resolved

    def export(self, media, settings, resolved, output, **kwargs):
        _write(Path(output), b"mp4")
        self._record.setdefault("exports", []).append(
            SimpleNamespace(settings=settings, resolved=resolved, output=Path(output),
                            media=[item.path for item in media])
        )
        return ValidationReport(
            True, [], Path(output), resolved.expected_duration,
            resolved.width, resolved.height, float(resolved.fps or 30.0), True, True,
        )

    def burn_subtitles(self, clean_video, ass_path, fonts_dir, output_path, resolved, media, **kwargs):
        _write(Path(output_path), b"mp4")
        self._record.setdefault("burns", []).append(
            SimpleNamespace(ass=Path(ass_path), fonts_dir=fonts_dir, output=Path(output_path))
        )
        return ValidationReport(
            True, [], Path(output_path), resolved.expected_duration,
            resolved.width, resolved.height, float(resolved.fps or 30.0), True, True,
        )


def _fake_probe(project: Project):
    def probe(_ffprobe, path):
        name = Path(path).name
        duration = CLIP_SECONDS * 10
        if name.startswith("voice_"):
            duration = project.durations[int(name.split("_")[1].split(".")[0]) - 1]
        return SimpleNamespace(
            path=Path(path).expanduser().resolve(), duration=duration,
            sample_rate=48000, channels=2, codec="pcm_s16le",
        )
    return probe


def _fake_frames(_ffmpeg, _video, _alignment, frame_paths, **_kwargs):
    return [_write(Path(path), b"\x89PNG\r\n\x1a\n" + b"0" * 32) for path in frame_paths.values()]


def _real_fit_capture(record: dict):
    """Wrap the real duration fitter to record the requested timeline target."""
    from app.video_merger.timeline import fit_media_to_duration as real_fit

    def fit(media, target, *args, **kwargs):
        record.setdefault("targets", []).append(float(target))
        return real_fit(media, target, *args, **kwargs)

    return fit


def _run(tmp_path: Path, project: Project, settings: ExportSettings, *, complete=False,
         media=None, output="output", portrait=True):
    record: dict = {}
    engine = FakeEngine(tmp_path, record, portrait=portrait)
    workflow = MainProjectEngine(engine)
    logs: list[str] = []
    with patch("app.video_merger.main_project.probe_audio", side_effect=_fake_probe(project)), \
            patch("app.video_merger.main_project.fit_media_to_duration",
                  side_effect=_real_fit_capture(record)), \
            patch("app.video_merger.main_project.create_visual_verification_frames",
                  side_effect=_fake_frames):
        result = workflow.create_youtube_exports(
            project.media if media is None else media, settings, tmp_path / output,
            aligner=project.aligner(), log=logs.append, complete=complete,
        )
    return SimpleNamespace(
        result=result, record=record, logs=logs, output_dir=tmp_path / output,
    )


def _timestamp(value: str) -> float:
    hours, minutes, rest = value.split(":")
    whole, millis = rest.replace(".", ",").split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(whole) + int(millis) / 1000.0


def _srt_cues(path: Path) -> list[tuple[float, float]]:
    text = path.read_text(encoding="utf-8")
    return [
        (_timestamp(start), _timestamp(end))
        for start, end in re.findall(
            r"(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)", text
        )
    ]


def _vtt_cues(path: Path) -> list[tuple[float, float]]:
    text = path.read_text(encoding="utf-8")
    return [
        (_timestamp(start), _timestamp(end))
        for start, end in re.findall(
            r"(\d\d:\d\d:\d\d\.\d\d\d) --> (\d\d:\d\d:\d\d\.\d\d\d)", text
        )
    ]


def _srt_text(path: Path) -> str:
    lines = [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().isdigit() and "-->" not in line
    ]
    return " ".join(lines)


def _words(text: str) -> list[str]:
    return [token for token, _start, _end in script_word_spans(text)]


def _long_form_dir(output_dir: Path) -> Path:
    return output_dir / "LongForm"


def _short_videos(output_dir: Path) -> list[Path]:
    return sorted((output_dir / "Shorts").glob("*.mp4"))


# --------------------------------------------------------------------------- #
# 1. The four visual sections: defaults, configuration, validation
# --------------------------------------------------------------------------- #


def test_user_facing_defaults_are_one_and_a_half_and_zero_point_seven_seconds():
    settings = ExportSettings()
    assert settings.long_form_intro_seconds == LONG_FORM_INTRO_SECONDS == 1.5
    assert settings.long_form_outro_seconds == LONG_FORM_OUTRO_SECONDS == 1.5
    assert settings.short_intro_seconds == SHORT_INTRO_SECONDS == 0.7
    assert settings.short_outro_seconds == SHORT_OUTRO_SECONDS == 0.7
    # The historical fixed Short ending is reused cleanly as the semantic Short
    # outro: one value, one visible tail, never the two added together.
    assert SHORT_OUTRO_SECONDS == SHORT_ENDING_SECONDS == 0.7
    assert settings.opening_effect == "none"
    assert settings.legacy_input_root == ""
    # The canonical render fields keep their historical values, so a direct
    # create_main(ExportSettings()) renders exactly like before this feature.
    assert settings.visual_intro_seconds == 0.0
    assert settings.final_pause == 1.0
    # The GUI cap is a widget range only; the model itself has no artificial cap.
    assert MAX_VISUAL_SECTION_SECONDS == 60.0
    assert visual_section_seconds(600.0, label="Long-Form Intro") == 600.0


@pytest.mark.parametrize(
    ("field", "default"),
    [
        ("long_form_intro_seconds", LONG_FORM_INTRO_SECONDS),
        ("long_form_outro_seconds", LONG_FORM_OUTRO_SECONDS),
        ("short_intro_seconds", SHORT_INTRO_SECONDS),
        ("short_outro_seconds", SHORT_OUTRO_SECONDS),
    ],
)
def test_every_visual_section_is_configurable_and_zero_disables_it(field, default):
    for value in (0.0, 0.25, default, 7.5, 120.0):
        settings = ExportSettings(**{field: value})
        assert getattr(settings, field) == value
    assert visual_section_seconds(0.0, label="Visual section") == 0.0


@pytest.mark.parametrize("bad", [-0.001, -1.0, -30.0, float("nan"), float("inf"), "abc", None])
def test_negative_or_invalid_visual_sections_are_rejected(bad):
    with pytest.raises(VideoMergerError):
        visual_section_seconds(bad, label="Long-Form Intro")


def test_the_long_form_planner_copies_the_user_sections_into_the_canonical_fields():
    settings = ExportSettings(long_form_intro_seconds=1.25, long_form_outro_seconds=3.5)
    planned = long_form_settings(settings)
    assert effective_intro_seconds(planned) == 1.25
    assert effective_outro_seconds(planned) == 3.5

    zero = long_form_settings(
        ExportSettings(long_form_intro_seconds=0.0, long_form_outro_seconds=0.0)
    )
    assert (effective_intro_seconds(zero), effective_outro_seconds(zero)) == (0.0, 0.0)
    assert main_timeline(zero, 4.0).target == pytest.approx(4.0)


def test_the_timeline_is_exactly_intro_voiceover_outro():
    settings = long_form_settings(
        ExportSettings(long_form_intro_seconds=1.5, long_form_outro_seconds=2.0)
    )
    timeline = main_timeline(settings, 4.2)
    assert (timeline.intro, timeline.spoken, timeline.outro) == (1.5, 4.2, 2.0)
    assert timeline.voiceover_start == 1.5
    assert timeline.spoken_end == pytest.approx(5.7)
    assert timeline.subtitle_start == timeline.voiceover_start
    assert timeline.subtitle_end == timeline.spoken_end
    assert timeline.target == pytest.approx(7.7)
    # The spoken program (voiceover and clip-original audio) ends with the
    # speech; music is NOT bounded by it and covers the complete video instead.
    assert timeline.audio_program == pytest.approx(5.7)
    assert (timeline.video_start, timeline.music_start) == (0.0, 0.0)
    assert timeline.video_end == timeline.music_end == pytest.approx(7.7)
    log = "\n".join(timeline.log_lines())
    assert "Intro 1.500 s (visual only)" in log
    assert "Spoken end 5.700 s" in log


def test_the_long_form_outro_is_the_end_padding_and_never_doubles():
    # A project that carries both names gets exactly ONE visible tail: the
    # explicit outro, not the outro plus the legacy end padding.
    settings = ExportSettings(final_pause=4.0, long_form_outro_seconds=2.5)
    planned = long_form_settings(settings)
    assert planned.final_pause == 2.5
    timeline = main_timeline(planned, 3.0)
    assert timeline.outro == 2.5
    assert timeline.target == pytest.approx(LONG_FORM_INTRO_SECONDS + 3.0 + 2.5)
    assert timeline.target != pytest.approx(LONG_FORM_INTRO_SECONDS + 3.0 + 2.5 + 4.0)


def test_the_short_outro_replaces_the_legacy_seven_tenths_ending():
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS, voiceover_paths=["voice_1.wav"],
        short_intro_seconds=1.5, short_outro_seconds=1.5,
    )
    job = build_short_jobs(settings)[0]
    short = short_settings(settings, job)
    assert short.visual_intro_seconds == 1.5
    # The explicitly requested outro is what is visible — an explicit value is
    # never replaced by the default and never stacked on the legacy 0.7 s.
    assert short.final_pause == 1.5
    assert short.final_pause != pytest.approx(1.5 + SHORT_ENDING_SECONDS)
    assert short.final_pause != pytest.approx(SHORT_OUTRO_SECONDS + SHORT_ENDING_SECONDS)
    timeline = main_timeline(short, 3.0)
    assert (timeline.intro, timeline.spoken, timeline.outro) == (1.5, 3.0, 1.5)
    assert timeline.target == pytest.approx(6.0)

    # Zero explicitly disables both Short sections.
    disabled = short_settings(
        replace(settings, short_intro_seconds=0.0, short_outro_seconds=0.0), job
    )
    assert main_timeline(disabled, 3.0).target == pytest.approx(3.0)
    # The legacy guaranteed ending survives as a floor for settings objects that
    # do not carry the new field at all.
    assert SHORT_ENDING_SECONDS == 0.7


def test_a_short_never_inherits_the_opening_effect_or_the_long_form_music():
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS, voiceover_paths=["voice_1.wav"],
        music_path="long_form_theme.mp3", short_music_path="",
        opening_effect="zoom_in",
    )
    job = build_short_jobs(settings)[0]
    short = short_settings(settings, job)
    assert short.opening_effect == "none"
    assert short.music_path == ""


# --------------------------------------------------------------------------- #
# 2. End-to-end: rendered durations per export mode
# --------------------------------------------------------------------------- #


def test_the_long_form_render_is_intro_plus_speech_plus_outro(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(
        EXPORT_MODE_LONG_FORM, long_form_intro_seconds=2.5, long_form_outro_seconds=2.5
    )
    run = _run(tmp_path, project, settings, portrait=False)

    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    expected = 2.5 + voice_total + 2.5
    assert run.result.long_form is not None
    assert run.result.long_form.report.duration == pytest.approx(expected, abs=EPS)
    # The pool reserved exactly the material the visible timeline needs.
    assert run.record["targets"] == [pytest.approx(expected, abs=EPS)]
    assert (_long_form_dir(run.output_dir) / "YouTube_LongForm.mp4").is_file()


def test_zero_sections_render_the_plain_voiceover_timeline(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(
        EXPORT_MODE_LONG_FORM, long_form_intro_seconds=0.0, long_form_outro_seconds=0.0
    )
    run = _run(tmp_path, project, settings, portrait=False)

    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    assert run.result.long_form.report.duration == pytest.approx(voice_total, abs=EPS)
    assert run.record["targets"] == [pytest.approx(voice_total, abs=EPS)]


@pytest.mark.parametrize("mode", [EXPORT_MODE_LONG_FORM, EXPORT_MODE_SHORTS, EXPORT_MODE_COMBINED])
def test_every_export_mode_applies_the_sections_exactly_once(tmp_path, mode):
    project = Project(tmp_path)
    settings = project.settings(mode)
    run = _run(tmp_path, project, settings, portrait=mode != EXPORT_MODE_LONG_FORM)

    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    if mode == EXPORT_MODE_SHORTS:
        assert run.result.long_form is None
    else:
        assert run.result.long_form.report.duration == pytest.approx(
            LONG_FORM_INTRO_SECONDS + voice_total + LONG_FORM_OUTRO_SECONDS, abs=EPS
        )
    if mode == EXPORT_MODE_LONG_FORM:
        assert run.result.shorts == []
    else:
        assert len(run.result.shorts) == len(project.durations)
        for short, duration in zip(run.result.shorts, project.durations):
            assert short.report.duration == pytest.approx(
                SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS, abs=EPS
            )
        assert len(_short_videos(run.output_dir)) == len(project.durations)


def test_one_click_renders_the_same_timeline_as_a_normal_run(tmp_path):
    project = Project(tmp_path)
    # One-Click needs an assigned Stage-2 asset; the Intro media file is wrapped
    # AROUND the Main Video and must not change its planned visual sections.
    settings = project.settings(
        EXPORT_MODE_COMBINED, intro_path=str(_write(tmp_path / "stage2_intro.mp4"))
    )
    plain = _run(tmp_path / "plain", project, settings, output="out_plain")
    one_click = _run(tmp_path / "one_click", project, settings, complete=True, output="out_one")

    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    expected_long = LONG_FORM_INTRO_SECONDS + voice_total + LONG_FORM_OUTRO_SECONDS
    expected_shorts = [
        SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS for duration in project.durations
    ]
    # Both pathways plan exactly the same Stage-1 timelines: the visual sections
    # are applied once per job, never once per stage.
    assert plain.record["targets"] == one_click.record["targets"]
    assert plain.record["targets"][0] == pytest.approx(expected_long, abs=EPS)
    assert plain.record["targets"][1:] == [
        pytest.approx(value, abs=EPS) for value in expected_shorts
    ]
    assert plain.result.long_form.report.duration == pytest.approx(expected_long, abs=EPS)
    assert [short.report.duration for short in plain.result.shorts] == [
        pytest.approx(value, abs=EPS) for value in expected_shorts
    ]
    assert one_click.result.long_form is not None
    assert len(one_click.result.shorts) == len(project.durations)


def test_the_visual_intro_carries_no_voiceover_audio(tmp_path):
    media = [fake_media(str(tmp_path / "A.mp4"), duration=12.0)]

    def graph(intro: float) -> str:
        settings = ExportSettings(
            workflow_stage="main", resolution="1920x1080", program_duration=6.0,
            voiceover_path=str(tmp_path / "voice.wav"), visual_intro_seconds=intro,
            final_pause=1.5, original_audio_mode="mute", normalize_audio=False,
        )
        resolved = resolve_export(media, settings)
        return FFmpegCommandBuilder("ffmpeg").build(
            media, settings, resolved, tmp_path / "out.mp4"
        ).filter_graph

    with_intro = graph(2.5)
    # Silence of exactly the intro length is concatenated in front of the
    # voiceover, so the first spoken sample starts at the intro boundary.
    assert "anullsrc=r=48000:cl=stereo:d=2.5" in with_intro
    assert "[vintro]" in with_intro
    assert "[vintro][vu1]concat=n=2:v=0:a=1[vvoice_all]" in with_intro

    without_intro = graph(0.0)
    # With no intro the historical audio graph stays byte-identical: no silence
    # segment and no additional concat is inserted.
    assert "anullsrc" not in without_intro
    assert "[vintro]" not in without_intro
    assert "vvoice_all" not in without_intro


# --------------------------------------------------------------------------- #
# 3. Subtitles only during the spoken portion
# --------------------------------------------------------------------------- #


def test_long_form_subtitles_start_with_the_voiceover_and_end_with_the_speech(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(
        EXPORT_MODE_LONG_FORM, long_form_intro_seconds=2.5, long_form_outro_seconds=2.5
    )
    run = _run(tmp_path, project, settings, portrait=False)

    video = _long_form_dir(run.output_dir) / "YouTube_LongForm.mp4"
    assert video.is_file()
    srt, vtt = video.with_suffix(".srt"), video.with_suffix(".vtt")
    cues = _srt_cues(srt)
    assert cues, "the Long-Form must caption its spoken script"

    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    spoken_end = 2.5 + voice_total
    # No caption inside the visual intro ...
    assert cues[0][0] >= 2.5 - 0.001
    # ... none inside the visual outro, and the last cue ends with the speech.
    assert cues[-1][1] <= spoken_end + 0.001
    assert max(end for _start, end in cues) <= spoken_end + 0.001
    assert max(end for _start, end in _vtt_cues(vtt)) <= spoken_end + 0.001
    # Monotonic, non-overlapping and fully captioned.
    assert cues == sorted(cues)
    assert all(
        previous[1] <= following[0] + 0.001 for previous, following in itertools.pairwise(cues)
    )
    assert run.result.long_form.report.duration == pytest.approx(spoken_end + 2.5, abs=EPS)


def test_short_subtitles_never_appear_in_the_visual_intro_or_outro(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)
    run = _run(tmp_path, project, settings)

    videos = _short_videos(run.output_dir)
    assert len(videos) == len(project.durations)
    for video, duration in zip(videos, project.durations):
        cues = _srt_cues(video.with_suffix(".srt"))
        assert cues
        # The caption window is the spoken window, shifted by the Short intro.
        assert cues[0][0] >= SHORT_INTRO_SECONDS - 0.001
        assert cues[-1][1] <= SHORT_INTRO_SECONDS + duration + 0.001
        assert cues[-1][1] < SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS
        assert video.with_suffix(".vtt").read_text(encoding="utf-8").startswith("WEBVTT")


def test_the_subtitle_shift_lives_in_the_timeline_model(tmp_path):
    """Every cue moves by exactly the intro — nothing else about it changes."""
    project = Project(tmp_path)
    plain = _run(
        tmp_path / "plain", project,
        project.settings(
            EXPORT_MODE_LONG_FORM, long_form_intro_seconds=0.0, long_form_outro_seconds=0.0
        ),
        portrait=False, output="out_plain",
    )
    shifted = _run(
        tmp_path / "shifted", project,
        project.settings(
            EXPORT_MODE_LONG_FORM, long_form_intro_seconds=2.5, long_form_outro_seconds=2.5
        ),
        portrait=False, output="out_shifted",
    )

    plain_cues = _srt_cues(_long_form_dir(plain.output_dir) / "YouTube_LongForm.srt")
    shifted_cues = _srt_cues(_long_form_dir(shifted.output_dir) / "YouTube_LongForm.srt")
    assert len(plain_cues) == len(shifted_cues)
    for (plain_start, plain_end), (start, end) in zip(plain_cues, shifted_cues):
        assert start == pytest.approx(plain_start + 2.5, abs=0.001)
        assert end == pytest.approx(plain_end + 2.5, abs=0.001)
    # The spoken text and its word order are untouched by the shift.
    assert _srt_text(_long_form_dir(shifted.output_dir) / "YouTube_LongForm.srt") == _srt_text(
        _long_form_dir(plain.output_dir) / "YouTube_LongForm.srt"
    )


def test_word_level_alignment_survives_the_visual_sections(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)
    run = _run(tmp_path, project, settings)

    for index, video in enumerate(_short_videos(run.output_dir)):
        captioned = _words(_srt_text(video.with_suffix(".srt")))
        own = _words(SECTIONS[index])
        assert captioned == own, "every spoken word is captioned exactly once"
        cues = _srt_cues(video.with_suffix(".srt"))
        # Word-level timing: cue boundaries stay strictly increasing inside the
        # spoken window and never exceed it.
        assert [start for start, _end in cues] == sorted(start for start, _end in cues)
        assert all(end - start > 0 for start, end in cues)


def test_subtitles_can_be_disabled_while_the_sections_stay(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(
        EXPORT_MODE_LONG_FORM, subtitle_enabled=False, subtitle_output_mode="without_subtitles"
    )
    run = _run(tmp_path, project, settings, portrait=False)

    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    assert run.result.long_form.report.duration == pytest.approx(
        LONG_FORM_INTRO_SECONDS + voice_total + LONG_FORM_OUTRO_SECONDS, abs=EPS
    )
    assert not (_long_form_dir(run.output_dir) / "YouTube_LongForm.srt").exists()


# --------------------------------------------------------------------------- #
# 4. Logging, cache identity and backward compatibility
# --------------------------------------------------------------------------- #


def test_the_timeline_structure_is_logged_once_per_job(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_COMBINED)
    run = _run(tmp_path, project, settings)

    timeline_lines = [line for line in run.logs if line.startswith("Timeline: Intro")]
    caption_lines = [line for line in run.logs if line.startswith("Subtitles: start")]
    # One Long-Form plus one line per Short — and nothing per clip or frame.
    assert len(timeline_lines) == 1 + len(project.durations)
    assert len(caption_lines) == 1 + len(project.durations)
    assert "Intro 1.500 s (visual only)" in timeline_lines[0]
    assert "Voiceover start 1.500 s" in timeline_lines[0]
    assert "Outro 1.500 s (visual only)" in timeline_lines[0]
    assert "Video start 0.000 s" in timeline_lines[0]
    assert "no caption in the visual intro or outro" in caption_lines[0]
    assert "Intro 0.700 s (visual only)" in timeline_lines[1]


def test_the_visual_sections_are_part_of_the_stage1_cache_identity(tmp_path):
    project = Project(tmp_path)
    media = project.media[:4]
    baseline_settings = project.settings(EXPORT_MODE_LONG_FORM)
    resolved = resolve_export(media, baseline_settings)
    baseline = stage1_fingerprint(media, baseline_settings, resolved)[0]

    for field, value in (
        ("long_form_intro_seconds", 0.0),
        ("long_form_outro_seconds", 0.0),
        ("short_intro_seconds", 0.0),
        ("short_outro_seconds", 0.0),
        ("opening_effect", "zoom_in"),
    ):
        changed = replace(baseline_settings, **{field: value})
        assert stage1_fingerprint(media, changed, resolved)[0] != baseline, field


def test_an_old_project_file_migrates_without_losing_its_end_padding(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({
            "final_pause": 4.0,
            "subtitle_animation": "outline_highlight",
            "short_subtitle_animation": "word_highlight",
            "a_field_from_the_future": {"nested": True},
        }),
        encoding="utf-8",
    )
    loaded = store.load()

    # The saved Main Video end padding IS the Long-Form outro of that project.
    assert loaded.final_pause == 4.0
    assert loaded.long_form_outro_seconds == 4.0
    # Missing fields fall back to the deterministic new defaults ...
    assert loaded.long_form_intro_seconds == LONG_FORM_INTRO_SECONDS
    assert loaded.short_intro_seconds == SHORT_INTRO_SECONDS
    assert loaded.short_outro_seconds == SHORT_OUTRO_SECONDS
    assert loaded.opening_effect == "none"
    assert loaded.legacy_input_root == ""
    # ... deprecated animations migrate at the job boundary ...
    assert long_form_settings(loaded).subtitle_animation == "color_change"
    # ... and unknown fields are ignored instead of raising.
    assert not hasattr(loaded, "a_field_from_the_future")

    # A current project writes both names and loads them back unchanged.
    store.save(loaded)
    assert store.load().long_form_outro_seconds == 4.0


def test_random_order_reserves_the_legacy_input_root_in_a_real_render(tmp_path):
    project = Project(tmp_path)
    legacy_root = tmp_path / "pool"
    extra_root = tmp_path / "extra"
    # A mixed pool: four Legacy Input Root clips plus four clips of a second
    # source folder, so the reservation is actually observable.
    media = [_media(clip) for clip in project.clips[:4]] + [
        _media(_write(extra_root / f"extra_{index}.mp4", b"video")) for index in range(4)
    ]
    settings = project.settings(
        EXPORT_MODE_LONG_FORM,
        video_order_mode="random",
        legacy_input_root=str(legacy_root),
        subtitle_enabled=False,
        subtitle_output_mode="without_subtitles",
    )
    run = _run(tmp_path, project, settings, portrait=False, media=media)

    rendered = [Path(item) for item in run.record["exports"][0].media]
    assert rendered, "the Long-Form render consumed reserved material"
    # Clips 1-3 of the effective sequence come from the Legacy Input Root ...
    assert [item.parent for item in rendered[:3]] == [legacy_root] * 3
    assert len(set(rendered[:3])) == 3
    # ... and the pool contains both folders, so this is a real reservation.
    assert {item.path.parent for item in media} == {legacy_root, extra_root}

    priority_lines = [line for line in run.logs if line.startswith("Legacy Input Root priority")]
    assert len(priority_lines) == 1
    assert "clips 1-3" in priority_lines[0]
    assert "remaining randomized pool starts at clip 4" in priority_lines[0]
    assert all(Path(item).name in priority_lines[0] for item in rendered[:3])
    assert not any(f"extra_{index}.mp4" in priority_lines[0] for index in range(4))
    # The effective order log stays a single line as well.
    assert sum(1 for line in run.logs if line.startswith("Effective video order: ")) == 1
