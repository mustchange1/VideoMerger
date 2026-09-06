"""Safe Stage-1 Main Video fingerprinting and reuse cache.

The Stage-1 cache deliberately models only the inputs that define the Main
Video render. Stage-2 composition choices (Intro, Add Image and Outro
controls) are not part of that fingerprint, so One-Click can reuse a valid Main
Video when only final-composition choices change. A separate Stage-2
fingerprint models the final composition; in particular, Add Image's selected
file/content and every render setting are included there.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .models import AudioAssetInfo, ExportSettings, MediaInfo, ResolvedExport
from .paths import project_root
from .subtitle_modes import normalize_subtitle_output_mode

CACHE_SCHEMA = 1
# 2: the Quote/Flyer artwork section was removed, which changed both payload
# shapes. Entries written by schema 1 must never be reused silently.
# 3: the explicit visual-only intro/outro sections and the Main Video opening
# effect were added to the Stage-1 payload. A cached render from schema 2 has a
# different timeline (no visual intro, a different tail) and must never be
# reused silently for the new settings.
# 4: background music now covers the COMPLETE video (it used to be trimmed at
# the spoken end, which left the visual outro silent) and Long-Form/Shorts
# received independent music volume and transition settings with new defaults.
# A cached render from schema 3 therefore contains different audio bytes and
# possibly different transitions, and must never be reused silently.
# 5: configured video folders can now carry a soft timeline-area role
# (1. Start & End / 2. Start to Middle / 3. Middle to End) plus start/end zone
# targets and a midpoint percentage, which change WHICH clips are selected for a
# render. A schema-4 entry was built without that source ordering and must never
# be reused silently.
FINGERPRINT_SCHEMA = 5
STAGE2_FINGERPRINT_SCHEMA = 2

# These are the settings that can change the bytes or duration of the Stage-1
# Main Video. Deliberately absent: workflow_stage, output_name, main_video_path,
# Intro/Outro paths and all other Stage-2-only composition controls.
_STAGE1_SETTING_FIELDS = (
    "export_mode",
    "aspect",
    "resolution",
    "fit_mode",
    "transition_type",
    "transition_ease",
    "transition_duration",
    # Output-specific transition settings. The canonical pair above already
    # carries the value this job resolved; these four participate as well, so a
    # Long-Form or Shorts transition change can never reuse an incompatible
    # cached render, even before the planner copied the value over.
    "long_form_transition_type",
    "long_form_transition_duration",
    "shorts_transition_type",
    "shorts_transition_duration",
    "background_blur",
    "background_darkness",
    "background_zoom",
    "normalize_audio",
    "fps_choice",
    "encoding",
    "crf",
    "preset",
    "quality_preset",
    "output_preset",
    "original_audio_mode",
    "voiceover_volume",
    "music_volume",
    "music_preset",
    "ducking_enabled",
    "ducking_attack_ms",
    "ducking_release_ms",
    # ``final_pause`` is the canonical visual outro (Main Video end padding):
    # the tail after the spoken audio, filled with video-only material.
    "final_pause",
    # Explicit visual-only timeline sections. ``visual_intro_seconds`` is the
    # canonical intro the renderer uses; the four collection-specific values are
    # included as well so a changed Long-Form or Short section can never reuse
    # an incompatible cached render, even before the planner copied it over.
    "visual_intro_seconds",
    "long_form_intro_seconds",
    "long_form_outro_seconds",
    "short_intro_seconds",
    "short_outro_seconds",
    # Subtle Main Video opening effect (none | zoom_in | zoom_out). It changes
    # rendered pixels, so it is part of the Stage-1 identity.
    "opening_effect",
    # Phase 4: inter-unit silence and ordering/global-script semantics are
    # render inputs; final_pause above remains the independent end padding.
    "voiceover_pause",
    "voiceover_order_mode",
    "script_mode",
    "global_script_path",
    "short_video_mode",
    "duration_fit_mode",
    "max_stretch_percent",
    "duration_before_merge",
    "duration_after_merge",
    "duration_after_merge_enabled",
    # Keep the legacy field in the digest so an old caller changing
    # video_speed cannot accidentally reuse a cache built with different
    # legacy semantics; new GUI projects use the canonical field above.
    "video_speed",
    "video_order_mode",
    # Soft timeline-area source ordering: which configured folder is used at
    # which approximate part of the timeline changes the selected clips, so it
    # is part of the render identity like the project order itself.
    "source_folder_areas",
    "timeline_area_start_seconds",
    "timeline_area_end_seconds",
    "timeline_area_midpoint_percent",
    "shorts_allow_area_middle_end",
    # Shorts are independent Stage-1 jobs; this prevents duplicate rows from
    # sharing a cache result merely because their audio path is the same.
    "render_variant_key",
    "allow_hdr_unsafe",
    "watermark_enabled",
    "watermark_scope",
    "watermark_position",
    "watermark_opacity",
    "watermark_size",
    "watermark_margin",
)

_SUBTITLE_SETTING_FIELDS = (
    "subtitle_language",
    "subtitle_style",
    "subtitle_animation",
    "subtitle_font",
    "subtitle_position",
    "subtitle_debug_overlay",
    "subtitle_model",
    "allow_alignment_warnings",
    "subtitle_output_mode",
)


# JSON's default encoder cannot serialize Path objects. Everything emitted by
# this module is normalized explicitly so the digest is stable across runs.
def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_signature(path: Path | str, *, content_hash: bool = False) -> dict[str, Any]:
    """Return a deterministic identity for an input file.

    Media files use path + stat data to avoid hashing potentially multi-gigabyte
    videos on every One-Click start. Text/script and small audio inputs
    additionally use a content digest, ensuring an edit invalidates the cache
    even if its timestamp or size is restored.
    """
    resolved = Path(path).expanduser().resolve()
    result: dict[str, Any] = {"path": str(resolved), "exists": False}
    try:
        stat = resolved.stat()
    except OSError:
        return result
    result.update({"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    if content_hash:
        result["sha256"] = _content_sha256(resolved)
    return result


def _audio_payload(asset: AudioAssetInfo) -> dict[str, Any]:
    return {
        # Audio inputs are normally much smaller than the video pool and are
        # authoritative Stage-1 content, so include their content digest as
        # well as their path/stat identity.
        "file": file_signature(asset.path, content_hash=True),
        "duration": float(asset.duration),
        "sample_rate": int(asset.sample_rate),
        "channels": int(asset.channels),
        "codec": str(asset.codec),
    }


def _media_payload(item: MediaInfo) -> dict[str, Any]:
    audio = asdict(item.audio)
    is_image = bool(getattr(item, "is_image_insertion", False))
    payload = {
        # A Stage-2 image is small and content-authoritative, unlike a normal
        # video clip where stat identity avoids hashing gigabytes on every run.
        "file": file_signature(item.path, content_hash=is_image),
        "duration": float(item.duration),
        "source_duration": float(item.source_duration or 0.0),
        "width": int(item.width),
        "height": int(item.height),
        "effective_width": int(item.effective_width),
        "effective_height": int(item.effective_height),
        "fps": float(item.fps),
        "fps_fraction": str(item.fps_fraction),
        "video_codec": str(item.video_codec),
        "pixel_format": str(item.pixel_format),
        "sar": str(item.sar),
        "dar": str(item.dar),
        "rotation": int(item.rotation),
        "audio": audio,
        "is_hdr": bool(item.is_hdr),
        "color_primaries": str(item.color_primaries),
        "color_transfer": str(item.color_transfer),
        "color_space": str(item.color_space),
        "playback_rate": float(item.playback_rate),
        "source_folder": str(getattr(item, "source_folder", "") or item.path.parent),
        "is_image_insertion": bool(getattr(item, "is_image_insertion", False)),
        "image_fit_mode": str(getattr(item, "image_fit_mode", "fit")),
        "image_zoom": int(getattr(item, "image_zoom", 100)),
        "image_filter": str(getattr(item, "image_filter", "natural")),
    }
    if is_image:
        payload["image_transition_type"] = str(
            getattr(item, "image_transition_type", "") or ""
        )
    return payload


def _resolved_payload(resolved: ResolvedExport) -> dict[str, Any]:
    return {
        "width": int(resolved.width),
        "height": int(resolved.height),
        "fps": float(resolved.fps),
        "fps_expr": str(resolved.fps_expr),
        "effective_durations": [float(value) for value in resolved.effective_durations],
        "transitions": [float(value) for value in resolved.transitions],
        "expected_duration": float(resolved.expected_duration),
        "encoder": str(resolved.encoder),
        "encoder_label": str(resolved.encoder_label),
        "crf": int(resolved.crf),
        "preset": str(resolved.preset),
        "quality_label": str(resolved.quality_label),
    }


def build_stage1_payload(
    media: Sequence[MediaInfo],
    settings: ExportSettings,
    resolved: ResolvedExport,
    *,
    voice_assets: Sequence[AudioAssetInfo] = (),
    script_files: Sequence[Path] = (),
    subtitle_requested: bool = False,
    music_asset: AudioAssetInfo | None = None,
    watermark_path: Path | None = None,
) -> dict[str, Any]:
    """Build the complete canonical payload used by the Stage-1 digest."""
    values: dict[str, Any] = {
        name: getattr(settings, name) for name in _STAGE1_SETTING_FIELDS
    }
    values["subtitle_requested"] = bool(subtitle_requested)
    if subtitle_requested:
        values.update({name: getattr(settings, name) for name in _SUBTITLE_SETTING_FIELDS})
    else:
        values.update({
            name: None for name in _SUBTITLE_SETTING_FIELDS
            if name != "subtitle_output_mode"
        })
    values["subtitle_output_mode"] = normalize_subtitle_output_mode(
        getattr(settings, "subtitle_output_mode", "with_subtitles")
    )

    # These settings only affect the render when the corresponding asset is
    # active. Avoid invalidating a render because an unused control changed.
    values["music_active"] = music_asset is not None
    if music_asset is not None:
        values.update({
            "music_volume": settings.music_volume,
            # Independent per-output music volumes. Like the canonical value
            # they only matter while a track is really mixed, so an unused
            # control must not invalidate an otherwise identical render.
            "long_form_music_volume": getattr(settings, "long_form_music_volume", None),
            "shorts_music_volume": getattr(settings, "shorts_music_volume", None),
            "music_preset": settings.music_preset,
            "ducking_enabled": settings.ducking_enabled,
            "ducking_attack_ms": settings.ducking_attack_ms,
            "ducking_release_ms": settings.ducking_release_ms,
        })
    else:
        for name in (
            "music_volume",
            "long_form_music_volume",
            "shorts_music_volume",
            "music_preset",
            "ducking_enabled",
            "ducking_attack_ms",
            "ducking_release_ms",
        ):
            values[name] = None

    values["watermark_active"] = bool(
        settings.watermark_enabled
        and watermark_path is not None
        and settings.watermark_scope in {"main", "both"}
    )
    if not values["watermark_active"]:
        for name in (
            "watermark_scope", "watermark_position", "watermark_opacity", "watermark_size", "watermark_margin"
        ):
            values[name] = None

    return {
        "schema": FINGERPRINT_SCHEMA,
        "settings": values,
        # ``media`` is the already fitted render sequence. Unused pool files
        # are intentionally absent because they cannot affect this render.
        "selected_media": [_media_payload(item) for item in media],
        "voiceovers": [_audio_payload(asset) for asset in voice_assets],
        "scripts": [file_signature(path, content_hash=True) for path in script_files],
        "music": _audio_payload(music_asset) if music_asset is not None else None,
        "watermark": file_signature(watermark_path, content_hash=True) if values["watermark_active"] else None,
        "resolved": _resolved_payload(resolved),
        "output_format": {
            "container": "mp4",
            "video_pixel_format": "yuv420p",
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
        },
    }


def stage1_fingerprint(
    media: Sequence[MediaInfo],
    settings: ExportSettings,
    resolved: ResolvedExport,
    *,
    voice_assets: Sequence[AudioAssetInfo] = (),
    script_files: Sequence[Path] = (),
    subtitle_requested: bool = False,
    music_asset: AudioAssetInfo | None = None,
    watermark_path: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return ``(sha256, canonical_payload)`` for a Stage-1 render."""
    payload = build_stage1_payload(
        media,
        settings,
        resolved,
        voice_assets=voice_assets,
        script_files=script_files,
        subtitle_requested=subtitle_requested,
        music_asset=music_asset,
        watermark_path=watermark_path,
    )
    encoded = _canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


_STAGE2_SETTING_FIELDS = (
    "aspect",
    "resolution",
    "transition_type",
    "transition_ease",
    "transition_duration",
    "intro_audio_mode",
    "outro_audio_mode",
    "outro_transition_enabled",
    "image_enabled",
    "image_position",
    "image_duration",
    "image_transition_type",
    "image_transition_duration",
    "image_fit_mode",
    "image_zoom",
    "image_filter",
)


def _stage2_path_payload(settings: ExportSettings, field: str, *, content_hash: bool = False) -> dict[str, Any]:
    value = str(getattr(settings, field, "") or "").strip()
    return file_signature(Path(value), content_hash=content_hash) if value else {
        "path": "", "exists": False,
    }


def build_stage2_payload(
    media: Sequence[MediaInfo],
    settings: ExportSettings,
    resolved: ResolvedExport,
) -> dict[str, Any]:
    """Build the independent final-composition fingerprint payload.

    Unlike the Stage-1 fingerprint, this payload intentionally contains the
    role paths and Stage-2 controls. Add Image is represented both as its
    normalized settings and as a content-hashed source identity, so changing
    any image setting or editing the selected bytes invalidates this stage
    without needlessly invalidating the reusable Main Video.
    """
    values = {name: getattr(settings, name) for name in _STAGE2_SETTING_FIELDS}
    values.update({
        "main_video_path": _stage2_path_payload(settings, "main_video_path"),
        "intro_path": _stage2_path_payload(settings, "intro_path"),
        "outro_path": _stage2_path_payload(settings, "outro_path"),
        "image_path": _stage2_path_payload(settings, "image_path", content_hash=True),
    })
    return {
        "schema": STAGE2_FINGERPRINT_SCHEMA,
        "settings": values,
        "composition": [_media_payload(item) for item in media],
        "resolved": _resolved_payload(resolved),
        "output_format": {
            "container": "mp4",
            "video_pixel_format": "yuv420p",
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
        },
    }


def stage2_fingerprint(
    media: Sequence[MediaInfo],
    settings: ExportSettings,
    resolved: ResolvedExport,
) -> tuple[str, dict[str, Any]]:
    """Return ``(sha256, payload)`` for a final Stage-2 composition."""
    payload = build_stage2_payload(media, settings, resolved)
    encoded = _canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{time.time_ns()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


class Stage1RenderCache:
    """Per-fingerprint manifests and recoverable subtitle sidecars."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else project_root() / "cache" / "stage1_render"

    def _directory(self, fingerprint: str) -> Path:
        return self.root / fingerprint

    def _manifest_path(self, fingerprint: str) -> Path:
        return self._directory(fingerprint) / "manifest.json"

    def load(self, fingerprint: str) -> dict[str, Any] | None:
        path = self._manifest_path(fingerprint)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != CACHE_SCHEMA or payload.get("fingerprint") != fingerprint:
            return None
        canonical_payload = payload.get("payload")
        if not isinstance(canonical_payload, dict):
            return None
        if hashlib.sha256(_canonical_json(canonical_payload).encode("utf-8")).hexdigest() != fingerprint:
            return None
        if not isinstance(payload.get("artifacts"), dict):
            return None
        return payload

    def save(
        self,
        fingerprint: str,
        payload: dict[str, Any],
        *,
        video: Path,
        video_no_subtitles: Path | None,
        srt: Path | None,
        vtt: Path | None,
        canonical_timeline: Path | None,
        subtitle_requested: bool,
        subtitle_output_mode: str | None = None,
    ) -> None:
        directory = self._directory(fingerprint)
        sidecars = directory / "sidecars"
        directory.mkdir(parents=True, exist_ok=True)
        sidecars.mkdir(parents=True, exist_ok=True)

        artifacts: dict[str, str | None] = {
            "video": str(video.expanduser().resolve()),
            "video_no_subtitles": str(video_no_subtitles.expanduser().resolve()) if video_no_subtitles else None,
            "srt": str(srt.expanduser().resolve()) if srt else None,
            "vtt": str(vtt.expanduser().resolve()) if vtt else None,
            "canonical_timeline": str(canonical_timeline.expanduser().resolve()) if canonical_timeline else None,
        }
        snapshots: dict[str, str | None] = {"srt": None, "vtt": None, "canonical_timeline": None}
        for key, source in (("srt", srt), ("vtt", vtt), ("canonical_timeline", canonical_timeline)):
            if source is not None and source.is_file() and source.stat().st_size > 0:
                target = sidecars / f"{key}{source.suffix or '.dat'}"
                _copy_atomic(source, target)
                snapshots[key] = str(target.resolve())

        manifest = {
            "schema": CACHE_SCHEMA,
            "fingerprint": fingerprint,
            "created_ns": time.time_ns(),
            "subtitle_requested": bool(subtitle_requested),
            "subtitle_output_mode": normalize_subtitle_output_mode(subtitle_output_mode),
            "payload": payload,
            "artifacts": artifacts,
            "sidecar_snapshots": snapshots,
        }
        manifest_path = self._manifest_path(fingerprint)
        temporary = manifest_path.with_name(f".{manifest_path.name}.{time.time_ns()}.tmp")
        try:
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    def restore_sidecars(self, record: dict[str, Any]) -> None:
        """Restore missing user-facing sidecars from cache snapshots."""
        artifacts = record.get("artifacts") or {}
        snapshots = record.get("sidecar_snapshots") or {}
        if not isinstance(artifacts, dict) or not isinstance(snapshots, dict):
            return
        for key in ("srt", "vtt", "canonical_timeline"):
            target_value = artifacts.get(key)
            snapshot_value = snapshots.get(key)
            if not isinstance(target_value, str) or not isinstance(snapshot_value, str):
                continue
            target = Path(target_value)
            snapshot = Path(snapshot_value)
            try:
                if snapshot.is_file() and snapshot.stat().st_size > 0 and (
                    not target.is_file() or target.stat().st_size <= 0
                ):
                    _copy_atomic(snapshot, target)
            except OSError:
                continue

    @staticmethod
    def artifact_paths(record: dict[str, Any]) -> dict[str, Path | None]:
        artifacts = record.get("artifacts") or {}
        result: dict[str, Path | None] = {}
        for key in ("video", "video_no_subtitles", "srt", "vtt", "canonical_timeline"):
            value = artifacts.get(key)
            result[key] = Path(value) if isinstance(value, str) and value else None
        return result


def load_cached_alignment(path: Path):
    """Load the alignment portion of a canonical timeline without ASR."""
    from .models import AlignmentResult, WordTiming

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Cached canonical timeline is not an object")
    raw_words = payload.get("words", [])
    if not isinstance(raw_words, list):
        raise ValueError("Cached canonical timeline words are not a list")
    words = [WordTiming(**dict(item)) for item in raw_words]
    return AlignmentResult(
        words=words,
        language=str(payload.get("language", "auto")),
        method=str(payload.get("method", "cached")),
        compatibility=float(payload.get("compatibility", 1.0)),
        average_confidence=float(payload.get("average_confidence", 1.0)),
        warnings=[],
        hard_breaks=[float(item) for item in payload.get("hard_breaks", [])],
    )
