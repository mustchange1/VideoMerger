"""Current defaults and preserved application behavior."""

from __future__ import annotations

from app.video_merger.models import ExportSettings, MediaInfo


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
    assert settings.subtitle_position == "Center"
    assert settings.aspect == "16:9"


def test_main_video_end_padding_default_remains_one_second():
    assert ExportSettings().final_pause == 1.0


def test_duration_fit_defaults():
    settings = ExportSettings()
    assert settings.duration_fit_mode == "cut"          # Cut Last Clip
    assert settings.max_stretch_percent == 10.0          # 10 % default limit


def test_global_video_speed_default_is_one():
    assert ExportSettings().video_speed == 1.0


def test_quote_flyer_section_is_removed_and_add_image_remains():
    """The Quote/Flyer PDF feature is gone; Add Image keeps its full control set."""
    settings = ExportSettings()
    for removed in (
        "quote_enabled", "quote_input_mode", "quote_artwork_path",
        "quote_pdf_page", "quote_artwork_fit_mode", "quote_duration",
    ):
        assert not hasattr(settings, removed)
    assert not any("quote" in name for name in ExportSettings.__dataclass_fields__)
    assert not any("quote" in name for name in MediaInfo.__dataclass_fields__)
    assert settings.image_enabled is False
    assert settings.image_path == ""
    assert settings.image_position == "after_intro"
    assert settings.image_duration == 4.0
    assert settings.image_fit_mode == "fit"


def test_long_form_and_shorts_music_are_separate_settings():
    """Two independent tracks; an empty Shorts track means a silent Short."""
    settings = ExportSettings()
    assert settings.music_path == ""
    assert settings.short_music_path == ""
    assert settings.music_volume == 44
    assert settings.music_preset == "balanced"


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
