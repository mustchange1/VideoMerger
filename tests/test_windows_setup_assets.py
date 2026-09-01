from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import app.selftest as selftest_module


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


def test_setup_selftest_handles_historical_subtitle_fixture_explicitly():
    selftest = (ROOT / "app" / "selftest.py").read_text(encoding="utf-8")
    test_windows = (ROOT / "test_windows.ps1").read_bytes().decode("utf-8-sig")
    assert '"test_evidence" / "1.2.1" / "subtitle_workflow" / "assets"' in selftest
    assert "KnownVoiceover.wav" in selftest
    assert "background.mp4" in selftest
    assert "Historical release subtitle fixture unavailable — application runtime is unaffected." in selftest
    assert "[SKIP]" in selftest
    assert "[FAIL]" in selftest
    assert "[OK]" in selftest
    assert "Subtitle End-to-End" in selftest
    assert "subtitles=filename=" in selftest
    assert "VIDEOMERGER_TEST_REAL_ALIGNMENT" in test_windows
    assert "VIDEOMERGER_RUN_2MIN_BENCHMARK" in test_windows


def _historical_assets(root: Path) -> Path:
    return root / "test_evidence" / "1.2.1" / "subtitle_workflow" / "assets"


def _successful_subtitle_result(root: Path) -> SimpleNamespace:
    output = root / "subtitle-selftest"
    paths = [
        output / "MainVideo.mp4",
        output / "MainVideo.srt",
        output / "MainVideo.vtt",
        output / "MainVideo.timeline.json",
        output / "MainVideo_no_subtitles.mp4",
    ]
    frames = [output / name for name in ("first.png", "middle.png", "final.png")]
    return SimpleNamespace(
        video=paths[0], srt=paths[1], vtt=paths[2], canonical_timeline=paths[3],
        video_no_subtitles=paths[4], verification_frames=frames,
    )


def test_historical_fixture_present_keeps_subtitle_e2e_path_active(monkeypatch, tmp_path, capsys):
    assets = _historical_assets(tmp_path)
    fixture_paths = {
        assets / "KnownVoiceover.wav", assets / "background.mp4", assets / "script.txt",
    }
    output_dir = tmp_path / "subtitle-selftest"
    monkeypatch.setattr(
        Path, "is_file", lambda path: path in fixture_paths or path.parent == output_dir,
    )
    calls = {}

    class FakeEngine:
        last_filter_graph = "[v]subtitles=filename=fixture[vout]"

        def analyze(self, paths):
            calls["analyze"] = paths
            return ["analyzed media"]

    class FakeMainProjectEngine:
        def __init__(self, engine):
            calls["engine"] = engine

        def create_main(self, media, settings, output):
            calls["create_main"] = (media, settings, output)
            return _successful_subtitle_result(tmp_path)

    monkeypatch.setattr(selftest_module, "project_root", lambda: tmp_path)
    monkeypatch.setattr(selftest_module, "MainProjectEngine", FakeMainProjectEngine)
    assert selftest_module._run_historical_subtitle_e2e(FakeEngine(), tmp_path)
    assert calls["analyze"] == [assets / "background.mp4"]
    assert calls["create_main"][0] == ["analyzed media"]
    assert calls["create_main"][1].voiceover_path == str(assets / "KnownVoiceover.wav")
    assert calls["create_main"][1].script_path == str(assets / "script.txt")
    assert "[OK] Subtitle End-to-End" in capsys.readouterr().out


def test_historical_fixture_absent_is_explicit_skip_and_setup_can_continue(monkeypatch, tmp_path, capsys):
    class UnexpectedEngine:
        last_filter_graph = ""

        def analyze(self, _paths):
            raise AssertionError("subtitle E2E must not run without historical fixture")

    monkeypatch.setattr(selftest_module, "project_root", lambda: tmp_path)
    assert selftest_module._run_historical_subtitle_e2e(UnexpectedEngine(), tmp_path)
    output = capsys.readouterr().out
    assert "[SKIP]" in output
    assert "Historical release subtitle fixture unavailable — application runtime is unaffected." in output


def test_unrelated_selftest_failure_remains_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        selftest_module,
        "run_diagnostics",
        lambda test_encoders=False: [SimpleNamespace(ok=False, name="FFmpeg", detail="broken")],
    )
    assert selftest_module.main() == 1
    assert "[FAIL] FFmpeg: broken" in capsys.readouterr().out


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
