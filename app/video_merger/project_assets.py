from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .errors import VideoMergerError
from .models import AudioAssetInfo
from .platform_utils import hidden_process_flags, safe_subprocess_env

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
_AUDIO_PROBE_CACHE: dict[tuple[str, str, int, int], AudioAssetInfo] = {}


def optional_path(value: str) -> Path | None:
    if not value.strip():
        return None
    return Path(value).expanduser().resolve()


def require_asset(path: Path | None, role: str, extensions: set[str] | None = None) -> Path:
    if path is None or not path.is_file():
        raise VideoMergerError(f"{role} fehlt oder ist keine lesbare Datei: {path or 'nicht ausgewählt'}")
    if extensions and path.suffix.casefold() not in extensions:
        raise VideoMergerError(f"Nicht unterstütztes Format für {role}: {path.suffix}")
    return path


def probe_audio(ffprobe: Path, path: Path) -> AudioAssetInfo:
    require_asset(path, "Audiodatei", AUDIO_EXTENSIONS)
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    cache_key = (str(Path(ffprobe).expanduser().resolve()), str(resolved), stat.st_size, stat.st_mtime_ns)
    cached = _AUDIO_PROBE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    command = [
        str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    if result.returncode != 0:
        raise VideoMergerError(f"Audioanalyse fehlgeschlagen ({path.name}): {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
        stream = next(item for item in data.get("streams", []) if item.get("codec_type") == "audio")
        duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
    except (ValueError, TypeError, StopIteration, json.JSONDecodeError) as exc:
        raise VideoMergerError(f"Keine gültige Audiospur in {path.name} gefunden.") from exc
    if duration <= 0 or sample_rate <= 0 or channels <= 0:
        raise VideoMergerError(f"Ungültige Audiodaten in {path.name}.")
    result = AudioAssetInfo(resolved, duration, sample_rate, channels, str(stream.get("codec_name") or ""))
    _AUDIO_PROBE_CACHE[cache_key] = result
    return result


def read_script(path: Path) -> str:
    require_asset(path, "Textskript", {".txt", ".text", ".md"})
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise VideoMergerError("Das Skript muss als UTF-8 gespeichert sein.") from exc
    text = text.strip()
    if not text:
        raise VideoMergerError("Das Textskript ist leer.")
    return text
