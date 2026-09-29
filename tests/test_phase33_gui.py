"""Phase 33 GUI tests: selection-engine UI, Analyze/Randomize, hold default.

Covers the Phase-33 UI contracts: the simplified Smart Visuals control set
(mode / image duration / Smart Insert Frequency / Analyze / Randomize), the
removed generation controls, the clickability fix of the Image Timeline
Visual Effects controls (they must be usable even while image insertions
are still disabled), the Typewriter hold default of 3.0 s for NEW/unset
configurations with explicitly saved values preserved, and the settings
round trip of the new fields.
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


# ---------------------------------------------------------------------------
# Image Timeline Visual Effects must be clickable (Phase 33 UX fix)
# ---------------------------------------------------------------------------
def test_image_timeline_effect_controls_clickable_while_insertions_disabled(window):
    # The user-facing bug: with Insertion Mode = Disabled (the default) the
    # whole Visual Effects area was locked. Effect selection is independent
    # from image insertion and must stay usable.
    for prefix in ("img_long", "img_short"):
        widgets = window._image_timeline_widgets[prefix]
        assert widgets["mode"].currentData() == "disabled"  # fresh default
        assert widgets["effect"].isEnabled() is True
        assert widgets["motion"].isEnabled() is True
        assert widgets["preview_button"].isEnabled() is True
        assert widgets["global_effect"].isEnabled() is True
        # The folder buttons stay clickable too.
        assert widgets["add_folder"].isEnabled() is True
        assert widgets["remove_folder"].isEnabled() is True
        # Sub-controls follow their own gate (effect chosen or not).
        assert widgets["effect_intensity"].isEnabled() is False
        widgets["effect"].setCurrentIndex(widgets["effect"].findData("crt_scanlines"))
        assert widgets["effect_intensity"].isEnabled() is True
        assert widgets["flicker"].isEnabled() is True


def test_image_timeline_insertion_controls_still_follow_the_mode(window):
    widgets = window._image_timeline_widgets["img_long"]
    assert widgets["every_n"].isEnabled() is False  # mode disabled
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("every_n"))
    assert widgets["every_n"].isEnabled() is True
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("percentage"))
    assert widgets["share"].isEnabled() is True
    assert widgets["every_n"].isEnabled() is False


# ---------------------------------------------------------------------------
# Smart Visuals: simplified control set, generation UI removed
# ---------------------------------------------------------------------------
def test_smart_visual_ui_is_the_simplified_phase33_set(window):
    widgets = window._smart_visual_widgets["sv_long"]
    for key in ("enabled", "folders", "mode", "image_duration", "insert_percent",
                "analyze_button", "randomize_button", "analyze_list",
                "image_transition", "image_visual_effect"):
        assert key in widgets, key
    # No generation controls remain in the Smart Visuals workflow UI.
    for gone in ("allow_generated", "fallback", "strategy", "strategy_percent",
                 "style", "style_custom", "threshold_mode", "threshold_custom",
                 "diagnostics"):
        assert gone not in widgets, gone
    # Defaults: Smart Match / 5.0 s / 25 %.
    assert widgets["mode"].currentData() == "smart_match"
    assert widgets["image_duration"].value() == pytest.approx(5.0)
    assert widgets["insert_percent"].value() == 25


def test_randomize_timeline_bumps_the_nonce_and_reanalyzes(window, tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    folder = tmp_path / "pool"
    folder.mkdir()
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
    )
    (folder / "one.jpg").write_bytes(png)
    (folder / "two.jpg").write_bytes(png)
    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    widgets["folders"].addItem(str(folder))
    assert widgets.get("_randomize_nonce", 0) == 0
    window._smart_visual_randomize_timeline("sv_long")
    assert widgets["_randomize_nonce"] == 1
    assert widgets["analyze_list"].count() >= 1  # re-analyzed immediately
    window._smart_visual_randomize_timeline("sv_long")
    assert widgets["_randomize_nonce"] == 2
    # The nonce reaches the settings (and therefore the render seed).
    settings = window._settings()
    assert settings.smart_visual_randomize_nonce == 2
    # Mode, duration and folders are untouched by Randomize.
    assert settings.smart_visual_mode == "smart_match"
    assert settings.smart_visual_image_duration == pytest.approx(5.0)
    assert settings.long_form_smart_visual_folders == [str(folder)]


def test_analyze_timeline_lists_readable_regions(window, tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    folder = tmp_path / "pool"
    folder.mkdir()
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
    )
    (folder / "alps_hike.jpg").write_bytes(png)
    script = tmp_path / "script.txt"
    script.write_text("We hike through the alpine meadows today.", encoding="utf-8")
    window.global_script_edit.setText(str(script))
    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    widgets["folders"].addItem(str(folder))
    window._smart_visual_analyze_timeline("sv_long")
    texts = [widgets["analyze_list"].item(i).text() for i in range(widgets["analyze_list"].count())]
    joined = "\n".join(texts)
    assert texts
    assert any("run source analysis first" in text.casefold() for text in texts)
    assert "alps_hike.jpg" not in joined


# ---------------------------------------------------------------------------
# Typewriter hold default: 3.0 s for new/unset, explicit values preserved
# ---------------------------------------------------------------------------
def test_typewriter_hold_defaults_to_3_seconds_when_unset(window):
    for prefix in ("tw_long", "tw_short"):
        widgets = window._typewriter_widgets[prefix]
        assert widgets["hold_seconds"].value() == pytest.approx(3.0)
    settings = window._settings()
    assert settings.typewriter_hold_seconds == pytest.approx(3.0)
    assert settings.short_typewriter_hold_seconds == pytest.approx(3.0)
    # An engine profile built from these settings holds 3.0 s.
    from app.video_merger.typewriter_intro import profile_from_settings

    assert profile_from_settings(settings).hold_seconds == pytest.approx(3.0)


def test_typewriter_hold_preserves_explicitly_saved_values(window):
    # A saved project with explicit hold values (even the old 0.5 s default)
    # must keep them - the new 3.0 s applies to unset configurations only.
    window.saved = ExportSettings(
        typewriter_intro_enabled=True,
        typewriter_hook_text="hook",
        typewriter_hold_seconds=0.5,
        short_typewriter_intro_enabled=True,
        short_typewriter_hook_text="hook",
        short_typewriter_hold_seconds=1.2,
    )
    window._load_typewriter_settings()
    assert window._typewriter_widgets["tw_long"]["hold_seconds"].value() == pytest.approx(0.5)
    assert window._typewriter_widgets["tw_short"]["hold_seconds"].value() == pytest.approx(1.2)
    # The engine agrees: explicit values win over the new default.
    from app.video_merger.typewriter_intro import profile_from_settings

    assert profile_from_settings(window.saved).hold_seconds == pytest.approx(0.5)


def test_typewriter_hold_missing_key_resolves_to_new_default():
    # A settings object without the key behaves like a NEW configuration.
    from app.video_merger.typewriter_intro import profile_from_settings

    class Bare:
        typewriter_intro_enabled = True
        typewriter_hook_text = "hook"

    assert profile_from_settings(Bare()).hold_seconds == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Phase 33 settings round trip
# ---------------------------------------------------------------------------
def test_phase33_fields_round_trip_through_save_and_load(window):
    long_w = window._smart_visual_widgets["sv_long"]
    long_w["enabled"].setChecked(True)
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("smart_inserts"))
    long_w["image_duration"].setValue(6.0)
    long_w["insert_percent"].setValue(30)
    window._smart_visual_randomize_timeline("sv_long")
    settings = window._settings()
    assert settings.smart_visual_mode == "smart_inserts"
    assert settings.smart_visual_image_duration == pytest.approx(6.0)
    assert settings.smart_visual_insert_percent == 30
    assert settings.smart_visual_randomize_nonce == 1
    # Load the saved state back into a fresh widget set.
    window.saved = settings
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("random_only"))
    long_w["image_duration"].setValue(1.0)
    window._load_smart_visual_settings()
    assert long_w["mode"].currentData() == "smart_inserts"
    assert long_w["image_duration"].value() == pytest.approx(6.0)
    assert long_w["insert_percent"].value() == 30
    assert long_w["_randomize_nonce"] == 1


def test_legacy_smart_visual_project_loads_safely(window, tmp_path):
    # A pre-Phase-33 project: no mode/duration/percent/nonce keys at all.
    import json

    from app.video_merger.settings_store import SettingsStore

    legacy = {
        "resolution": "320x180",
        "smart_visual_enabled": True,
        "smart_visual_style": "documentary",          # legacy field survives
        "smart_visual_threshold_mode": "high",        # legacy field survives
        "typewriter_intro_enabled": True,
        "typewriter_hook_text": "hook",
        "typewriter_hold_seconds": 0.5,               # explicit old value
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = SettingsStore(path).load()
    # Phase 33 safe defaults fill the missing keys ...
    assert loaded.smart_visual_mode == "smart_match"
    assert loaded.smart_visual_image_duration == pytest.approx(5.0)
    assert loaded.smart_visual_insert_percent == 25
    assert loaded.smart_visual_randomize_nonce == 0
    # ... while every explicit legacy value is preserved untouched.
    assert loaded.smart_visual_style == "documentary"
    assert loaded.smart_visual_threshold_mode == "high"
    assert loaded.typewriter_hold_seconds == pytest.approx(0.5)
