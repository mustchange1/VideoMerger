"""Phase 28 – dedicated Shorts sources, flexible music modes, position dedup,
Shorts After Merge up to 3.50x and render-cache discipline.

Regression contract: every configuration that existed before Phase 28 keeps
its exact behavior — an unchanged project must produce an unchanged render
identity, the historical whole-sequence music loop stays the default, and the
Long-Form output never reads a Shorts-only setting.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.models import ExportSettings
from app.video_merger.music_tracks import (
    build_music_segment_plan,
    normalize_music_track,
    normalize_playback_mode,
    normalize_sequence_mode,
    sequence_is_legacy,
)
from app.video_merger.render_cache import build_stage1_payload
from app.video_merger.settings_store import SettingsStore
from app.video_merger.subtitles import SUBTITLE_POSITIONS, normalize_subtitle_position
from app.video_merger.target import resolve_export
from app.video_merger.timeline import duration_after_merge_value
from app.video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_SHORTS,
    build_short_jobs,
    long_form_settings,
    short_settings,
)
from tests.conftest import fake_media


def _track(path: str, duration: float = 10.0, mode: str = "once", repeat: int = 1,
           trim_start: float = 0.0, trim_duration: float = 0.0) -> dict:
    return {
        "path": path, "trim_start": trim_start, "trim_duration": trim_duration,
        "duration": duration, "playback_mode": mode, "repeat_count": repeat,
    }


# ---------------------------------------------------------------------------
# Data model (feature 12): minimum new explicit fields, safe defaults
# ---------------------------------------------------------------------------
def test_phase28_fields_default_to_legacy_behavior():
    settings = ExportSettings()
    assert settings.shorts_video_folders == []
    assert settings.shorts_duration_after_merge is None
    assert normalize_sequence_mode(settings.music_sequence_mode) == "loop_sequence"
    assert normalize_sequence_mode(settings.short_music_sequence_mode) == "loop_sequence"
    # Canonical position labels; both render exactly like the legacy strings.
    assert settings.subtitle_position == "Center"
    assert settings.short_subtitle_position == "Bottom"


# ---------------------------------------------------------------------------
# Center vs Middle deduplication (feature 8)
# ---------------------------------------------------------------------------
def test_user_facing_choice_set_contains_one_option_per_spot():
    assert list(SUBTITLE_POSITIONS) == ["Bottom", "Center", "Medium-Low", "Top"]
    assert "Middle" not in SUBTITLE_POSITIONS
    assert "Bottom Center" not in SUBTITLE_POSITIONS


def test_normalize_subtitle_position_migrates_duplicates():
    assert normalize_subtitle_position("Middle", "long") == "Center"
    assert normalize_subtitle_position("middle", "short") == "Center"
    assert normalize_subtitle_position("Bottom Center", "short") == "Bottom"
    assert normalize_subtitle_position("bottom-center", "short") == "Bottom"
    assert normalize_subtitle_position("Bottom", "short") == "Bottom"
    assert normalize_subtitle_position("Top", "short") == "Top"
    # Unknown legacy spellings fall back to the collection default, exactly
    # like the renderer does, so the visible label matches the render spot.
    assert normalize_subtitle_position("Top Center", "short") == "Medium-Low"
    assert normalize_subtitle_position("", "long") == "Center"
    assert normalize_subtitle_position("", "short") == "Medium-Low"


def test_settings_store_migrates_legacy_positions(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "subtitle_position": "Middle",
        "short_subtitle_position": "Bottom Center",
    }), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.subtitle_position == "Center"
    assert loaded.short_subtitle_position == "Bottom"


def test_settings_store_roundtrips_phase28_fields(tmp_path):
    path = tmp_path / "settings.json"
    selected = ExportSettings(
        shorts_video_folders=[str(tmp_path / "Shorts A"), str(tmp_path / "Shorts B")],
        shorts_duration_after_merge=3.5,
        music_sequence_mode="play_once",
        short_music_sequence_mode="loop_sequence",
        music_tracks=[_track("/m/a.mp3", mode="loop")],
    )
    SettingsStore(path).save(selected)
    loaded = SettingsStore(path).load()
    assert loaded == selected
    assert loaded.shorts_video_folders == selected.shorts_video_folders
    assert loaded.shorts_duration_after_merge == 3.5
    assert loaded.music_sequence_mode == "play_once"
    assert loaded.music_tracks[0]["playback_mode"] == "loop"


# ---------------------------------------------------------------------------
# Music playback / sequence modes (feature 2)
# ---------------------------------------------------------------------------
def test_playback_mode_normalization():
    assert normalize_playback_mode(None) == "once"
    assert normalize_playback_mode("LOOP") == "loop"
    assert normalize_playback_mode("repeat") == "repeat"
    assert normalize_playback_mode("bogus") == "once"
    assert normalize_sequence_mode("") == "loop_sequence"
    assert normalize_sequence_mode("PLAY_ONCE") == "play_once"
    assert normalize_sequence_mode("bogus") == "loop_sequence"


def test_track_normalization_keeps_modes_consistent():
    track = normalize_music_track({"path": "/m/a.mp3", "playback_mode": "repeat", "repeat_count": 4})
    assert track["playback_mode"] == "repeat"
    assert track["repeat_count"] == 4
    # Only "repeat" keeps a count; loop/once always carry 1.
    loop = normalize_music_track({"path": "/m/a.mp3", "playback_mode": "loop", "repeat_count": 9})
    assert loop["playback_mode"] == "loop"
    assert loop["repeat_count"] == 1
    clamped = normalize_music_track({"path": "/m/a.mp3", "playback_mode": "repeat", "repeat_count": 5000})
    assert clamped["repeat_count"] == 1000


def test_sequence_is_legacy_classification():
    legacy_single = [{"path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0}]
    assert sequence_is_legacy(legacy_single, "loop_sequence")
    # play_once must END the music after the single play; only the explicit
    # segment graph can express that, so it is not legacy.
    assert not sequence_is_legacy(legacy_single, "play_once")
    trimmed = [{"path": "/m/a.mp3", "trim_start": 1.0, "trim_duration": 0.0}]
    assert not sequence_is_legacy(trimmed, "loop_sequence")
    looping = [_track("/m/a.mp3", mode="loop")]
    assert not sequence_is_legacy(looping, "loop_sequence")
    multi_once = [
        {"path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0},
        {"path": "/m/b.mp3", "trim_start": 0.0, "trim_duration": 0.0},
    ]
    assert sequence_is_legacy(multi_once, "loop_sequence")
    assert not sequence_is_legacy(multi_once, "play_once")


def test_segment_plan_mode1_play_once_sequence():
    tracks = [_track("/m/a.mp3", duration=2.0), _track("/m/b.mp3", duration=3.0)]
    plan = build_music_segment_plan(tracks, "loop_sequence", 12.0)
    assert [seg["path"] for seg in plan] == ["/m/a.mp3", "/m/b.mp3", "/m/a.mp3", "/m/b.mp3", "/m/a.mp3"]
    assert sum(seg["duration"] for seg in plan) == pytest.approx(12.0)
    # The final occurrence is clamped to the boundary, never overshooting.
    assert plan[-1]["duration"] == pytest.approx(2.0)


def test_segment_plan_play_once_ends_after_last_track():
    tracks = [_track("/m/a.mp3", duration=2.0), _track("/m/b.mp3", duration=3.0)]
    plan = build_music_segment_plan(tracks, "play_once", 12.0)
    assert [seg["path"] for seg in plan] == ["/m/a.mp3", "/m/b.mp3"]
    assert sum(seg["duration"] for seg in plan) == pytest.approx(5.0)


def test_segment_plan_per_track_loop_consumes_the_remainder():
    tracks = [
        _track("/m/a.mp3", duration=2.0),
        _track("/m/b.mp3", duration=3.0, mode="loop"),
        _track("/m/c.mp3", duration=2.0),
    ]
    plan = build_music_segment_plan(tracks, "loop_sequence", 14.0)
    # A plays once, then B loops until the target: C is never reached.
    assert plan[0]["path"] == "/m/a.mp3"
    assert all(seg["path"] == "/m/b.mp3" for seg in plan[1:])
    assert sum(seg["duration"] for seg in plan) == pytest.approx(14.0)


def test_segment_plan_repeat_n_then_continues():
    tracks = [
        _track("/m/a.mp3", duration=2.0, mode="repeat", repeat=3),
        _track("/m/b.mp3", duration=2.0),
    ]
    plan = build_music_segment_plan(tracks, "play_once", 100.0)
    assert [seg["path"] for seg in plan] == ["/m/a.mp3"] * 3 + ["/m/b.mp3"]
    assert sum(seg["duration"] for seg in plan) == pytest.approx(8.0)


def test_segment_plan_legacy_single_track_is_untouched():
    # The legacy one-track configuration never builds a segment plan here;
    # the renderer keeps the exact historical -stream_loop graph.
    from app.video_merger.command_builder import _music_sequence_plan

    settings = ExportSettings(
        music_track_plan=[{"path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0, "duration": 10.0}],
        music_sequence_mode="loop_sequence",
    )
    assert _music_sequence_plan(settings) == []


# ---------------------------------------------------------------------------
# Command builder graph selection (features 2 + 5)
# ---------------------------------------------------------------------------
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


def test_multitrack_legacy_keeps_phase27_graph(tmp_path):
    tracks = [
        {"path": str(tmp_path / "m1.mp3"), "trim_start": 0.0, "trim_duration": 0.0, "duration": 2.0},
        {"path": str(tmp_path / "m2.mp3"), "trim_start": 0.0, "trim_duration": 0.0, "duration": 2.0},
    ]
    built, _ = _main_build(tmp_path, music_track_plan=tracks, music_sequence_mode="loop_sequence")
    graph = built.filter_graph
    assert "mseg" not in graph  # no Phase-28 segment chain
    assert "concat" in graph


def test_per_track_loop_mode_builds_segment_graph(tmp_path):
    tracks = [
        _track(str(tmp_path / "m1.mp3"), duration=2.0),
        _track(str(tmp_path / "m2.mp3"), duration=2.0, mode="loop"),
    ]
    built, _ = _main_build(tmp_path, music_track_plan=tracks, music_sequence_mode="loop_sequence")
    graph = built.filter_graph
    # 3 s program: m1 plays once (2 s), then m2 loops into the remaining 1 s.
    assert "mseg0" in graph and "mseg1" in graph
    assert "concat=n=2:v=0:a=1[music_seq]" in graph


def test_play_once_sequence_mode_builds_segment_graph(tmp_path):
    tracks = [
        _track(str(tmp_path / "m1.mp3"), duration=2.0),
        _track(str(tmp_path / "m2.mp3"), duration=2.0),
    ]
    built, _ = _main_build(tmp_path, music_track_plan=tracks, music_sequence_mode="play_once")
    graph = built.filter_graph
    assert "mseg" in graph


# ---------------------------------------------------------------------------
# Render-cache discipline (feature 5): music never touches ASR/alignment
# ---------------------------------------------------------------------------
def test_stage1_payload_ignores_music_modes_when_music_inactive(tmp_path):
    media = [fake_media(str(tmp_path / "A.mp4"), duration=2)]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", program_duration=2,
        original_audio_mode="mute", normalize_audio=False,
        music_sequence_mode="play_once",
    )
    resolved = resolve_export(media, settings)
    payload = build_stage1_payload(media, settings, resolved)
    assert payload["settings"]["music_sequence_mode"] is None
    assert "music_tracks" not in payload


def test_stage1_payload_default_sequence_keeps_legacy_fingerprint(tmp_path):
    from app.video_merger.models import AudioAssetInfo

    media = [fake_media(str(tmp_path / "A.mp4"), duration=2)]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", program_duration=2,
        original_audio_mode="mute", normalize_audio=False,
    )
    resolved = resolve_export(media, settings)
    music = AudioAssetInfo(path=Path("/m/a.mp3"), duration=10.0, sample_rate=48000, channels=2)
    payload = build_stage1_payload(
        media, settings, resolved, music_asset=music,
        music_track_plan=[{"path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0, "duration": 10.0}],
    )
    # Unchanged single-track default: no new key, cache stays valid.
    assert "music_tracks" not in payload
    assert payload["settings"]["music_sequence_mode"] == "loop_sequence"


def test_stage1_payload_changes_only_with_actual_music_changes(tmp_path):
    from app.video_merger.models import AudioAssetInfo

    media = [fake_media(str(tmp_path / "A.mp4"), duration=2)]
    settings = ExportSettings(
        resolution="160x90", workflow_stage="main", program_duration=2,
        original_audio_mode="mute", normalize_audio=False,
    )
    resolved = resolve_export(media, settings)
    music = AudioAssetInfo(path=Path("/m/a.mp3"), duration=10.0, sample_rate=48000, channels=2)
    plan = [{"path": "/m/a.mp3", "trim_start": 0.0, "trim_duration": 0.0, "duration": 10.0}]

    base = build_stage1_payload(media, settings, resolved, music_asset=music, music_track_plan=plan)
    again = build_stage1_payload(media, settings, resolved, music_asset=music, music_track_plan=plan)
    assert base == again  # nothing changed => identical render identity

    looped = [dict(plan[0], playback_mode="loop")]
    changed = build_stage1_payload(media, settings, resolved, music_asset=music, music_track_plan=looped)
    assert changed != base
    assert changed["music_tracks"][0]["playback_mode"] == "loop"
    # "once" with repeat_count=1 IS the default and adds no identity.
    defaulted = [dict(plan[0], playback_mode="once", repeat_count=1)]
    assert build_stage1_payload(
        media, settings, resolved, music_asset=music, music_track_plan=defaulted
    ) == base

    # The sequence mode only reaches the payload while music is really mixed.
    from dataclasses import replace

    play_once = replace(settings, music_sequence_mode="play_once")
    payload_once = build_stage1_payload(
        media, play_once, resolved, music_asset=music, music_track_plan=plan
    )
    assert payload_once["settings"]["music_sequence_mode"] == "play_once"
    assert payload_once != base


# ---------------------------------------------------------------------------
# Shorts Duration After Merge up to 3.50x (feature 9)
# ---------------------------------------------------------------------------
def test_shorts_after_merge_resolution_none_follows_shared():
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=["/v/a.wav"],
        duration_after_merge=1.25,
        duration_after_merge_enabled=True,
        shorts_duration_after_merge=None,
    )
    job = build_short_jobs(settings)[0]
    short = short_settings(settings, job)
    assert short.duration_after_merge == pytest.approx(1.25)
    assert short.duration_after_merge_enabled is True
    # Long-Form never reads the Shorts override.
    long_job = long_form_settings(settings)
    assert long_job.duration_after_merge == pytest.approx(1.25)
    assert long_job.duration_after_merge_enabled is True


@pytest.mark.parametrize("value", [2.0, 2.5, 3.0, 3.5])
def test_shorts_after_merge_upper_range(value):
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=["/v/a.wav"],
        duration_after_merge=1.0,
        duration_after_merge_enabled=False,
        shorts_duration_after_merge=value,
    )
    job = build_short_jobs(settings)[0]
    short = short_settings(settings, job)
    assert short.duration_after_merge == pytest.approx(value)
    assert short.duration_after_merge_enabled is True
    assert duration_after_merge_value(short) == pytest.approx(value)
    # The shared Long-Form controls stay untouched by the Shorts override.
    assert settings.duration_after_merge == 1.0
    assert settings.duration_after_merge_enabled is False


def test_shorts_after_merge_one_disables_itself():
    settings = ExportSettings(
        export_mode=EXPORT_MODE_SHORTS,
        voiceover_paths=["/v/a.wav"],
        shorts_duration_after_merge=1.0,
    )
    short = short_settings(settings, build_short_jobs(settings)[0])
    assert short.duration_after_merge == pytest.approx(1.0)
    assert short.duration_after_merge_enabled is False


def test_combined_mode_shorts_sequence_mode_maps_onto_job():
    settings = ExportSettings(
        export_mode=EXPORT_MODE_COMBINED,
        voiceover_paths=["/v/a.wav"],
        short_music_sequence_mode="play_once",
        music_sequence_mode="loop_sequence",
    )
    short = short_settings(settings, build_short_jobs(settings)[0])
    assert normalize_sequence_mode(short.music_sequence_mode) == "play_once"
    long_job = long_form_settings(settings)
    assert normalize_sequence_mode(long_job.music_sequence_mode) == "loop_sequence"


# ---------------------------------------------------------------------------
# Dedicated Shorts video sources (feature 1)
# ---------------------------------------------------------------------------
class _FakeEngine:
    """analyze() without FFmpeg probing; records the requested paths."""

    def __init__(self):
        self.calls: list[list[Path]] = []

    def analyze(self, paths, log=lambda _m: None):
        paths = list(paths)
        self.calls.append(paths)
        return [fake_media(str(path), duration=4) for path in sorted(paths)]


def _make_shorts_engine(tmp_path):
    from app.video_merger.main_project import MainProjectEngine

    return MainProjectEngine(_FakeEngine())


def test_shorts_folder_pool_inactive_without_configuration(tmp_path):
    project = _make_shorts_engine(tmp_path)
    settings = ExportSettings(shorts_video_folders=[])
    messages: list[str] = []
    assert project._shorts_folder_pool(settings, None, None, messages.append) is None
    assert messages == []


def test_shorts_folder_pool_uses_only_dedicated_folders(tmp_path):
    project = _make_shorts_engine(tmp_path)
    shorts_folder = tmp_path / "ShortsOnly"
    shorts_folder.mkdir()
    (shorts_folder / "s1.mp4").write_bytes(b"x")
    (shorts_folder / "s2.mp4").write_bytes(b"x")
    long_folder = tmp_path / "LongOnly"
    long_folder.mkdir()
    (long_folder / "l1.mp4").write_bytes(b"x")

    settings = ExportSettings(shorts_video_folders=[str(shorts_folder)])
    messages: list[str] = []
    pool = project._shorts_folder_pool(settings, None, None, messages.append)
    assert pool is not None
    names = {Path(item.path).name for item in pool}
    assert names == {"s1.mp4", "s2.mp4"}
    assert not any("l1" in str(item.path) for item in pool)
    assert any("dedizierten Shorts-Ordner" in message for message in messages)


def test_shorts_folder_pool_skips_missing_and_fails_on_zero(tmp_path):
    project = _make_shorts_engine(tmp_path)
    present = tmp_path / "ok"
    present.mkdir()
    (present / "a.mp4").write_bytes(b"x")
    settings = ExportSettings(
        shorts_video_folders=[str(tmp_path / "gone"), str(present)]
    )
    messages: list[str] = []
    pool = project._shorts_folder_pool(settings, None, None, messages.append)
    assert pool is not None and len(pool) == 1
    assert any("fehlt" in message for message in messages)

    from app.video_merger.errors import VideoMergerError

    with pytest.raises(VideoMergerError):
        project._shorts_folder_pool(
            ExportSettings(shorts_video_folders=[str(tmp_path / "nope")]), None, None, lambda _m: None
        )
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(VideoMergerError):
        project._shorts_folder_pool(
            ExportSettings(shorts_video_folders=[str(empty)]), None, None, lambda _m: None
        )


def test_long_form_settings_never_read_shorts_folders(tmp_path):
    settings = ExportSettings(
        export_mode=EXPORT_MODE_COMBINED,
        voiceover_paths=["/v/a.wav"],
        shorts_video_folders=[str(tmp_path / "ShortsOnly")],
        shorts_duration_after_merge=3.5,
        short_music_sequence_mode="play_once",
    )
    long_job = long_form_settings(settings)
    # The Long-Form job keeps the shared values; the Shorts-only overrides are
    # resolved exclusively inside short_settings().
    assert long_job.duration_after_merge == pytest.approx(1.0)
    assert long_job.duration_after_merge_enabled is False
    assert normalize_sequence_mode(long_job.music_sequence_mode) == "loop_sequence"


# ---------------------------------------------------------------------------
# Shorts subtitle sync guard (feature 7): monotonic per-word boundaries
# ---------------------------------------------------------------------------
def test_per_word_ass_boundaries_stay_monotonic_under_compression(tmp_path):
    from app.video_merger.models import AlignmentResult, WordTiming
    from app.video_merger.subtitles import build_cues, write_ass

    # Words packed tighter than the ASS centisecond grid - the exact situation
    # that produced overlapping/equal timestamps before the Phase-28 guard.
    script = "a b c d e f g h"
    words: list[WordTiming] = []
    char_cursor = 0
    for index, token in enumerate(script.split()):
        start = index * 0.004
        position = script.index(token, char_cursor)
        char_cursor = position + len(token)
        words.append(WordTiming(
            text=token, start=start, end=start + 0.003,
            script_start=position, script_end=char_cursor,
        ))
    alignment = AlignmentResult(
        words=words, language="German",
        method="fixture word timestamps", compatibility=1.0, average_confidence=1.0,
    )
    cues = build_cues(
        script, alignment, "short_1",
        program_end=words[-1].end + 0.2,
        width=1080, height=1920, font_key="inter", font_size_percent=100,
    )
    path = tmp_path / "subs.ass"
    write_ass(script, cues, path, "short_1", "Bottom", 1080, 1920,
              animation="word_highlight", font_key="inter")
    content = path.read_text(encoding="utf-8")
    dialogue_times = []
    for line in content.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        start_raw, end_raw = fields[1].strip(), fields[2].strip()

        def _cs(raw: str) -> int:
            hours, minutes, rest = raw.split(":")
            seconds, centis = rest.split(".")
            return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 100 + int(centis)

        dialogue_times.append((_cs(start_raw), _cs(end_raw)))
    assert dialogue_times, "expected per-word Dialogue events"
    previous_end = -1
    for start, end in dialogue_times:
        assert end > start, f"degenerate event {start}-{end}"
        assert start >= previous_end, f"overlap at {start} < {previous_end}"
        previous_end = end


# ---------------------------------------------------------------------------
# GUI (features 1, 2, 6, 8, 9, 11)
# ---------------------------------------------------------------------------
@pytest.fixture()
def window(qtbot):
    from app.video_merger.gui.main_window import MainWindow

    main = MainWindow()
    qtbot.addWidget(main)
    return main


def test_gui_position_combos_offer_one_canonical_choice_per_spot(window):
    for combo in (window.subtitle_position_combo, window.short_subtitle_position_combo):
        assert [combo.itemText(i) for i in range(combo.count())] == list(SUBTITLE_POSITIONS)


def test_gui_shorts_folder_list_roundtrips(window, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem

    folder_a, folder_b = tmp_path / "SA", tmp_path / "SB"
    for folder in (folder_a, folder_b):
        item = QListWidgetItem(str(folder))
        item.setData(Qt.UserRole, str(folder))
        window.shorts_folders_list.addItem(item)
    assert window._configured_shorts_folders() == [str(folder_a), str(folder_b)]
    # Reorder, then remove.
    window.shorts_folders_list.setCurrentRow(0)
    window._move_shorts_folder(1)
    assert window._configured_shorts_folders() == [str(folder_b), str(folder_a)]
    window.shorts_folders_list.setCurrentRow(0)
    window._remove_shorts_folder()
    assert window._configured_shorts_folders() == [str(folder_a)]
    window._clear_shorts_folders()
    assert window._configured_shorts_folders() == []
    # The Long-Form list is untouched by all of this.
    assert window._configured_source_folders() == []


def test_gui_shorts_settings_flow_into_export_settings(window, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem

    folder = tmp_path / "Shorts"
    item = QListWidgetItem(str(folder))
    item.setData(Qt.UserRole, str(folder))
    window.shorts_folders_list.addItem(item)
    window.shorts_duration_after_merge_combo.setCurrentIndex(
        window.shorts_duration_after_merge_combo.findData(3.5)
    )
    settings = window._settings()
    assert settings.shorts_video_folders == [str(folder)]
    assert settings.shorts_duration_after_merge == pytest.approx(3.5)
    window.shorts_duration_after_merge_combo.setCurrentIndex(0)
    assert window._settings().shorts_duration_after_merge is None


def test_gui_music_track_mode_editing(window, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem

    def add(list_widget, name):
        path = tmp_path / name
        item = QListWidgetItem(name)
        item.setData(Qt.UserRole, str(path))
        list_widget.addItem(item)

    for name in ("a.mp3", "b.mp3"):
        add(window.music_tracks_list, name)
    window.music_tracks_list.setCurrentRow(1)
    window._set_music_track_mode(
        window.music_tracks_list, window.music_repeat_spin, window.music_track_mode_label, "loop"
    )
    entries = window._music_track_entries(window.music_tracks_list)
    assert [entry["playback_mode"] for entry in entries] == ["once", "loop"]
    assert "loops until the end" in window.music_tracks_list.item(1).text()

    window.music_repeat_spin.setValue(5)
    window._set_music_track_mode(
        window.music_tracks_list, window.music_repeat_spin, window.music_track_mode_label, "repeat"
    )
    entries = window._music_track_entries(window.music_tracks_list)
    assert entries[1]["playback_mode"] == "repeat"
    assert entries[1]["repeat_count"] == 5
    assert "×5" in window.music_tracks_list.item(1).text()

    # Settings carry the modes; the sequence combo is independent.
    window.music_sequence_mode_combo.setCurrentIndex(
        window.music_sequence_mode_combo.findData("play_once")
    )
    settings = window._settings()
    assert [track["playback_mode"] for track in settings.music_tracks] == ["once", "repeat"]
    assert settings.music_sequence_mode == "play_once"
    assert settings.short_music_sequence_mode == "loop_sequence"
    # The Shorts profile stayed untouched: zero cross-leak.
    assert settings.short_music_tracks == []


def test_gui_music_state_restores_modes(window, tmp_path):
    settings = ExportSettings(
        music_tracks=[
            {"path": str(tmp_path / "a.mp3"), "trim_start": 0.0, "trim_duration": 0.0,
             "playback_mode": "once", "repeat_count": 1},
            {"path": str(tmp_path / "b.mp3"), "trim_start": 0.0, "trim_duration": 0.0,
             "playback_mode": "loop", "repeat_count": 1},
        ],
        music_sequence_mode="play_once",
    )
    window._populate_music_tracks(window.music_tracks_list, settings.music_tracks, "")
    index = window.music_sequence_mode_combo.findData("play_once")
    window.music_sequence_mode_combo.setCurrentIndex(index)
    assert window.music_sequence_mode_combo.currentData() == "play_once"
    entries = window._music_track_entries(window.music_tracks_list)
    assert [entry["playback_mode"] for entry in entries] == ["once", "loop"]


def test_gui_large_shorts_preview_uses_only_short_settings(window, monkeypatch):
    captured: dict = {}

    def fake_open(self, profile):
        captured["profile"] = profile

    monkeypatch.setattr(type(window), "_open_large_subtitle_preview", fake_open)
    window.short_subtitle_preview_button.click()
    assert captured["profile"] == "short"
    window.subtitle_preview_button.click()
    assert captured["profile"] == "long"


def test_gui_large_shorts_preview_dialog_builds_from_short_controls(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    window.short_subtitle_style_combo.setCurrentIndex(0)
    window.short_subtitle_position_combo.setCurrentText("Top")
    window.short_subtitle_font_size_spin.setValue(150)
    window.radio_16.setChecked(True)  # Long-Form aspect must NOT leak in

    opened: dict = {}

    def fake_exec(self):
        opened["title"] = self.windowTitle()
        opened["size"] = (self.width(), self.height())
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window._open_large_subtitle_preview("short")
    assert opened["title"] == "YouTube Shorts Subtitle Preview"
    window._open_large_subtitle_preview("long")
    assert opened["title"] == "Subtitle Style Preview"
