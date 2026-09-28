"""Phase-35 gating, persistence and cache-isolation regressions."""
from __future__ import annotations

import json

from app.video_merger.models import ExportSettings
from app.video_merger.settings_store import SettingsStore
from app.video_merger.smart_visuals import smart_visual_profile_from_settings
from app.video_merger.youtube_outputs import ShortJob, short_settings


def test_old_project_loads_without_phase35_state(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"smart_visual_enabled": False}), encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.smart_visual_timeline_mode == "auto"
    assert loaded.smart_visual_manual_overrides == {}
    assert loaded.smart_visual_timeline_confirmed is False


def test_phase35_state_roundtrips_separately(tmp_path):
    path = tmp_path / "settings.json"
    original = ExportSettings(
        smart_visual_enabled=True,
        smart_visual_timeline_mode="hybrid",
        smart_visual_manual_overrides={"2": {"start": 3.25, "duration": 1.5}},
        smart_visual_timeline_confirmed=True,
    )
    SettingsStore(path).save(original)
    loaded = SettingsStore(path).load()
    assert loaded.smart_visual_timeline_mode == "hybrid"
    assert loaded.smart_visual_manual_overrides == original.smart_visual_manual_overrides
    assert loaded.smart_visual_timeline_confirmed is True


def test_disabled_profile_keeps_phase35_inactive_even_with_stale_overrides():
    profile = smart_visual_profile_from_settings(ExportSettings(
        smart_visual_enabled=False,
        smart_visual_manual_overrides={"0": {"start": 9.0}},
    ))
    assert profile.active is False
    assert profile.manual_overrides == {"0": {"start": 9.0}}


def test_long_form_and_shorts_manual_state_do_not_leak(tmp_path):
    voice = tmp_path / "voice.wav"
    voice.touch()
    base = ExportSettings(
        voiceover_paths=[str(voice)],
        smart_visual_timeline_mode="hybrid",
        smart_visual_manual_overrides={"0": {"start": 1.0}},
        smart_visual_timeline_confirmed=True,
        shorts_smart_visual_timeline_mode="manual",
        shorts_smart_visual_manual_overrides={"1": {"removed": True}},
        shorts_smart_visual_timeline_confirmed=False,
    )
    job = ShortJob(
        index=1, voiceover_path=voice, script_path=None,
        output_name="Short_001", cache_key="one",
    )
    short = short_settings(base, job)
    assert short.smart_visual_timeline_mode == "manual"
    assert short.smart_visual_manual_overrides == {"1": {"removed": True}}
    assert short.smart_visual_timeline_confirmed is False
    assert base.smart_visual_manual_overrides == {"0": {"start": 1.0}}
