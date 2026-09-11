"""Separate Shorts music, the fixed 0.7 s video-only Short ending, one script
text file per Short, and the removed Quote/Flyer PDF feature.

Four contracts are pinned here.

1. **Separate music** — ``music_path`` is the Long-Form/basic track and
   ``short_music_path`` is the Shorts track. The two are strictly separate: the
   Long-Form track is never mixed into a Short, and a Short without its own
   selection has no background music at all. Volume, preset, ducking, looping
   and trimming stay shared behavior for whichever track is active, and each
   track keeps its own render-cache identity.

2. **Video-only Short ending** — every Short is a visual intro, exactly its own
   voiceover duration, and a visual outro of additional *visual* material. The
   spoken audio stays the authoritative duration, the caption timeline is shifted
   by the intro and ends with the voiceover, and no cue of this or another Short
   can appear in the intro or the ending. :data:`SHORT_ENDING_SECONDS` (0.7 s)
   survives as the guaranteed floor for a project that carries no explicit Short
   outro; an explicit outro *replaces* that floor instead of stacking a second
   visible ending. The Long-Form keeps its freely configurable end padding, which
   is exactly its visual outro.

3. **One ``.txt`` per Short** — beside every rendered Short, one text file with
   the identical final name contains exactly the script text that Short uses
   (its derived global-script section or its matched individual script). The
   content is reused from the already derived script, so the sidecar never
   triggers additional speech recognition.

4. **Quote/Flyer removed, Add Image preserved** — the artwork/PDF section, its
   GUI, its model fields, its CLI flags and its PDF dependency are gone, while
   Add Image, Intro/Outro and the complete Stage-2 timeline keep working and
   old project files still load.

The render assertions run the REAL ``create_main``/``create_youtube_exports``
pipeline with a fake FFmpeg engine, so durations, subtitle timelines, output
names and sidecars are the values the application really produces.
"""

from __future__ import annotations

import json
import re
import sys
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
from app.video_merger.main_project import MainProjectEngine, global_script_path
from app.video_merger.models import (
    LONG_FORM_INTRO_SECONDS,
    SHORT_INTRO_SECONDS,
    SHORT_OUTRO_SECONDS,
    AudioAssetInfo,
    AudioInfo,
    ExportSettings,
    MediaInfo,
    ValidationReport,
)
from app.video_merger.render_cache import (
    build_stage1_payload,
    build_stage2_payload,
    stage2_fingerprint,
)
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export
from app.video_merger.video_pool import ShortsVideoPool
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
    NO_SCRIPT_SECTION,
    SHORT_ENDING_SECONDS,
    build_short_jobs,
    long_form_settings,
    short_script_text_path,
    short_settings,
    write_short_script_text,
)

# One global script whose three paragraphs are spoken by three voiceovers.
SECTIONS = [
    "Ruhe ist die erste Antwort des Tages.",
    "Bewegung folgt dem Gedanken langsam.",
    "Stille trägt das Ende dieser Geschichte.",
]
GLOBAL_SCRIPT = "\n\n".join(SECTIONS)
DURATIONS = [4.0, 3.5, 5.0]
PAUSE = 0.7
CLIP_SECONDS = 3.0


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _sentence(index: int) -> str:
    """A distinct spoken sentence per unit, so caption mixing is detectable."""
    return f"Dies ist der gesprochene Satz Nummer {index + 1} mit mehreren verschiedenen Worten."


@pytest.fixture(autouse=True)
def _private_project_root(tmp_path, monkeypatch):
    """Keep derived sections, caches and staged files inside the test's tmp."""
    root = tmp_path / "project"
    monkeypatch.setattr("app.video_merger.script_sections.project_root", lambda: root)
    monkeypatch.setattr("app.video_merger.main_project.project_root", lambda: root)
    return root


def _spoken(section: str, duration: float) -> list[RecognizedWord]:
    """Realistic per-unit recognition strictly inside that unit's own audio."""
    tokens = section.split()
    slot = duration / max(1, len(tokens))
    return [
        RecognizedWord(
            token.strip(".,"), round(index * slot + 0.1, 3),
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
    """Voiceovers, scripts, music and clips for one deterministic project."""

    def __init__(self, tmp_path: Path, *, durations=DURATIONS, sections=SECTIONS, matched=False):
        self.root = tmp_path
        self.durations = list(durations)
        self.sections = list(sections)
        self.voices = [
            _write(tmp_path / f"voice_{index + 1}.wav", b"audio")
            for index in range(len(durations))
        ]
        self.script = _write(tmp_path / "global_script.txt", GLOBAL_SCRIPT.encode("utf-8"))
        self.long_music = _write(tmp_path / "music" / "long_form_theme.mp3", b"music")
        self.short_music = _write(tmp_path / "music" / "shorts_theme.mp3", b"music")
        self.clips = [
            _write(tmp_path / "pool" / f"clip_{index}.mp4", b"video") for index in range(12)
        ]
        self.matched = matched
        self.matched_scripts: list[Path] = []
        if matched:
            # Basename-matched individual scripts: voice_N.wav <-> voice_N.txt.
            for index, section in enumerate(self.sections):
                self.matched_scripts.append(
                    _write(tmp_path / f"voice_{index + 1}.txt", section.encode("utf-8"))
                )
        recognized = {
            str(path.resolve()): _spoken(section, duration)
            for path, section, duration in zip(self.voices, self.sections, self.durations)
        }
        self.recognize_calls: list[str] = []

        def recognize(path, _language):
            key = str(Path(path).expanduser().resolve())
            if key not in recognized:
                raise AssertionError(f"unexpected voiceover {path}")
            self.recognize_calls.append(key)
            return list(recognized[key]), "de"

        self.recognize = recognize

    @property
    def media(self) -> list[MediaInfo]:
        return [_media(clip) for clip in self.clips]

    def aligner(self) -> LocalWordAligner:
        return LocalWordAligner("tiny", self.recognize, use_cache=False)

    def settings(self, mode: str = EXPORT_MODE_SHORTS, **overrides) -> ExportSettings:
        values = {
            "export_mode": mode,
            "aspect": "9:16" if mode == EXPORT_MODE_SHORTS else "16:9",
            "resolution": "Auto",
            "voiceover_paths": [str(path) for path in self.voices],
            "voiceover_path": str(self.voices[0]),
            "script_mode": "matched" if self.matched else "single",
            "global_script_path": "" if self.matched else str(self.script),
            "script_paths": (
                [str(path) for path in self.matched_scripts]
                if self.matched
                else [str(self.script)]
            ),
            "script_path": "" if self.matched else str(self.script),
            "subtitle_enabled": True,
            "subtitle_output_mode": "with_subtitles",
            "subtitle_language": "German",
            "subtitle_style": "long_3",
            "subtitle_position": "Bottom Center",
            "short_subtitle_style": "short_2",
            "short_subtitle_font": "inter",
            "short_subtitle_animation": "word_highlight",
            "short_subtitle_position": "Top Center",
            "voiceover_pause": PAUSE,
            "final_pause": 2.5,
            "music_path": str(self.long_music),
            "short_music_path": str(self.short_music),
            "music_volume": 44,
            "music_preset": "balanced",
        }
        values.update(overrides)
        return ExportSettings(**values)


class FakeEngine:
    """A render engine that plans and 'renders' without FFmpeg binaries."""

    def __init__(self, tmp_path: Path, record: dict, portrait: bool = True, recognize_count=None):
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
        self._recognize_count = recognize_count or (lambda: 0)

    def _snapshot_recognition(self):
        """How much speech recognition had run when this render finished."""
        self._record["recognize_calls_during_render"] = self._recognize_count()

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
        self._snapshot_recognition()
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
        self._snapshot_recognition()
        self._record.setdefault("burns", []).append(
            SimpleNamespace(ass=Path(ass_path), fonts_dir=fonts_dir, output=Path(output_path))
        )
        return ValidationReport(
            True, [], Path(output_path), resolved.expected_duration,
            resolved.width, resolved.height, float(resolved.fps or 30.0), True, True,
        )


def _fake_probe(project: Project):
    """probe_audio stand-in: voiceover durations by name, music is long."""
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
    created = []
    for path in frame_paths.values():
        created.append(_write(Path(path), b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    return created


def _real_fit_capture(record: dict):
    """Wrap the real duration fitter to record the requested timeline target."""
    from app.video_merger.timeline import fit_media_to_duration as real_fit

    def fit(media, target, *args, **kwargs):
        record.setdefault("targets", []).append(float(target))
        return real_fit(media, target, *args, **kwargs)

    return fit


def _run(tmp_path: Path, project: Project, settings: ExportSettings, *, complete=False,
         media=None, aligner=None, output="output", portrait=True):
    """Run the real YouTube orchestrator with a fake FFmpeg engine."""
    record: dict = {}
    engine = FakeEngine(tmp_path, record, portrait=portrait,
                        recognize_count=lambda: len(project.recognize_calls))
    workflow = MainProjectEngine(engine)
    logs: list[str] = []
    if aligner is None:
        aligner = project.aligner()
    with patch("app.video_merger.main_project.probe_audio", side_effect=_fake_probe(project)), \
            patch("app.video_merger.main_project.fit_media_to_duration",
                  side_effect=_real_fit_capture(record)), \
            patch("app.video_merger.main_project.create_visual_verification_frames",
                  side_effect=_fake_frames):
        result = workflow.create_youtube_exports(
            project.media if media is None else media, settings, tmp_path / output,
            aligner=aligner, log=logs.append, complete=complete,
        )
    return SimpleNamespace(
        result=result, record=record, logs=logs, workflow=workflow, engine=engine,
        aligner=aligner, output_dir=tmp_path / output,
    )


def _srt_cue_ends(path: Path) -> list[float]:
    def seconds(value: str) -> float:
        hours, minutes, rest = value.split(":")
        whole, millis = rest.split(",")
        return int(hours) * 3600 + int(minutes) * 60 + int(whole) + int(millis) / 1000.0

    text = path.read_text(encoding="utf-8")
    return [
        seconds(end)
        for _start, end in re.findall(
            r"(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)", text
        )
    ]


def _srt_text(path: Path) -> str:
    """Only the caption lines of an SRT file, without indices and timestamps."""
    lines = [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().isdigit() and "-->" not in line
    ]
    return " ".join(lines)


def _words(text: str) -> list[str]:
    return [token for token, _start, _end in script_word_spans(text)]


def _short_videos(output_dir: Path) -> list[Path]:
    return sorted((output_dir / "Shorts").glob("*.mp4"))


def _text_files(output_dir: Path) -> list[Path]:
    return sorted((output_dir / "Shorts").glob("*.txt"))


# --------------------------------------------------------------------------- #
# 1. Separate music for Long-Form and Shorts
# --------------------------------------------------------------------------- #


def test_short_settings_use_only_the_shorts_music_track(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)
    job = build_short_jobs(settings)[0]

    short = short_settings(settings, job)

    assert short.music_path == str(project.short_music)
    assert short.export_mode == EXPORT_MODE_SHORTS
    assert short.aspect == "9:16"


def test_short_without_own_music_stays_silent_even_with_long_form_music(tmp_path):
    """Strict separation: there is no inheritance and no fallback."""
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS, short_music_path="")
    job = build_short_jobs(settings)[0]

    assert short_settings(settings, job).music_path == ""


def test_long_form_settings_keep_their_own_track_and_ignore_the_shorts_track(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_LONG_FORM)

    long_form = long_form_settings(settings)

    assert long_form.music_path == str(project.long_music)
    assert long_form.aspect == "16:9"
    # The Shorts selection exists in the model but never renders a landscape job.
    assert long_form.short_music_path == str(project.short_music)


def test_shared_music_controls_stay_identical_for_both_outputs(tmp_path):
    """Volume, preset, ducking and looping are shared behavior, not per track."""
    project = Project(tmp_path)
    settings = project.settings(
        EXPORT_MODE_COMBINED, music_volume=31, music_preset="soft",
        ducking_enabled=False, ducking_attack_ms=40, ducking_release_ms=600,
    )
    job = build_short_jobs(settings)[0]

    short, long_form = short_settings(settings, job), long_form_settings(settings)

    for value in (short, long_form):
        assert value.music_volume == 31
        assert value.music_preset == "soft"
        assert value.ducking_enabled is False
        assert value.ducking_attack_ms == 40
        assert value.ducking_release_ms == 600


def test_combined_export_gives_every_job_only_its_own_track(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_COMBINED)

    run = _run(tmp_path, project, settings)

    exports = run.record["exports"]
    landscape = [item for item in exports if item.settings.aspect == "16:9"]
    portrait = [item for item in exports if item.settings.aspect == "9:16"]
    assert landscape and portrait
    assert {item.settings.music_path for item in landscape} == {str(project.long_music)}
    assert {item.settings.music_path for item in portrait} == {str(project.short_music)}


def test_ffmpeg_command_loops_only_the_selected_track(tmp_path):
    """The real command mixes the Shorts track into a Short, never the other."""
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_COMBINED)

    run = _run(tmp_path, project, settings)

    builder = FFmpegCommandBuilder(tmp_path / "ffmpeg")
    renders = {
        "short": next(item for item in run.record["exports"] if item.settings.aspect == "9:16"),
        "long": next(item for item in run.record["exports"] if item.settings.aspect == "16:9"),
    }
    for kind, expected, forbidden in (
        ("short", project.short_music, project.long_music),
        ("long", project.long_music, project.short_music),
    ):
        render = renders[kind]
        command = builder.build(
            [_media(path) for path in render.media], render.settings,
            render.resolved, tmp_path / "probe.mp4",
        ).command
        assert str(expected) in command
        looped = [command[index + 3] for index, token in enumerate(command)
                  if token == "-stream_loop"]
        assert looped == [str(expected)]
        assert str(forbidden) not in command


def test_music_selection_is_part_of_the_stage1_cache_identity(tmp_path):
    """Each track caches separately; changing one invalidates that render."""
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)
    media = project.media[:2]
    resolved = resolve_export(media, settings)
    voice = AudioAssetInfo(project.voices[0], 4.0, 48000, 2, "pcm_s16le")

    def digest(music: Path | str) -> str:
        payload = build_stage1_payload(
            media, settings, resolved,
            voice_assets=[voice], script_files=[], subtitle_requested=False,
            music_asset=(
                AudioAssetInfo(Path(music), 30.0, 48000, 2, "mp3") if music else None
            ),
            watermark_path=None,
        )
        return json.dumps(payload, sort_keys=True, default=str)

    assert digest(project.short_music) != digest(project.long_music)
    assert digest(project.short_music) == digest(project.short_music)
    assert digest("") != digest(project.short_music)


def test_cli_maps_music_and_short_music_to_separate_fields(tmp_path, monkeypatch):
    """``--music`` stays Long-Form; ``--short-music`` is the new Shorts track."""
    from app import cli

    captured: dict = {}

    class RecordingWorkflow:
        def __init__(self, engine):
            self.engine = engine

        def create_youtube_exports(self, media, settings, output, **kwargs):
            captured["settings"] = settings
            return SimpleNamespace(primary_output=Path(output) / "done.mp4")

    monkeypatch.setattr(cli, "locate_ffmpeg", lambda: (tmp_path / "ffmpeg", tmp_path / "ffprobe"))
    monkeypatch.setattr(cli, "discover_videos", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, "VideoMergerEngine", lambda ffmpeg, ffprobe: SimpleNamespace(
        preflight=lambda log=None: None, analyze=lambda paths, log=None: []))
    monkeypatch.setattr(cli, "MainProjectEngine", RecordingWorkflow)
    monkeypatch.setattr(cli, "GeneratedOutputStore", lambda: SimpleNamespace(
        paths=lambda: set(), add=lambda output: None))
    monkeypatch.setattr(cli, "ProjectOrderStore", lambda: None)
    monkeypatch.setattr(cli, "order_media_for_video_order", lambda media, mode, **kwargs: media)
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "--stage", "main", "--input", str(tmp_path), "--output", str(tmp_path / "out"),
        "--export-mode", "shorts", "--music", str(tmp_path / "long.mp3"),
        "--short-music", str(tmp_path / "shorts.mp3"),
    ])

    assert cli.main() == 0
    assert captured["settings"].music_path == str(tmp_path / "long.mp3")
    assert captured["settings"].short_music_path == str(tmp_path / "shorts.mp3")

    # The removed Quote/Flyer flags are gone from the parser.
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "--stage", "main", "--input", str(tmp_path), "--output", str(tmp_path / "out"),
        "--quote-artwork", str(tmp_path / "flyer.pdf"),
    ])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_settings_store_round_trips_both_music_tracks(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    saved = ExportSettings(music_path="long.mp3", short_music_path="shorts.mp3")

    store.save(saved)
    loaded = store.load()

    assert loaded.music_path == "long.mp3"
    assert loaded.short_music_path == "shorts.mp3"
    # A project saved before this feature simply has no Shorts track.
    (tmp_path / "settings.json").write_text(
        json.dumps({"music_path": "long.mp3"}), encoding="utf-8"
    )
    assert SettingsStore(tmp_path / "settings.json").load().short_music_path == ""


# --------------------------------------------------------------------------- #
# 2. The fixed 0.7 s video-only Short ending
# --------------------------------------------------------------------------- #


def test_short_ending_constant_is_seven_tenths_of_a_second():
    assert SHORT_ENDING_SECONDS == 0.7


def test_every_short_renders_exactly_its_voiceover_plus_the_fixed_ending(tmp_path):
    """A Short is intro + its own voiceover + outro, whatever the padding is."""
    project = Project(tmp_path)
    for user_padding in (0.0, 1.0, 2.5, 5.0):
        settings = project.settings(EXPORT_MODE_SHORTS, final_pause=user_padding)
        run = _run(tmp_path / f"pad_{user_padding}", project, settings)

        jobs = build_short_jobs(settings)
        for job in jobs:
            # The explicit Short outro REPLACES the legacy 0.7 s floor instead of
            # stacking a second visible ending behind the spoken audio.
            assert short_settings(settings, job).final_pause == SHORT_OUTRO_SECONDS
        shorts = run.result.shorts
        assert len(shorts) == len(project.durations)
        for short, duration in zip(shorts, project.durations):
            assert short.report.duration == pytest.approx(
                SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS, abs=1e-6
            )
        # The timeline target handed to the real duration fitter matches.
        assert run.record["targets"] == [
            pytest.approx(SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS, abs=1e-6)
            for duration in project.durations
        ]


def test_long_form_keeps_the_configurable_end_padding(tmp_path):
    project = Project(tmp_path)
    # The Long-Form outro IS the canonical end padding: one configurable visual
    # tail after the spoken audio, never a second padding stacked behind it.
    settings = project.settings(
        EXPORT_MODE_LONG_FORM, final_pause=2.5, long_form_outro_seconds=1.75
    )

    run = _run(tmp_path, project, settings, portrait=False)

    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    assert long_form_settings(settings).final_pause == 1.75
    total = LONG_FORM_INTRO_SECONDS + voice_total + 1.75
    assert run.result.long_form.report.duration == pytest.approx(total, abs=1e-6)
    assert run.record["targets"] == [pytest.approx(total, abs=1e-6)]


def test_short_ending_contains_no_subtitle_cue(tmp_path):
    """Captions end with the voiceover; the extra 0.7 s stays purely visual."""
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)

    run = _run(tmp_path, project, settings)

    videos = _short_videos(run.output_dir)
    assert len(videos) == len(project.durations)
    for video, duration, short in zip(videos, project.durations, run.result.shorts):
        sidecar = video.with_suffix(".srt")
        assert sidecar.is_file()
        ends = _srt_cue_ends(sidecar)
        assert ends, "a Short with a spoken section must produce cues"
        # No cue reaches into the visual intro or the visual ending ...
        assert max(ends) <= SHORT_INTRO_SECONDS + duration + 0.001
        assert max(ends) < SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS
        # ... while the video itself is intro + speech + outro.
        assert short.report.duration == pytest.approx(
            SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS, abs=1e-6
        )


def test_no_short_shows_words_of_another_short_in_its_ending(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)

    run = _run(tmp_path, project, settings)

    videos = _short_videos(run.output_dir)
    for index, (video, section, duration) in enumerate(zip(videos, SECTIONS, project.durations)):
        captioned = _words(_srt_text(video.with_suffix(".srt")))
        own = _words(section)
        foreign = {
            token for other in SECTIONS[:index] + SECTIONS[index + 1:] for token in _words(other)
        } - set(own)
        # Only this Short's own words are captioned, and they all end before the
        # visual beginning of the fixed ending.
        assert captioned == own
        assert not set(captioned) & foreign
        assert max(
            _srt_cue_ends(video.with_suffix(".srt"))
        ) <= SHORT_INTRO_SECONDS + duration + 0.001


def test_shorts_pool_reserves_additional_material_for_the_ending(tmp_path):
    """The without-replacement pool plans intro + voiceover + outro of material."""
    # 3.0 s voiceovers + 3.0 s clips in Full-Timeline Loop: reaching the spoken
    # duration needs one clip, reaching intro and outro needs the second one.
    project = Project(tmp_path, durations=[3.0, 3.0, 3.0],
                      sections=[_sentence(index) for index in range(3)])
    settings = project.settings(EXPORT_MODE_SHORTS, short_video_mode="loop")

    def reserve(target: float) -> list[MediaInfo]:
        return ShortsVideoPool(project.media).take_for_duration(
            target, settings.transition_duration, 30.0, settings.short_video_mode,
            duration_fit_mode=settings.duration_fit_mode,
            max_stretch_percent=settings.max_stretch_percent,
            playback_rate=1.0,
        )

    short_total = SHORT_INTRO_SECONDS + 3.0 + SHORT_OUTRO_SECONDS
    assert len(reserve(3.0)) == 1
    # Intro and outro need raw material beyond the single spoken clip; how many
    # of the 3.0 s clips are reserved depends on the looped transition overlap.
    assert len(reserve(short_total)) > 1

    run = _run(tmp_path, project, settings)
    assert any("without-replacement pool planned before rendering" in message
               for message in run.logs)
    for short in run.result.shorts:
        assert short.report.duration == pytest.approx(short_total, abs=1e-6)


def test_shorts_never_share_clips_while_the_pool_lasts(tmp_path):
    """Hold mode: each Short consumes its own raw prefix, without replacement."""
    project = Project(tmp_path, durations=[3.0, 3.0, 3.0],
                      sections=[_sentence(index) for index in range(3)])
    settings = project.settings(EXPORT_MODE_SHORTS)

    run = _run(tmp_path, project, settings)

    assigned = [{Path(item).name for item in export.media} for export in run.record["exports"]]
    assert len(assigned) == 3
    assert not (assigned[0] & assigned[1]) and not (assigned[1] & assigned[2])
    assert not (assigned[0] & assigned[2])


def test_ending_is_produced_with_hold_and_loop_when_material_is_short(tmp_path):
    project = Project(tmp_path)
    for mode in ("hold", "loop"):
        settings = project.settings(EXPORT_MODE_SHORTS, short_video_mode=mode)
        single_clip = [_media(project.clips[0])]

        run = _run(tmp_path / mode, project, settings, media=single_clip)

        for short, duration in zip(run.result.shorts, project.durations):
            assert short.report.duration == pytest.approx(
                SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS, abs=1e-6
            )


# --------------------------------------------------------------------------- #
# 3. One script text file per Short
# --------------------------------------------------------------------------- #


def test_every_short_gets_one_text_file_named_like_its_video(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)

    run = _run(tmp_path, project, settings)

    videos, texts = _short_videos(run.output_dir), _text_files(run.output_dir)
    assert len(videos) == len(project.durations)
    assert [path.stem for path in texts] == [path.stem for path in videos]
    assert short_script_text_path(videos[0]) == texts[0]


def test_text_file_contains_only_that_shorts_own_global_script_section(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)

    run = _run(tmp_path, project, settings)

    texts = _text_files(run.output_dir)
    assert len(texts) == len(SECTIONS)
    for path, section in zip(texts, SECTIONS):
        assert path.read_text(encoding="utf-8") == section + "\n"
    # The union of all sidecars is the complete global script, nothing else.
    assert [token for path in texts for token in _words(path.read_text(encoding="utf-8"))] == (
        _words(GLOBAL_SCRIPT)
    )


def test_matched_individual_scripts_produce_matching_text_files(tmp_path):
    project = Project(tmp_path, matched=True)
    settings = project.settings(EXPORT_MODE_SHORTS)

    run = _run(tmp_path, project, settings)

    texts = _text_files(run.output_dir)
    assert len(texts) == len(SECTIONS)
    for path, script in zip(texts, project.matched_scripts):
        assert path.read_text(encoding="utf-8") == script.read_text(encoding="utf-8") + "\n"


def test_audio_only_short_gets_no_text_file_while_the_others_do(tmp_path):
    """A voiceover that speaks no part of the script has no text to publish."""
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)

    with patch.object(MainProjectEngine, "_short_script_sections",
                      return_value={1: NO_SCRIPT_SECTION}):
        run = _run(tmp_path, project, settings)

    videos = _short_videos(run.output_dir)
    texts = {path.stem for path in _text_files(run.output_dir)}
    assert len(videos) == 3
    assert videos[1].stem not in texts
    assert {videos[0].stem, videos[2].stem} <= texts
    assert any("speaks no script text" in message for message in run.logs)


def test_text_file_follows_a_bumped_video_name(tmp_path):
    """An existing output name is bumped; the sidecar follows the final name."""
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS)
    shorts_dir = tmp_path / "output" / "Shorts"
    _write(shorts_dir / "001.mp4", b"older render")

    _run(tmp_path, project, settings)

    bumped = shorts_dir / "001_2.mp4"
    assert bumped.is_file() and bumped.read_bytes() == b"mp4"
    assert (shorts_dir / "001_2.txt").read_text(encoding="utf-8") == SECTIONS[0] + "\n"
    assert short_script_text_path(bumped) == shorts_dir / "001_2.txt"
    # The pre-existing render is never touched and gets no sidecar of its own.
    assert (shorts_dir / "001.mp4").read_bytes() == b"older render"
    assert not (shorts_dir / "001.txt").exists()
    # The remaining Shorts keep their stable numbering.
    assert (shorts_dir / "002.txt").read_text(encoding="utf-8") == SECTIONS[1] + "\n"


def test_the_sidecars_never_trigger_additional_speech_recognition(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_COMBINED)

    run = _run(tmp_path, project, settings)

    # Recognition happens only inside the alignment passes: the count after the
    # final render is identical to the final count, so publishing the text files
    # added no ASR work at all.
    calls_during_render = run.record["recognize_calls_during_render"]
    assert calls_during_render == len(project.recognize_calls)
    assert len(_text_files(run.output_dir)) == len(project.durations)


def test_one_click_publishes_the_text_file_next_to_the_final_short_video(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS, intro_path=str(_write(tmp_path / "intro.mp4")))

    run = _run(tmp_path, project, settings, complete=True)

    finals = [short.final_video for short in run.result.shorts]
    assert len(finals) == len(project.durations)
    for final, section in zip(finals, SECTIONS):
        sidecar = short_script_text_path(final)
        assert sidecar.is_file()
        assert sidecar.read_text(encoding="utf-8") == section + "\n"


def test_long_form_receives_no_script_text_sidecar(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_LONG_FORM)

    run = _run(tmp_path, project, settings, portrait=False)

    long_form_dir = run.output_dir / "LongForm"
    assert sorted(path.suffix for path in long_form_dir.iterdir()) == [".mp4", ".srt", ".vtt"]


def test_write_short_script_text_helper_contract(tmp_path):
    video = _write(tmp_path / "Shorts" / "004.mp4", b"mp4")
    script = _write(tmp_path / "section.txt", b"Erster Satz.\nZweiter Satz.")

    target = write_short_script_text(video, script)

    assert target == tmp_path / "Shorts" / "004.txt"
    assert target.read_text(encoding="utf-8") == "Erster Satz.\nZweiter Satz.\n"
    assert write_short_script_text(video, "") is None
    assert write_short_script_text(video, None) is None
    assert short_script_text_path(tmp_path / "001_2.mp4") == tmp_path / "001_2.txt"


def test_without_subtitles_shorts_still_get_their_own_text_files(tmp_path):
    """No captions are rendered, yet every text file holds only its own words."""
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_SHORTS, subtitle_output_mode="without_subtitles")

    run = _run(tmp_path, project, settings)

    videos = _short_videos(run.output_dir)
    assert len(videos) == len(SECTIONS)
    assert not list((run.output_dir / "Shorts").glob("*.srt"))
    assert not list((run.output_dir / "Shorts").glob("*.vtt"))
    for video, section in zip(videos, SECTIONS):
        assert video.with_suffix(".txt").read_text(encoding="utf-8") == section + "\n"


# --------------------------------------------------------------------------- #
# 4. Quote/Flyer removed, Add Image preserved
# --------------------------------------------------------------------------- #


def test_quote_flyer_feature_is_removed_from_the_application():
    from app.video_merger import models

    assert not Path(models.__file__).with_name("quote_artwork.py").exists()
    assert not any("quote" in name for name in ExportSettings.__dataclass_fields__)
    assert not any("quote" in name for name in MediaInfo.__dataclass_fields__)
    with pytest.raises(ImportError):
        __import__("app.video_merger.quote_artwork", fromlist=["prepare_quote_artwork"])
    # The GUI keeps no widget, handler or preview of the removed section and
    # the PDF preview canvas is gone with it.
    source = Path(models.__file__).parent.joinpath("gui", "main_window.py").read_text(encoding="utf-8")
    for token in ("quote_check", "quote_artwork", "quote_pdf_page", "QuotePreviewCanvas",
                  "_update_quote_preview", "Include Quote"):
        assert token not in source
    preview = Path(models.__file__).with_name("subtitle_preview.py").read_text(encoding="utf-8")
    assert "QuotePreviewCanvas" not in preview
    assert "import fitz" not in preview


def test_pymupdf_is_no_longer_a_dependency():
    requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
    text = requirements.read_text(encoding="utf-8").casefold()
    assert "pymupdf" not in text
    assert "fitz" not in text


def test_old_project_files_with_quote_keys_still_load(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "music_path": "long.mp3", "quote_enabled": True, "quote_input_mode": "artwork",
        "quote_artwork_path": "flyer.pdf", "quote_pdf_page": 3,
        "quote_artwork_fit_mode": "crop", "quote_duration": 4.0,
        "image_enabled": True, "image_path": "add.png",
    }), encoding="utf-8")

    loaded = SettingsStore(path).load()

    assert loaded.music_path == "long.mp3"
    assert loaded.image_enabled is True and loaded.image_path == "add.png"
    assert not hasattr(loaded, "quote_artwork_path")


def test_add_image_still_composes_before_and_after_main(tmp_path):
    intro = _write(tmp_path / "intro.mp4")
    main = _write(tmp_path / "main.mp4")
    outro = _write(tmp_path / "outro.mp4")
    image = _write(tmp_path / "add-image.jpg")
    captured: list[list[MediaInfo]] = []

    class Stage2Engine:
        ffprobe_path = tmp_path / "ffprobe"
        analyzer = SimpleNamespace(probe_raw=lambda path: {
            "streams": [{"codec_type": "video", "width": 800, "height": 1200}]})

        def analyze(self, paths, log=None):
            return [_media(path, portrait=False) for path in paths]

        def make_plan(self, media, settings, log=None):
            captured.append(list(media))
            return resolve_export(media, settings)

        def export(self, media, settings, resolved, output, **kwargs):
            _write(Path(output), b"mp4")
            return ValidationReport(True, [], Path(output), resolved.expected_duration,
                                    1920, 1080, 30.0, True, True)

    for position, expected in (
        ("before_main", ["intro.mp4", "add-image.jpg", "main.mp4", "outro.mp4"]),
        ("after_main", ["intro.mp4", "main.mp4", "add-image.jpg", "outro.mp4"]),
    ):
        captured.clear()
        MainProjectEngine(Stage2Engine()).add_outro(
            ExportSettings(
                workflow_stage="outro", main_video_path=str(main), intro_path=str(intro),
                outro_path=str(outro), image_enabled=True, image_path=str(image),
                image_position=position, resolution="1920x1080",
            ),
            tmp_path / f"out_{position}",
        )
        names = [item.path.name for item in captured[-1]]
        assert names == expected
        assert [item.is_image_insertion for item in captured[-1]].count(True) == 1
        assert not any(getattr(item, "is_quote_artwork", False) for item in captured[-1])


def test_stage2_fingerprint_tracks_add_image_and_has_no_quote_fields(tmp_path):
    main = _write(tmp_path / "main.mp4")
    image = _write(tmp_path / "add-image.jpg")
    media = [_media(main, portrait=False)]
    settings = ExportSettings(
        workflow_stage="outro", main_video_path=str(main), resolution="1920x1080",
    )
    resolved = resolve_export(media, settings)

    payload = build_stage2_payload(media, settings, resolved)
    assert not any("quote" in key for key in payload["settings"])
    assert "image_path" in payload["settings"]

    baseline = stage2_fingerprint(media, settings, resolved)[0]
    with_image = stage2_fingerprint(
        media, replace(settings, image_enabled=True, image_path=str(image)), resolved
    )[0]
    assert with_image != baseline


# --------------------------------------------------------------------------- #
# 5. Preserved end-to-end behavior
# --------------------------------------------------------------------------- #


def test_combined_mode_renders_the_long_form_and_every_short_exactly_once(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(
        EXPORT_MODE_COMBINED, final_pause=1.5, long_form_outro_seconds=1.5
    )

    run = _run(tmp_path, project, settings)

    assert run.result.long_form is not None
    assert len(run.result.shorts) == len(project.durations)
    assert len(_short_videos(run.output_dir)) == len(project.durations)
    assert (run.output_dir / "LongForm" / "YouTube_LongForm.mp4").is_file()
    voice_total = sum(project.durations) + PAUSE * (len(project.durations) - 1)
    # Long-Form: visual intro + spoken audio + the configured outro.
    # Shorts: visual intro + spoken audio + the Short outro.
    assert run.result.long_form.report.duration == pytest.approx(
        LONG_FORM_INTRO_SECONDS + voice_total + 1.5, abs=1e-6
    )
    for short, duration in zip(run.result.shorts, project.durations):
        assert short.report.duration == pytest.approx(
            SHORT_INTRO_SECONDS + duration + SHORT_OUTRO_SECONDS, abs=1e-6
        )


def test_global_script_stays_complete_for_the_long_form(tmp_path):
    project = Project(tmp_path)
    settings = project.settings(EXPORT_MODE_COMBINED)

    run = _run(tmp_path, project, settings)

    long_form_settings_used = next(
        item.settings for item in run.record["exports"] if item.settings.aspect == "16:9"
    )
    assert global_script_path(long_form_settings_used).read_text(encoding="utf-8") == GLOBAL_SCRIPT
    shorts = run.output_dir / "Shorts"
    for path, section in zip(sorted(shorts.glob("*.txt")), SECTIONS):
        assert path.read_text(encoding="utf-8") == section + "\n"
