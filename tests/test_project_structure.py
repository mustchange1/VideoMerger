from __future__ import annotations

import os
from pathlib import Path

from app.video_merger.paths import locate_ffmpeg, project_root


ROOT = Path(__file__).resolve().parents[1]


def test_single_project_root_contains_every_documented_entry_point():
    required = (
        "PROJECT_ROOT.txt",
        "setup_windows.ps1",
        "run_windows.ps1",
        "diagnostics_windows.ps1",
        "README.md",
        "README_DE.md",
        "requirements.txt",
        "app/main.py",
    )
    assert project_root() == ROOT
    for relative in required:
        assert (ROOT / relative).is_file(), relative


def test_project_root_does_not_depend_on_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert project_root() == ROOT


def test_explicit_local_ffmpeg_directory_is_resolved_independent_of_cwd(tmp_path, monkeypatch):
    binary_dir = tmp_path / "ÄÖÜ (Local Tools)" / "bin"
    binary_dir.mkdir(parents=True)
    ffmpeg_name, ffprobe_name = ("ffmpeg.exe", "ffprobe.exe") if os.name == "nt" else ("ffmpeg", "ffprobe")
    (binary_dir / ffmpeg_name).write_bytes(b"binary")
    (binary_dir / ffprobe_name).write_bytes(b"binary")
    monkeypatch.setenv("VIDEOMERGER_FFMPEG_DIR", str(binary_dir))
    monkeypatch.chdir(tmp_path)
    ffmpeg, ffprobe = locate_ffmpeg()
    assert ffmpeg == (binary_dir / ffmpeg_name).resolve()
    assert ffprobe == (binary_dir / ffprobe_name).resolve()


def test_windows_scripts_resolve_root_from_their_own_file_location():
    for name in ("setup_windows.ps1", "run_windows.ps1", "diagnostics_windows.ps1"):
        text = (ROOT / name).read_bytes().decode("utf-8-sig")
        assert "$MyInvocation.MyCommand.Path" in text
        assert "PROJECT_ROOT.txt" in text
