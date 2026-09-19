"""Phase 27/28: canonical multiple-music-track sequence helpers.

The music system supports an explicit ordered list of tracks. The historical
(and default) behavior loops the ENTIRE sequence as one unit during rendering
(A → B → C → A → B → C …). Phase 28 adds two additive layers on top:

* a per-track ``playback_mode`` — ``once`` (default), ``loop`` (the track
  repeats until the required duration is satisfied; later tracks are not
  reached) or ``repeat`` (exactly ``repeat_count`` plays, then the next
  track follows); and
* a per-profile ``sequence_mode`` — ``loop_sequence`` (default, historical)
  or ``play_once`` (the music ends after the final track while the video
  continues).

A single configured track remains a valid one-item sequence, and legacy
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

#: Phase 28 playback modes of one track inside a sequence. "once" plays the
#: track a single time, "loop" repeats this one track until the required
#: audio duration is satisfied (later tracks of the sequence are never
#: reached), and "repeat" plays the track exactly ``repeat_count`` times and
#: then continues with the next track. Entries without a mode stay "once", so
#: every pre-Phase-28 sequence keeps its exact historical behavior.
TRACK_PLAY_ONCE = "once"
TRACK_PLAY_LOOP = "loop"
TRACK_PLAY_REPEAT = "repeat"
TRACK_PLAYBACK_MODES = (TRACK_PLAY_ONCE, TRACK_PLAY_LOOP, TRACK_PLAY_REPEAT)
MAX_TRACK_REPEAT_COUNT = 1000

#: What happens after the final track of a sequence. ``loop_sequence`` is the
#: historical Phase-27 behavior: the ENTIRE ordered sequence loops as one unit
#: (A → B → C → A → B → C …). ``play_once`` ends the music after the last
#: track; the video continues without music.
SEQUENCE_LOOP = "loop_sequence"
SEQUENCE_PLAY_ONCE = "play_once"
SEQUENCE_MODES = (SEQUENCE_LOOP, SEQUENCE_PLAY_ONCE)

#: Safety bound for expanded segment plans; even a 2-second loop covering a
#: 100-minute render stays far below it.
MAX_PLAN_SEGMENTS = 5000


def normalize_playback_mode(value: object) -> str:
    """Return one canonical per-track playback mode (unknown → "once")."""
    key = str(value or "").strip().casefold()
    return key if key in TRACK_PLAYBACK_MODES else TRACK_PLAY_ONCE


def normalize_sequence_mode(value: object) -> str:
    """Return one canonical sequence mode (unknown/empty → "loop_sequence")."""
    key = str(value or "").strip().casefold()
    return key if key in SEQUENCE_MODES else SEQUENCE_LOOP


def normalize_music_track(value: object) -> dict | None:
    """Return one sanitized track dict, or None when the entry is unusable."""
    if isinstance(value, str):
        path = value.strip()
        if not path:
            return None
        return {
            "path": path, "trim_start": 0.0, "trim_duration": 0.0,
            "playback_mode": TRACK_PLAY_ONCE, "repeat_count": 1,
        }
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
    try:
        repeat_count = int(value.get("repeat_count", 1) or 1)
    except (TypeError, ValueError):
        repeat_count = 1
    mode = normalize_playback_mode(value.get("playback_mode"))
    if mode != TRACK_PLAY_REPEAT:
        repeat_count = 1
    return {
        "path": path,
        "trim_start": max(0.0, min(MAX_TRACK_TRIM_START, trim_start)),
        "trim_duration": max(0.0, min(MAX_TRACK_TRIM_DURATION, trim_duration)),
        "playback_mode": mode,
        "repeat_count": max(1, min(MAX_TRACK_REPEAT_COUNT, repeat_count)),
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


def effective_short_music_tracks(settings, anchor: object = None) -> list[dict]:
    """Return the authoritative ordered SHORTS sequence for one settings object.

    Strictly separate from :func:`effective_music_tracks`: the Long-Form
    sequence never leaks into a Short. Resolution order mirrors the
    historical single-track Shorts behavior exactly:

    1. A per-Short override (``short_music_overrides`` keyed by the Short's
       anchor voiceover) wins and is a one-track sequence.
    2. A populated ``short_music_tracks`` list (Phase 27 multi-track).
    3. The legacy single ``short_music_path`` migrates to a one-item sequence.
    4. No Shorts music at all (an empty list — a Short without its own track
       stays silent and never inherits the Long-Form track).
    """
    override = ""
    if anchor is not None:
        try:
            lookup = getattr(settings, "short_music_overrides", None) or {}
            override = str(lookup.get(str(anchor), "") or "").strip()
        except AttributeError:
            override = ""
    if override:
        return [{"path": override, "trim_start": 0.0, "trim_duration": 0.0}]
    tracks = normalize_music_tracks(getattr(settings, "short_music_tracks", None))
    if tracks:
        return tracks
    legacy = str(getattr(settings, "short_music_path", "") or "").strip()
    if legacy:
        return [{"path": legacy, "trim_start": 0.0, "trim_duration": 0.0}]
    return []


def music_track_paths(settings) -> list[Path]:
    """Return the ordered resolved file paths of the effective sequence."""
    return [
        Path(track["path"]).expanduser().resolve()
        for track in effective_music_tracks(settings)
    ]


def track_effective_duration(track: dict) -> float:
    """The probed playable duration of one track after its trim window."""
    return max(0.0, float(track.get("duration", 0.0) or 0.0))


def sequence_is_legacy(tracks: list[dict], sequence_mode: str) -> bool:
    """True when a sequence keeps the exact pre-Phase-28 render graph.

    A single track without trim stays legacy under the historical
    whole-sequence loop (the ``-stream_loop -1`` single-input graph); with
    ``play_once`` the music must end after that one play, which only the
    explicit segment graph can express. Multiple tracks stay legacy when every
    track simply plays once and the complete sequence loops as one unit — the
    Phase-27 whole-sequence loop graph, byte-identical to before.
    """
    mode = normalize_sequence_mode(sequence_mode)
    if not tracks:
        return True
    if len(tracks) == 1:
        first = tracks[0]
        return (
            mode == SEQUENCE_LOOP
            and float(first.get("trim_start", 0.0) or 0.0) <= 1e-9
            and float(first.get("trim_duration", 0.0) or 0.0) <= 1e-9
            and normalize_playback_mode(first.get("playback_mode")) == TRACK_PLAY_ONCE
        )
    return mode == SEQUENCE_LOOP and all(
        normalize_playback_mode(track.get("playback_mode")) == TRACK_PLAY_ONCE
        for track in tracks
    )


def build_music_segment_plan(
    tracks: list[dict],
    sequence_mode: str,
    target_duration: float,
) -> list[dict]:
    """Expand one sequence into the ordered segments covering a target.

    The walk follows the configured semantics exactly:

    * ``once`` appends one occurrence and continues with the next track.
    * ``repeat`` appends ``repeat_count`` occurrences and continues.
    * ``loop`` repeats this single track until the target is satisfied; later
      tracks are never reached — an infinite loop cannot hand over to a
      successor, and this is the documented, UI-visible meaning.
    * After the final track, ``loop_sequence`` wraps back to the first track
      (the historical whole-sequence loop), while ``play_once`` ends the
      music; the remaining video plays without music.

    Every emitted segment carries its source path, trim window and duration;
    the final segment is clamped to the remaining target so the graph always
    stops exactly at the audio boundary.
    """
    normalized = [track for track in (tracks or []) if str(track.get("path", "") or "").strip()]
    target = max(0.0, float(target_duration or 0.0))
    if not normalized or target <= 1e-9:
        return []
    loop_sequence = normalize_sequence_mode(sequence_mode) == SEQUENCE_LOOP
    segments: list[dict] = []
    position = 0.0
    index = 0
    while position < target - 1e-9 and len(segments) < MAX_PLAN_SEGMENTS:
        track = normalized[index]
        mode = normalize_playback_mode(track.get("playback_mode"))
        duration = track_effective_duration(track)
        if duration <= 1e-9:
            # An unprobeable track must never deadlock the plan; it is
            # skipped exactly like a zero-length source.
            index += 1
            if index >= len(normalized):
                if not loop_sequence:
                    break
                index = 0
                if all(track_effective_duration(t) <= 1e-9 for t in normalized):
                    break
            continue
        copies = MAX_TRACK_REPEAT_COUNT if mode == TRACK_PLAY_LOOP else (
            track.get("repeat_count", 1) if mode == TRACK_PLAY_REPEAT else 1
        )
        for _ in range(int(copies)):
            if position >= target - 1e-9 or len(segments) >= MAX_PLAN_SEGMENTS:
                break
            remaining = target - position
            segments.append({
                "path": str(track["path"]),
                "trim_start": float(track.get("trim_start", 0.0) or 0.0),
                "trim_duration": float(track.get("trim_duration", 0.0) or 0.0),
                "duration": min(duration, remaining),
            })
            position += min(duration, remaining)
            if mode == TRACK_PLAY_LOOP and position < target - 1e-9:
                continue
        index += 1
        if index >= len(normalized):
            if not loop_sequence:
                break
            index = 0
    return segments
