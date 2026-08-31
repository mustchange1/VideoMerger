from __future__ import annotations

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.models import ExportSettings
from app.video_merger.settings_store import SettingsStore
from app.video_merger.target import resolve_export
from app.video_merger.transition_effects import (
    EASE_OPTIONS,
    TRANSITION_OPTIONS,
    ease_expression,
    normalize_ease,
    normalize_transition,
    transition_blur_sigma,
    xfade_expression,
)
from tests.conftest import fake_media


EXPECTED_TRANSITIONS = [
    ("smooth_blur", "Smooth Blur Crossfade"),
    ("cross_dissolve", "Cross Dissolve"),
    ("film_dissolve", "Film Dissolve"),
    ("additive_dissolve", "Additive Dissolve"),
]


def test_transition_catalog_has_exact_primary_choices_and_safe_defaults():
    assert [(key, label) for key, label, _ in TRANSITION_OPTIONS] == EXPECTED_TRANSITIONS
    assert [label for _key, label in EASE_OPTIONS] == [
        "Linear", "Ease In", "Ease Out", "Ease In + Ease Out"
    ]
    settings = ExportSettings()
    assert settings.transition_type == "cross_dissolve"
    assert settings.transition_ease == "ease_in_out"
    assert normalize_transition("rejected_unknown_transition") == "cross_dissolve"
    assert normalize_ease("rejected_unknown_curve") == "ease_in_out"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("linear", "P"),
        ("ease_in", "P*P"),
        ("ease_out", "1-(1-P)*(1-P)"),
        ("ease_in_out", "P*P*(3-2*P)"),
    ],
)
def test_easing_expressions(name, expected):
    assert ease_expression(name) == expected


def test_visual_expressions_are_tasteful_and_plane_aware():
    cross = xfade_expression("cross_dissolve", "ease_in_out")
    film = xfade_expression("film_dissolve", "ease_in_out")
    additive = xfade_expression("additive_dissolve", "ease_in_out")
    assert cross == xfade_expression("smooth_blur", "ease_in_out")
    assert "pow(" not in cross and "min(255" not in cross
    assert "if(eq(PLANE,0)" in film and "pow(" in film
    assert "if(eq(PLANE,0)" in additive and "0.08*min(A,B)" in additive
    assert "flash" not in additive.lower()


def test_transition_and_easing_selection_persist_in_settings(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    selected = ExportSettings(transition_type="film_dissolve", transition_ease="ease_out")
    store.save(selected)
    loaded = SettingsStore(tmp_path / "settings.json").load()
    assert loaded.transition_type == "film_dissolve"
    assert loaded.transition_ease == "ease_out"


def test_transition_specific_blur_is_bounded():
    assert transition_blur_sigma("cross_dissolve", 1920, 1080) == 0
    assert transition_blur_sigma("additive_dissolve", 1920, 1080) == 0
    assert 1.5 <= transition_blur_sigma("film_dissolve", 1920, 1080) <= 4.0
    assert 8.0 <= transition_blur_sigma("smooth_blur", 1920, 1080) <= 24.0


@pytest.mark.parametrize("transition", [key for key, _label in EXPECTED_TRANSITIONS])
def test_all_transition_graphs_use_prepared_canvases_and_same_audio_path(transition):
    media = [fake_media("A.mp4", 1920, 1080), fake_media("B.mp4", 1080, 1920)]
    settings = ExportSettings(
        resolution="1920x1080", transition_type=transition,
        transition_ease="ease_in_out", transition_duration=0.5,
    )
    resolved = resolve_export(media, settings)
    graph = FFmpegCommandBuilder("ffmpeg").build_filter_graph(media, settings, resolved)
    assert graph.count("scale=w=1920:h=1080") >= 2
    assert graph.index("scale=w=1920:h=1080") < graph.index("xfade=transition=custom")
    assert ":expr='" + xfade_expression(transition, "ease_in_out") + "'" in graph
    assert "acrossfade=d=0.5:c1=tri:c2=tri" in graph
    if transition in {"smooth_blur", "film_dissolve"}:
        assert "[blurin" in graph
    else:
        # A contain-mode background may itself use gblur; these modes must not
        # add transition blur streams.
        assert "[blurin" not in graph
