from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import MediaAnalysisError
from .models import AudioInfo, MediaInfo
from .paths import project_root
from .platform_utils import hidden_process_flags, safe_subprocess_env

_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_HDR_PRIMARIES = {"bt2020"}
_CACHE_SCHEMA = 2


def _ratio_to_float(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rotation(stream: dict[str, Any]) -> int:
    value: Any = (stream.get("tags") or {}).get("rotate", 0)
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            value = side_data["rotation"]
            break
    try:
        return int(round(float(value))) % 360
    except (TypeError, ValueError):
        return 0


class MediaAnalyzer:
    """FFprobe analyzer with safe stat-keyed persistent metadata caching."""

    _write_lock = threading.Lock()

    def __init__(self, ffprobe_path: Path | str, cache_path: Path | None = None):
        self.ffprobe_path = str(ffprobe_path)
        self.cache_path = cache_path or (project_root() / "cache" / "media_analysis.json")
        self._cache = self._load_cache()
        self.last_cache_hit = False

    def _load_cache(self) -> dict[str, dict]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("schema") != _CACHE_SCHEMA:
                return {}
            return dict(data.get("entries") or {})
        except (OSError, ValueError, TypeError):
            return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(f".{threading.get_ident()}.tmp")
            temporary.write_text(
                json.dumps({"schema": _CACHE_SCHEMA, "entries": self._cache}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self._write_lock:
                temporary.replace(self.cache_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass

    def _signature(self, path: Path) -> tuple[str, str]:
        resolved = path.expanduser().resolve()
        try:
            stat = resolved.stat()
            signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            # ``probe_raw`` remains authoritative and will produce the normal
            # production error. This fallback also keeps synthetic analyzer
            # unit tests independent from filesystem fixture creation.
            signature = "missing"
        key = str(resolved)
        return key, signature

    @staticmethod
    def _from_cached(path: Path, payload: dict) -> MediaInfo:
        data = dict(payload)
        audio = AudioInfo(**dict(data.pop("audio", {})))
        data["path"] = path
        data["audio"] = audio
        return MediaInfo(**data)

    def probe_raw(self, path: Path | str) -> dict[str, Any]:
        media_path = Path(path)
        command = [
            self.ffprobe_path, "-v", "error", "-show_streams", "-show_format", "-of", "json",
            str(media_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaAnalysisError(f"FFprobe konnte {media_path.name} nicht analysieren: {exc}") from exc
        if completed.returncode != 0:
            reason = completed.stderr.strip() or "Unbekannter FFprobe-Fehler"
            raise MediaAnalysisError(f"Ungültige/beschädigte Datei {media_path.name}: {reason}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise MediaAnalysisError(f"FFprobe lieferte ungültige Daten für {media_path.name}.") from exc

    def analyze(self, path: Path | str) -> MediaInfo:
        media_path = Path(path).expanduser().resolve()
        cache_key, signature = self._signature(media_path)
        entry = self._cache.get(cache_key)
        if entry and entry.get("signature") == signature:
            try:
                result = self._from_cached(media_path, entry["media"])
                self.last_cache_hit = True
                return result
            except (TypeError, ValueError, KeyError):
                self._cache.pop(cache_key, None)
        self.last_cache_hit = False
        data = self.probe_raw(media_path)
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise MediaAnalysisError(f"Die Datei enthält keinen Videostream: {media_path.name}")
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        format_info = data.get("format") or {}
        duration = _to_float(video.get("duration")) or _to_float(format_info.get("duration"))
        if duration <= 0:
            duration = _to_float((video.get("tags") or {}).get("DURATION"))
        if duration <= 0:
            raise MediaAnalysisError(f"Die Videodauer konnte nicht ermittelt werden: {media_path.name}")
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        if width <= 0 or height <= 0:
            raise MediaAnalysisError(f"Ungültige Videoauflösung in {media_path.name}.")
        fps_fraction = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
        fps = _ratio_to_float(fps_fraction)
        if fps <= 0:
            fps_fraction = video.get("r_frame_rate") or "30/1"
            fps = _ratio_to_float(fps_fraction) or 30.0
        rotation = _rotation(video)
        effective_width, effective_height = width, height
        if rotation in {90, 270}:
            effective_width, effective_height = height, width
        color_primaries = str(video.get("color_primaries") or "")
        color_transfer = str(video.get("color_transfer") or "")
        color_space = str(video.get("color_space") or "")
        is_hdr = color_transfer in _HDR_TRANSFERS or color_primaries in _HDR_PRIMARIES
        warnings: list[str] = []
        if is_hdr:
            warnings.append("HDR/BT.2020 erkannt; der sichere SDR-Export ist in Version 1.2.1 blockiert.")
        audio = AudioInfo()
        if audio_stream is not None:
            audio = AudioInfo(
                present=True, codec=str(audio_stream.get("codec_name") or ""),
                sample_rate=int(audio_stream.get("sample_rate") or 0),
                channels=int(audio_stream.get("channels") or 0),
                channel_layout=str(audio_stream.get("channel_layout") or ""),
            )
        result = MediaInfo(
            path=media_path, duration=duration, width=width, height=height,
            effective_width=effective_width, effective_height=effective_height,
            fps=fps, fps_fraction=str(fps_fraction),
            video_codec=str(video.get("codec_name") or ""),
            pixel_format=str(video.get("pix_fmt") or ""),
            sar=str(video.get("sample_aspect_ratio") or "1:1"),
            dar=str(video.get("display_aspect_ratio") or ""), rotation=rotation,
            audio=audio, is_hdr=is_hdr, color_primaries=color_primaries,
            color_transfer=color_transfer, color_space=color_space, warnings=warnings,
        )
        payload = asdict(result)
        payload.pop("path", None)
        self._cache[cache_key] = {"signature": signature, "media": payload}
        self._save_cache()
        return result

    def analyze_many(self, paths: list[Path], log=lambda _message: None) -> list[MediaInfo]:
        results: list[MediaInfo] = []
        for index, path in enumerate(paths, start=1):
            result = self.analyze(path)
            source = "Cache" if self.last_cache_hit else "FFprobe"
            log(f"Analysiere {index}/{len(paths)}: {path.name} ({source})")
            results.append(result)
        return results
