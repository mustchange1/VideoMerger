"""Asset tests: VM Automatic Windows launcher, setup, docs and README."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_cmd_exists_and_references_runner():
    cmd = ROOT / "VM Automatic starten.cmd"
    assert cmd.is_file()
    text = cmd.read_text(encoding="utf-8-sig")
    assert "run_vm_automatic.ps1" in text


def test_powershell_scripts_exist_with_utf8_bom_and_crlf():
    for name in ("setup_vm_automatic.ps1", "run_vm_automatic.ps1"):
        raw = (ROOT / name).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{name} needs a UTF-8 BOM"
        assert b"\r\n" in raw, f"{name} needs CRLF line endings (Windows PowerShell)"


def test_run_script_launches_vm_automatic_module():
    text = (ROOT / "run_vm_automatic.ps1").read_text(encoding="utf-8-sig")
    assert "vm_automatic" in text
    assert "pythonw" in text.lower()


def test_setup_script_checks_watchdog_dependency():
    text = (ROOT / "setup_vm_automatic.ps1").read_text(encoding="utf-8-sig")
    assert "watchdog" in text


def test_readme_de_exists_and_starts_with_required_headline():
    readme = ROOT / "README_VM_AUTOMATIC_DE.md"
    assert readme.is_file()
    first_line = readme.read_text(encoding="utf-8-sig").splitlines()[0].strip()
    assert first_line.startswith("SO STARTEN SIE VM AUTOMATIC")
    text = readme.read_text(encoding="utf-8-sig")
    # the required topic must be stated explicitly
    assert "ersetzt VideoMerger nicht" in text or "nicht ersetzt" in text


def test_requirements_include_watchdog():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "watchdog" in text
