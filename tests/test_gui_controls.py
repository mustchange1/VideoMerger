from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from app.video_merger.gui.main_window import MainWindow
from app.video_merger.models import ExportSettings
from app.video_merger.project_order import ProjectOrderStore
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
        assert window.pause_combo.currentData() == 1.0
        assert window.music_volume_slider.value() == 22
        assert window.ducking_check.isChecked()
        assert window.subtitle_language_combo.currentText() == "German"
        assert window.subtitle_style_combo.currentData() == "long_1"
        # 1.2.4: Animation-Default "static_phrase" (vor 1.2.4 "type_reveal").
        assert window.subtitle_animation_combo.currentData() == "static_phrase"
        assert window.subtitle_font_combo.currentData() == "modern_sans_bold"
        assert window.subtitle_position_combo.currentText() == "Bottom"
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
        assert window.subtitle_position_combo.currentText() == "Medium-Low"
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
