"""Phase 32 GUI tests: fallback policy, image rendering + completion sound.

Verifies the new Smart Visual controls (Allow Generated Images, fallback
policy, image transition + duration, image visual effect + intensity) and
the new Typewriter completion-sound controls: presence, defaults, gating,
per-output separation, settings round trip and enriched plan previews.
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


SMART_PHASE32_KEYS = (
    "allow_generated", "fallback",
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


def test_fresh_window_defaults_preserve_phase31_behavior(window):
    for prefix in ("sv_long", "sv_short"):
        widgets = window._smart_visual_widgets[prefix]
        assert widgets["allow_generated"].isChecked() is True
        assert widgets["fallback"].currentData() == "generate_image"
        assert widgets["image_transition"].currentData() == "project"
        assert widgets["image_transition_duration"].value() == pytest.approx(0.0)
        assert widgets["image_visual_effect"].currentData() == "none"
        assert widgets["image_visual_effect_intensity"].currentData() == "low"
    settings = window._settings()
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
def test_allow_generated_off_locks_generation_only_controls(window):
    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    assert widgets["strategy"].isEnabled() is True
    assert widgets["style"].isEnabled() is True
    widgets["allow_generated"].setChecked(False)
    assert widgets["strategy"].isEnabled() is False
    assert widgets["style"].isEnabled() is False
    assert widgets["fallback"].isEnabled() is True
    widgets["allow_generated"].setChecked(True)
    assert widgets["strategy"].isEnabled() is True


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


def test_disabled_smart_visuals_lock_phase32_controls(window):
    widgets = window._smart_visual_widgets["sv_short"]
    widgets["enabled"].setChecked(False)
    assert widgets["fallback"].isEnabled() is False
    assert widgets["image_transition"].isEnabled() is False
    assert widgets["image_visual_effect"].isEnabled() is False


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
    assert long_w["allow_generated"].isChecked() is False
    assert long_w["fallback"].currentData() == "random_video"
    assert long_w["image_transition"].currentData() == "film_dissolve"
    assert long_w["image_transition_duration"].value() == pytest.approx(0.6)
    assert long_w["image_visual_effect"].currentData() == "soft_shimmer"
    assert long_w["image_visual_effect_intensity"].currentData() == "medium"
    assert short_w["allow_generated"].isChecked() is True
    assert short_w["fallback"].currentData() == "skip"
    assert short_w["image_transition"].currentData() == "smooth_blur"
    assert short_w["image_transition_duration"].value() == pytest.approx(0.3)
    assert short_w["image_visual_effect"].currentData() == "crt_broadcast"
    assert short_w["image_visual_effect_intensity"].currentData() == "high"


def test_ui_writes_phase32_settings_per_profile(window):
    long_w = window._smart_visual_widgets["sv_long"]
    long_w["enabled"].setChecked(True)
    long_w["allow_generated"].setChecked(False)
    long_w["fallback"].setCurrentIndex(long_w["fallback"].findData("random_image"))
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
    short_w["fallback"].setCurrentIndex(short_w["fallback"].findData("best_available"))

    settings = window._settings()
    assert settings.smart_visual_allow_generated is False
    assert settings.smart_visual_fallback == "random_image"
    assert settings.long_form_image_transition_type == "additive_dissolve"
    assert settings.long_form_image_transition_duration == pytest.approx(0.8)
    assert settings.long_form_image_visual_effect == "gentle_flicker"
    assert settings.long_form_image_visual_effect_intensity == "high"
    assert settings.shorts_smart_visual_fallback == "best_available"
    # Shorts image rendering untouched by the Long-Form edits above.
    assert settings.shorts_image_transition_type == "project"
    assert settings.shorts_image_visual_effect == "none"


def test_ui_round_trip_keeps_phase32_values(window):
    long_w = window._smart_visual_widgets["sv_long"]
    long_w["enabled"].setChecked(True)
    long_w["fallback"].setCurrentIndex(long_w["fallback"].findData("random_video"))
    settings = window._settings()
    window.saved = settings
    long_w["fallback"].setCurrentIndex(long_w["fallback"].findData("skip"))
    window._load_smart_visual_settings()
    assert long_w["fallback"].currentData() == "random_video"


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
# Enriched Smart Visual plan preview (Feature E)
# ---------------------------------------------------------------------------
def test_plan_preview_records_expose_phase32_fields(window, tmp_path, monkeypatch):
    import app.video_merger.paths as paths_module
    import app.video_merger.smart_visuals as sv

    monkeypatch.setattr(paths_module, "project_root", lambda: tmp_path)
    folder = tmp_path / "pool"
    folder.mkdir()
    (folder / "glacier_mountain_ice.jpg").write_bytes(b"ximg")

    import app.video_merger.image_generation as ig

    monkeypatch.setattr(ig, "resolve_generation_provider", lambda ffmpeg_path=None: (None, ["test"]))

    widgets = window._smart_visual_widgets["sv_long"]
    widgets["enabled"].setChecked(True)
    widgets["folders"].addItem(str(folder))
    widgets["threshold_mode"].setCurrentIndex(widgets["threshold_mode"].findData("custom"))
    widgets["threshold_custom"].setValue(0.99)  # force fallbacks
    widgets["fallback"].setCurrentIndex(widgets["fallback"].findData("random_image"))
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

    # The GUI preview must build without errors and fill the list widget.
    window._smart_visual_preview_plan("sv_long")
    assert widgets["plan_list"].count() > 0

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
    assert records, "the preview must contain at least one slot"
    for record in records:
        assert record["fallback_mode"]
        assert record["duration"] > 0
        assert "match" in record and "reason" in record
    chosen = [record for record in records if record["selected"] != "–"]
    assert chosen
    for record in chosen:
        assert record["image_transition"] == "film_dissolve (0.50s)"
        assert record["image_effect"] == "soft_shimmer"
        assert record["image_effect_intensity"] == "medium"


def test_fallback_combo_offers_all_documented_policies(window):
    widgets = window._smart_visual_widgets["sv_long"]
    data = {widgets["fallback"].itemData(i) for i in range(widgets["fallback"].count())}
    assert data == {"generate_image", "random_video", "random_image", "skip", "best_available"}


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
