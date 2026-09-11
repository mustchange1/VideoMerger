"""Feature-parity acceptance tests: the product is a superset of the last
known-good state (``afc8019``, Phase 26) plus the Phase-27 additions.

The focus is on the two areas that had known regressions after an incomplete
merge base:

* Shorts keep their OWN music configuration (single track and multi-track
  sequence); changing the Long-Form music never changes the Shorts music and
  vice versa, and the whole sequence loops as a unit per output profile.
* Shorts keep their own transition type and duration; changing one profile's
  transition never changes the other's, every historical transition option
  stays available, and each profile resolves its own values.

Both profiles keep their exact historical defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.video_merger import youtube_outputs
from app.video_merger.models import (
    ExportSettings,
    LONG_FORM_MUSIC_VOLUME,
    SHORTS_MUSIC_VOLUME,
    SHORTS_TRANSITION_DURATION,
    LONG_FORM_TRANSITION_DURATION,
    TRANSITION_DURATION_LEGACY_DEFAULT,
)
from app.video_merger.music_tracks import (
    effective_music_tracks,
    effective_short_music_tracks,
)
from app.video_merger.settings_store import SettingsStore
from app.video_merger.subtitles import DEFAULT_LONG_ANIMATION, DEFAULT_SHORT_ANIMATION


@pytest.fixture
def window():
    # The autouse ``_isolated_settings_store`` fixture already redirects the
    # store into the test's tmp dir, so a plain MainWindow() is isolated.
    from PySide6.QtWidgets import QApplication

    from app.video_merger.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    main = MainWindow()
    try:
        yield main
    finally:
        main.close()
        app.processEvents()


def _track(tmp_path: Path, rel: str, seconds: float) -> dict:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"id3")
    return {"path": str(path), "trim_start": 0.0, "trim_duration": 0.0, "duration": seconds}


def _export_settings(tmp_path: Path, **overrides) -> ExportSettings:
    voiceover = tmp_path / "voiceover_01.wav"
    voiceover.parent.mkdir(parents=True, exist_ok=True)
    voiceover.write_bytes(b"RIFF")
    base = dict(
        voiceover_paths=[str(voiceover)],
        voiceover_path=str(voiceover),
        subtitle_enabled=False,
    )
    base.update(overrides)
    return ExportSettings(**base)


# ---------------------------------------------------------------------------
# Defaults: every historical default stays exactly where it was
# ---------------------------------------------------------------------------


def test_historical_defaults_are_unchanged():
    settings = ExportSettings()

    # Shared canonical transition fields keep the legacy defaults; the new
    # per-output defaults (2.0 s) are applied only at resolution time.
    assert settings.transition_type == "cross_dissolve"
    assert settings.transition_duration == TRANSITION_DURATION_LEGACY_DEFAULT == 1.0
    assert settings.long_form_transition_type == ""
    assert settings.shorts_transition_type == ""
    assert settings.long_form_transition_duration is None
    assert settings.shorts_transition_duration is None

    # Music volume: shared 44 % plus unconfigured per-output fields.
    assert settings.music_volume == LONG_FORM_MUSIC_VOLUME == 44
    assert settings.long_form_music_volume is None
    assert settings.shorts_music_volume is None
    assert settings.ducking_enabled is True

    # Subtitle defaults (Long-Form and Shorts collections, Phase 22-24 values).
    assert settings.subtitle_animation == DEFAULT_LONG_ANIMATION == "static_phrase"
    assert settings.short_subtitle_animation == DEFAULT_SHORT_ANIMATION == "phrase_focus"
    assert settings.subtitle_language == "German"
    assert settings.subtitle_debug_overlay is False
    assert settings.subtitle_font_size == 100
    assert settings.short_subtitle_font_size == 100

    # Duration Before Merge is a playback-rate multiplier, never a time value.
    assert settings.duration_before_merge == 0.70
    assert settings.duration_before_merge_shorts == 0.70

    # Music alignment gate stays fail-closed.
    assert settings.allow_alignment_warnings is False

    # No settings file means no tracks configured at all.
    assert settings.music_tracks == []
    assert settings.short_music_tracks == []
    assert settings.music_path == ""
    assert settings.short_music_path == ""


def test_resolution_applies_per_output_defaults_independently():
    settings = ExportSettings()

    long = youtube_outputs.long_form_settings(settings)
    assert long.transition_type == "cross_dissolve"
    assert long.transition_duration == LONG_FORM_TRANSITION_DURATION == 2.0
    assert long.music_volume == LONG_FORM_MUSIC_VOLUME

    job = youtube_outputs.build_short_jobs(_export_settings(Path("/tmp/nonexistent-audio")))[0]
    short = youtube_outputs.short_settings(settings, job)
    assert short.transition_type == "cross_dissolve"
    assert short.transition_duration == SHORTS_TRANSITION_DURATION == 2.0
    assert short.music_volume == SHORTS_MUSIC_VOLUME
    assert short.aspect == "9:16"
    assert long.aspect == "16:9"


# ---------------------------------------------------------------------------
# Long-Form and Shorts music sequences are strictly independent
# ---------------------------------------------------------------------------


def test_short_settings_uses_only_the_shorts_music_sequence(tmp_path):
    short_a = _track(tmp_path, "short/a.mp3", 4.0)
    short_b = _track(tmp_path, "short/b.mp3", 6.0)
    long_a = _track(tmp_path, "long/a.mp3", 5.0)
    settings = _export_settings(
        tmp_path,
        music_tracks=[dict(long_a)],
        music_path=long_a["path"],
        short_music_tracks=[dict(short_a), dict(short_b)],
        short_music_path=short_a["path"],
    )

    job = youtube_outputs.build_short_jobs(settings)[0]
    short = youtube_outputs.short_settings(settings, job)

    # Both the legacy single field and the sequence come from ONE resolution,
    # so they can never disagree — and the Long-Form track is gone.
    assert short.music_path == short_a["path"]
    assert [t["path"] for t in short.music_tracks] == [short_a["path"], short_b["path"]]

    # The Long-Form job keeps only its own sequence.
    long = youtube_outputs.long_form_settings(settings)
    assert [t["path"] for t in effective_music_tracks(long)] == [long_a["path"]]


def test_changing_one_profile_never_touches_the_other(tmp_path):
    short_x = _track(tmp_path, "short/x.mp3", 2.0)
    long_a = _track(tmp_path, "long/a.mp3", 5.0)

    settings = _export_settings(
        tmp_path,
        music_tracks=[dict(long_a)],
        music_path=long_a["path"],
        short_music_tracks=[dict(short_x)],
        short_music_path=short_x["path"],
    )
    # Emulate the GUI: removing the Shorts track clears only the Shorts side.
    settings.short_music_tracks = []
    settings.short_music_path = ""
    assert [t["path"] for t in effective_short_music_tracks(settings)] == []
    assert [t["path"] for t in effective_music_tracks(settings)] == [long_a["path"]]

    # Emulate the GUI: removing the Long-Form track clears only that side.
    settings.music_tracks = []
    settings.music_path = ""
    assert effective_music_tracks(settings) == []
    settings.short_music_tracks = [dict(short_x)]
    assert [t["path"] for t in effective_short_music_tracks(settings)] == [short_x["path"]]


def test_shorts_music_resolution_order_override_sequence_legacy(tmp_path):
    """Per-Short override → multi-track sequence → legacy single path → none."""
    anchor = tmp_path / "voiceover_01.wav"
    anchor.write_bytes(b"RIFF")
    override = _track(tmp_path, "override.mp3", 2.0)
    seq_a = _track(tmp_path, "seq_a.mp3", 3.0)
    seq_b = _track(tmp_path, "seq_b.mp3", 4.0)

    settings = _export_settings(
        tmp_path,
        short_music_tracks=[dict(seq_a), dict(seq_b)],
        short_music_path=seq_a["path"],
        short_music_overrides={str(anchor): override["path"]},
    )

    # Anchor with an explicit per-Short override → only that track is used.
    resolved = effective_short_music_tracks(settings, str(anchor))
    assert [t["path"] for t in resolved] == [override["path"]]
    # Any other anchor (or none) plays the full ordered sequence.
    resolved = effective_short_music_tracks(settings, str(tmp_path / "other.wav"))
    assert [t["path"] for t in resolved] == [seq_a["path"], seq_b["path"]]
    assert [t["path"] for t in effective_short_music_tracks(settings)] == [
        seq_a["path"],
        seq_b["path"],
    ]

    # Without the override the sequence wins over the legacy single path…
    settings.short_music_overrides = {}
    assert [t["path"] for t in effective_short_music_tracks(settings)] == [
        seq_a["path"],
        seq_b["path"],
    ]
    # …and without a sequence the legacy single path migrates unchanged.
    settings.short_music_tracks = []
    resolved = effective_short_music_tracks(settings)
    assert len(resolved) == 1
    assert resolved[0]["path"] == seq_a["path"]
    assert resolved[0]["trim_start"] == 0.0
    assert resolved[0]["trim_duration"] == 0.0

    # Nothing configured: a Short stays silent and never inherits Long-Form.
    settings.short_music_path = ""
    settings.music_tracks = [dict(seq_b)]
    assert effective_short_music_tracks(settings) == []


def test_legacy_short_music_path_still_renders_without_a_sequence(tmp_path):
    legacy = _track(tmp_path, "legacy.mp3", 3.0)
    settings = _export_settings(tmp_path, short_music_path=legacy["path"])

    job = youtube_outputs.build_short_jobs(settings)[0]
    short = youtube_outputs.short_settings(settings, job)
    assert short.music_path == legacy["path"]
    assert [t["path"] for t in short.music_tracks] == [legacy["path"]]


# ---------------------------------------------------------------------------
# Shorts transition independence (type AND duration), all options preserved
# ---------------------------------------------------------------------------


def test_short_and_long_transition_profiles_are_independent(tmp_path):
    settings = _export_settings(
        tmp_path,
        transition_type="cross_dissolve",
        transition_duration=TRANSITION_DURATION_LEGACY_DEFAULT,
        long_form_transition_type="film_dissolve",
        long_form_transition_duration=3.5,
        shorts_transition_type="additive_dissolve",
        shorts_transition_duration=1.2,
    )

    long = youtube_outputs.long_form_settings(settings)
    assert long.transition_type == "film_dissolve"
    assert long.transition_duration == 3.5

    job = youtube_outputs.build_short_jobs(settings)[0]
    short = youtube_outputs.short_settings(settings, job)
    assert short.transition_type == "additive_dissolve"
    assert short.transition_duration == 1.2

    # Changing the Shorts profile leaves the Long-Form values untouched…
    settings.shorts_transition_type = "smooth_blur"
    settings.shorts_transition_duration = 0.8
    long2 = youtube_outputs.long_form_settings(settings)
    assert long2.transition_type == "film_dissolve"
    assert long2.transition_duration == 3.5
    short2 = youtube_outputs.short_settings(settings, youtube_outputs.build_short_jobs(settings)[0])
    assert short2.transition_type == "smooth_blur"
    assert short2.transition_duration == 0.8

    # …and vice versa.
    settings.long_form_transition_type = "additive_dissolve"
    settings.long_form_transition_duration = 2.4
    long3 = youtube_outputs.long_form_settings(settings)
    assert long3.transition_type == "additive_dissolve"
    assert long3.transition_duration == 2.4
    short3 = youtube_outputs.short_settings(settings, youtube_outputs.build_short_jobs(settings)[0])
    assert short3.transition_type == "smooth_blur"
    assert short3.transition_duration == 0.8


def test_unconfigured_profiles_fall_back_to_shared_then_default():
    settings = ExportSettings()
    # Legacy project: only the shared transition was ever saved. A shared
    # duration equal to the historical marker (1.0) is treated as
    # "never configured" and receives the new per-output default.
    long = youtube_outputs.long_form_settings(settings)
    assert long.transition_type == "cross_dissolve"
    assert long.transition_duration == LONG_FORM_TRANSITION_DURATION

    # A non-default shared duration migrates into both outputs.
    settings.transition_duration = 1.4
    settings.transition_type = "film_dissolve"
    long2 = youtube_outputs.long_form_settings(settings)
    assert long2.transition_type == "film_dissolve"
    assert long2.transition_duration == 1.4


def test_every_historical_transition_option_stays_available():
    from app.video_merger.transition_effects import TRANSITION_OPTIONS

    keys = [key for key, _label, _description in TRANSITION_OPTIONS]
    assert keys == [
        "smooth_blur",
        "cross_dissolve",
        "film_dissolve",
        "additive_dissolve",
    ]


# ---------------------------------------------------------------------------
# Persistence & migration
# ---------------------------------------------------------------------------


def test_settings_store_roundtrip_keeps_both_music_profiles(tmp_path):
    long_a = _track(tmp_path, "long/a.mp3", 5.0)
    short_x = _track(tmp_path, "short/x.mp3", 2.0)

    settings = _export_settings(
        tmp_path,
        music_tracks=[dict(long_a)],
        music_path=long_a["path"],
        short_music_tracks=[dict(short_x)],
        short_music_path=short_x["path"],
        shorts_transition_type="film_dissolve",
        shorts_transition_duration=1.1,
        long_form_transition_type="smooth_blur_dissolve",
        long_form_transition_duration=2.2,
        shorts_music_volume=30,
        long_form_music_volume=50,
    )

    path = tmp_path / "settings.json"
    SettingsStore(path).save(settings)
    loaded = SettingsStore(path).load()

    assert [t["path"] for t in loaded.music_tracks] == [long_a["path"]]
    assert loaded.music_path == long_a["path"]
    assert [t["path"] for t in loaded.short_music_tracks] == [short_x["path"]]
    assert loaded.short_music_path == short_x["path"]
    assert loaded.shorts_transition_type == "film_dissolve"
    assert loaded.shorts_transition_duration == 1.1
    assert loaded.long_form_transition_type == "smooth_blur_dissolve"
    assert loaded.long_form_transition_duration == 2.2
    assert loaded.shorts_music_volume == 30
    assert loaded.long_form_music_volume == 50


def test_legacy_settings_file_migrates_without_losing_music(tmp_path):
    """A settings file from before the multi-track feature loads unchanged:
    legacy single music files migrate to one-track sequences at resolution
    time, and shared volume/transition values are copied into both outputs."""
    legacy_long = _track(tmp_path, "long_legacy.mp3", 5.0)
    legacy_short = _track(tmp_path, "short_legacy.mp3", 3.0)
    path = tmp_path / "settings.json"
    path.write_text(
        """{
  "music_path": "%s",
  "short_music_path": "%s",
  "music_volume": 37,
  "transition_type": "film_dissolve",
  "transition_duration": 1.4,
  "version": 1
}"""
        % (legacy_long["path"], legacy_short["path"]),
        encoding="utf-8",
    )

    loaded = SettingsStore(path).load()
    # Legacy single tracks become one-track sequences without trimming.
    assert [t["path"] for t in effective_music_tracks(loaded)] == [legacy_long["path"]]
    assert [t["path"] for t in effective_short_music_tracks(loaded)] == [legacy_short["path"]]
    # Shared volume and transition migrate into BOTH output profiles.
    assert loaded.long_form_music_volume == 37
    assert loaded.shorts_music_volume == 37
    assert loaded.long_form_transition_type == "film_dissolve"
    assert loaded.shorts_transition_type == "film_dissolve"
    assert loaded.long_form_transition_duration == 1.4
    assert loaded.shorts_transition_duration == 1.4


def test_gui_restores_both_music_profiles_from_saved_settings(window, tmp_path):
    """A fresh window over an existing settings file restores BOTH sequences;
    applying the restored state never crosses the profiles."""
    from app.video_merger.gui.main_window import MainWindow

    long_a = _track(tmp_path, "long/a.mp3", 5.0)
    short_x = _track(tmp_path, "short/x.mp3", 2.0)
    short_y = _track(tmp_path, "short/y.mp3", 3.0)

    saved = _export_settings(
        tmp_path,
        music_tracks=[dict(long_a)],
        music_path=long_a["path"],
        short_music_tracks=[dict(short_x), dict(short_y)],
        short_music_path=short_x["path"],
    )
    SettingsStore(window.store.path).save(saved)

    restored = MainWindow()
    try:
        assert restored._music_track_paths(restored.music_tracks_list) == [long_a["path"]]
        assert restored._music_track_paths(restored.short_music_tracks_list) == [
            short_x["path"],
            short_y["path"],
        ]
        assert restored.music_edit.text() == long_a["path"]
        assert restored.short_music_edit.text() == short_x["path"]

        settings = restored._settings()
        assert [t["path"] for t in settings.music_tracks] == [long_a["path"]]
        assert [t["path"] for t in settings.short_music_tracks] == [
            short_x["path"],
            short_y["path"],
        ]
    finally:
        restored.close()
