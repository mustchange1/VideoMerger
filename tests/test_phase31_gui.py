"""Phase 31 GUI tests: Smart Visual Hybrid sections.

Verifies the new "8 · Smart Visuals" UX: both profile sections exist with
their complete control set, the feature defaults to DISABLED with empty
folders (no forced AI matching), Long-Form and Shorts are strictly
separate, controls enable/disable consistently, the media index button
reports incremental counts, the plan preview works without rendering, and
the save/load round trip keeps old projects untouched.
"""
from __future__ import annotations

import json
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


def test_smart_visual_sections_exist_with_full_control_set(window):
    assert hasattr(window, "sv_long_box") and hasattr(window, "sv_short_box")
    for prefix in ("sv_long", "sv_short"):
        widgets = window._smart_visual_widgets[prefix]
        for key in (
            "enabled", "folders", "add_folder", "remove_folder", "folder_up",
            "folder_down", "folder_clear", "priority", "cadence", "threshold_mode",
            "threshold_custom", "strategy", "strategy_percent", "repetition_window",
            "style", "style_custom", "index_button", "index_status", "plan_button",
            "plan_list", "diagnostics",
        ):
            assert key in widgets, key


def test_fresh_window_has_smart_visuals_disabled_by_default(window):
    for prefix in ("sv_long", "sv_short"):
        widgets = window._smart_visual_widgets[prefix]
        assert widgets["enabled"].isChecked() is False
        assert widgets["folders"].count() == 0
        assert widgets["priority"].currentData() == "balanced"
        assert widgets["threshold_mode"].currentData() == "medium"
        assert widgets["strategy"].currentData() == "only_when_no_match"
        assert widgets["style"].currentData() == "cinematic"
        assert widgets["cadence"].currentData() == "adaptive"
        # Disabled => dependent controls are locked.
        assert widgets["priority"].isEnabled() is False
        assert widgets["plan_button"].isEnabled() is False
    settings = window._settings()
    assert settings.smart_visual_enabled is False
    assert settings.shorts_smart_visual_enabled is False
    assert settings.long_form_smart_visual_folders == []
    assert settings.shorts_smart_visual_folders == []


def test_old_project_loads_with_smart_visuals_off(window, tmp_path):
    # A project saved before Phase 31 carries none of the new keys.
    legacy = {"resolution": "320x180", "crf": 22, "timeline_image_mode": "every_n"}
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    from app.video_merger.settings_store import SettingsStore

    loaded = SettingsStore(path).load()
    assert loaded.smart_visual_enabled is False
    assert loaded.shorts_smart_visual_enabled is False
    assert loaded.timeline_image_mode == "every_n"  # historical value untouched


def test_enable_toggles_control_availability(window):
    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    assert widgets["priority"].isEnabled() is True
    assert widgets["plan_button"].isEnabled() is True
    assert widgets["threshold_custom"].isEnabled() is False  # only for custom mode
    widgets["threshold_mode"].setCurrentIndex(widgets["threshold_mode"].findData("custom"))
    assert widgets["threshold_custom"].isEnabled() is True
    widgets["strategy"].setCurrentIndex(widgets["strategy"].findData("custom_percent"))
    assert widgets["strategy_percent"].isEnabled() is True
    widgets["style"].setCurrentIndex(widgets["style"].findData("custom"))
    assert widgets["style_custom"].isEnabled() is True
    widgets["enabled"].setChecked(False)
    assert widgets["priority"].isEnabled() is False
    assert widgets["threshold_custom"].isEnabled() is False


def test_folder_list_controls_and_settings_roundtrip(window):
    widgets = window._smart_visual_widgets["sv_long"]
    folders = widgets["folders"]
    for folder in ("/tmp/a", "/tmp/b", "/tmp/c"):
        folders.addItem(folder)
    assert folders.count() == 3
    folders.setCurrentRow(2)
    window._smart_visual_move_folder("sv_long", -1)
    assert folders.item(1).text() == "/tmp/c" and folders.currentRow() == 1
    window._smart_visual_remove_folder("sv_long")
    assert folders.count() == 2

    widgets["enabled"].setChecked(True)
    settings = window._settings()
    assert settings.long_form_smart_visual_folders == ["/tmp/a", "/tmp/b"]
    assert settings.smart_visual_enabled is True
    # The Shorts profile stays untouched by Long-Form edits.
    assert settings.shorts_smart_visual_enabled is False
    assert settings.shorts_smart_visual_folders == []


def test_profiles_are_strictly_separate_in_settings(window):
    long_w = window._smart_visual_widgets["sv_long"]
    short_w = window._smart_visual_widgets["sv_short"]
    long_w["enabled"].setChecked(True)
    long_w["folders"].addItem("/tmp/lf_media")
    long_w["style"].setCurrentIndex(long_w["style"].findData("documentary"))
    long_w["threshold_mode"].setCurrentIndex(long_w["threshold_mode"].findData("high"))
    short_w["enabled"].setChecked(True)
    short_w["folders"].addItem("/tmp/short_media")
    short_w["style"].setCurrentIndex(short_w["style"].findData("minimal"))
    short_w["threshold_mode"].setCurrentIndex(short_w["threshold_mode"].findData("low"))

    settings = window._settings()
    assert settings.smart_visual_enabled is True
    assert settings.long_form_smart_visual_folders == ["/tmp/lf_media"]
    assert settings.smart_visual_style == "documentary"
    assert settings.smart_visual_threshold_mode == "high"
    assert settings.shorts_smart_visual_enabled is True
    assert settings.shorts_smart_visual_folders == ["/tmp/short_media"]
    assert settings.shorts_smart_visual_style == "minimal"
    assert settings.shorts_smart_visual_threshold_mode == "low"


def test_load_saved_smart_visual_settings_into_widgets(window):
    window.saved = ExportSettings(
        smart_visual_enabled=True,
        long_form_smart_visual_folders=["/tmp/saved_lf"],
        smart_visual_source_priority="image_first",
        smart_visual_threshold_mode="custom",
        smart_visual_threshold_custom=0.42,
        smart_visual_generation_strategy="every_3rd",
        smart_visual_repetition_window=5,
        smart_visual_style="custom",
        smart_visual_style_custom="soft watercolor",
        smart_visual_cadence="every_2",
        shorts_smart_visual_enabled=True,
        shorts_smart_visual_folders=["/tmp/saved_sh"],
        shorts_smart_visual_style="minimal",
    )
    window._load_smart_visual_settings()
    long_w = window._smart_visual_widgets["sv_long"]
    short_w = window._smart_visual_widgets["sv_short"]
    assert long_w["enabled"].isChecked() is True
    assert long_w["folders"].item(0).text() == "/tmp/saved_lf"
    assert long_w["priority"].currentData() == "image_first"
    assert long_w["threshold_mode"].currentData() == "custom"
    assert long_w["threshold_custom"].value() == pytest.approx(0.42)
    assert long_w["strategy"].currentData() == "every_3rd"
    assert long_w["repetition_window"].value() == 5
    assert long_w["style"].currentData() == "custom"
    assert long_w["style_custom"].text() == "soft watercolor"
    assert long_w["cadence"].currentData() == "every_2"
    assert short_w["enabled"].isChecked() is True
    assert short_w["folders"].item(0).text() == "/tmp/saved_sh"
    assert short_w["style"].currentData() == "minimal"


def test_build_index_button_reports_incremental_counts(window, tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    folder = tmp_path / "City"
    folder.mkdir()
    (folder / "street.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
        )
    )
    widgets = window._smart_visual_widgets["sv_long"]
    widgets["folders"].addItem(str(folder))
    window._smart_visual_build_index("sv_long")
    status = widgets["index_status"].text()
    assert "1 media indexed" in status
    assert "1 new" in status
    # Second run reuses the cached entry (incremental).
    window._smart_visual_build_index("sv_long")
    assert "1 reused from cache" in widgets["index_status"].text()


def test_plan_preview_without_folders_reports_empty(window):
    widgets = window._smart_visual_widgets["sv_long"]
    window._smart_visual_preview_plan("sv_long")
    assert widgets["plan_list"].count() >= 1
    assert "disabled" in widgets["plan_list"].item(0).text().casefold()


def test_plan_preview_with_enabled_profile_lists_slots_or_diagnostics(window, tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    folder = tmp_path / "city"
    folder.mkdir()
    (folder / "city street.png").write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
        )
    )
    script = tmp_path / "script.txt"
    script.write_text("The city streets are busy today.", encoding="utf-8")
    window.global_script_edit.setText(str(script))

    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    widgets["folders"].addItem(str(folder))
    window._smart_visual_preview_plan("sv_long")
    texts = [widgets["plan_list"].item(i).text() for i in range(widgets["plan_list"].count())]
    assert texts, "the preview must list at least one row"
    # The diagnostics row reports the local generation state honestly.
    assert "Local Generation" in widgets["diagnostics"].text()
