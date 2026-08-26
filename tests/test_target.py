import pytest

from app.video_merger.models import ExportSettings
from app.video_merger.target import choose_fps, parse_resolution, resolve_export, safe_transition_durations
from tests.conftest import fake_media


def test_parse_resolution_is_even_and_valid():
    assert parse_resolution("1921x1081") == (1920, 1080)
    with pytest.raises(Exception):
        parse_resolution("nope")


def test_auto_keeps_uniform_full_hd():
    resolved = resolve_export([fake_media(), fake_media("b.mp4")], ExportSettings(aspect="16:9"))
    assert (resolved.width, resolved.height) == (1920, 1080)


def test_vertical_auto_and_aspect_validation():
    media = [fake_media(width=1080, height=1920)]
    resolved = resolve_export(media, ExportSettings(aspect="9:16"))
    assert (resolved.width, resolved.height) == (1080, 1920)
    with pytest.raises(Exception, match="Höhe"):
        resolve_export(media, ExportSettings(aspect="9:16", resolution="1920x1080"))


def test_mixed_fps_prefers_30_and_family_cadence():
    assert choose_fps([fake_media(fps=24), fake_media("b", fps=60)], "Auto") == (30.0, "30")
    assert choose_fps([fake_media(fps=25), fake_media("b", fps=50)], "Auto") == (25.0, "25")


def test_short_clips_get_safe_transition():
    effective, transitions = safe_transition_durations([0.03, 0.4, 2.0], 1.0, 30)
    assert effective[0] >= 0.12
    assert transitions[0] < effective[0] / 2
    assert transitions[1] < effective[1] / 2


def test_expected_duration_accounts_for_overlaps():
    media = [fake_media(duration=2), fake_media("b", duration=3), fake_media("c", duration=4)]
    resolved = resolve_export(media, ExportSettings(transition_duration=0.5))
    assert resolved.expected_duration == pytest.approx(8.0)
