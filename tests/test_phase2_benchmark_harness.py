from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "phase2_windows_baseline.ps1"
DOCUMENTATION = ROOT / "docs" / "PERFORMANCE_PHASE2_WINDOWS_BASELINE.md"


def test_phase2_windows_benchmark_assets_are_distributed_and_self_contained():
    script = SCRIPT.read_text(encoding="utf-8")
    documentation = DOCUMENTATION.read_text(encoding="utf-8")

    for marker in (
        "baseline.result.json",
        "Win32_ProcessStartTrace",
        "Win32_ProcessStopTrace",
        "discovered_pool_count",
        "selected_clip_count",
        "total_wall_clock_seconds",
        "ffmpeg_process_count",
        "ffprobe_process_count",
        "full_video_encode_count",
        "stream_copy_assembly_count",
        "per_chunk_runtime",
        "chunk_count",
        "stage1_runtime_seconds",
        "subtitle_burn_runtime_seconds",
        "stage2_runtime_seconds",
        "validation_runtime_seconds",
        "peak_ram_bytes",
        "output_facts",
        "'not measured'",
    ):
        assert marker in script

    assert "scripts/phase2_windows_baseline.ps1" in documentation
    assert "baseline.result.json" in documentation
    assert "python -u -m app.cli --stage complete" in documentation
    assert "/tmp/" not in script
    assert "/home/" not in script
