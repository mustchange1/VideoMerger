from app.video_merger import hardware


def test_auto_selects_first_runtime_verified_hardware(monkeypatch):
    monkeypatch.setattr(hardware, "available_encoders", lambda _path: {
        "libx264": True, "h264_nvenc": False, "h264_qsv": True, "h264_amf": False,
    })
    encoder, label, warnings = hardware.resolve_encoder("ffmpeg", "Auto")
    assert encoder == "h264_qsv"
    assert label == "Intel Quick Sync"
    assert warnings == []


def test_unavailable_explicit_hardware_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(hardware, "available_encoders", lambda _path: {
        "libx264": True, "h264_nvenc": False, "h264_qsv": False, "h264_amf": False,
    })
    encoder, label, warnings = hardware.resolve_encoder("ffmpeg", "NVIDIA NVENC")
    assert encoder == "libx264"
    assert label == "CPU (libx264)"
    assert warnings and "CPU-Fallback" in warnings[0]


def test_hardware_and_cpu_encoding_arguments_are_real():
    assert "h264_nvenc" in hardware.encoder_arguments("h264_nvenc", 18, "slow")
    assert "h264_qsv" in hardware.encoder_arguments("h264_qsv", 18, "medium")
    assert "h264_amf" in hardware.encoder_arguments("h264_amf", 18, "fast")
    cpu = hardware.encoder_arguments("libx264", 18, "slow")
    assert cpu[cpu.index("-c:v") + 1] == "libx264"
    assert cpu[cpu.index("-preset") + 1] == "slow"
    assert cpu[cpu.index("-crf") + 1] == "18"
