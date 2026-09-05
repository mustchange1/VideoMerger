from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from app.video_merger.gui.main_window import MainWindow
from app.video_merger.models import (
    LONG_FORM_INTRO_SECONDS,
    LONG_FORM_MUSIC_VOLUME,
    LONG_FORM_OUTRO_SECONDS,
    LONG_FORM_TRANSITION_DURATION,
    SHORT_INTRO_SECONDS,
    SHORT_OUTRO_SECONDS,
    SHORTS_MUSIC_VOLUME,
    SHORTS_TRANSITION_DURATION,
    ExportSettings,
)
from app.video_merger.project_order import ProjectOrderStore
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export
from tests.conftest import fake_media


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_gui_exposes_exact_transition_and_advanced_easing_choices(qt_app):
    window = MainWindow()
    try:
        assert [window.transition_combo.itemText(i) for i in range(window.transition_combo.count())] == [
            "Smooth Blur Crossfade", "Cross Dissolve", "Film Dissolve", "Additive Dissolve"
        ]
        assert [window.ease_combo.itemText(i) for i in range(window.ease_combo.count())] == [
            "Linear", "Ease In", "Ease Out", "Ease In + Ease Out"
        ]
        window.transition_combo.setCurrentIndex(1)
        assert window._settings().transition_type == "cross_dissolve"
        assert "professioneller" in window.transition_description.text()
        window.ease_combo.setCurrentIndex(0)
        assert window._settings().transition_ease == "linear"
    finally:
        window.close()


def test_gui_voiceover_pause_control_constructs_and_updates_settings(qt_app, tmp_path):
    window = MainWindow()
    try:
        # Construction itself is the regression guard for the connected slot.
        assert window.voiceover_pause_combo.currentData() == pytest.approx(0.7)
        assert window.voiceover_pause_spin.value() == pytest.approx(0.7)
        assert window._settings().voiceover_pause == pytest.approx(0.7)
        assert window.voiceover_pause_spin.isEnabled() is False

        window.voiceover_pause_combo.setCurrentIndex(
            window.voiceover_pause_combo.findData(1.5)
        )
        assert window.voiceover_pause_spin.value() == pytest.approx(1.5)
        assert window._settings().voiceover_pause == pytest.approx(1.5)
        assert window.voiceover_pause_spin.isEnabled() is False

        window.voiceover_pause_combo.setCurrentIndex(
            window.voiceover_pause_combo.findData(-1.0)
        )
        window.voiceover_pause_spin.setValue(1.35)
        assert window._settings().voiceover_pause == pytest.approx(1.35)
        assert window.voiceover_pause_spin.isEnabled() is True

        # The normal project save/load path keeps the selected custom value.
        window.store = SettingsStore(tmp_path / "settings.json")
        window._save_project()
        assert window.store.load().voiceover_pause == pytest.approx(1.35)
    finally:
        window.close()


def test_gui_12_workflow_controls_defaults_and_auto_style_collection(qt_app):
    window = MainWindow()
    try:
        assert window.main_button.text() == "CREATE MAIN VIDEO"
        assert window.final_button.text() == "CREATE FINAL VIDEO"
        assert window.complete_button.text() == "CREATE FINAL VIDEO – ONE CLICK"
        assert window.subtitle_style_combo.count() == 10
        assert window.subtitle_animation_combo.count() == 5
        # 1.2.4: 7 Bundled/Verfügbare Fonts (3 alt + 4 neue Long-Form-Fonts).
        assert window.subtitle_font_combo.count() == 7
        # 1.2.4: Intro-Original-Audio default "original" (vor 1.2.4 "mute").
        assert window.original_audio_combo.currentData() == "original"
        assert window.outro_audio_combo.currentData() == "original"
        assert window.transition_combo.currentData() == "cross_dissolve"
        # Independent per-output transitions: a new project shows Cross Dissolve
        # / 2.00 s for BOTH outputs, and each control writes only its own value.
        assert window.transition_spin.value() == LONG_FORM_TRANSITION_DURATION == 2.0
        assert window.short_transition_combo.currentData() == "cross_dissolve"
        assert window.short_transition_spin.value() == SHORTS_TRANSITION_DURATION == 2.0
        collected = window._settings()
        assert collected.long_form_transition_type == "cross_dissolve"
        assert collected.long_form_transition_duration == 2.0
        assert collected.shorts_transition_type == "cross_dissolve"
        assert collected.shorts_transition_duration == 2.0
        window.short_transition_spin.setValue(1.0)
        window.short_transition_combo.setCurrentIndex(
            max(0, window.short_transition_combo.findData("film_dissolve"))
        )
        assert window.transition_spin.value() == 2.0
        assert window.transition_combo.currentData() == "cross_dissolve"
        assert window._settings().long_form_transition_duration == 2.0
        assert window._settings().shorts_transition_duration == 1.0
        assert window._settings().shorts_transition_type == "film_dissolve"
        window.short_transition_spin.setValue(2.0)
        window.short_transition_combo.setCurrentIndex(
            max(0, window.short_transition_combo.findData("cross_dissolve"))
        )
        # The Long-Form outro IS the Main Video end padding: one control, one
        # canonical value, and a new project uses the new 1.5 s default.
        assert window.end_padding_spin.value() == LONG_FORM_OUTRO_SECONDS == 1.5
        assert window._settings().final_pause == 1.5
        assert window._settings().long_form_outro_seconds == 1.5
        assert window.long_intro_spin.value() == LONG_FORM_INTRO_SECONDS == 1.5
        assert window.short_intro_spin.value() == SHORT_INTRO_SECONDS == 0.7
        assert window.short_outro_spin.value() == SHORT_OUTRO_SECONDS == 0.7
        # Independent per-output music volumes (44 % each by default).
        assert window.music_volume_slider.value() == LONG_FORM_MUSIC_VOLUME == 44
        assert window.short_music_volume_slider.value() == SHORTS_MUSIC_VOLUME == 44
        assert window._settings().long_form_music_volume == 44
        assert window._settings().shorts_music_volume == 44
        window.short_music_volume_slider.setValue(50)
        assert window.music_volume_slider.value() == 44
        assert window._settings().shorts_music_volume == 50
        assert window._settings().long_form_music_volume == 44
        window.music_volume_slider.setValue(35)
        assert window.short_music_volume_slider.value() == 50
        assert window._settings().music_volume == 35
        window.music_volume_slider.setValue(44)
        window.short_music_volume_slider.setValue(44)
        assert window.music_preset_combo.currentData() == ("balanced", 44)
        # Separate background music: one Long-Form field, one Shorts field.
        assert window.music_edit.text() == ""
        assert window.short_music_edit.text() == ""
        assert window._settings().music_path == ""
        assert window._settings().short_music_path == ""
        window.music_edit.setText("long_form_theme.mp3")
        window.short_music_edit.setText("shorts_theme.mp3")
        assert window._settings().music_path == "long_form_theme.mp3"
        assert window._settings().short_music_path == "shorts_theme.mp3"
        window.music_edit.setText("")
        window.short_music_edit.setText("")
        # The removed Quote/Flyer section left no widget or handler behind.
        for removed in (
            "quote_check", "quote_artwork_path_edit", "quote_pdf_page_spin",
            "quote_artwork_fit_combo", "quote_duration_spin", "quote_preview",
            "_update_quote_preview", "_sync_quote_visibility", "_quote_dimensions",
        ):
            assert not hasattr(window, removed)
        assert window.ducking_check.isChecked()
        assert window.subtitle_language_combo.currentText() == "German"
        assert window.subtitle_style_combo.currentData() == "long_1"
        # 1.2.4: Animation-Default "static_phrase" (vor 1.2.4 "type_reveal").
        assert window.subtitle_animation_combo.currentData() == "static_phrase"
        assert window.subtitle_font_combo.currentData() == "modern_sans_bold"
        assert window.subtitle_position_combo.currentText() == "Center"
        assert not window.subtitle_debug_check.isChecked()
        # 1.2.4: Die Subtitle-Preview ist jetzt ein echter Renderer-Canvas
        # (SubtitlePreviewCanvas), kein QLabel mehr -> Layout muss gefüllt sein.
        assert window.subtitle_live_preview._layout is not None
        assert any(any(word for word in line) for line in window.subtitle_live_preview._layout.lines)
        assert not window.watermark_check.isChecked()
        assert window.watermark_scope_combo.currentData() == "both"
        window.radio_9.setChecked(True)
        assert window.subtitle_style_combo.currentData() == "short_1"
        assert window.subtitle_animation_combo.currentData() == "word_highlight"
        assert window.subtitle_position_combo.currentText() == "Bottom Center"
        window.subtitle_style_combo.setCurrentIndex(window.subtitle_style_combo.findData("long_3"))
        assert window._settings().subtitle_style == "long_3"  # manual override remains available
    finally:
        window.close()


def test_gui_move_renumbers_persists_resets_and_locks_during_job(qt_app, tmp_path):
    folder = tmp_path / "GUI Clips"
    folder.mkdir()
    paths = []
    for name in ("B.mp4", "A.mp4", "C.mp4"):
        path = folder / name
        path.touch()
        paths.append(path)
    media = [fake_media(str(path), width=160, height=90) for path in paths]
    settings = ExportSettings(resolution="160x90")
    resolved = resolve_export(media, settings)
    state = tmp_path / "order.json"

    window = MainWindow()
    try:
        window.order_store = ProjectOrderStore(state)
        window.order_store.order(folder, paths)
        window.input_edit.setText(str(folder))
        window._on_analysis(media, resolved)
        window._move_row(0, 2)
        assert [item.path.name for item in window.current_media] == ["A.mp4", "C.mp4", "B.mp4"]
        assert [window.files_table.item(row, 0).text() for row in range(3)] == ["1", "2", "3"]
        assert [path.name for path in ProjectOrderStore(state).order(folder, paths)] == [
            "A.mp4", "C.mp4", "B.mp4"
        ]

        window._set_busy(True)
        assert not window.files_table.isEnabled()
        assert not window.files_table.dragEnabled()
        assert not window.move_up_button.isEnabled()
        window._move_row(0, 1)
        assert [item.path.name for item in window.current_media] == ["A.mp4", "C.mp4", "B.mp4"]
        window._set_busy(False)

        window._reset_project_order()
        # 1.2.3: Reset restores the natural numeric/alphabetical default order
        # (A, B, C), never the manual order and never the detector first-in
        # sequence. The persisted store must reflect the same natural order.
        assert [item.path.name for item in window.current_media] == ["A.mp4", "B.mp4", "C.mp4"]
        assert [path.name for path in ProjectOrderStore(state).order(folder, list(reversed(paths)))] == [
            "A.mp4", "B.mp4", "C.mp4"
        ]
    finally:
        window.close()


def test_gui_global_script_and_order_mode_roundtrip(qt_app, tmp_path):
    window = MainWindow()
    try:
        window.store = SettingsStore(tmp_path / "settings.json")
        voices = [tmp_path / "voice_2.wav", tmp_path / "voice_1.wav"]
        global_script = tmp_path / "CompleteScript.txt"
        window.voiceover_paths_list = [str(path) for path in voices]
        window.voiceover_scripts_list = ["", ""]
        window.subtitle_check.setChecked(False)
        window.global_script_edit.setText(str(global_script))
        assert window.subtitle_check.isChecked()
        window.voiceover_order_combo.setCurrentIndex(window.voiceover_order_combo.findData("manual"))
        window._render_voiceover_table()

        settings = window._settings()
        assert settings.global_script_path == str(global_script)
        assert settings.script_mode == "single"
        assert settings.script_paths == [str(global_script)]
        assert settings.voiceover_order_mode == "manual"
        window._save_project()
        saved = window.store.load()
        assert saved.global_script_path == str(global_script)
        assert saved.voiceover_order_mode == "manual"

        window.saved = saved
        window._load_settings()
        assert window.global_script_edit.text() == str(global_script)
        assert window.voiceover_order_combo.currentData() == "manual"
    finally:
        window.close()


def test_gui_delete_all_voiceovers_clears_project_only(qt_app, tmp_path):
    source_files = [tmp_path / "voice_1.wav", tmp_path / "voice_2.wav"]
    for path in source_files:
        path.write_bytes(b"source")
    global_script = tmp_path / "complete.txt"
    global_script.write_text("spoken words", encoding="utf-8")

    window = MainWindow()
    try:
        window.store = SettingsStore(tmp_path / "settings.json")
        window.voiceover_paths_list = [str(path) for path in source_files]
        window.voiceover_scripts_list = [str(tmp_path / "voice_1.txt"), ""]
        window.global_script_edit.setText(str(global_script))
        window.script_mode_combo.setCurrentIndex(window.script_mode_combo.findData("matched"))
        window.voiceover_order_combo.setCurrentIndex(window.voiceover_order_combo.findData("manual"))
        window.voiceover_pause_combo.setCurrentIndex(window.voiceover_pause_combo.findData(1.5))
        window._render_voiceover_table()
        window._delete_all_voiceovers()

        assert window.voiceover_paths_list == []
        assert window.voiceover_scripts_list == []
        assert window.global_script_edit.text() == ""
        assert window.script_mode_combo.currentData() == "single"
        assert window.voiceover_order_combo.currentData() == "natural"
        assert window.voiceover_pause_combo.currentData() == pytest.approx(0.7)
        assert not window.subtitle_check.isChecked()
        assert window.voiceover_table.rowCount() == 0
        saved = window.store.load()
        assert saved.voiceover_paths == []
        assert saved.script_paths == []
        assert saved.global_script_path == ""
        assert all(path.is_file() for path in source_files)
    finally:
        window.close()


def test_gui_clear_all_scripts_preserves_voiceover_rows(qt_app, tmp_path):
    voices = [tmp_path / "voice_1.wav", tmp_path / "voice_2.wav"]
    window = MainWindow()
    try:
        window.store = SettingsStore(tmp_path / "settings.json")
        window.voiceover_paths_list = [str(path) for path in voices]
        window.voiceover_scripts_list = ["first.txt", "second.txt"]
        window.global_script_edit.setText("complete.txt")
        window._render_voiceover_table()
        window._clear_all_scripts()
        assert window.voiceover_paths_list == [str(path) for path in voices]
        assert window.voiceover_scripts_list == ["", ""]
        assert window.global_script_edit.text() == ""
        assert window.voiceover_table.rowCount() == 2
        assert all(window.voiceover_table.item(row, 2).text() == "— no script —" for row in range(2))
    finally:
        window.close()
