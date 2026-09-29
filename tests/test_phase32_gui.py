"""Phase 32 GUI tests: fallback policy, image rendering + completion sound.

PHASE 33 CONTRACT CHANGE (documented): the Smart Visual generation controls
(Allow Generated Images, fallback policy, strategy, style, threshold) were
removed from the UI - Smart Visuals is a pure selection engine now. The
image rendering controls (image transition + duration, image visual effect
+ intensity) and the Typewriter completion-sound controls stay unchanged;
the tests below verify them together with the new Phase-33 controls.
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


# Phase 33: the image RENDERING controls survive (they never generated
# anything); the generation toggle/policy widgets were removed from the UI.
SMART_PHASE32_KEYS = (
    "image_transition", "image_transition_duration",
    "image_visual_effect", "image_visual_effect_intensity",
)
TYPEWRITER_PHASE32_KEYS = (
    "completion_sound_enabled", "completion_sound_preset", "completion_sound_volume",
)


# ---------------------------------------------------------------------------
# Presence + defaults
# ---------------------------------------------------------------------------
def test_smart_visual_sections_have_phase32_controls(window):
    for prefix in ("sv_long", "sv_short"):
        widgets = window._smart_visual_widgets[prefix]
        for key in SMART_PHASE32_KEYS:
            assert key in widgets, key
        # Phase 33: the generation controls are gone from the UI.
        for gone in ("allow_generated", "fallback", "strategy", "style"):
            assert gone not in widgets, gone


def test_fresh_window_defaults_preserve_image_rendering_defaults(window):
    for prefix in ("sv_long", "sv_short"):
        widgets = window._smart_visual_widgets[prefix]
        assert widgets["image_transition"].currentData() == "project"
        assert widgets["image_transition_duration"].value() == pytest.approx(0.0)
        assert widgets["image_visual_effect"].currentData() == "none"
        assert widgets["image_visual_effect_intensity"].currentData() == "low"
    settings = window._settings()
    # Legacy generation fields keep their stored (default) values untouched.
    assert settings.smart_visual_allow_generated is True
    assert settings.smart_visual_fallback == "generate_image"
    assert settings.long_form_image_transition_type == "project"
    assert settings.long_form_image_transition_duration is None
    assert settings.long_form_image_visual_effect == "none"


def test_typewriter_sections_have_completion_controls_with_defaults(window):
    for prefix in ("tw_long", "tw_short"):
        widgets = window._typewriter_widgets[prefix]
        for key in TYPEWRITER_PHASE32_KEYS:
            assert key in widgets, key
        assert widgets["completion_sound_enabled"].isChecked() is True
        assert widgets["completion_sound_preset"].currentData() == "enter_return"
        assert widgets["completion_sound_volume"].value() == 40


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------
def test_insert_frequency_gating_follows_the_selection_mode(window):
    # Phase 33 replacement for the removed allow_generated gating test: the
    # Smart Insert Frequency control only applies to the Mostly Random mode.
    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("smart_match"))
    assert widgets["insert_percent"].isEnabled() is False
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("smart_inserts"))
    assert widgets["insert_percent"].isEnabled() is False
    widgets["mode"].setCurrentIndex(widgets["mode"].findData("random_only"))
    assert widgets["insert_percent"].isEnabled() is False


def test_effect_none_locks_the_intensity_control(window):
    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    assert widgets["image_visual_effect_intensity"].isEnabled() is False
    widgets["image_visual_effect"].setCurrentIndex(
        widgets["image_visual_effect"].findData("soft_shimmer")
    )
    assert widgets["image_visual_effect_intensity"].isEnabled() is True
    widgets["image_visual_effect"].setCurrentIndex(
        widgets["image_visual_effect"].findData("none")
    )
    assert widgets["image_visual_effect_intensity"].isEnabled() is False


def test_disabled_smart_visuals_lock_profile_controls(window):
    widgets = window._smart_visual_widgets["sv_short"]
    widgets["enabled"].setChecked(False)
    assert widgets["mode"].isEnabled() is False
    assert widgets["image_duration"].isEnabled() is False
    assert widgets["image_transition"].isEnabled() is False
    assert widgets["image_visual_effect"].isEnabled() is False
    widgets["enabled"].setChecked(True)
    assert widgets["mode"].isEnabled() is True
    assert widgets["image_transition"].isEnabled() is True
    assert widgets["image_visual_effect"].isEnabled() is True


# ---------------------------------------------------------------------------
# Settings round trip + per-output separation
# ---------------------------------------------------------------------------
def test_saved_phase32_settings_load_into_both_profiles(window):
    window.saved = ExportSettings(
        smart_visual_enabled=True,
        smart_visual_allow_generated=False,
        smart_visual_fallback="random_video",
        long_form_image_transition_type="film_dissolve",
        long_form_image_transition_duration=0.6,
        long_form_image_visual_effect="soft_shimmer",
        long_form_image_visual_effect_intensity="medium",
        shorts_smart_visual_enabled=True,
        shorts_smart_visual_allow_generated=True,
        shorts_smart_visual_fallback="skip",
        shorts_image_transition_type="smooth_blur",
        shorts_image_transition_duration=0.3,
        shorts_image_visual_effect="crt_broadcast",
        shorts_image_visual_effect_intensity="high",
    )
    window._load_smart_visual_settings()
    long_w = window._smart_visual_widgets["sv_long"]
    short_w = window._smart_visual_widgets["sv_short"]
    # Phase 33: only the image RENDERING controls load into widgets; the
    # legacy generation toggle/policy stay stored but have no widgets.
    assert long_w["image_transition"].currentData() == "film_dissolve"
    assert long_w["image_transition_duration"].value() == pytest.approx(0.6)
    assert long_w["image_visual_effect"].currentData() == "soft_shimmer"
    assert long_w["image_visual_effect_intensity"].currentData() == "medium"
    assert short_w["image_transition"].currentData() == "smooth_blur"
    assert short_w["image_transition_duration"].value() == pytest.approx(0.3)
    assert short_w["image_visual_effect"].currentData() == "crt_broadcast"
    assert short_w["image_visual_effect_intensity"].currentData() == "high"


def test_ui_writes_phase32_settings_per_profile(window):
    long_w = window._smart_visual_widgets["sv_long"]
    long_w["enabled"].setChecked(True)
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("smart_inserts"))
    long_w["image_duration"].setValue(6.0)
    long_w["insert_percent"].setValue(35)
    long_w["image_transition"].setCurrentIndex(
        long_w["image_transition"].findData("additive_dissolve")
    )
    long_w["image_transition_duration"].setValue(0.8)
    long_w["image_visual_effect"].setCurrentIndex(
        long_w["image_visual_effect"].findData("gentle_flicker")
    )
    long_w["image_visual_effect_intensity"].setCurrentIndex(
        long_w["image_visual_effect_intensity"].findData("high")
    )
    short_w = window._smart_visual_widgets["sv_short"]
    short_w["enabled"].setChecked(True)
    short_w["mode"].setCurrentIndex(short_w["mode"].findData("random_only"))

    settings = window._settings()
    # Phase 33 selection engine settings per profile.
    assert settings.smart_visual_mode == "smart_inserts"
    assert settings.smart_visual_image_duration == pytest.approx(6.0)
    assert settings.smart_visual_insert_percent == 35
    assert settings.shorts_smart_visual_mode == "random_only"
    # Image rendering settings per profile.
    assert settings.long_form_image_transition_type == "additive_dissolve"
    assert settings.long_form_image_transition_duration == pytest.approx(0.8)
    assert settings.long_form_image_visual_effect == "gentle_flicker"
    assert settings.long_form_image_visual_effect_intensity == "high"
    # Legacy generation fields keep their stored defaults (no UI edits them).
    assert settings.smart_visual_allow_generated is True
    assert settings.smart_visual_fallback == "generate_image"
    # Shorts image rendering untouched by the Long-Form edits above.
    assert settings.shorts_image_transition_type == "project"
    assert settings.shorts_image_visual_effect == "none"


def test_ui_round_trip_keeps_saved_values(window):
    # Phase 33 replacement: the fallback-policy widget no longer exists, so
    # the round trip now proves (a) the selection-engine fields survive a
    # save/load cycle and (b) legacy generation fields stored in the project
    # are carried forward untouched by the UI.
    long_w = window._smart_visual_widgets["sv_long"]
    long_w["enabled"].setChecked(True)
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("smart_inserts"))
    long_w["image_duration"].setValue(7.5)
    window.saved = ExportSettings(
        smart_visual_allow_generated=False,
        smart_visual_fallback="skip",
    )
    settings = window._settings()
    # The UI never edits the legacy generation fields: stored values survive.
    assert settings.smart_visual_allow_generated is False
    assert settings.smart_visual_fallback == "skip"
    assert settings.smart_visual_mode == "smart_inserts"
    assert settings.smart_visual_image_duration == pytest.approx(7.5)
    window.saved = settings
    long_w["mode"].setCurrentIndex(long_w["mode"].findData("random_only"))
    long_w["image_duration"].setValue(1.0)
    window._load_smart_visual_settings()
    assert long_w["mode"].currentData() == "smart_inserts"
    assert long_w["image_duration"].value() == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# Typewriter completion sound round trip
# ---------------------------------------------------------------------------
def test_saved_completion_settings_load_into_both_profiles(window):
    window.saved = ExportSettings(
        typewriter_intro_enabled=True,
        typewriter_hook_text="hook",
        typewriter_completion_sound_enabled=False,
        typewriter_completion_sound_preset="typewriter_return",
        typewriter_completion_sound_volume=70,
        short_typewriter_intro_enabled=True,
        short_typewriter_hook_text="hook",
        short_typewriter_completion_sound_enabled=True,
        short_typewriter_completion_sound_preset="mechanical_keypress",
        short_typewriter_completion_sound_volume=15,
    )
    window._load_typewriter_settings()
    long_w = window._typewriter_widgets["tw_long"]
    short_w = window._typewriter_widgets["tw_short"]
    assert long_w["completion_sound_enabled"].isChecked() is False
    assert long_w["completion_sound_preset"].currentData() == "typewriter_return"
    assert long_w["completion_sound_volume"].value() == 70
    assert short_w["completion_sound_enabled"].isChecked() is True
    assert short_w["completion_sound_preset"].currentData() == "mechanical_keypress"
    assert short_w["completion_sound_volume"].value() == 15


def test_ui_writes_completion_settings_per_profile(window):
    long_w = window._typewriter_widgets["tw_long"]
    long_w["completion_sound_enabled"].setChecked(False)
    long_w["completion_sound_preset"].setCurrentIndex(
        long_w["completion_sound_preset"].findData("mechanical_keypress")
    )
    long_w["completion_sound_volume"].setValue(60)
    short_w = window._typewriter_widgets["tw_short"]
    short_w["completion_sound_volume"].setValue(25)

    settings = window._settings()
    assert settings.typewriter_completion_sound_enabled is False
    assert settings.typewriter_completion_sound_preset == "mechanical_keypress"
    assert settings.typewriter_completion_sound_volume == 60
    # The Shorts profile keeps its own (default) enabled state.
    assert settings.short_typewriter_completion_sound_enabled is True
    assert settings.short_typewriter_completion_sound_preset == "enter_return"
    assert settings.short_typewriter_completion_sound_volume == 25


def test_typewriter_profile_from_ui_carries_completion_settings(window):
    long_w = window._typewriter_widgets["tw_long"]
    long_w["hook_text"].setPlainText("hook")
    long_w["completion_sound_enabled"].setChecked(True)
    long_w["completion_sound_preset"].setCurrentIndex(
        long_w["completion_sound_preset"].findData("typewriter_return")
    )
    long_w["completion_sound_volume"].setValue(33)
    profile = window._typewriter_profile_from_ui("tw_long")
    assert profile.text == "hook"
    assert profile.completion_sound_enabled is True
    assert profile.completion_sound_preset == "typewriter_return"
    assert profile.completion_sound_volume == 33


# ---------------------------------------------------------------------------
# Enriched Smart Visual Analyze Timeline (Phase 33)
# ---------------------------------------------------------------------------
def test_analyze_timeline_records_expose_rendering_fields(window, tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    folder = tmp_path / "pool"
    folder.mkdir()
    (folder / "glacier_mountain_ice.jpg").write_bytes(b"ximg")
    (folder / "river_valley_water.jpg").write_bytes(b"yimg")

    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    widgets["folders"].addItem(str(folder))
    widgets["image_duration"].setValue(4.5)
    widgets["image_transition"].setCurrentIndex(
        widgets["image_transition"].findData("film_dissolve")
    )
    widgets["image_transition_duration"].setValue(0.5)
    widgets["image_visual_effect"].setCurrentIndex(
        widgets["image_visual_effect"].findData("soft_shimmer")
    )
    widgets["image_visual_effect_intensity"].setCurrentIndex(
        widgets["image_visual_effect_intensity"].findData("medium")
    )

    # The GUI analysis must build without errors and fill the list widget
    # WITHOUT rendering anything.
    window._smart_visual_analyze_timeline("sv_long")
    assert widgets["analyze_list"].count() > 0

    # Build the exact same plan the GUI builds and check the record schema.
    from app.video_merger.smart_visuals import build_smart_visual_plan

    profile = window._smart_visual_profile_from_ui("sv_long")
    rendering = window._smart_visual_image_rendering_from_ui("sv_long")
    script = tmp_path / "script.txt"
    script.write_text(
        "The mountain glacier melts faster every year. "
        "Rivers carry the meltwater down into the valley. "
        "A totally unrelated sentence about zebras in the zoo. ",
        encoding="utf-8",
    )
    plan = build_smart_visual_plan(
        profile=profile,
        script_text=script.read_text(encoding="utf-8"),
        program_duration=24.0,
        width=1280,
        height=720,
        fps=30.0,
        cache_dir=tmp_path / "cache",
        ffprobe_path="ffprobe-not-used",
        seed_parts=("gui-test",),
        log=lambda *_a, **_k: None,
        **rendering,
    )
    records = plan.to_records()
    assert records, "the analysis must contain at least one slot"
    for record in records:
        assert record["fallback_mode"]
        assert record["duration"] > 0
        assert "match" in record and "reason" in record
        assert record["source"] in {"SMART", "RANDOM", "NONE"}
        assert record["insert_duration"] == pytest.approx(4.5)
    chosen = [record for record in records if record["selected"] != "–"]
    assert chosen
    for record in chosen:
        assert record["image_transition"] == "film_dissolve (0.50s)"
        assert record["image_effect"] == "soft_shimmer"
        assert record["image_effect_intensity"] == "medium"


def test_mode_combo_offers_all_documented_selection_modes(window):
    # Phase 33 replacement for the removed fallback-policy combo test.
    widgets = window._smart_visual_widgets["sv_long"]
    data = {widgets["mode"].itemData(i) for i in range(widgets["mode"].count())}
    assert data == {"smart_match", "smart_inserts", "random_only"}


def test_image_transition_and_effect_combos_offer_documented_choices(window):
    widgets = window._smart_visual_widgets["sv_long"]
    transitions = {
        widgets["image_transition"].itemData(i)
        for i in range(widgets["image_transition"].count())
    }
    assert {"project", "none", "cross_dissolve", "film_dissolve", "smooth_blur",
            "additive_dissolve"} <= transitions
    effects = {
        widgets["image_visual_effect"].itemData(i)
        for i in range(widgets["image_visual_effect"].count())
    }
    assert effects == {"none", "soft_shimmer", "gentle_flicker", "film_flicker",
                       "crt_broadcast", "soft_glow_pulse"}
    intensities = {
        widgets["image_visual_effect_intensity"].itemData(i)
        for i in range(widgets["image_visual_effect_intensity"].count())
    }
    assert intensities == {"low", "medium", "high"}
    completion = {
        window._typewriter_widgets["tw_long"]["completion_sound_preset"].itemData(i)
        for i in range(window._typewriter_widgets["tw_long"]["completion_sound_preset"].count())
    }
    assert completion == {"enter_return", "mechanical_keypress", "typewriter_return", "off"}
