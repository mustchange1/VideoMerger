"""1.2.4: Große Video-Pools – Metadata-Discovery und Required-Only-Auswahl.

Der Input-Ordner ist eine *Quellbibliothek*, keine Render-Warteschlange.
VideoMerger rendert Pool-Clips nie vor; die FFmpeg-Pipeline verarbeitet nur
die Clips, die die aktive Reihenfolge braucht, um die Voiceover-Timeline zu
bedecken. Sobald das Ziel abgedeckt ist, stoppt die Auswahl – der Rest bleibt
unangetastet (kein Decode, kein Filter, kein Übergang, kein Encode).

Die Auswahl-Mathematik ist identisch mit der echten Render-Timeline
(:mod:`timeline` / :func:`fit_media_to_duration`): derselbe
Transition-Clamp-Algorithmus, dasselbe kürzeste-geordneter-Präfix-Prinzip.
Damit entspricht "Benötigt" in der GUI exakt dem, was der Export rendert.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import MediaInfo
from .target import safe_transition_durations

MODE_EXACT = "exact"          # Ziel wird von einem Präfix der aktiven Reihenfolge gedeckt
MODE_HOLD = "hold"            # Material zu kurz → alle Clips + Hold Last Frame
MODE_LOOP = "loop"            # Material zu kurz → alle Clips, Full-Timeline-Loop
MODE_FULL = "full"            # keine Voiceover-Zielgröße → komplette aktive Reihenfolge


@dataclass(frozen=True)
class PoolStatus:
    """Render-Status eines Video-Pools für die aktuelle aktive Reihenfolge."""

    total: int
    required: int
    selected: int
    unused: int
    target_duration: float
    mode: str
    covered: bool

    @property
    def summary_line(self) -> str:
        """Einzelner GUI-Text für die Pool-Zusammenfassung."""
        if self.total == 0:
            return "Video-Pool: keine Dateien erkannt."
        target = f"{self.target_duration:.2f} s" if self.target_duration > 0 else "–"
        if self.mode == MODE_FULL:
            return (
                f"Video-Pool: {self.total} Dateien im Input-Ordner · alle {self.total} werden "
                f"gerendert (keine Voiceover-Zielgröße) · nicht verwendet: 0 · Ziel: {target}"
            )
        if self.mode == MODE_EXACT:
            return (
                f"Video-Pool: {self.total} Dateien im Input-Ordner · benötigt: {self.required} · "
                f"ausgewählt: {self.selected} · nicht verwendet: {self.unused} · "
                f"Ziel-Dauer: {target}"
            )
        extra = "Full-Timeline-Loop" if self.mode == MODE_LOOP else "Hold Last Frame"
        return (
            f"Video-Pool: {self.total} Dateien im Input-Ordner · benötigt: {self.required} (alles) · "
            f"ausgewählt: {self.selected} · nicht verwendet: {self.unused} · Ziel-Dauer: {target} · "
            f"Material zu kurz → {extra}"
        )


def _effective_and_transitions(
    durations: list[float], transition_duration: float, fps: float
) -> tuple[list[float], list[float]]:
    """O(n)-Vorverarbeitung exakt nach :func:`target.safe_transition_durations`."""
    effective, transitions = safe_transition_durations(durations, transition_duration, fps)
    return effective, transitions


def prefix_durations(
    durations: list[float], transition_duration: float, fps: float
) -> list[float]:
    """Dauer jedes geordneten Präfixes [1..n] in O(n).

    ``prefix_durations[k]`` ist die logische Kettendauer der ersten ``k + 1``
    Clips (inklusive ihrer k internen Übergänge) – dieselbe Größe, mit der
    :func:`timeline._duration` und :func:`timeline.fit_media_to_duration`
    entscheiden, ob die Stimme abgedeckt ist.
    """
    effective, transitions = _effective_and_transitions(durations, transition_duration, fps)
    result: list[float] = []
    running = 0.0
    for index, value in enumerate(effective):
        if index > 0:
            running -= transitions[index - 1]
        running += value
        result.append(running)
    return result


def required_prefix_length(
    durations: list[float],
    target_duration: float,
    transition_duration: float,
    fps: float,
) -> int:
    """Anzahl Clips des kürzesten geordneten Präfixes, das das Ziel deckt.

    Gibt ``len(durations)`` zurück, wenn nicht einmal die komplette aktive
    Reihenfolge ausreicht (dann greift Hold Last Frame / Full-Timeline Loop).
    """
    if not durations:
        return 0
    if target_duration <= 0:
        return len(durations)
    # Gleiche Untergrenze wie timeline.fit_media_to_duration, damit GUI-Status
    # und Render-Timeline identisch entscheiden.
    minimum = max(0.12, 3.0 / max(fps, 1.0))
    durations = [max(value, minimum) for value in durations]
    for index, value in enumerate(prefix_durations(durations, transition_duration, fps), start=1):
        if value >= target_duration - 1e-6:
            return index
    return len(durations)


def compute_pool_status(
    media: list[MediaInfo],
    target_duration: float,
    transition_duration: float,
    fps: float,
    short_video_mode: str = "hold",
) -> PoolStatus:
    """Berechnet Required / Selected / Not-Used für die aktuelle aktive Reihenfolge.

    * Ohne Voiceover-Ziel (``target_duration <= 0``) wird die komplette aktive
      Reihenfolge gerendert – der Klassik-Workflow bleibt erhalten.
    * Deckt ein Präfix das Ziel, sind genau diese Clips "benötigt"; alle
      Clips dahinter sind "nicht verwendet" und erscheinen weder im
      ``-i``-Input noch im Filtergraph des Exports.
    * Deckt nichts das Ziel, werden alle Clips gebraucht und Hold Last Frame
      bzw. Full-Timeline Loop ergänzt die fehlende Zeit (wie in
      :mod:`timeline`) – es gibt dann keine ungenutzten Clips.
    """
    total = len(media)
    target = max(0.0, float(target_duration or 0.0))
    if total == 0:
        return PoolStatus(0, 0, 0, 0, target, MODE_FULL, False)
    if target <= 0:
        return PoolStatus(total, total, total, 0, 0.0, MODE_FULL, True)

    minimum = max(0.12, 3.0 / max(fps, 1.0))
    durations = [max(item.duration, minimum) for item in media]
    one_pass = prefix_durations(durations, transition_duration, fps)
    if one_pass and one_pass[-1] >= target - 1e-6:
        required = required_prefix_length(durations, target, transition_duration, fps)
        return PoolStatus(total, required, required, total - required, target, MODE_EXACT, True)

    mode = MODE_LOOP if short_video_mode == "loop" else MODE_HOLD
    return PoolStatus(total, total, total, 0, target, mode, False)


def select_required_media(
    media: list[MediaInfo],
    target_duration: float,
    transition_duration: float,
    fps: float,
    short_video_mode: str = "hold",
) -> tuple[list[MediaInfo], list[str]]:
    """Liefert exakt die Clips, die die FFmpeg-Pipeline verarbeiten soll.

    Deckt das Material das Ziel, ist das Ergebnis der kürzeste geordnete
    Präfix (letzter Clip gekürzt) – ungenutzte Pool-Dateien sind enthalten
    *nicht*. Ist das Ziel nicht deckbar, übernimmt
    :func:`timeline.fit_media_to_duration` (Hold Last Frame bzw.
    Full-Timeline-Loop) und verwendet alle Clips der aktiven Reihenfolge.
    """
    from .timeline import fit_media_to_duration

    return fit_media_to_duration(
        media, target_duration, transition_duration, fps, short_video_mode
    )
