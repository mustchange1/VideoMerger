from datetime import datetime

from app.video_merger.output_manager import make_output_path, sanitize_filename


def test_generated_output_filename(tmp_path):
    result = make_output_path(tmp_path, "9:16", now=datetime(2026, 8, 23, 11, 30, 15))
    assert result.name == "merged_9x16_2026-08-23_11-30-15.mp4"


def test_custom_filename_is_windows_safe(tmp_path):
    result = make_output_path(tmp_path, "16:9", 'Mein:Film?*.mp4')
    assert result.name == "Mein_Film__.mp4"
    assert sanitize_filename("CON") == "_CON"


def test_existing_file_is_not_overwritten(tmp_path):
    first = make_output_path(tmp_path, "16:9", "film")
    first.touch()
    second = make_output_path(tmp_path, "16:9", "film")
    assert second.name == "film_2.mp4"
