"""Phase 30 GUI tests: Image Timeline & Visual Effects sections.

Verifies the Image Sources UX (Add/Remove/Move Up/Move Down/Clear), the
complete rule set for Long-Form and Shorts, strictly separate profiles,
safe defaults on fresh projects, and the save/load round trip through the
real settings model.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover - headless guard parity
    pytest.skip(f"PySide6 nicht verfügbar: {exc}", allow_module_level=True)

from app.video_merger.gui.main_window import MainWindow
from app.video_merger.models import ExportSettings


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture()
def window(app):
    win = MainWindow()
    yield win
    win.close()


def test_image_timeline_sections_exist_with_source_controls(window):
    assert hasattr(window, "img_long_box") and hasattr(window, "img_short_box")
    for prefix in ("img_long", "img_short"):
        widgets = window._image_timeline_widgets[prefix]
        for key in (
            "folders", "add_folder", "remove_folder", "folder_up", "folder_down",
            "folder_clear", "mode", "every_n", "share", "min_gap", "duration_mode",
            "duration", "duration_spin", "duration_min", "duration_max", "motion",
            "effect", "effect_intensity", "flicker", "global_effect",
            "global_intensity", "global_flicker", "preview_button", "preview_label",
        ):
            assert key in widgets, key


def test_fresh_window_has_safe_disabled_defaults(window):
    for prefix in ("img_long", "img_short"):
        widgets = window._image_timeline_widgets[prefix]
        assert widgets["mode"].currentData() == "disabled"
        assert widgets["folders"].count() == 0
        assert widgets["every_n"].value() == 4
        assert widgets["min_gap"].value() == 2
        assert widgets["duration"].currentData() == 2.5
        assert widgets["motion"].currentData() == "zoom_in"
        assert widgets["effect"].currentData() == "off"
        assert widgets["effect_intensity"].value() == 20
        assert widgets["global_effect"].currentData() == "off"
    settings = window._settings()
    assert settings.timeline_image_mode == "disabled"
    assert settings.shorts_image_mode == "disabled"
    assert settings.long_form_image_folders == []
    assert settings.shorts_image_folders == []
    assert settings.global_tv_effect == "off"


def test_folder_list_controls(window):
    widgets = window._image_timeline_widgets["img_long"]
    folders = widgets["folders"]
    for folder in ("/tmp/a", "/tmp/b", "/tmp/c"):
        folders.addItem(folder)
    assert folders.count() == 3
    folders.setCurrentRow(2)
    window._image_timeline_move_folder("img_long", -1)
    assert folders.item(1).text() == "/tmp/c" and folders.currentRow() == 1
    window._image_timeline_move_folder("img_long", 1)
    assert folders.item(2).text() == "/tmp/c"
    window._image_timeline_remove_folder("img_long")
    assert folders.count() == 2
    window._image_timeline_clear_folders("img_long")
    assert folders.count() == 0


def test_duration_presets_and_custom(window):
    widgets = window._image_timeline_widgets["img_long"]
    presets = [float(widgets["duration"].itemData(i)) for i in range(widgets["duration"].count())]
    for expected in (1.0, 2.0, 2.5, 3.0, 4.0, 5.0):
        assert expected in presets
    assert -1.0 in presets  # Custom …


def test_profiles_are_strictly_separate(window):
    long_w = window._image_timeline_widgets["img_long"]
    short_w = window._image_timeline_widgets["img_short"]
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("every_n"))
    long_w["folders"].addItem("/tmp/lf_images")
    long_w["effect"].setCurrentIndex(long_w["effect"].findData("vhs"))
    settings = window._settings()
    assert settings.timeline_image_mode == "every_n"
    assert settings.timeline_image_effect == "vhs"
    assert settings.long_form_image_folders == ["/tmp/lf_images"]
    # Shorts keeps its disabled defaults - no cross-feeding
    assert settings.shorts_image_mode == "disabled"
    assert settings.shorts_image_folders == []
    assert settings.shorts_image_effect == "off"
    assert short_w["mode"].currentData() == "disabled"


def test_settings_round_trip_load(window):
    long_w = window._image_timeline_widgets["img_long"]
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("percentage"))
    long_w["share"].setValue(35)
    long_w["folders"].addItem("/tmp/lf")
    long_w["duration_mode"].setCurrentIndex(long_w["duration_mode"].findData("range"))
    long_w["duration_min"].setValue(1.5)
    long_w["duration_max"].setValue(3.5)
    long_w["motion"].setCurrentIndex(long_w["motion"].findData("ken_burns"))
    long_w["global_effect"].setCurrentIndex(long_w["global_effect"].findData("broadcast"))
    long_w["global_intensity"].setValue(42)
    short_w = window._image_timeline_widgets["img_short"]
    short_w["mode"].setCurrentIndex(short_w["mode"].findData("every_n"))
    short_w["every_n"].setValue(3)
    short_w["folders"].addItem("/tmp/sh")
    short_w["global_effect"].setCurrentIndex(short_w["global_effect"].findData("crt_scanlines"))

    saved = window._settings()
    window.saved = saved
    window._load_image_timeline_settings()

    assert long_w["mode"].currentData() == "percentage"
    assert long_w["share"].value() == 35
    assert long_w["folders"].item(0).text() == "/tmp/lf"
    assert long_w["duration_mode"].currentData() == "range"
    assert long_w["duration_min"].value() == 1.5 and long_w["duration_max"].value() == 3.5
    assert long_w["motion"].currentData() == "ken_burns"
    assert long_w["global_effect"].currentData() == "broadcast"
    assert long_w["global_intensity"].value() == 42
    assert short_w["mode"].currentData() == "every_n"
    assert short_w["every_n"].value() == 3
    assert short_w["folders"].item(0).text() == "/tmp/sh"
    assert short_w["global_effect"].currentData() == "crt_scanlines"


def test_legacy_project_load_keeps_image_sections_disabled(window):
    window.saved = ExportSettings()  # pre-Phase-30 model state
    window._load_image_timeline_settings()
    for prefix in ("img_long", "img_short"):
        widgets = window._image_timeline_widgets[prefix]
        assert widgets["mode"].currentData() == "disabled"
        assert widgets["folders"].count() == 0


def test_sync_controls_enable_only_relevant_fields(window):
    widgets = window._image_timeline_widgets["img_long"]
    assert widgets["every_n"].isEnabled() is False  # disabled mode
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("every_n"))
    assert widgets["every_n"].isEnabled() is True
    assert widgets["share"].isEnabled() is False
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("percentage"))
    assert widgets["share"].isEnabled() is True
    assert widgets["every_n"].isEnabled() is False
    widgets["effect"].setCurrentIndex(widgets["effect"].findData("vhs"))
    window._sync_image_timeline_controls("img_long")
    assert widgets["effect_intensity"].isEnabled() is True
    widgets["global_effect"].setCurrentIndex(widgets["global_effect"].findData("vhs"))
    window._sync_image_timeline_controls("img_long")
    assert widgets["global_intensity"].isEnabled() is True
