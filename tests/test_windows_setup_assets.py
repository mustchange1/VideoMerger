from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_setup_contains_valid_pyside6_verification_and_expression_executes():
    script = (ROOT / "setup_windows.ps1").read_bytes().decode("utf-8-sig")
    expected = '& $VenvPython -c "import PySide6; print(\'PySide6 OK:\', PySide6.__version__)"'
    broken = "import PySide6; print(PySide6 "
    assert expected in script
    assert broken not in script
    expression = "import PySide6; print('PySide6 OK:', PySide6.__version__)"
    result = subprocess.run(
        [sys.executable, "-c", expression],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("PySide6 OK: ")


def test_windows_setup_prepares_project_local_alignment_model_and_cache():
    setup = (ROOT / "setup_windows.ps1").read_bytes().decode("utf-8-sig")
    run = (ROOT / "run_windows.ps1").read_bytes().decode("utf-8-sig")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "faster-whisper" in requirements
    assert "tools\\alignment_models" in setup
    assert "WhisperModel('small'" in setup
    assert "$env:HF_HOME" in setup and "$env:HF_HOME" in run
    assert "OpenAI API" not in setup


def test_setup_selftest_includes_real_subtitle_workflow_fixture():
    selftest = (ROOT / "app" / "selftest.py").read_text(encoding="utf-8")
    test_windows = (ROOT / "test_windows.ps1").read_bytes().decode("utf-8-sig")
    evidence = ROOT / "test_evidence" / "1.2.2" / "subtitle_workflow" / "assets"
    assert all((evidence / name).is_file() for name in (
        "KnownVoiceover.wav", "script.txt", "background.mp4"
    ))
    assert "Subtitle End-to-End" in selftest
    assert "subtitles=filename=" in selftest
    assert "VIDEOMERGER_TEST_REAL_ALIGNMENT" in test_windows
    assert "VIDEOMERGER_RUN_2MIN_BENCHMARK" in test_windows


def test_windows_powershell_scripts_use_utf8_bom_crlf_and_intact_german():
    for relative in ("setup_windows.ps1", "run_windows.ps1", "diagnostics_windows.ps1"):
        raw = (ROOT / relative).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
        text = raw.decode("utf-8-sig")
        for character in "äöüÄÖÜß":
            assert character in text
        for broken in ("Ã¤", "Ã¶", "Ã¼"):
            assert broken not in text
