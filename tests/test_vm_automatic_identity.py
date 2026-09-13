"""Unit tests: VM Automatic job identity + audio/script pairing."""
from __future__ import annotations

from pathlib import Path


from app.vm_automatic.identity import (
    is_audio_name, is_ignored_name, is_script_name, normalize_stem,
)
from app.vm_automatic.pairing import scan_watch_folder


# ---------------------------------------------------------------------- #
# Identity
# ---------------------------------------------------------------------- #
def test_normalize_stem_basic():
    assert normalize_stem("Topic_001.mp3") == "topic_001"
    assert normalize_stem("Topic_001.txt") == "topic_001"
    assert normalize_stem("TOPIC_002.WAV") == "topic_002"
    assert normalize_stem("My Script.text") == "my script"


def test_audio_whitelist_matches_videomerger_formats():
    for name in ("a.mp3", "a.wav", "a.m4a", "a.aac", "a.flac", "a.ogg", "a.opus"):
        assert is_audio_name(name), name
    assert not is_audio_name("a.txt")
    assert not is_audio_name("a.mp4")


def test_script_whitelist_matches_videomerger_formats():
    for name in ("a.txt", "a.text", "a.md"):
        assert is_script_name(name), name
    assert not is_script_name("a.mp3")


def test_ignored_names_cover_temp_hidden_and_generated():
    for name in (
        "job.mp3.tmp", "job.mp3.part", "job.mp3.crdownload",
        ".hidden.mp3", "~$script.txt", "merged_16x9_x.mp4",
        "MainVideo_16x9.mp4", "FinalVideo_16x9.mp4", "preview_transition_x.mp4",
        "job.mp3.download",
    ):
        assert is_ignored_name(name), name
    assert not is_ignored_name("Topic_001.mp3")
    # configurable extra markers
    assert is_ignored_name("job.mp3.draft", extra_markers=[".draft"])


# ---------------------------------------------------------------------- #
# Pairing
# ---------------------------------------------------------------------- #
def _drop(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"x")


def test_pairing_matching_audio_and_script(tmp_path):
    _drop(tmp_path, "Topic_001.mp3", "Topic_001.txt")
    pairings = scan_watch_folder(tmp_path)
    assert set(pairings) == {"topic_001"}
    pairing = pairings["topic_001"]
    assert pairing.audio is not None and pairing.audio.name == "Topic_001.mp3"
    assert pairing.script is not None and pairing.script.name == "Topic_001.txt"
    assert pairing.complete
    assert pairing.state_label == "READY"
    assert pairing.display_id == "Topic_001"


def test_pairing_case_insensitive_stem(tmp_path):
    _drop(tmp_path, "Topic_001.mp3", "topic_001.txt")
    pairings = scan_watch_folder(tmp_path)
    assert pairings["topic_001"].complete


def test_pairing_missing_script(tmp_path):
    _drop(tmp_path, "Topic_001.mp3")
    pairings = scan_watch_folder(tmp_path)
    pairing = pairings["topic_001"]
    assert pairing.audio is not None
    assert pairing.script is None
    assert not pairing.complete
    assert pairing.state_label == "WAITING_FOR_SCRIPT"


def test_pairing_missing_audio(tmp_path):
    _drop(tmp_path, "Topic_001.txt")
    pairings = scan_watch_folder(tmp_path)
    pairing = pairings["topic_001"]
    assert pairing.audio is None
    assert pairing.script is not None
    assert pairing.state_label == "WAITING_FOR_AUDIO"


def test_pairing_mismatched_names_never_merge(tmp_path):
    _drop(tmp_path, "Topic_001.mp3", "Topic_2.txt")
    pairings = scan_watch_folder(tmp_path)
    assert set(pairings) == {"topic_001", "topic_2"}
    assert pairings["topic_001"].state_label == "WAITING_FOR_SCRIPT"
    assert pairings["topic_2"].state_label == "WAITING_FOR_AUDIO"
    # no accidental pairing: neither job is complete
    assert not pairings["topic_001"].complete and not pairings["topic_2"].complete


def test_pairing_multiple_jobs(tmp_path):
    _drop(
        tmp_path,
        "Job_001.mp3", "Job_001.txt",
        "Job_002.mp3", "Job_002.txt",
        "Job_003.wav", "Job_003.md",
        "Job_010.mp3", "Job_010.txt",
    )
    pairings = scan_watch_folder(tmp_path)
    assert set(pairings) == {"job_001", "job_002", "job_003", "job_010"}
    assert all(p.complete for p in pairings.values())


def test_pairing_conflicts_recorded_not_merged(tmp_path):
    _drop(tmp_path, "Job_001.mp3", "Job_001.wav", "Job_001.txt")
    pairings = scan_watch_folder(tmp_path)
    pairing = pairings["job_001"]
    assert len(pairing.audio_conflicts) == 1
    assert pairing.state_label == "CONFLICT"


def test_pairing_temp_files_ignored(tmp_path):
    _drop(tmp_path, "Job_001.mp3", "Job_001.mp3.tmp", "Job_001.txt", "scratch.jpg")
    pairings = scan_watch_folder(tmp_path)
    assert set(pairings) == {"job_001"}
    assert pairings["job_001"].complete


def test_pairing_empty_folder(tmp_path):
    assert scan_watch_folder(tmp_path) == {}


def test_pairing_nested_folders_not_joined(tmp_path):
    _drop(tmp_path / "nested", "Job_001.mp3")
    _drop(tmp_path, "Job_001.txt")
    pairings = scan_watch_folder(tmp_path)
    assert set(pairings) == {"job_001"}
    assert pairings["job_001"].audio is None  # nested file is not a job part
