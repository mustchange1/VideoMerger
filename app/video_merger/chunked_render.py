"""Planning helpers for automatic Windows-safe segmented rendering.

A segment ends only after an existing visual transition has completed. The
first occurrence of the next clip is intentionally present in both neighboring
segment commands; the second segment trims that already-rendered transition
prefix before assembly. The assembled output therefore contains each logical
frame exactly once while each FFmpeg command contains a bounded input set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence


# Windows CreateProcess has a much smaller practical limit than the theoretical
# command-line limit once quoting, environment and FFmpeg parsing are included.
# Keep the existing 30,000-character guard as the final backstop, but plan well
# below it so ordinary variation in paths cannot make a segment unsafe.
WINDOWS_COMMAND_LIMIT = 30_000
SAFE_COMMAND_TARGET = 26_000


class ChunkingError(RuntimeError):
    """Raised when one indivisible clip cannot fit in a safe segment."""


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    number: int
    media_start: int
    media_stop: int  # exclusive; the overlap clip is included here when needed
    logical_start: float
    logical_end: float
    duration: float
    video_window_start: float
    audio_window_start: float
    next_start: int | None = None

    @property
    def media_count(self) -> int:
        return self.media_stop - self.media_start


def _boundary_before_clip(
    durations: Sequence[float], transitions: Sequence[float], clip_index: int,
) -> float:
    """Timestamp after the transition before ``clip_index`` has completed."""
    if clip_index <= 0:
        return 0.0
    boundary_transition = float(transitions[clip_index - 1])
    if boundary_transition > 1e-9:
        # The preceding command must include the next clip to render the
        # complete transition. The boundary is after that transition.
        return (
            sum(float(value) for value in durations[: clip_index + 1])
            - sum(float(value) for value in transitions[:clip_index])
        )
    # With no transition the safe boundary is the ordinary end of the prior
    # clip; no overlap is necessary or desirable.
    return (
        sum(float(value) for value in durations[:clip_index])
        - sum(float(value) for value in transitions[: clip_index - 1])
    )


def plan_segments(
    durations: Sequence[float],
    transitions: Sequence[float],
    fits: Callable[[int, int, float, float, float, float], bool],
) -> list[ChunkPlan]:
    """Greedily choose the largest safe transition-aware segment prefix.

    ``fits`` receives ``(media_start, media_stop, logical_start,
    logical_end, video_window_start, duration)`` and must measure the actual
    generated command, not merely the number of paths. Candidates grow until
    the conservative command target would be exceeded. The final candidate is
    allowed to end inside the final clip because that is the already-supported
    voiceover/end-padding trim; all intermediate boundaries are post-transition
    boundaries.
    """
    count = len(durations)
    if count == 0:
        return []
    if len(transitions) != max(0, count - 1):
        raise ChunkingError("Die Chunk-Planung erhielt keine gültigen Übergangsdauern.")
    total = sum(float(value) for value in durations) - sum(float(value) for value in transitions)
    if total <= 0:
        raise ChunkingError("Die visuelle Timeline ist für Chunked Rendering leer.")

    starts = [_boundary_before_clip(durations, transitions, index) for index in range(count)]
    plans: list[ChunkPlan] = []
    start = 0
    number = 1
    while start < count:
        logical_start = starts[start]
        video_window_start = 0.0 if start == 0 else max(0.0, float(transitions[start - 1]))
        best: ChunkPlan | None = None

        # Intermediate candidates end at the boundary before ``next_start``
        # and include that next clip so its existing cross-dissolve is rendered
        # exactly once in the preceding segment.
        for next_start in range(start + 1, count):
            logical_end = starts[next_start]
            duration = logical_end - logical_start
            overlap = float(transitions[next_start - 1]) > 1e-9
            candidate = ChunkPlan(
                number=number,
                media_start=start,
                media_stop=next_start + (1 if overlap else 0),
                logical_start=logical_start,
                logical_end=logical_end,
                duration=duration,
                video_window_start=video_window_start,
                audio_window_start=logical_start,
                next_start=next_start,
            )
            if not fits(
                candidate.media_start, candidate.media_stop,
                candidate.logical_start, candidate.logical_end,
                candidate.video_window_start, candidate.duration,
            ):
                break
            best = candidate

        # The final candidate ends at the exact logical timeline endpoint. It
        # is also the only candidate that may represent a partial final clip.
        final_candidate = ChunkPlan(
            number=number,
            media_start=start,
            media_stop=count,
            logical_start=logical_start,
            logical_end=total,
            duration=total - logical_start,
            video_window_start=video_window_start,
            audio_window_start=logical_start,
        )
        if fits(
            final_candidate.media_start, final_candidate.media_stop,
            final_candidate.logical_start, final_candidate.logical_end,
            final_candidate.video_window_start, final_candidate.duration,
        ):
            best = final_candidate

        if best is None:
            raise ChunkingError(
                "Ein einzelner Clip bzw. seine notwendige Übergangsgrenze "
                "passt nicht in den sicheren FFmpeg-Befehl."
            )
        plans.append(best)
        if best.next_start is None:
            break
        # With a transition, the next segment starts with the overlap clip;
        # without one, it starts at the ordinary next clip boundary.
        start = best.next_start
        number += 1
    return plans
