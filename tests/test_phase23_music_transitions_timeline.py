"""Phase 23: music covers the complete video; independent output audio/transitions.

Contracts pinned here. Everything runs the REAL planner, the REAL command
builder, the REAL orchestrator (with a fake FFmpeg engine, reused from the
Phase-22 harness) and the REAL settings store, so the asserted numbers are the
values the application really produces:

1. **New default timings** — Long-Form intro/outro 1.5 s each, Short intro/outro
   0.7 s each, everywhere: model constants, a fresh ``ExportSettings()``, the
   planned Long-Form/Short jobs, the CLI and the cache paths.

2. **Music covers the complete video** — the canonical timeline defines video
   start/end, voiceover start/end and music start/end. Configured background
   music starts at 0.000 s (never delayed by the visual intro), plays under the
   voiceover and continues through the visual outro until the final frame; the
   render graph trims the looped track to the complete target, not to the spoken
   program. Without a track nothing is invented.

3. **Voiceover and subtitles stay inside the spoken section** — the voiceover
   begins exactly after the visual intro and never reaches into the outro, and
   captions run exactly from the voiceover start to the spoken end.

4. **Independent per-output settings** — Long-Form and Shorts each own their
   music volume (44 % default) and transition (Cross Dissolve / 2.0 s default).
   Changing one never changes the other, in Combined mode and One-Click too, and
   an existing project keeps its saved shared values through migration.

5. **Cache identity and backward compatibility** — every new render-affecting
   setting participates in the Stage-1 fingerprint (schema 4), old shared values
   migrate into both outputs, explicit new values are never overwritten and
   unknown fields are ignored.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.video_merger.command_builder import (
    MUSIC_LOOP_WINDOW_SECONDS,
    FFmpegCommandBuilder,
    _number,
    music_outro_loop,
)
from app.video_merger.errors import VideoMergerError
from app.video_merger.models import (
    DEFAULT_TRANSITION_TYPE,
    LONG_FORM_INTRO_SECONDS,
    LONG_FORM_MUSIC_VOLUME,
    LONG_FORM_OUTRO_SECONDS,
    LONG_FORM_TRANSITION_DURATION,
    MAX_MUSIC_VOLUME_PERCENT,
    MUSIC_VOLUME_PERCENT,
    SHORT_INTRO_SECONDS,
    SHORT_OUTRO_SECONDS,
    SHORTS_MUSIC_VOLUME,
    SHORTS_TRANSITION_DURATION,
    TRANSITION_DURATION_LEGACY_DEFAULT,
    AudioAssetInfo,
    ExportSettings,
)
from app.video_merger.render_cache import (
    FINGERPRINT_SCHEMA,
    build_stage1_payload,
    stage1_fingerprint,
)
from app.video_merger.settings_store import SettingsStore
from app.video_merger.subtitles import (
    DEFAULT_SHORT_ANIMATION,
    SHORT_ANIMATION_OPTIONS,
    normalize_subtitle_animation,
)
from app.video_merger.target import resolve_export
from app.video_merger.video_pool import (
    LEGACY_PRIORITY_CLIPS,
    media_source_folder,
    order_media_for_video_order,
)
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
    SHORT_ENDING_SECONDS,
    build_short_jobs,
    long_form_settings,
    main_timeline,
    output_music_volume,
    output_transition_duration,
    output_transition_type,
    short_settings,
)
from tests.conftest import fake_media

# The Phase-22 harness already drives the real orchestrator without FFmpeg
# binaries; reusing it keeps one proven fake-engine implementation.
from tests.test_phase22_visual_sections import Project, _run, _srt_cues

SPOKEN = 3.0
EPS = 1e-6


@pytest.fixture(autouse=True)
def _private_project_root(tmp_path, monkeypatch):
    """Keep derived sections, caches and staged files inside the test's tmp."""
    root = tmp_path / "project"
    monkeypatch.setattr("app.video_merger.script_sections.project_root", lambda: root)
    monkeypatch.setattr("app.video_merger.main_project.project_root", lambda: root)
    return root


def _short_job(settings: ExportSettings):
    """One Short job; a settings object without a voiceover gets a dummy unit."""
    if not list(getattr(settings, "voiceover_paths", []) or []) and not getattr(
        settings, "voiceover_path", ""
    ):
        settings = replace(settings, voiceover_paths=["voice_1.wav"])
    return build_short_jobs(settings)[0]


def _planned_pair(**overrides) -> tuple[ExportSettings, ExportSettings]:
    """Resolve the Long-Form and the Short settings of one project."""
    base = ExportSettings(
        export_mode=EXPORT_MODE_COMBINED, voiceover_paths=["voice_1.wav"], **overrides
    )
    return long_form_settings(base), short_settings(base, _short_job(base))


# --------------------------------------------------------------------------- #
# A/B. Default timings, configurability and validation
# --------------------------------------------------------------------------- #


def test_the_new_default_timings_are_one_and_a_half_and_zero_point_seven():
    assert (LONG_FORM_INTRO_SECONDS, LONG_FORM_OUTRO_SECONDS) == (1.5, 1.5)
    assert (SHORT_INTRO_SECONDS, SHORT_OUTRO_SECONDS) == (0.7, 0.7)
    settings = ExportSettings()
    assert settings.long_form_intro_seconds == 1.5
    assert settings.long_form_outro_seconds == 1.5
    assert settings.short_intro_seconds == 0.7
    assert settings.short_outro_seconds == 0.7
    # The historical fixed Short ending is the same 0.7 s tail, reused as the
    # semantic outro instead of being stacked behind it.
    assert SHORT_OUTRO_SECONDS == SHORT_ENDING_SECONDS == 0.7

    planned_long, planned_short = _planned_pair()
    assert (planned_long.visual_intro_seconds, planned_long.final_pause) == (1.5, 1.5)
    assert (planned_short.visual_intro_seconds, planned_short.final_pause) == (0.7, 0.7)
    # The canonical defaults of a new project are therefore exactly the values
    # the two real renders of the acceptance run produce: 1.5 + 3.0 + 1.5 = 6.0 s
    # for the Long-Form and 0.7 + 3.0 + 0.7 = 4.4 s for a Short.
    assert main_timeline(planned_long, SPOKEN).target == pytest.approx(6.0)
    assert main_timeline(planned_short, SPOKEN).target == pytest.approx(4.4)


@pytest.mark.parametrize(
    ("field", "planned"),
    [
        ("long_form_intro_seconds", "long"),
        ("long_form_outro_seconds", "long"),
        ("short_intro_seconds", "short"),
        ("short_outro_seconds", "short"),
    ],
)
def test_every_section_is_configurable_and_zero_disables_it(field, planned):
    for value in (0.0, 0.35, 1.5, 12.0):
        long_job, short_job = _planned_pair(**{field: value})
        target = long_job if planned == "long" else short_job
        canonical = (
            target.visual_intro_seconds if field.endswith("intro_seconds") else target.final_pause
        )
        assert canonical == pytest.approx(value), field


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("long_form_intro_seconds", "Long-Form Intro"),
        ("long_form_outro_seconds", "Long-Form Outro"),
        ("short_intro_seconds", "Short Intro"),
        ("short_outro_seconds", "Short Outro"),
    ],
)
def test_negative_or_non_numeric_sections_are_rejected_not_clamped(field, label):
    for invalid in (-0.5, -12.0):
        with pytest.raises(VideoMergerError) as error:
            _planned_pair(**{field: invalid})
        assert label in str(error.value)
        assert "cannot be negative" in str(error.value)
    with pytest.raises(VideoMergerError):
        _planned_pair(**{field: "abc"})
    # There is no artificial upper bound: a long section stays exactly as asked.
    long_job, short_job = _planned_pair(**{field: 180.0})
    assert 180.0 in (long_job.visual_intro_seconds, long_job.final_pause,
                     short_job.visual_intro_seconds, short_job.final_pause)


# --------------------------------------------------------------------------- #
# C–G. The music timeline: 0.000 s to the final video frame
# --------------------------------------------------------------------------- #


def test_the_canonical_timeline_defines_the_complete_audio_contract():
    planned_long, planned_short = _planned_pair()
    long_timeline = main_timeline(planned_long, SPOKEN)
    assert (long_timeline.video_start, long_timeline.music_start) == (0.0, 0.0)
    assert long_timeline.voiceover_start == pytest.approx(1.5)
    assert long_timeline.spoken_end == pytest.approx(4.5)
    assert long_timeline.video_end == long_timeline.music_end == pytest.approx(6.0)
    # The spoken program (voiceover and clip-original audio) is shorter than the
    # music window: this is exactly the difference the fix removes.
    assert long_timeline.audio_program == pytest.approx(4.5)
    assert long_timeline.music_end > long_timeline.audio_program

    short_timeline = main_timeline(planned_short, SPOKEN)
    assert (short_timeline.music_start, short_timeline.music_end) == (0.0, pytest.approx(4.4))
    assert short_timeline.voiceover_start == pytest.approx(0.7)
    assert short_timeline.spoken_end == pytest.approx(3.7)


def _main_graph(tmp_path: Path, **changes):
    """Build one real Stage-1 filter graph with an explicit target duration."""
    media = [
        fake_media(str(tmp_path / "A.mp4"), duration=4.0),
        fake_media(str(tmp_path / "B.mp4"), duration=4.0),
        fake_media(str(tmp_path / "C.mp4"), duration=4.0),
    ]
    values = {
        "resolution": "320x180",
        "workflow_stage": "main",
        # intro 1.5 s + spoken 3.0 s == the spoken program; the visual outro of
        # 1.5 s makes the complete 6.0 s target.
        "program_duration": 4.5,
        "timeline_target_duration": 6.0,
        "visual_intro_seconds": 1.5,
        "final_pause": 1.5,
        "voiceover_paths": [str(tmp_path / "voice.wav")],
        "music_path": str(tmp_path / "music.mp3"),
        "original_audio_mode": "mute",
        "normalize_audio": False,
        "ducking_enabled": False,
    }
    values.update(changes)
    settings = ExportSettings(**values)
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "out.mp4")
    return built, settings, resolved


def _chain(graph: str, label: str) -> str:
    """Return the single filter chain that produces ``[label]``."""
    matches = [line for line in graph.split(";") if line.rstrip().endswith(f"[{label}]")]
    assert len(matches) == 1, f"expected exactly one chain for {label}: {matches}"
    return matches[0]


def test_the_music_window_is_the_complete_video_and_not_the_spoken_program(tmp_path):
    built, settings, resolved = _main_graph(tmp_path)
    graph = built.filter_graph
    music = _chain(graph, "music_pre")

    # The complete rendered window, i.e. intro + spoken + visual outro.
    assert resolved.expected_duration == pytest.approx(6.0)
    assert music.endswith("apad=pad_dur=6.25,atrim=duration=6[music_pre]")
    assert music.count("atrim=duration=6") == 2, "the loop extension and the safety net"
    # The visual outro is covered by repeating the tail of the trimmed music
    # inside the graph, seamlessly and bounded — never by reading the looped
    # input right up to the output end (that deadlocks FFmpeg 6.0 at 0 % CPU).
    assert "aloop=loop=1:size=72000:start=144000" in music
    assert music.index("atrim=duration=4.5") < music.index("aloop=")
    # Music is never delayed: no adelay/asetpts offset in front of it, and the
    # input is stream-looped so an endless source is available.
    assert "adelay" not in music
    assert music.index("asetpts=PTS-STARTPTS") > music.index("atrim=duration=4.5")
    music_input = built.command.index(settings.music_path)
    assert built.command[music_input - 3:music_input] == ["-stream_loop", "-1", "-i"]
    # The final mux length is the complete video, so the track reaches the end.
    assert built.command[built.command.index("-t") + 1] == "6"


def test_the_loop_extension_disappears_when_there_is_no_visual_outro(tmp_path):
    # ``long_form_outro_seconds`` is the canonical field and ``final_pause`` the
    # legacy fallback, so both are zeroed; the explicit timeline target follows
    # the canonical structure (intro 1.5 s + spoken 3.0 s + no outro).
    built, _settings, resolved = _main_graph(
        tmp_path, final_pause=0.0, long_form_outro_seconds=0.0, timeline_target_duration=4.5
    )
    music = _chain(built.filter_graph, "music_pre")
    assert resolved.expected_duration == pytest.approx(4.5)
    assert "aloop" not in music
    # Without an outro the historical chain is byte-identical: read the program,
    # keep the safety pad, cut at the video end.
    assert music.endswith(
        "volume=0.44,atrim=duration=4.5,asetpts=PTS-STARTPTS,"
        "apad=pad_dur=4.75,atrim=duration=4.5[music_pre]"
    )


@pytest.mark.parametrize(
    ("program", "target", "expected"),
    [
        (4.5, 6.0, ",aloop=loop=1:size=72000:start=144000,atrim=duration=6,asetpts=PTS-STARTPTS"),
        (3.7, 4.4, ",aloop=loop=1:size=48000:start=129600,atrim=duration=4.4,asetpts=PTS-STARTPTS"),
        (4.5, 10.0, ",aloop=loop=2:size=216000:start=0,atrim=duration=10,asetpts=PTS-STARTPTS"),
        (0.4, 1.1, ",aloop=loop=2:size=19200:start=0,atrim=duration=1.1,asetpts=PTS-STARTPTS"),
        (4.5, 4.5, ""),          # no visual outro → unchanged chain
        (6.0, 4.5, ""),          # nothing to cover
        (0.0, 6.0, ""),          # no program to repeat
    ],
)
def test_the_outro_loop_is_bounded_seamless_and_exact(program, target, expected):
    assert music_outro_loop(program, target) == expected


@pytest.mark.parametrize("program", [0.4, 1.0, 3.7, 4.5, 30.0, 120.0, 900.0])
@pytest.mark.parametrize("outro", [0.0, 0.7, 1.5, 5.5, 30.0])
def test_the_loop_always_covers_the_outro_with_bounded_memory(program, outro):
    extension = music_outro_loop(program, program + outro)
    target = program + outro
    if outro <= 1e-6 or program <= 1e-6:
        assert extension == ""
        return
    match = re.fullmatch(
        r",aloop=loop=(\d+):size=(\d+):start=(\d+),"
        r"atrim=duration=([\d.]+),asetpts=PTS-STARTPTS",
        extension,
    )
    assert match, extension
    loop, size, start = int(match[1]), int(match[2]), int(match[3])
    # The repeated tail plus the program always reaches the video end …
    assert program + loop * (size / 48000) >= target - 1e-6
    # … the loop starts inside the program, so the outro continues seamlessly …
    assert 0 <= start <= round(program * 48000)
    # … the buffered window stays inside the documented memory bound …
    assert size <= round(MUSIC_LOOP_WINDOW_SECONDS * 48000)
    # … and the extension is cut exactly at the video end.
    assert match[4] == _number(target)


def test_voiceover_still_starts_after_the_intro_and_stops_at_the_spoken_end(tmp_path):
    built, _settings, _resolved = _main_graph(tmp_path)
    graph = built.filter_graph
    voice = _chain(graph, "voice_pre")
    # The visual intro is a real silent segment of the voiceover timeline, so
    # the first spoken sample starts at 1.500 s.
    assert "anullsrc=r=48000:cl=stereo:d=1.5" in graph
    assert "[vintro]" in voice or "concat=n=2" in graph
    # Speech is trimmed at the spoken program end (4.5 s) and only padded with
    # silence afterwards: no voiceover can reach into the visual outro. The one
    # full-length trim is that trailing silence pad, never spoken audio.
    assert "atrim=duration=4.5" in voice
    assert voice.index("atrim=duration=4.5") < voice.index("apad=pad_dur=6.25")
    assert voice.count("atrim=duration=6") == 1
    assert voice.endswith("apad=pad_dur=6.25,atrim=duration=6[voice_pre]")


def test_without_music_no_artificial_audio_is_created(tmp_path):
    built, _settings, _resolved = _main_graph(tmp_path, music_path="")
    graph = built.filter_graph
    assert "music_pre" not in graph
    assert "anullsrc=r=48000:cl=stereo:d=6" not in graph
    assert "-stream_loop" not in built.command
    # The voiceover timeline keeps its silent intro segment, which is the only
    # silence this render intentionally contains.
    assert "anullsrc=r=48000:cl=stereo:d=1.5" in graph


def test_music_volume_is_applied_to_the_complete_music_window(tmp_path):
    built, _settings, _resolved = _main_graph(tmp_path, music_volume=35)
    music = _chain(built.filter_graph, "music_pre")
    assert "volume=0.35" in music
    assert music.endswith("apad=pad_dur=6.25,atrim=duration=6[music_pre]")
    # The gain is applied before the trim and before the outro loop, so it is
    # identical in the visual intro, under the voiceover and in the visual outro.
    assert music.index("volume=0.35") < music.index("atrim=duration=4.5") < music.index("aloop=")


# --------------------------------------------------------------------------- #
# H–L. Real orchestration: voiceover, subtitles and the log contract
# --------------------------------------------------------------------------- #


def test_a_real_long_form_run_keeps_captions_inside_the_spoken_section(tmp_path):
    project = Project(tmp_path, durations=[SPOKEN], sections=["Ein Satz mit mehreren Worten hier."])
    settings = project.settings(EXPORT_MODE_LONG_FORM)
    run = _run(tmp_path, project, settings)

    export = run.record["exports"][0]
    planned = export.settings
    timeline = main_timeline(planned, SPOKEN)
    assert (timeline.intro, timeline.spoken, timeline.outro) == (1.5, SPOKEN, 1.5)
    assert export.resolved.expected_duration == pytest.approx(6.0)
    # The spoken program ends before the video: the visual outro has no speech.
    assert planned.program_duration == pytest.approx(4.5)

    video = run.result.long_form.video
    cues = _srt_cues(video.with_suffix(".srt"))
    assert cues, "the Long-Form run produced no caption"
    assert cues[0][0] >= timeline.voiceover_start - 0.05
    assert cues[-1][1] <= timeline.spoken_end + 0.05
    assert (video.with_suffix(".vtt")).read_text(encoding="utf-8").startswith("WEBVTT")


def test_the_log_states_the_complete_audio_contract_once_per_job(tmp_path):
    project = Project(tmp_path)
    shorts_theme = project.root / "music" / "shorts_theme.mp3"
    shorts_theme.write_bytes(b"shorts music")
    run = _run(
        tmp_path, project,
        project.settings(EXPORT_MODE_COMBINED, short_music_path=str(shorts_theme)),
    )
    logs = run.logs
    jobs = 1 + len(project.durations)

    timeline_lines = [line for line in logs if line.startswith("Timeline: Intro")]
    music_lines = [line for line in logs if line.startswith("Music: start 0.000 s")]
    subtitle_lines = [line for line in logs if line.startswith("Subtitles: start")]
    output_lines = [line for line in logs if line.startswith("Output settings (")]
    assert len(timeline_lines) == jobs
    assert len(music_lines) == jobs
    assert len(subtitle_lines) == jobs
    assert len(output_lines) == jobs

    long_timeline = timeline_lines[0]
    assert "Intro 1.500 s (visual only)" in long_timeline
    assert "Voiceover start 1.500 s" in long_timeline
    assert "Outro 1.500 s (visual only)" in long_timeline
    assert "Video start 0.000 s" in long_timeline
    assert "Video end" in long_timeline
    assert "Music: start 0.000 s" in music_lines[0]
    assert "(video end)" in music_lines[0]
    assert "continuous through the visual intro, the voiceover and the visual outro" in music_lines[0]
    assert "deckt das komplette Video ab (0.000 s →" in "\n".join(logs)
    assert "Output settings (Long-Form): Music volume 44 %" in output_lines[0]
    assert "Transition Cross Dissolve / 2.000 s" in output_lines[0]
    assert "Output settings (Short): Music volume 44 %" in output_lines[1]
    assert "Intro 0.700 s (visual only)" in timeline_lines[1]
    assert "Transition Cross Dissolve / 2.000 s" in output_lines[1]
    # No per-clip or per-frame spam: exactly one timeline block, one music window
    # line, one asset line and one output-settings line per job.
    assert len([line for line in logs if line.startswith("Music: ")]) == 2 * jobs


def test_a_run_without_music_logs_the_silent_contract(tmp_path):
    project = Project(tmp_path, durations=[SPOKEN], sections=["Ein Satz mit mehreren Worten hier."])
    run = _run(tmp_path, project, project.settings(EXPORT_MODE_LONG_FORM, music_path=""))
    assert any(line.startswith("Music: not configured") for line in run.logs)
    assert not any(line.startswith("Music: start") for line in run.logs)


# --------------------------------------------------------------------------- #
# M. Independent music volumes
# --------------------------------------------------------------------------- #


def test_the_music_volume_defaults_are_forty_four_percent_for_both_outputs():
    assert MUSIC_VOLUME_PERCENT == LONG_FORM_MUSIC_VOLUME == SHORTS_MUSIC_VOLUME == 44
    assert ExportSettings().music_volume == 44
    planned_long, planned_short = _planned_pair()
    assert planned_long.music_volume == 44
    assert planned_short.music_volume == 44


def test_long_form_and_shorts_music_volumes_are_fully_independent():
    planned_long, planned_short = _planned_pair(
        long_form_music_volume=35, shorts_music_volume=50
    )
    assert planned_long.music_volume == 35
    assert planned_short.music_volume == 50

    # Changing one output never changes the other.
    only_long = replace(planned_long, music_volume=35)
    assert only_long.music_volume != planned_short.music_volume
    changed_long, unchanged_short = _planned_pair(
        long_form_music_volume=20, shorts_music_volume=50
    )
    assert changed_long.music_volume == 20
    assert unchanged_short.music_volume == 50
    changed_short = _planned_pair(long_form_music_volume=20, shorts_music_volume=70)[1]
    assert changed_short.music_volume == 70


def test_a_saved_shared_music_volume_migrates_into_both_outputs():
    shared = ExportSettings(music_volume=31, voiceover_paths=["voice_1.wav"])
    planned_long = long_form_settings(shared)
    planned_short = short_settings(shared, _short_job(shared))
    assert planned_long.music_volume == 31
    assert planned_short.music_volume == 31
    # An explicit output value always wins over the shared fallback.
    mixed = replace(shared, shorts_music_volume=58)
    assert long_form_settings(mixed).music_volume == 31
    assert short_settings(mixed, _short_job(mixed)).music_volume == 58


def test_music_volume_resolution_clamps_and_rejects_garbage():
    settings = ExportSettings()
    assert output_music_volume(settings, 0, label="Music Volume", default=44) == 0
    assert output_music_volume(settings, 100, label="Music Volume", default=44) == 100
    # Out-of-range values clamp into the render-safe window instead of failing
    # an export, and 0 % stays true silence.
    assert output_music_volume(settings, -20, label="Music Volume", default=44) == 0
    assert output_music_volume(
        settings, MAX_MUSIC_VOLUME_PERCENT + 500, label="Music Volume", default=44
    ) == MAX_MUSIC_VOLUME_PERCENT
    assert output_music_volume(settings, None, label="Music Volume", default=44) == 44
    for invalid in ("loud", float("nan"), float("inf")):
        with pytest.raises(VideoMergerError):
            output_music_volume(settings, invalid, label="Music Volume", default=44)


def test_shorts_never_inherit_the_long_form_music_track():
    planned_long, planned_short = _planned_pair(
        music_path="long_form_theme.mp3", short_music_path="shorts_theme.mp3"
    )
    assert planned_long.music_path == "long_form_theme.mp3"
    assert planned_short.music_path == "shorts_theme.mp3"
    silent_short = _planned_pair(music_path="long_form_theme.mp3", short_music_path="")[1]
    assert silent_short.music_path == ""


# --------------------------------------------------------------------------- #
# N/O. Independent transitions
# --------------------------------------------------------------------------- #


def test_the_transition_defaults_are_cross_dissolve_two_seconds_for_both_outputs():
    assert DEFAULT_TRANSITION_TYPE == "cross_dissolve"
    assert LONG_FORM_TRANSITION_DURATION == SHORTS_TRANSITION_DURATION == 2.0
    planned_long, planned_short = _planned_pair()
    assert planned_long.transition_type == "cross_dissolve"
    assert planned_long.transition_duration == pytest.approx(2.0)
    assert planned_short.transition_type == "cross_dissolve"
    assert planned_short.transition_duration == pytest.approx(2.0)
    # Every existing transition type stays available for both outputs.
    for family in ("smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"):
        long_job, short_job = _planned_pair(
            long_form_transition_type=family, shorts_transition_type=family
        )
        assert long_job.transition_type == family
        assert short_job.transition_type == family


def test_long_form_and_shorts_transitions_are_fully_independent():
    planned_long, planned_short = _planned_pair(
        long_form_transition_type="smooth_blur",
        long_form_transition_duration=2.0,
        shorts_transition_type="film_dissolve",
        shorts_transition_duration=1.0,
    )
    assert (planned_long.transition_type, planned_long.transition_duration) == ("smooth_blur", 2.0)
    assert (planned_short.transition_type, planned_short.transition_duration) == (
        "film_dissolve", 1.0,
    )
    # Changing only the Shorts pair leaves the Long-Form pair untouched.
    changed_long, unchanged_short = _planned_pair(
        long_form_transition_duration=3.0, shorts_transition_duration=1.0
    )
    assert changed_long.transition_duration == pytest.approx(3.0)
    assert unchanged_short.transition_duration == pytest.approx(1.0)


def test_a_saved_shared_transition_is_the_migration_fallback():
    legacy = ExportSettings(
        transition_type="smooth_blur",
        transition_duration=1.3,
        voiceover_paths=["voice_1.wav"],
    )
    planned_long = long_form_settings(legacy)
    planned_short = short_settings(legacy, _short_job(legacy))
    for planned in (planned_long, planned_short):
        assert planned.transition_type == "smooth_blur"
        assert planned.transition_duration == pytest.approx(1.3)

    # A settings object that still carries the historical shared default was
    # never configured, so it receives the new per-output default.
    assert TRANSITION_DURATION_LEGACY_DEFAULT == 1.0
    fresh = ExportSettings(voiceover_paths=["voice_1.wav"])
    assert fresh.transition_duration == TRANSITION_DURATION_LEGACY_DEFAULT
    assert long_form_settings(fresh).transition_duration == pytest.approx(2.0)
    assert short_settings(fresh, _short_job(fresh)).transition_duration == pytest.approx(2.0)

    # Zero stays a valid explicit hard cut for each output on its own.
    hard_cut, short_job = _planned_pair(
        long_form_transition_duration=0.0, shorts_transition_duration=2.0
    )
    assert hard_cut.transition_duration == 0.0
    assert short_job.transition_duration == pytest.approx(2.0)


def test_transition_resolution_rejects_invalid_values():
    settings = ExportSettings()
    for invalid in (-0.5, "long", float("nan"), float("inf")):
        with pytest.raises(VideoMergerError):
            output_transition_duration(
                settings, invalid, label="Transition Duration", default=2.0
            )
    # An unusable family falls back to the safe default instead of failing.
    assert output_transition_type(settings, "star_wipe", label="Transition") == "cross_dissolve"
    assert output_transition_type(settings, "", label="Transition") == "cross_dissolve"


# --------------------------------------------------------------------------- #
# P/Q. Combined mode and One-Click use each output's own settings
# --------------------------------------------------------------------------- #


def _combined_settings(project: Project, **overrides) -> ExportSettings:
    return project.settings(
        EXPORT_MODE_COMBINED,
        long_form_music_volume=30,
        shorts_music_volume=55,
        long_form_transition_type="smooth_blur",
        long_form_transition_duration=2.0,
        shorts_transition_type="film_dissolve",
        shorts_transition_duration=1.0,
        short_music_path=str(project.root / "music" / "shorts_theme.mp3"),
        **overrides,
    )


def test_combined_mode_renders_every_output_with_its_own_settings(tmp_path):
    project = Project(tmp_path)
    (project.root / "music" / "shorts_theme.mp3").write_bytes(b"shorts music")
    run = _run(tmp_path, project, _combined_settings(project))

    exports = run.record["exports"]
    long_export = exports[0]
    short_exports = exports[1:]
    assert len(short_exports) == len(project.durations)

    assert long_export.settings.music_volume == 30
    assert long_export.settings.transition_type == "smooth_blur"
    assert long_export.settings.transition_duration == pytest.approx(2.0)
    assert long_export.settings.visual_intro_seconds == pytest.approx(1.5)
    assert long_export.settings.final_pause == pytest.approx(1.5)
    assert long_export.settings.aspect == "16:9"

    for export in short_exports:
        assert export.settings.music_volume == 55
        assert export.settings.transition_type == "film_dissolve"
        assert export.settings.transition_duration == pytest.approx(1.0)
        assert export.settings.visual_intro_seconds == pytest.approx(0.7)
        assert export.settings.final_pause == pytest.approx(0.7)
        assert export.settings.aspect == "9:16"
        assert export.settings.music_path.endswith("shorts_theme.mp3")


def test_one_click_uses_the_same_independent_settings(tmp_path):
    project = Project(tmp_path)
    (project.root / "music" / "shorts_theme.mp3").write_bytes(b"shorts music")
    outro = (project.root / "outro.mp4")
    outro.write_bytes(b"outro")
    run = _run(
        tmp_path, project,
        _combined_settings(project, outro_path=str(outro), outro_audio_mode="original"),
        complete=True,
    )
    assert run.record["exports"], "One-Click produced no render"
    long_renders = [
        export.settings for export in run.record["exports"]
        if export.settings.export_mode == EXPORT_MODE_LONG_FORM
    ]
    short_renders = [
        export.settings for export in run.record["exports"]
        if export.settings.export_mode == EXPORT_MODE_SHORTS
    ]
    assert long_renders and short_renders
    # Stage 1 and every Stage-2 pass of one job keep that job's own values.
    for planned in long_renders:
        assert planned.music_volume == 30
        assert planned.transition_type == "smooth_blur"
        assert planned.transition_duration == pytest.approx(2.0)
        assert planned.visual_intro_seconds == pytest.approx(1.5)
    for planned in short_renders:
        assert planned.music_volume == 55
        assert planned.transition_type == "film_dissolve"
        assert planned.transition_duration == pytest.approx(1.0)
        assert planned.visual_intro_seconds == pytest.approx(0.7)
    # One-Click still composes a final video per output.
    assert run.result.long_form is not None
    assert len(run.result.shorts) == len(project.durations)


# --------------------------------------------------------------------------- #
# R. Cache identity
# --------------------------------------------------------------------------- #


def _identity(tmp_path: Path, *, music: bool = False, **changes):
    media = [fake_media(str(tmp_path / f"clip_{index}.mp4"), duration=4.0) for index in range(3)]
    values = {
        "resolution": "320x180", "workflow_stage": "main", "program_duration": 4.5,
        "timeline_target_duration": 6.0, "voiceover_path": str(tmp_path / "voice.wav"),
    }
    values.update(changes)
    settings = ExportSettings(**values)
    resolved = resolve_export(media, settings)
    asset = (
        AudioAssetInfo(path=tmp_path / "music.mp3", duration=30.0, sample_rate=48000, channels=2)
        if music else None
    )
    digest = stage1_fingerprint(media, settings, resolved, music_asset=asset)[0]
    return digest, media, settings, resolved, asset


@pytest.mark.parametrize(
    ("field", "value", "music"),
    [
        ("long_form_intro_seconds", 0.0, False),
        ("long_form_outro_seconds", 0.0, False),
        ("short_intro_seconds", 0.0, False),
        ("short_outro_seconds", 0.0, False),
        ("opening_effect", "zoom_in", False),
        ("long_form_transition_type", "film_dissolve", False),
        ("long_form_transition_duration", 1.0, False),
        ("shorts_transition_type", "additive_dissolve", False),
        ("shorts_transition_duration", 0.5, False),
        ("long_form_music_volume", 30, True),
        ("shorts_music_volume", 55, True),
    ],
)
def test_every_new_render_setting_changes_the_stage1_identity(tmp_path, field, value, music):
    baseline, media, settings, resolved, asset = _identity(tmp_path, music=music)
    changed = replace(settings, **{field: value})
    assert stage1_fingerprint(
        media, changed, resolved, music_asset=asset
    )[0] != baseline, field


def test_music_volumes_only_matter_while_a_track_is_really_mixed(tmp_path):
    """An unused control must not invalidate an otherwise identical render."""
    silent, media, settings, resolved, _asset = _identity(tmp_path, music=False)
    for field in ("long_form_music_volume", "shorts_music_volume"):
        assert stage1_fingerprint(
            media, replace(settings, **{field: 12}), resolved
        )[0] == silent, field
    with_music, _media, _settings, _resolved, asset = _identity(tmp_path, music=True)
    assert stage1_fingerprint(
        media, replace(settings, long_form_music_volume=12), resolved, music_asset=asset
    )[0] != with_music


def test_the_schema_bump_makes_old_entries_unreachable(tmp_path):
    # 5: configured folders can carry a soft timeline-area role plus zone
    # targets, which change WHICH clips a render selects, so a schema-4 entry
    # (built without that source ordering) can never be reused silently.
    assert FINGERPRINT_SCHEMA == 5
    media = [fake_media(str(tmp_path / "clip.mp4"), duration=4.0)]
    settings = ExportSettings(resolution="320x180", workflow_stage="main")
    resolved = resolve_export(media, settings)
    digest, payload = stage1_fingerprint(media, settings, resolved)
    assert payload["schema"] == 5
    # Fail closed: a payload from the previous schema (music stopped at the
    # spoken end, no timeline-area source ordering) can never produce the
    # current digest, so it cannot be reused.
    stale = dict(payload)
    stale["schema"] = 4
    assert build_stage1_payload(media, settings, resolved)["schema"] == 5
    assert json.dumps(stale, sort_keys=True) != json.dumps(payload, sort_keys=True)
    assert digest and isinstance(digest, str)


def test_the_music_window_change_is_part_of_the_identity_even_with_equal_settings(tmp_path):
    """Two renders that differ only in the outro must never share a cache entry."""
    with_outro, media, settings, resolved, _asset = _identity(
        tmp_path, final_pause=1.5, timeline_target_duration=6.0
    )
    without_outro = stage1_fingerprint(
        media,
        replace(settings, final_pause=0.0, timeline_target_duration=4.5),
        resolved,
    )[0]
    assert with_outro != without_outro


# --------------------------------------------------------------------------- #
# S. Backward compatibility of saved projects
# --------------------------------------------------------------------------- #


def _write_project(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "config" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_old_project_keeps_its_saved_shared_values(tmp_path):
    path = _write_project(tmp_path, {
        "music_volume": 31,
        "transition_type": "smooth_blur",
        "transition_duration": 1.3,
        "final_pause": 4.0,
        "long_form_intro_seconds": 2.5,
        "short_intro_seconds": 1.5,
        "short_outro_seconds": 1.5,
        "unknown_future_field": {"nested": True},
    })
    loaded = SettingsStore(path).load()
    # Shared values were copied into both new output-specific settings.
    assert loaded.long_form_music_volume == 31
    assert loaded.shorts_music_volume == 31
    assert loaded.long_form_transition_type == "smooth_blur"
    assert loaded.shorts_transition_type == "smooth_blur"
    assert loaded.long_form_transition_duration == pytest.approx(1.3)
    assert loaded.shorts_transition_duration == pytest.approx(1.3)
    # Explicit saved sections and the migrated end padding stay untouched.
    assert loaded.long_form_outro_seconds == pytest.approx(4.0)
    assert loaded.long_form_intro_seconds == pytest.approx(2.5)
    assert loaded.short_intro_seconds == pytest.approx(1.5)
    assert loaded.short_outro_seconds == pytest.approx(1.5)
    # Unknown fields are ignored safely.
    assert not hasattr(loaded, "unknown_future_field")

    planned_long = long_form_settings(loaded)
    planned_short = short_settings(loaded, _short_job(loaded))
    assert planned_long.music_volume == 31
    assert planned_short.music_volume == 31
    assert planned_long.transition_duration == pytest.approx(1.3)
    assert planned_short.transition_duration == pytest.approx(1.3)
    assert planned_long.visual_intro_seconds == pytest.approx(2.5)
    assert planned_short.visual_intro_seconds == pytest.approx(1.5)


def test_a_project_without_the_new_settings_receives_the_new_defaults(tmp_path):
    path = _write_project(tmp_path, {"music_path": "theme.mp3"})
    loaded = SettingsStore(path).load()
    assert loaded.long_form_intro_seconds == LONG_FORM_INTRO_SECONDS == 1.5
    assert loaded.long_form_outro_seconds == LONG_FORM_OUTRO_SECONDS == 1.5
    assert loaded.short_intro_seconds == SHORT_INTRO_SECONDS == 0.7
    assert loaded.short_outro_seconds == SHORT_OUTRO_SECONDS == 0.7
    planned_long = long_form_settings(loaded)
    planned_short = short_settings(loaded, _short_job(loaded))
    assert planned_long.music_volume == planned_short.music_volume == 44
    assert planned_long.transition_type == planned_short.transition_type == "cross_dissolve"
    assert planned_long.transition_duration == pytest.approx(2.0)
    assert planned_short.transition_duration == pytest.approx(2.0)


def test_explicit_new_values_are_never_overwritten_by_the_migration(tmp_path):
    path = _write_project(tmp_path, {
        "music_volume": 31,
        "long_form_music_volume": 35,
        "shorts_music_volume": 50,
        "transition_duration": 1.3,
        "long_form_transition_duration": 2.5,
        "shorts_transition_duration": 0.8,
        "shorts_transition_type": "additive_dissolve",
    })
    loaded = SettingsStore(path).load()
    assert loaded.long_form_music_volume == 35
    assert loaded.shorts_music_volume == 50
    assert loaded.music_volume == 31
    assert loaded.long_form_transition_duration == pytest.approx(2.5)
    assert loaded.shorts_transition_duration == pytest.approx(0.8)
    assert loaded.shorts_transition_type == "additive_dissolve"
    # This project saved no shared transition TYPE, so nothing was copied: the
    # empty value stays unset and the resolver falls back to Cross Dissolve.
    assert loaded.long_form_transition_type == ""
    planned_long = long_form_settings(loaded)
    assert planned_long.transition_type == "cross_dissolve"
    assert planned_long.music_volume == 35
    assert planned_long.transition_duration == pytest.approx(2.5)


def test_settings_round_trip_keeps_the_independent_values(tmp_path):
    store = SettingsStore(tmp_path / "config" / "settings.json")
    settings = ExportSettings(
        long_form_music_volume=35, shorts_music_volume=50,
        long_form_transition_type="smooth_blur", long_form_transition_duration=2.0,
        shorts_transition_type="film_dissolve", shorts_transition_duration=1.0,
        long_form_intro_seconds=1.5, long_form_outro_seconds=1.5,
        short_intro_seconds=0.7, short_outro_seconds=0.7,
    )
    store.save(settings)
    loaded = store.load()
    assert loaded.long_form_music_volume == 35
    assert loaded.shorts_music_volume == 50
    assert loaded.long_form_transition_type == "smooth_blur"
    assert loaded.shorts_transition_type == "film_dissolve"
    assert loaded.shorts_transition_duration == pytest.approx(1.0)
    assert loaded.short_outro_seconds == pytest.approx(0.7)


def test_the_cli_defaults_and_flags_are_independent_per_output(tmp_path, monkeypatch):
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

    def run_cli(*extra: str) -> ExportSettings:
        monkeypatch.setattr(sys, "argv", [
            "app.cli", "--stage", "main", "--input", str(tmp_path),
            "--output", str(tmp_path / "out"),
            "--export-mode", EXPORT_MODE_COMBINED, *extra,
        ])
        assert cli.main() == 0
        return captured["settings"]

    defaults = run_cli()
    assert defaults.long_form_intro_seconds == pytest.approx(1.5)
    assert defaults.long_form_outro_seconds == pytest.approx(1.5)
    assert defaults.short_intro_seconds == pytest.approx(0.7)
    assert defaults.short_outro_seconds == pytest.approx(0.7)
    assert defaults.long_form_music_volume == 44
    assert defaults.shorts_music_volume == 44
    assert defaults.long_form_transition_type == "cross_dissolve"
    assert defaults.shorts_transition_type == "cross_dissolve"
    assert defaults.long_form_transition_duration == pytest.approx(2.0)
    assert defaults.shorts_transition_duration == pytest.approx(2.0)
    # The shared canonical value keeps its historical default for a basic merge.
    assert defaults.transition_duration == pytest.approx(TRANSITION_DURATION_LEGACY_DEFAULT)

    independent = run_cli(
        "--long-music-volume", "30", "--short-music-volume", "55",
        "--long-transition", "2.0", "--short-transition", "1.0",
        "--long-transition-effect", "smooth_blur", "--short-transition-effect", "film_dissolve",
    )
    assert independent.long_form_music_volume == 30
    assert independent.shorts_music_volume == 55
    assert independent.long_form_transition_duration == pytest.approx(2.0)
    assert independent.shorts_transition_duration == pytest.approx(1.0)
    assert independent.long_form_transition_type == "smooth_blur"
    assert independent.shorts_transition_type == "film_dissolve"

    # An explicitly given shared value remains the migration fallback for both.
    shared = run_cli("--music-volume", "22", "--transition", "1.3")
    assert shared.long_form_music_volume == 22
    assert shared.shorts_music_volume == 22
    assert shared.long_form_transition_duration == pytest.approx(1.3)
    assert shared.shorts_transition_duration == pytest.approx(1.3)
    assert shared.transition_duration == pytest.approx(1.3)


# --------------------------------------------------------------------------- #
# T/U/V. Preserved behaviour: randomization, animations, Add Image
# --------------------------------------------------------------------------- #


def test_the_random_legacy_input_root_priority_is_unchanged(tmp_path):
    legacy_root = tmp_path / "legacy"
    other_root = tmp_path / "other"
    media = [
        fake_media(str(legacy_root / f"L{index}.mp4"), duration=3.0) for index in range(4)
    ] + [
        fake_media(str(other_root / f"O{index}.mp4"), duration=3.0) for index in range(6)
    ]
    ordered = order_media_for_video_order(
        media, "random", seed=20260905, legacy_root=str(legacy_root)
    )
    prefix = ordered[:LEGACY_PRIORITY_CLIPS]
    assert len(prefix) == LEGACY_PRIORITY_CLIPS
    assert all(media_source_folder(item) == media_source_folder(media[0]) for item in prefix)
    assert len({item.path for item in prefix}) == LEGACY_PRIORITY_CLIPS
    # Deterministic for one seed, and the rest of the pool is untouched logic.
    again = order_media_for_video_order(
        media, "random", seed=20260905, legacy_root=str(legacy_root)
    )
    assert [item.path for item in again] == [item.path for item in ordered]
    # Without a legacy root the historical unbiased shuffle is bit-identical.
    plain = order_media_for_video_order(media, "random", seed=7)
    plain_again = order_media_for_video_order(media, "random", seed=7, legacy_root="")
    assert [item.path for item in plain] == [item.path for item in plain_again]
    for mode in ("natural", "alphabetical", "manual"):
        assert [item.path for item in order_media_for_video_order(
            media, mode, legacy_root=str(legacy_root)
        )] == [item.path for item in order_media_for_video_order(media, mode)]


def test_the_short_subtitle_animation_rules_are_unchanged():
    assert "word_highlight" not in SHORT_ANIMATION_OPTIONS
    assert DEFAULT_SHORT_ANIMATION == "phrase_focus"
    assert normalize_subtitle_animation("word_highlight", "short") == DEFAULT_SHORT_ANIMATION
    assert normalize_subtitle_animation("outline_highlight", "short") != "outline_highlight"
    planned_short = _planned_pair(short_subtitle_animation="word_highlight")[1]
    assert planned_short.subtitle_animation == DEFAULT_SHORT_ANIMATION


def test_add_image_settings_survive_both_planners():
    values = {
        "image_enabled": True, "image_path": "flyer.png", "image_position": "after_main",
        "image_duration": 5.0, "image_transition_type": "film_dissolve",
        "image_transition_duration": 1.25, "image_fit_mode": "fill", "image_zoom": 140,
        "image_filter": "warm",
    }
    for planned in _planned_pair(**values):
        for field, value in values.items():
            assert getattr(planned, field) == value, field


def test_opening_effect_stays_long_form_only_and_defaults_to_none():
    planned_long, planned_short = _planned_pair(opening_effect="zoom_out")
    assert planned_long.opening_effect == "zoom_out"
    assert planned_short.opening_effect == "none"
    assert ExportSettings().opening_effect == "none"


# --------------------------------------------------------------------------- #
# Diagnostics: the resolved per-output values are visible and fail closed
# --------------------------------------------------------------------------- #


def _diagnostics(settings: ExportSettings) -> dict:
    from unittest.mock import patch

    from app.video_merger import diagnostics

    with patch.object(diagnostics, "locate_ffmpeg", lambda: ("ffmpeg", "ffprobe")), \
            patch("app.video_merger.project_assets.probe_audio",
                  lambda ffprobe, path: SimpleNamespace(
                      path=Path(path), duration=9.0, sample_rate=48000, channels=2, codec="mp3")):
        return {
            item.name: (item.ok, item.detail)
            for item in diagnostics.run_project_diagnostics(settings)
        }


def test_diagnostics_show_the_resolved_output_audio_and_transitions():
    items = _diagnostics(ExportSettings(
        long_form_music_volume=35, shorts_music_volume=50,
        long_form_transition_type="smooth_blur", long_form_transition_duration=2.0,
        shorts_transition_type="film_dissolve", shorts_transition_duration=1.0,
    ))
    ok, detail = items["Output Music & Transitions"]
    assert ok
    assert "Long-Form music 35 %" in detail
    assert "Shorts music 50 %" in detail
    assert "0.000 s → video end" in detail
    assert "Long-Form transition Smooth Blur Crossfade / 2.000 s" in detail
    assert "Shorts transition Film Dissolve / 1.000 s" in detail

    # A project that only carries the shared values shows the migrated result.
    _ok, migrated = _diagnostics(ExportSettings(music_volume=22, transition_duration=1.3))[
        "Output Music & Transitions"
    ]
    assert "Long-Form music 22 %" in migrated
    assert "Shorts music 22 %" in migrated
    assert "Cross Dissolve / 1.300 s" in migrated


@pytest.mark.parametrize(
    "overrides",
    [
        {"long_form_music_volume": "loud"},
        {"shorts_transition_duration": -1.0},
        {"long_form_transition_duration": float("nan")},
    ],
)
def test_diagnostics_report_invalid_output_values_instead_of_raising(overrides):
    ok, detail = _diagnostics(ExportSettings(**overrides))["Output Music & Transitions"]
    assert ok is False
    assert detail
    # The rest of the diagnostics stay readable for a broken project.
    assert "Visual Timeline Sections" in _diagnostics(ExportSettings(**overrides))
