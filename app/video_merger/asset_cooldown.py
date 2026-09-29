"""Persistent cross-video asset cooldown history.

Only successful renders call :meth:`AssetCooldownHistory.record_video`. Planning
and preview are read-only, so a failed render can never consume an asset.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable

from .paths import project_root

DEFAULT_COOLDOWN_VIDEOS = 3
_HISTORY_LOCK = threading.RLock()


def clamp_cooldown_videos(value: object) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_VIDEOS


def canonical_asset_id(value: str | Path) -> str:
    path = Path(value).expanduser()
    try:
        path = path.resolve(strict=False)
    except OSError:
        path = Path(os.path.abspath(str(path)))
    return os.path.normcase(str(path))


class AssetCooldownHistory:
    """Atomic JSON-backed history, represented as newest completed video last."""

    schema = "videomerger-asset-cooldown-v1"

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else project_root() / "data" / "asset_cooldown_history.json"

    def _load_unlocked(self) -> list[list[str]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        raw = payload.get("videos", []) if isinstance(payload, dict) else []
        if not isinstance(raw, list):
            return []
        result: list[list[str]] = []
        for video in raw:
            if not isinstance(video, list):
                continue
            normalized = sorted({canonical_asset_id(item) for item in video if isinstance(item, str) and item.strip()})
            result.append(normalized)
        return result

    def videos(self) -> list[list[str]]:
        with _HISTORY_LOCK:
            return self._load_unlocked()

    def blocked_assets(self, cooldown_videos: object = DEFAULT_COOLDOWN_VIDEOS) -> set[str]:
        count = clamp_cooldown_videos(cooldown_videos)
        if count <= 0:
            return set()
        with _HISTORY_LOCK:
            history = self._load_unlocked()
        return {asset for video in history[-count:] for asset in video}

    def record_video(self, assets: Iterable[str | Path], *, retain_videos: int = 100) -> None:
        normalized = sorted({canonical_asset_id(asset) for asset in assets if str(asset).strip()})
        if not normalized:
            return
        with _HISTORY_LOCK:
            history = self._load_unlocked()
            history.append(normalized)
            history = history[-max(1, int(retain_videos)):]
            payload = {"schema": self.schema, "videos": history}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)

    def clear(self) -> None:
        with _HISTORY_LOCK:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
