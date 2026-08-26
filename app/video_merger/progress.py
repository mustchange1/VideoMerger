from __future__ import annotations

import time

from .models import MediaSequence, ProgressEvent, ResolvedExport


class ProgressTracker:
    def __init__(
        self,
        media: MediaSequence,
        resolved: ResolvedExport,
        transition_name: str = "Smooth Blur Crossfade",
    ):
        self.media = list(media)
        self.resolved = resolved
        self.transition_name = transition_name
        self.started = time.monotonic()
        self._starts: list[float] = []
        cursor = 0.0
        for index, duration in enumerate(resolved.effective_durations):
            self._starts.append(cursor)
            transition = resolved.transitions[index] if index < len(resolved.transitions) else 0.0
            cursor += duration - transition

    def event(self, out_time: float) -> ProgressEvent:
        total = max(self.resolved.expected_duration, 0.001)
        percent = max(0.0, min(99.5, out_time / total * 100.0))
        elapsed = time.monotonic() - self.started
        remaining = None
        if out_time > 0.05:
            remaining = max(0.0, elapsed * (total - out_time) / out_time)
        clip_index = 0
        for index, start in enumerate(self._starts):
            if out_time >= start:
                clip_index = index
        clip_index = min(clip_index, len(self.media) - 1)
        stage = f"Verarbeite Clip {clip_index + 1}/{len(self.media)}"
        current_file = self.media[clip_index].path.name
        for transition_index, transition_duration in enumerate(self.resolved.transitions):
            transition_start = self._starts[transition_index + 1]
            if transition_start <= out_time <= transition_start + transition_duration:
                stage = f"Übergang {transition_index + 1}/{len(self.resolved.transitions)} · {self.transition_name}"
                current_file = f"{self.media[transition_index].path.name} → {self.media[transition_index + 1].path.name}"
                break
        if percent >= 98:
            stage = "Finalisiere MP4 …"
        return ProgressEvent(
            percent=percent,
            out_time=out_time,
            total_time=total,
            elapsed=elapsed,
            remaining=remaining,
            stage=stage,
            current_file=current_file,
        )

    def completed(self) -> ProgressEvent:
        elapsed = time.monotonic() - self.started
        return ProgressEvent(100.0, self.resolved.expected_duration, self.resolved.expected_duration, elapsed, 0.0, "Export abgeschlossen", "")
