"""Phase 27: canonical multiple-music-track sequence helpers.

The music system supports an explicit ordered list of tracks. The ENTIRE
sequence loops as one unit during rendering (A → B → C → A → B → C …); an
individual track is never looped on its own while other tracks remain. A
single configured track remains a valid one-item sequence, and legacy
projects that only know ``music_path`` keep working unchanged through
:func:`effective_music_tracks`.

Track entries are plain JSON-friendly dicts so the existing SettingsStore
persists them without custom serializers:

``{"path": str, "trim_start": float, "trim_duration": float}``

``trim_duration`` 0.0 means "play from trim_start to the end of the file".
Existing volume/ducking/preset behavior stays global and untouched.
"""

from __future__ import annotations

from pathlib import Path

# Per-track trim bounds. They mirror the conservative clamps used elsewhere
# for manual duration inputs and can never produce a negative or unbounded
# segment.
MAX_TRACK_TRIM_START = 3600.0
MAX_TRACK_TRIM_DURATION = 3600.0


def normalize_music_track(value: object) -> dict | None:
    """Return one sanitized track dict, or None when the entry is unusable."""
    if isinstance(value, str):
        path = value.strip()
        if not path:
            return None
        return {"path": path, "trim_start": 0.0, "trim_duration": 0.0}
    if not isinstance(value, dict):
        return None
    path = str(value.get("path", "") or "").strip()
    if not path:
        return None
    try:
        trim_start = float(value.get("trim_start", 0.0) or 0.0)
    except (TypeError, ValueError):
        trim_start = 0.0
    try:
        trim_duration = float(value.get("trim_duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        trim_duration = 0.0
    return {
        "path": path,
        "trim_start": max(0.0, min(MAX_TRACK_TRIM_START, trim_start)),
        "trim_duration": max(0.0, min(MAX_TRACK_TRIM_DURATION, trim_duration)),
    }


def normalize_music_tracks(value: object) -> list[dict]:
    """Return the sanitized ordered track list (empty list when nothing)."""
    if not isinstance(value, (list, tuple)):
        return []
    tracks: list[dict] = []
    for entry in value:
        track = normalize_music_track(entry)
        if track is not None:
            tracks.append(track)
    return tracks


def effective_music_tracks(settings) -> list[dict]:
    """Return the authoritative ordered sequence for one settings object.

    A populated ``music_tracks`` list wins. A legacy single ``music_path``
    migrates to a one-item sequence without touching the saved field, so old
    projects and direct API callers keep their exact historical behavior.
    """
    tracks = normalize_music_tracks(getattr(settings, "music_tracks", None))
    if tracks:
        return tracks
    legacy = str(getattr(settings, "music_path", "") or "").strip()
    if legacy:
        return [{"path": legacy, "trim_start": 0.0, "trim_duration": 0.0}]
    return []


def music_track_paths(settings) -> list[Path]:
    """Return the ordered resolved file paths of the effective sequence."""
    return [
        Path(track["path"]).expanduser().resolve()
        for track in effective_music_tracks(settings)
    ]
