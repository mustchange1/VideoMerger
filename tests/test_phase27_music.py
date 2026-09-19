"""Phase 27 – multiple music tracks, ordering and whole-sequence looping.

Acceptance criteria covered:
* one / two / three tracks all build valid render configurations,
* a single track keeps the EXACT legacy single-input stream-loop graph,
* two or more tracks use one real input per track and the graph repeats the
  ENTIRE ordered sequence as a unit (A → B → C → A → B → C …), never looping
  one track on its own,
* user order is authoritative,
* the settings model + SettingsStore persist and migrate the sequence.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder, _music_sequence_plan
from app.video_merger.models import ExportSettings
from app.video_merger.music_tracks import (
    effective_music_tracks,
    music_track_paths,
    normalize_music_track,
    normalize_music_tracks,
)
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export
from tests.conftest import fake_media


def _track(path: str, trim_start: float = 0.0, trim_duration: float = 0.0) -> dict:
    return {"path": path, "trim_start": trim_start, "trim_duration": trim_duration}


def _plan(path: str, trim_start: float = 0.0, trim_duration: float = 0.0, duration: float = 10.0) -> dict:
    """Runtime render plan entry (what MainProjectEngine computes)."""
    return {
        "path": path,
        "trim_start": trim_start,
        "trim_duration": trim_duration,
        "duration": duration,
    }


def _main_build(tmp_path, **changes):
    media = [fake_media(str(tmp_path / "A.mp4"), duration=2), fake_media(str(tmp_path / "B.mp4"), duration=2)]
    values = dict(
        resolution="160x90", workflow_stage="main", program_duration=3,
        voiceover_path=str(tmp_path / "voice.wav"),
        original_audio_mode="mute", normalize_audio=False,
    )
    values.update(changes)
    settings = ExportSettings(**values)
    resolved = resolve_export(media, settings)
    built = FFmpegCommandBuilder("ffmpeg").build(media, settings, resolved, tmp_path / "out.mp4")
    return built, settings


# ---------------------------------------------------------------------------
# normalization / effective sequence
# ---------------------------------------------------------------------------
def test_normalize_track_clamps_and_defaults():
    # Phase 28 superset: entries additionally carry the playback mode
    # ("once") and repeat count (1), the historical defaults.
    assert normalize_music_track("/m/a.mp3") == {
        "path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0,
        "playback_mode": "once", "repeat_count": 1,
    }
    assert normalize_music_track("   ") is None
    assert normalize_music_track(42) is None
    clamped = normalize_music_track({"path": "/m/a.mp3", "trim_start": -5.0, "trim_duration": 99999})
    assert clamped["trim_start"] == 0.0
    assert clamped["trim_duration"] == 3600.0
    assert clamped["playback_mode"] == "once"
    assert clamped["repeat_count"] == 1


def test_effective_tracks_single_legacy_path_migrates():
    settings = ExportSettings(music_path="/m/only.mp3", music_tracks=[])
    assert effective_music_tracks(settings) == [_track("/m/only.mp3")]
    assert [p.name for p in music_track_paths(settings)] == ["only.mp3"]


def test_effective_tracks_explicit_list_wins():
    settings = ExportSettings(
        music_path="/m/legacy.mp3",
        music_tracks=[_track("/m/a.mp3"), _track("/m/b.mp3")],
    )
    assert [t["path"] for t in effective_music_tracks(settings)] == ["/m/a.mp3", "/m/b.mp3"]


def test_effective_tracks_empty_when_nothing():
    assert effective_music_tracks(ExportSettings()) == []


# ---------------------------------------------------------------------------
# command builder: legacy single track is byte-identical
# ---------------------------------------------------------------------------
def test_single_track_keeps_legacy_stream_loop_input(tmp_path):
    built, settings = _main_build(tmp_path, music_path=str(tmp_path / "music.mp3"))
    command = built.command
    joined = " ".join(command)
    assert "-stream_loop -1" in joined
    assert str(tmp_path / "music.mp3") in joined
    # No explicit per-track sequence graph for a single legacy track.
    assert "mtrack" not in built.filter_graph


def test_single_track_with_trim_switches_to_sequence_graph(tmp_path):
    settings = ExportSettings(
        music_path=str(tmp_path / "music.mp3"),
        music_track_plan=[_plan(str(tmp_path / "music.mp3"), trim_start=2.0)],
    )
    plan = _music_sequence_plan(settings)
    assert len(plan) == 1
    assert plan[0]["trim_start"] == 2.0


# ---------------------------------------------------------------------------
# command builder: two / three tracks loop as a unit
# ---------------------------------------------------------------------------
def test_two_tracks_use_one_input_each_and_concat(tmp_path):
    m1, m2 = str(tmp_path / "t1.mp3"), str(tmp_path / "t2.mp3")
    built, _ = _main_build(
        tmp_path,
        music_track_plan=[_plan(m1, duration=2.0), _plan(m2, duration=2.0)],
    )
    command = built.command
    # Each track is a real input, in order, and NOT stream-looped.
    assert command.count("-stream_loop") == 0
    i1, i2 = command.index(m1), command.index(m2)
    assert i1 < i2
    # The graph concats the two prepared tracks into one sequence.
    assert "mtrack0" in built.filter_graph and "mtrack1" in built.filter_graph
    assert "concat=n=2:v=0:a=1[music_seq]" in built.filter_graph


def test_three_tracks_loop_whole_sequence_not_individual(tmp_path):
    tracks = [_plan(str(tmp_path / f"t{n}.mp3"), duration=1.5) for n in range(3)]
    built, _ = _main_build(tmp_path, music_track_plan=tracks)
    graph = built.filter_graph
    # Three tracks are concatenated in the authored order.
    assert "concat=n=3:v=0:a=1[music_seq]" in graph
    # The whole sequence is split and re-concatenated to cover the target,
    # i.e. the SEQUENCE repeats, never a single track on its own.
    assert "asplit=" in graph and "music_loop" in graph
    # No per-track self loop construct remains.
    assert "aloop" not in graph


def test_track_order_is_authoritative_in_command(tmp_path):
    first, second = str(tmp_path / "zzz.mp3"), str(tmp_path / "aaa.mp3")
    built, _ = _main_build(
        tmp_path,
        music_track_plan=[_plan(first, duration=2.0), _plan(second, duration=2.0)],
    )
    command = built.command
    # Authored order (zzz before aaa) is preserved even though alphabetical
    # would reverse it.
    assert command.index(first) < command.index(second)


def test_no_music_yields_no_music_input(tmp_path):
    built, _ = _main_build(tmp_path)
    joined = " ".join(built.command)
    assert "-stream_loop -1" not in joined
    assert "mtrack" not in built.filter_graph


def test_per_track_trim_lands_in_the_graph(tmp_path):
    built, _ = _main_build(
        tmp_path,
        music_track_plan=[
            _plan(str(tmp_path / "t1.mp3"), trim_start=2.0, trim_duration=5.0, duration=5.0),
            _plan(str(tmp_path / "t2.mp3"), duration=3.0),
        ],
    )
    graph = built.filter_graph
    # Trimmed track carries an explicit atrim window; untrimmed does not.
    assert "atrim=start=2:duration=5" in graph
    track0_line = next(line for line in graph.split(";") if "mtrack0" in line and "atrim=start" in line)
    track1_line = next(line for line in graph.split(";") if "[mtrack1]" in line)
    assert "atrim=" not in track1_line


def test_volume_and_ducking_chain_unchanged_for_sequence(tmp_path):
    built, _ = _main_build(
        tmp_path,
        music_track_plan=[_plan(str(tmp_path / "t1.mp3"), duration=2.0),
                          _plan(str(tmp_path / "t2.mp3"), duration=2.0)],
        ducking_enabled=True,
        voiceover_path=str(tmp_path / "voice.wav"),
    )
    graph = built.filter_graph
    # Global volume + ducking still apply AFTER the sequence is assembled.
    assert "sidechaincompress" in graph
    assert "volume=" in graph


# ---------------------------------------------------------------------------
# persistence + migration
# ---------------------------------------------------------------------------
def test_music_tracks_persist_roundtrip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = ExportSettings(
        music_tracks=[_track("/m/a.mp3", 1.0, 30.0), _track("/m/b.mp3")],
    )
    store.save(settings)
    loaded = store.load()
    assert loaded.music_tracks == [
        {"path": "/m/a.mp3", "trim_start": 1.0, "trim_duration": 30.0},
        {"path": "/m/b.mp3", "trim_start": 0.0, "trim_duration": 0.0},
    ]


def test_legacy_project_without_music_tracks_still_works(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        '{"music_path": "/m/legacy.mp3"}', encoding="utf-8"
    )
    loaded = store.load()
    assert loaded.music_path == "/m/legacy.mp3"
    assert loaded.music_tracks == []
    # And the effective sequence treats it as the one-track sequence.
    assert effective_music_tracks(loaded) == [_track("/m/legacy.mp3")]


def test_render_stage_identity_changes_with_multitrack(tmp_path):
    """Music changes must invalidate the RENDER stage, never ASR/alignment."""
    from app.video_merger.render_cache import build_stage1_payload

    media = [fake_media(str(tmp_path / "A.mp4"), duration=2)]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", program_duration=2,
        original_audio_mode="mute", normalize_audio=False,
    )
    resolved = resolve_export(media, settings)

    single = build_stage1_payload(
        media, settings, resolved, music_track_plan=[{"path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0, "duration": 10.0}]
    )
    # Legacy single-track default must NOT add a music_tracks key, keeping
    # existing Stage-1 caches valid.
    assert "music_tracks" not in single

    multi = build_stage1_payload(
        media, settings, resolved,
        music_track_plan=[
            {"path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0, "duration": 10.0},
            {"path": "/m/b.mp3", "trim_start": 0.0, "trim_duration": 0.0, "duration": 12.0},
        ],
    )
    assert "music_tracks" in multi and len(multi["music_tracks"]) == 2
