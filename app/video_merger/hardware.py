from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from .platform_utils import hidden_process_flags, safe_subprocess_env

ENCODER_MAP = {
    "CPU": ("libx264", "CPU (libx264)"),
    "NVIDIA NVENC": ("h264_nvenc", "NVIDIA NVENC"),
    "Intel Quick Sync": ("h264_qsv", "Intel Quick Sync"),
    "AMD AMF": ("h264_amf", "AMD AMF"),
}


@functools.lru_cache(maxsize=8)
def available_encoders(ffmpeg_path_text: str) -> dict[str, bool]:
    """Runtime-test encoders instead of trusting only `ffmpeg -encoders`.

    FFmpeg builds often list hardware encoders even when no compatible GPU or
    driver is present. A one-frame test prevents false-positive GUI choices.
    """
    ffmpeg = str(ffmpeg_path_text)
    result = {"libx264": False, "h264_nvenc": False, "h264_qsv": False, "h264_amf": False}
    try:
        listing = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
            creationflags=hidden_process_flags(), env=safe_subprocess_env(),
        )
        text = listing.stdout + listing.stderr
    except (OSError, subprocess.TimeoutExpired):
        return result
    for encoder in result:
        if encoder not in text:
            continue
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1:d=0.1",
            "-frames:v", "1", "-an", "-c:v", encoder,
        ]
        if encoder == "h264_amf":
            command += ["-usage", "transcoding"]
        command += ["-f", "null", "-"]
        try:
            probe = subprocess.run(
                command, capture_output=True, timeout=12,
                creationflags=hidden_process_flags(), env=safe_subprocess_env(),
            )
            result[encoder] = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            result[encoder] = False
    return result


def resolve_encoder(ffmpeg_path: Path | str, requested: str) -> tuple[str, str, list[str]]:
    available = available_encoders(str(ffmpeg_path))
    warnings: list[str] = []
    if requested == "Auto":
        for encoder, label in (
            ("h264_nvenc", "NVIDIA NVENC"),
            ("h264_qsv", "Intel Quick Sync"),
            ("h264_amf", "AMD AMF"),
        ):
            if available.get(encoder):
                return encoder, label, warnings
        return "libx264", "CPU (libx264)", warnings
    encoder, label = ENCODER_MAP.get(requested, ENCODER_MAP["CPU"])
    if not available.get(encoder):
        warnings.append(f"{label} ist nicht verfügbar; automatischer CPU-Fallback wird verwendet.")
        return "libx264", "CPU (libx264)", warnings
    return encoder, label, warnings


def encoder_arguments(encoder: str, crf: int, preset: str) -> list[str]:
    crf = max(0, min(35, int(crf)))
    if encoder == "h264_nvenc":
        nv_preset = {"fast": "p4", "medium": "p6", "slow": "p7"}.get(preset, "p6")
        return ["-c:v", encoder, "-preset", nv_preset, "-tune", "hq", "-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-profile:v", "high"]
    if encoder == "h264_qsv":
        qsv_preset = {"fast": "faster", "medium": "medium", "slow": "slower"}.get(preset, "medium")
        return ["-c:v", encoder, "-preset", qsv_preset, "-global_quality", str(crf), "-profile:v", "high"]
    if encoder == "h264_amf":
        quality = {"fast": "speed", "medium": "balanced", "slow": "quality"}.get(preset, "balanced")
        return ["-c:v", encoder, "-quality", quality, "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-profile:v", "high"]
    x264_preset = preset if preset in {"fast", "medium", "slow"} else "medium"
    return ["-c:v", "libx264", "-preset", x264_preset, "-crf", str(crf), "-profile:v", "high"]
