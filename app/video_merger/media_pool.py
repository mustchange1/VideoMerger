"""Unified image/video pool used by the Master Timeline."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .asset_cooldown import canonical_asset_id
from .models import MediaInfo


@dataclass(slots=True, frozen=True)
class MediaPoolAsset:
    path: str
    kind: str  # image | video
    category: str = ""
    keywords: tuple[str, ...] = ()
    metadata_text: str = ""
    source_media: MediaInfo | None = field(default=None, compare=False, hash=False, repr=False)

    @property
    def asset_id(self) -> str:
        return canonical_asset_id(self.path)


@dataclass(slots=True)
class MediaPool:
    assets: list[MediaPoolAsset] = field(default_factory=list)

    @classmethod
    def unified(cls, source_videos: Sequence[MediaInfo], indexed_media: Iterable[object]) -> "MediaPool":
        """Merge source videos and indexed media, deduplicated by canonical path."""
        by_id: dict[str, MediaPoolAsset] = {}
        for media in source_videos:
            asset = MediaPoolAsset(
                path=str(media.path), kind="video", category=Path(media.path).parent.name,
                keywords=(Path(media.path).stem,), metadata_text=Path(media.path).stem,
                source_media=media,
            )
            by_id[asset.asset_id] = asset
        for entry in indexed_media:
            path = str(getattr(entry, "path", "") or "")
            kind = str(getattr(entry, "kind", "") or "").casefold()
            if not path or kind not in {"image", "video"}:
                continue
            asset = MediaPoolAsset(
                path=path,
                kind=kind,
                category=str(getattr(entry, "category", "") or ""),
                keywords=tuple(str(value) for value in (getattr(entry, "keywords", ()) or ())),
                metadata_text=str(getattr(entry, "metadata_text", "") or ""),
                source_media=by_id.get(canonical_asset_id(path), MediaPoolAsset(path, kind)).source_media,
            )
            by_id[asset.asset_id] = asset
        return cls(sorted(by_id.values(), key=lambda item: (item.kind, item.asset_id)))

    def without(self, blocked: Iterable[str | Path]) -> "MediaPool":
        blocked_ids = {canonical_asset_id(path) for path in blocked}
        return MediaPool([asset for asset in self.assets if asset.asset_id not in blocked_ids])

    def asset_by_id(self, asset_id: str) -> MediaPoolAsset | None:
        """Resolve one canonical asset without ever matching by display name."""
        wanted = canonical_asset_id(asset_id)
        return next((asset for asset in self.assets if asset.asset_id == wanted), None)

    @property
    def fingerprint(self) -> str:
        """Stable identity of every eligible physical asset and relevant metadata."""
        records = []
        for asset in sorted(self.assets, key=lambda item: item.asset_id):
            source = asset.source_media
            records.append({
                "asset_id": asset.asset_id,
                "path": canonical_asset_id(asset.path),
                "kind": asset.kind,
                "category": asset.category,
                "keywords": list(asset.keywords),
                "metadata_text": asset.metadata_text,
                "duration": float(source.source_duration or source.duration) if source is not None else None,
                "width": int(source.width) if source is not None else None,
                "height": int(source.height) if source is not None else None,
                "fps": float(source.fps) if source is not None else None,
                "video_codec": str(source.video_codec) if source is not None else None,
            })
        encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def images(self) -> list[MediaPoolAsset]:
        return [asset for asset in self.assets if asset.kind == "image"]

    @property
    def videos(self) -> list[MediaPoolAsset]:
        return [asset for asset in self.assets if asset.kind == "video"]
