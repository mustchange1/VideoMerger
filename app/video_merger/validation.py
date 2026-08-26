from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

from .models import ResolvedExport, ValidationReport
from .platform_utils import hidden_process_flags, safe_subprocess_env


def _fps(text: str) -> float:
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _top_level_mp4_atoms(path: Path, limit: int = 256) -> list[str]:
    atoms: list[str] = []
    file_size = path.stat().st_size
    position = 0
    try:
        with path.open("rb") as handle:
            while position + 8 <= file_size and len(atoms) < limit:
                handle.seek(position)
                header = handle.read(8)
                if len(header) != 8:
                    break
                atom_size = int.from_bytes(header[:4], "big")
                atom_type = header[4:8].decode("ascii", errors="replace")
                header_size = 8
                if atom_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        break
                    atom_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif atom_size == 0:
                    atom_size = file_size - position
                if atom_size < header_size or position + atom_size > file_size:
                    break
                atoms.append(atom_type)
                position += atom_size
    except OSError:
        return []
    return atoms


def validate_output(path: Path, ffprobe_path: Path | str, expected: ResolvedExport) -> ValidationReport:
    details: list[str] = []
    if not path.is_file():
        return ValidationReport(False, ["Ausgabedatei wurde nicht erstellt."], path)
    size = path.stat().st_size
    if size <= 0:
        return ValidationReport(False, ["Ausgabedatei ist leer."], path)
    details.append(f"Dateigröße: {size / (1024 * 1024):.2f} MiB")
    command = [
        str(ffprobe_path), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]
    try:
        probe = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ValidationReport(False, details + [f"FFprobe-Validierung fehlgeschlagen: {exc}"], path)
    if probe.returncode != 0:
        return ValidationReport(False, details + [f"MP4 kann nicht gelesen werden: {probe.stderr.strip()}"], path)
    try:
        data = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return ValidationReport(False, details + ["FFprobe-Validierungsdaten sind ungültig."], path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((data.get("format") or {}).get("duration") or 0)
    width = int((video or {}).get("width") or 0)
    height = int((video or {}).get("height") or 0)
    fps = _fps((video or {}).get("avg_frame_rate") or "0/0")
    sar = str((video or {}).get("sample_aspect_ratio") or "")
    video_duration = float((video or {}).get("duration") or duration or 0)
    audio_duration = float((audio or {}).get("duration") or duration or 0)
    video_codec = str((video or {}).get("codec_name") or "")
    pixel_format = str((video or {}).get("pix_fmt") or "")
    audio_codec = str((audio or {}).get("codec_name") or "")
    audio_rate = int((audio or {}).get("sample_rate") or 0)
    audio_channels = int((audio or {}).get("channels") or 0)
    bit_rate = int((data.get("format") or {}).get("bit_rate") or 0)
    display_ratio = (width / height) if height else 0.0
    expected_ratio = expected.width / expected.height
    failures: list[str] = []
    if video is None:
        failures.append("Kein Videostream in der Ausgabe.")
    if audio is None:
        failures.append("Kein Audiostream in der Ausgabe.")
    if (width, height) != (expected.width, expected.height):
        failures.append(f"Falsche Auflösung: {width}x{height}, erwartet {expected.resolution_text}.")
    if abs(fps - expected.fps) > 0.12:
        failures.append(f"Falsche Framerate: {fps:.3f}, erwartet {expected.fps:.3f}.")
    if sar not in {"1:1", "1/1"}:
        failures.append(f"Falsches Pixel-Seitenverhältnis (SAR): {sar or 'unbekannt'}, erwartet 1:1.")
    if abs(display_ratio - expected_ratio) > 0.002:
        failures.append(f"Falsches Display-Seitenverhältnis: {display_ratio:.5f}, erwartet {expected_ratio:.5f}.")
    # A sub-frame transition on an extremely short padded clip can make the
    # encoded video stream end up to a few frames shorter than container audio.
    # 0.20 s still catches meaningful A/V truncation while permitting that safe
    # clamp edge case.
    duration_tolerance = max(0.20, expected.expected_duration * 0.025)
    if abs(duration - expected.expected_duration) > duration_tolerance:
        failures.append(f"Unerwartete Dauer: {duration:.3f} s, erwartet ca. {expected.expected_duration:.3f} s.")
    if abs(video_duration - expected.expected_duration) > duration_tolerance:
        failures.append(
            f"Videostream-Dauer: {video_duration:.3f} s, erwartet ca. {expected.expected_duration:.3f} s."
        )
    if abs(audio_duration - expected.expected_duration) > max(0.15, duration_tolerance):
        failures.append(
            f"Audiostream-Dauer: {audio_duration:.3f} s, erwartet ca. {expected.expected_duration:.3f} s."
        )
    if video_codec != "h264":
        failures.append(f"Unerwarteter Video-Codec: {video_codec or 'fehlt'}, erwartet H.264.")
    if pixel_format != "yuv420p":
        failures.append(f"Unerwartetes Pixel-Format: {pixel_format or 'fehlt'}, erwartet yuv420p.")
    if audio_codec != "aac":
        failures.append(f"Unerwarteter Audio-Codec: {audio_codec or 'fehlt'}, erwartet AAC-LC.")
    if audio_rate != 48000:
        failures.append(f"Unerwartete Audio-Samplerate: {audio_rate}, erwartet 48000 Hz.")
    if audio_channels != 2:
        failures.append(f"Unerwartete Kanalzahl: {audio_channels}, erwartet Stereo.")
    format_name = str((data.get("format") or {}).get("format_name") or "")
    if not any(token in format_name for token in ("mp4", "mov")):
        failures.append(f"Unerwarteter Container: {format_name or 'unbekannt'}.")
    atoms = _top_level_mp4_atoms(path)
    if "moov" not in atoms or "mdat" not in atoms:
        failures.append("MP4-Atomstruktur enthält moov/mdat nicht wie erwartet.")
    elif atoms.index("moov") > atoms.index("mdat"):
        failures.append("MP4 Fast Start fehlt: moov steht hinter mdat.")
    else:
        details.append("Fast Start: OK (moov vor mdat)")
    details.extend([
        f"Container: {format_name}",
        f"Video: {video_codec or 'fehlt'}, {pixel_format or 'unbekannt'}, {width}x{height}, {fps:.3f} fps, SAR {sar}, {video_duration:.3f} s",
        f"Audio: {audio_codec or 'fehlt'}, {audio_rate} Hz, {audio_channels} Kanäle, {audio_duration:.3f} s",
        f"Bitrate: {bit_rate} bit/s",
        f"Dauer: {duration:.3f} s",
    ])
    details.extend(failures)
    return ValidationReport(
        ok=not failures, details=details, path=path, duration=duration,
        width=width, height=height, fps=fps, has_video=video is not None, has_audio=audio is not None,
    )
