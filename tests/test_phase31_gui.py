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
        # Phase 33 control set: selection mode, image duration, Smart Insert
        # frequency, Analyze/Randomize Timeline and the folder/index tools.
        for key in (
            "enabled", "folders", "add_folder", "remove_folder", "folder_up",
            "folder_down", "folder_clear", "mode", "image_duration",
            "insert_percent", "index_button", "index_status", "analyze_button",
            "randomize_button", "analyze_list", "image_transition",
            "image_transition_duration", "image_visual_effect",
            "image_visual_effect_intensity",
        ):
            assert key in widgets, key
        # The removed Phase-31/32 generation controls must be gone.
        for gone in (
            "priority", "cadence", "threshold_mode", "strategy", "style",
            "allow_generated", "fallback", "plan_button", "plan_list",
            "diagnostics",
        ):
            assert gone not in widgets, gone


def test_fresh_window_has_smart_visuals_disabled_by_default(window):
    for prefix in ("sv_long", "sv_short"):
        widgets = window._smart_visual_widgets[prefix]
        assert widgets["enabled"].isChecked() is False
        assert widgets["folders"].count() == 0
        # Phase 33 safe defaults.
        assert widgets["mode"].currentData() == "smart_match"
        assert widgets["image_duration"].value() == pytest.approx(5.0)
        assert widgets["insert_percent"].value() == 25
        # Disabled => dependent controls are locked.
        assert widgets["mode"].isEnabled() is False
        assert widgets["analyze_button"].isEnabled() is False
    settings = window._settings()
    assert settings.smart_visual_enabled is False
    assert settings.shorts_smart_visual_enabled is False
    assert settings.long_form_smart_visual_folders == []
    assert settings.shorts_smart_visual_folders == []
    # Phase 33 selection-engine defaults for a fresh configuration.
    assert settings.smart_visual_mode == "smart_match"
    assert settings.smart_visual_image_duration == pytest.approx(5.0)
    assert settings.smart_visual_insert_percent == 25


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
    assert widgets["mode"].isEnabled() is True
    assert widgets["image_duration"].isEnabled() is True
    assert widgets["analyze_button"].isEnabled() is True
    assert widgets["randomize_button"].isEnabled() is True
    # Smart Insert Frequency only applies to the Mostly Random mode.
    assert widgets["insert_percent"].isEnabled() is False
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("smart_inserts"))
    assert widgets["insert_percent"].isEnabled() is True
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("random_only"))
    assert widgets["insert_percent"].isEnabled() is False
    widgets["enabled"].setChecked(False)
    assert widgets["mode"].isEnabled() is False
    assert widgets["analyze_button"].isEnabled() is False


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
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("smart_inserts"))
    long_w["image_duration"].setValue(7.0)
    long_w["insert_percent"].setValue(40)
    short_w["enabled"].setChecked(True)
    short_w["folders"].addItem("/tmp/short_media")
    short_w["mode"].setCurrentIndex(short_w["mode"].findData("random_only"))
    short_w["image_duration"].setValue(3.0)

    settings = window._settings()
    assert settings.smart_visual_enabled is True
    assert settings.long_form_smart_visual_folders == ["/tmp/lf_media"]
    assert settings.smart_visual_mode == "smart_inserts"
    assert settings.smart_visual_image_duration == pytest.approx(7.0)
    assert settings.smart_visual_insert_percent == 40
    assert settings.shorts_smart_visual_enabled is True
    assert settings.shorts_smart_visual_folders == ["/tmp/short_media"]
    assert settings.shorts_smart_visual_mode == "random_only"
    assert settings.shorts_smart_visual_image_duration == pytest.approx(3.0)
    # The Shorts insert frequency keeps its own (default) value.
    assert settings.shorts_smart_visual_insert_percent == 25


def test_load_saved_smart_visual_settings_into_widgets(window):
    window.saved = ExportSettings(
        smart_visual_enabled=True,
        long_form_smart_visual_folders=["/tmp/saved_lf"],
        smart_visual_mode="smart_inserts",
        smart_visual_image_duration=6.5,
        smart_visual_insert_percent=40,
        smart_visual_randomize_nonce=3,
        shorts_smart_visual_enabled=True,
        shorts_smart_visual_folders=["/tmp/saved_sh"],
        shorts_smart_visual_mode="random_only",
        shorts_smart_visual_image_duration=2.5,
    )
    window._load_smart_visual_settings()
    long_w = window._smart_visual_widgets["sv_long"]
    short_w = window._smart_visual_widgets["sv_short"]
    assert long_w["enabled"].isChecked() is True
    assert long_w["folders"].item(0).text() == "/tmp/saved_lf"
    assert long_w["mode"].currentData() == "smart_inserts"
    assert long_w["image_duration"].value() == pytest.approx(6.5)
    assert long_w["insert_percent"].value() == 40
    assert long_w["_randomize_nonce"] == 3
    assert short_w["enabled"].isChecked() is True
    assert short_w["folders"].item(0).text() == "/tmp/saved_sh"
    assert short_w["mode"].currentData() == "random_only"
    assert short_w["image_duration"].value() == pytest.approx(2.5)


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


def test_analyze_timeline_without_folders_reports_empty(window):
    widgets = window._smart_visual_widgets["sv_long"]
    window._smart_visual_analyze_timeline("sv_long")
    assert widgets["analyze_list"].count() >= 1
    assert "disabled" in widgets["analyze_list"].item(0).text().casefold()


def test_analyze_timeline_with_enabled_profile_lists_regions(window, tmp_path, monkeypatch):
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
    window._smart_visual_analyze_timeline("sv_long")
    texts = [widgets["analyze_list"].item(i).text() for i in range(widgets["analyze_list"].count())]
    assert texts, "the analysis must list at least one row"
    # The header row reports the Phase 33 selection-engine settings and the
    # per-region rows expose the Smart-or-Random source. Nothing is rendered.
    assert any("Selection Mode" in text for text in texts)
    assert any("Source:" in text for text in texts)
