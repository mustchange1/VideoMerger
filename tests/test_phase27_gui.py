"""Phase 27 – GUI regression tests (offscreen).

Covers the visually separated Long-Form / Shorts subtitle groups, the two
independent live previews, the per-profile Duration Before Merge controls,
the multi-track music sequence UI and the debug-overlay default.
"""
from __future__ import annotations

import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from app.video_merger.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture()
def window():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    main = MainWindow()
    yield main
    main.close()
    app.processEvents()


# --------------------------------------------------------------------------- #
# Separate subtitle groups + independent previews
# --------------------------------------------------------------------------- #
def test_long_and_short_groups_exist_with_own_previews(window):
    assert window.subtitle_live_preview is not None
    assert window.short_subtitle_live_preview is not None
    long_layout = window.subtitle_live_preview.current_layout()
    short_layout = window.short_subtitle_live_preview.current_layout()
    assert long_layout is not None and short_layout is not None
    # Correct 16:9 vs 9:16 renderer geometry per profile.
    assert (long_layout.width, long_layout.height) == (1920, 1080)
    assert (short_layout.width, short_layout.height) == (1080, 1920)


def test_long_controls_do_not_move_the_shorts_preview(window):
    short_before = window.short_subtitle_live_preview.current_layout()
    window.subtitle_position_combo.setCurrentText("Top")
    window.subtitle_font_size_spin.setValue(180)
    window.subtitle_style_combo.setCurrentIndex(
        window.subtitle_style_combo.findData("long_3")
    )
    long_after = window.subtitle_live_preview.current_layout()
    short_after = window.short_subtitle_live_preview.current_layout()
    # Long preview reflects every change …
    assert long_after.preset_key == "long_3"
    assert long_after.font_size > short_before.font_size or long_after.font_size != _default_long_size(window)
    # … while the Shorts preview stays byte-identical.
    assert short_after.font_size == short_before.font_size
    assert short_after.margin_v == short_before.margin_v
    assert short_after.alignment == short_before.alignment
    assert short_after.preset_key == short_before.preset_key


def _default_long_size(window):
    from app.video_merger.subtitle_presets import get_preset
    from app.video_merger.subtitles import _font_size
    return _font_size(1920, 1080, get_preset("long_1"), 100)


def test_short_controls_do_not_move_the_long_preview(window):
    long_before = window.subtitle_live_preview.current_layout()
    window.short_subtitle_position_combo.setCurrentText("Top")
    window.short_subtitle_font_size_spin.setValue(60)
    window.short_subtitle_style_combo.setCurrentIndex(
        window.short_subtitle_style_combo.findData("short_3")
    )
    long_after = window.subtitle_live_preview.current_layout()
    short_after = window.short_subtitle_live_preview.current_layout()
    assert short_after.preset_key == "short_3"
    assert (long_after.font_size, long_after.margin_v, long_after.alignment,
            long_after.preset_key) == (
        long_before.font_size, long_before.margin_v, long_before.alignment,
        long_before.preset_key,
    )


def test_position_top_moves_caption_visually(window):
    window.subtitle_position_combo.setCurrentText("Bottom")
    bottom = window.subtitle_live_preview.current_layout()
    window.subtitle_position_combo.setCurrentText("Top")
    top = window.subtitle_live_preview.current_layout()
    assert top.alignment != bottom.alignment or top.margin_v != bottom.margin_v


def test_shorts_position_uses_vertical_geometry(window):
    window.short_subtitle_position_combo.setCurrentText("Top")
    top = window.short_subtitle_live_preview.current_layout()
    window.short_subtitle_position_combo.setCurrentText("Bottom")
    bottom = window.short_subtitle_live_preview.current_layout()
    assert (top.width, top.height) == (1080, 1920)
    assert top.alignment != bottom.alignment or top.margin_v != bottom.margin_v


def test_font_size_spin_live_changes_wrapping_geometry(window):
    window.subtitle_font_size_spin.setValue(100)
    base = window.subtitle_live_preview.current_layout()
    window.subtitle_font_size_spin.setValue(200)
    big = window.subtitle_live_preview.current_layout()
    assert big.font_size == pytest.approx(base.font_size * 2, rel=0.02)


def test_preview_renderer_matches_production_renderer(window):
    """ONE config path drives preview and production (no fake preview)."""
    from app.video_merger.subtitle_presets import get_preset
    from app.video_merger.subtitle_preview import preview_cue
    from app.video_merger.subtitles import _font_size, _position

    window.subtitle_font_size_spin.setValue(135)
    layout = window.subtitle_live_preview.current_layout()
    preset = get_preset(str(window.subtitle_style_combo.currentData()))
    assert layout.font_size == _font_size(1920, 1080, preset, 135)
    alignment, margin_v = _position(
        window.subtitle_position_combo.currentText(), 1920, 1080, preset.collection
    )
    assert (layout.alignment, layout.margin_v) == (alignment, margin_v)
    # And the shared routine is the same one the canvas uses directly.
    direct = preview_cue(
        "Ein Beispieltext für die Vorschau.",
        str(window.subtitle_font_combo.currentData()),
        str(window.subtitle_style_combo.currentData()),
        window.subtitle_position_combo.currentText(),
        1920, 1080, font_size_percent=135,
    )
    assert direct.font_size == layout.font_size


def test_include_image_toggle_controls_preview_background(window, tmp_path):
    image = tmp_path / "bg.png"
    from PySide6.QtGui import QImage
    qimage = QImage(64, 36, QImage.Format_ARGB32)
    qimage.fill(0xFF336699)
    assert qimage.save(str(image))

    window.image_check.setChecked(True)
    window.image_path_edit.setText(str(image))
    assert window.subtitle_live_preview.has_background_image() is False

    window.subtitle_preview_image_check.setChecked(True)
    assert window.subtitle_live_preview.has_background_image() is True
    assert window.short_subtitle_live_preview.has_background_image() is True

    window.subtitle_preview_image_check.setChecked(False)
    assert window.subtitle_live_preview.has_background_image() is False


def test_debug_overlay_is_off_by_default_and_persists_user_choice(window, tmp_path):
    assert window.subtitle_debug_check.isChecked() is False
    settings = window._settings()
    assert settings.subtitle_debug_overlay is False
    window.subtitle_debug_check.setChecked(True)
    assert window._settings().subtitle_debug_overlay is True


# --------------------------------------------------------------------------- #
# Per-profile Duration Before Merge controls
# --------------------------------------------------------------------------- #
def test_duration_before_merge_has_independent_long_and_short_controls(window):
    assert window.duration_before_merge_combo.currentData() == pytest.approx(0.70)
    assert window.duration_before_merge_shorts_combo.currentData() == pytest.approx(0.70)
    window.duration_before_merge_shorts_combo.setCurrentIndex(
        window.duration_before_merge_shorts_combo.findData(1.25)
    )
    settings = window._settings()
    assert settings.duration_before_merge == pytest.approx(0.70)
    assert settings.duration_before_merge_shorts == pytest.approx(1.25)


# --------------------------------------------------------------------------- #
# Multi-track music sequence UI
# --------------------------------------------------------------------------- #
def test_music_track_list_add_remove_and_order(window, tmp_path):
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    c = tmp_path / "c.mp3"
    for path in (a, b, c):
        path.write_bytes(b"id3")

    from PySide6.QtWidgets import QListWidgetItem
    from PySide6.QtCore import Qt as _Qt

    def add(path):
        item = QListWidgetItem(path.name)
        item.setData(_Qt.UserRole, str(path))
        window.music_tracks_list.addItem(item)

    add(a)
    add(b)
    add(c)
    assert window._music_track_paths(window.music_tracks_list) == [str(a), str(b), str(c)]

    # Reorder: move C up one step → a, c, b.
    window.music_tracks_list.setCurrentRow(2)
    window._move_music_track(window.music_tracks_list, -1)
    assert window._music_track_paths(window.music_tracks_list) == [str(a), str(c), str(b)]

    settings = window._settings()
    assert [track["path"] for track in settings.music_tracks] == [str(a), str(c), str(b)]
    assert settings.music_path == str(a)  # legacy field mirrors the first track

    # Remove the middle entry.
    window.music_tracks_list.setCurrentRow(1)
    window._remove_music_track(window.music_tracks_list)
    assert window._music_track_paths(window.music_tracks_list) == [str(a), str(b)]


def test_shorts_music_sequence_is_independent_from_long_form(window, tmp_path):
    """Changing one profile's music sequence never changes the other's."""
    from PySide6.QtWidgets import QListWidgetItem
    from PySide6.QtCore import Qt as _Qt

    def add(widget, path):
        item = QListWidgetItem(path.name)
        item.setData(_Qt.UserRole, str(path))
        widget.addItem(item)

    long_a = tmp_path / "long_a.mp3"
    short_x = tmp_path / "short_x.mp3"
    short_y = tmp_path / "short_y.mp3"
    for path in (long_a, short_x, short_y):
        path.write_bytes(b"id3")

    add(window.music_tracks_list, long_a)
    add(window.short_music_tracks_list, short_x)
    add(window.short_music_tracks_list, short_y)

    settings = window._settings()
    assert [t["path"] for t in settings.music_tracks] == [str(long_a)]
    assert [t["path"] for t in settings.short_music_tracks] == [str(short_x), str(short_y)]
    assert settings.music_path == str(long_a)
    assert settings.short_music_path == str(short_x)

    # Editing the Shorts sequence leaves the Long-Form sequence untouched …
    window.short_music_tracks_list.setCurrentRow(0)
    window._remove_music_track(window.short_music_tracks_list)
    settings = window._settings()
    assert [t["path"] for t in settings.music_tracks] == [str(long_a)]
    assert [t["path"] for t in settings.short_music_tracks] == [str(short_y)]
    # … and vice versa.
    window.music_tracks_list.setCurrentRow(0)
    window._remove_music_track(window.music_tracks_list)
    settings = window._settings()
    assert settings.music_tracks == []
    assert settings.music_path == ""
    assert [t["path"] for t in settings.short_music_tracks] == [str(short_y)]
    assert settings.short_music_path == str(short_y)


def test_music_tracks_persist_through_settings_roundtrip(window, tmp_path):
    track = tmp_path / "song.mp3"
    short_track = tmp_path / "short_song.mp3"
    for path in (track, short_track):
        path.write_bytes(b"id3")
    from PySide6.QtWidgets import QListWidgetItem
    from PySide6.QtCore import Qt as _Qt

    def add(widget, path):
        item = QListWidgetItem(path.name)
        item.setData(_Qt.UserRole, str(path))
        widget.addItem(item)

    add(window.music_tracks_list, track)
    add(window.short_music_tracks_list, short_track)
    window._sync_music_state()

    from app.video_merger.settings_store import SettingsStore

    store = SettingsStore(window.store.path)
    loaded = store.load()
    assert [t["path"] for t in loaded.music_tracks] == [str(track)]
    assert loaded.music_path == str(track)
    assert [t["path"] for t in loaded.short_music_tracks] == [str(short_track)]
    assert loaded.short_music_path == str(short_track)


def test_alignment_warning_help_text_is_explicit(window):
    tooltip = window.alignment_warning_check.toolTip()
    assert "fail-closed" in tooltip.casefold()
    assert "safety override" in tooltip.casefold()


def test_debug_overlay_tooltip_warns_about_diagnostic_only(window):
    tooltip = window.subtitle_debug_check.toolTip()
    assert "diagnostic" in tooltip.casefold()
