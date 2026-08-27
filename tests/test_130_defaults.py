"""1.3.0 – All preserved defaults (§14 of the release specification).

Every good 1.2.4 default must remain exactly as it was; the new 1.3.0
features add their own defaults on top (Cut Last Clip, 10 % stretch limit,
1.00x speed, ~1 s end padding, Quote disabled / 2.0 s / cleanest style).
"""

from __future__ import annotations

from app.video_merger.models import ExportSettings


def test_audio_mode_defaults_remain_original():
    settings = ExportSettings()
    assert settings.original_audio_mode == "original"
    assert settings.intro_audio_mode == "original"
    assert settings.outro_audio_mode == "original"


def test_subtitle_animation_default_remains_static_phrase():
    assert ExportSettings().subtitle_animation == "static_phrase"


def test_output_preset_and_quality_defaults_remain():
    settings = ExportSettings()
    assert settings.output_preset == "youtube_landscape"
    assert settings.quality_preset == "maximum"
    assert settings.subtitle_style == "long_1"
    assert settings.subtitle_font == "modern_sans_bold"
    assert settings.subtitle_position == "Bottom"
    assert settings.aspect == "16:9"


def test_main_video_end_padding_default_remains_one_second():
    assert ExportSettings().final_pause == 1.0


def test_duration_fit_defaults():
    settings = ExportSettings()
    assert settings.duration_fit_mode == "cut"          # Cut Last Clip
    assert settings.max_stretch_percent == 10.0          # 10 % default limit


def test_global_video_speed_default_is_one():
    assert ExportSettings().video_speed == 1.0


def test_quote_card_defaults():
    settings = ExportSettings()
    assert settings.quote_enabled is False               # disabled unless enabled
    assert settings.quote_duration == 2.0                # 2.0 s
    assert settings.quote_style == "clean_editorial"     # cleanest/most readable
    assert settings.quote_zoom_percent == 4.0            # subtle zoom
    assert settings.quote_font_weight == "bold"
    assert settings.quote_position == "center"
    assert settings.quote_safe_padding_percent == 8.0
    assert settings.quote_transition_duration == 0.0     # global transition applies
    assert settings.quote_text == ""


def test_existing_behavior_defaults_are_untouched():
    settings = ExportSettings()
    assert settings.transition_type == "smooth_blur"
    assert settings.transition_duration == 0.5
    assert settings.short_video_mode == "hold"
    assert settings.normalize_audio is True
    assert settings.ducking_enabled is True
    assert settings.music_volume == 22
    assert settings.voiceover_volume == 100
    assert settings.subtitle_language == "German"
    assert settings.subtitle_debug_overlay is False
    assert settings.watermark_enabled is False
    assert settings.script_mode == "single"
