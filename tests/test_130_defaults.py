"""Current defaults and preserved application behavior."""

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


def test_quote_flyer_defaults():
    settings = ExportSettings()
    assert settings.quote_enabled is False
    assert settings.quote_input_mode == "artwork"
    assert settings.quote_artwork_path == ""
    assert settings.quote_pdf_page == 1
    assert settings.quote_artwork_fit_mode == "fit"
    assert settings.quote_duration == 2.0


def test_transition_and_music_defaults_are_cinematic_but_safe():
    settings = ExportSettings()
    assert settings.transition_type == "cross_dissolve"
    assert settings.transition_duration == 1.0
    assert settings.short_video_mode == "hold"
    assert settings.normalize_audio is True
    assert settings.ducking_enabled is True
    assert settings.music_volume == 44
    assert settings.music_preset == "balanced"
    assert settings.voiceover_volume == 100
    assert settings.subtitle_language == "German"
    assert settings.subtitle_debug_overlay is False
    assert settings.watermark_enabled is False
    assert settings.script_mode == "single"
