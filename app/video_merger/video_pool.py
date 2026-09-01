"""1.2.4: Große Video-Pools – Metadata-Discovery und Required-Only-Auswahl.

Der Input-Ordner ist eine *Quellbibliothek*, keine Render-Warteschlange.
VideoMerger rendert Pool-Clips nie vor; die FFmpeg-Pipeline verarbeitet nur
die Clips, die die aktive Reihenfolge braucht, um die Voiceover-Timeline zu
bedecken. Sobald das Ziel abgedeckt ist, stoppt die Auswahl – der Rest bleibt
unangetastet (kein Decode, kein Filter, kein Übergang, kein Encode).

Die Auswahl-Mathematik ist identisch mit der echten Render-Timeline
(:mod:`timeline` / :func:`timeline.fit_media_to_duration`): derselbe
Transition-Clamp-Algorithmus, dasselbe kürzeste-geordneter-Präfix-Prinzip.
1.3.0: Duration-Fit-Modus (Cut/Stretch) und die globale Video-Geschwindigkeit
fließen in dieselbe Entscheidung ein, damit GUI-Status und Render-Timeline
identisch entscheiden.

1.3.0 Effizienz: :func:`prefix_durations` berechnet alle Präfix-Summen in
einem einzigen O(n)-Durchlauf (ein einziger ``safe_transition_durations``
Aufruf); :func:`compute_pool_status` leitet „benötigt“ aus genau dieser
einen Liste ab statt sie doppelt zu berechnen.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import random
from collections import OrderedDict
from pathlib import Path

from .models import MediaInfo
from .project_order import natural_sort_key, randomize_order
from .target import safe_transition_durations

MODE_EXACT = "exact"          # Ziel wird von einem Präfix der aktiven Reihenfolge gedeckt
MODE_HOLD = "hold"            # Material zu kurz → alle Clips + Hold Last Frame
MODE_LOOP = "loop"            # Material zu kurz → alle Clips, Full-Timeline-Loop
MODE_FULL = "full"            # keine Voiceover-Zielgröße → komplette aktive Reihenfolge

# Project-level video ordering. ``folder_alternating`` is retained as a
# compatibility value for projects created before the four-mode selector was
# introduced; it has the old Natural/folder-aware semantics and is never shown
# as a separate current GUI option.
VIDEO_ORDER_NATURAL = "natural"
VIDEO_ORDER_ALPHABETICAL = "alphabetical"
VIDEO_ORDER_RANDOM = "random"
VIDEO_ORDER_MANUAL = "manual"
VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING = "folder_alternating"
VIDEO_ORDER_MODES = (
    VIDEO_ORDER_NATURAL,
    VIDEO_ORDER_ALPHABETICAL,
    VIDEO_ORDER_RANDOM,
    VIDEO_ORDER_MANUAL,
)


def normalize_video_order_mode(value: object, default: str = VIDEO_ORDER_NATURAL) -> str:
    """Normalize persisted/API video-order values without losing legacy data."""
    candidate = str(value or "").strip().casefold()
    aliases = {
        "natural / alphabetical": VIDEO_ORDER_NATURAL,
        "alphabetic": VIDEO_ORDER_ALPHABETICAL,
        "randomized": VIDEO_ORDER_RANDOM,
        "folder-aware": VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING,
        "folder_alternating": VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING,
    }
    candidate = aliases.get(candidate, candidate)
    allowed = set(VIDEO_ORDER_MODES) | {VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING}
    return candidate if candidate in allowed else default


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


def media_source_folder(item: MediaInfo) -> str:
    """Return a stable folder key for old and new MediaInfo objects."""
    value = str(getattr(item, "source_folder", "") or "").strip()
    if value:
        return os.path.normcase(str(Path(value).expanduser().resolve()))
    return os.path.normcase(str(item.path.expanduser().resolve().parent))


def folder_aware_order(
    media: list[MediaInfo],
    *,
    rng: random.Random | None = None,
    seed: int | None = None,
) -> list[MediaInfo]:
    """Interleave source folders without adjacent duplicates when possible.

    The order within each source folder is never changed. Folder selection is
    randomized among all eligible alternatives; without an explicit RNG a
    stable content-derived seed keeps One-Click/cache runs reproducible while
    still producing a non-natural folder sequence. Once only one folder has
    remaining clips, same-folder continuation is the deliberate fallback.
    """
    if len(media) < 2:
        return list(media)
    buckets: "OrderedDict[str, list[MediaInfo]]" = OrderedDict()
    for item in media:
        buckets.setdefault(media_source_folder(item), []).append(item)
    if len(buckets) < 2:
        return list(media)
    if rng is None:
        if seed is None:
            token = "\0".join(
                f"{media_source_folder(item)}\0{item.path.expanduser().resolve()}" for item in media
            )
            seed = int(hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()[:16], 16)
        rng = random.Random(seed)
    remaining = {key: list(values) for key, values in buckets.items()}
    result: list[MediaInfo] = []
    previous: str | None = None
    remaining_total = len(media)
    while remaining_total:
        eligible = [key for key, values in remaining.items() if values and key != previous]
        if not eligible:
            eligible = [key for key, values in remaining.items() if values]
        chosen = rng.choice(eligible)
        result.append(remaining[chosen].pop(0))
        remaining_total -= 1
        previous = chosen
    return result


def order_media_for_video_order(
    media: list[MediaInfo],
    mode: str = VIDEO_ORDER_NATURAL,
    *,
    rng: random.Random | None = None,
    seed: int | None = None,
) -> list[MediaInfo]:
    """Return one effective project sequence before duration selection.

    The order contract lives here so the GUI preview, Required-Only pool
    mathematics, Main Project workflow, CLI, and direct engine callers all
    consume the same sequence:

    * Natural sorts each source folder by the existing numeric-aware natural
      key, then applies the established no-adjacent-folder rule.
    * Alphabetical sorts by filename (case-insensitive); folder alternation is
      still applied when multiple source folders are configured.
    * Random first makes a genuine Fisher-Yates permutation of the current
      pool, then applies folder alternation. The injected RNG/seed is useful
      for tests and reproducible callers; normal exports use a fresh RNG.
    * Manual never changes the supplied sequence.

    The legacy ``folder_alternating`` value deliberately preserves the old
    behavior of using the caller's persisted per-folder sequence. It is an
    alias for the former automatic mode, not a fifth user-facing mode.
    """
    values = list(media)
    normalized = normalize_video_order_mode(mode)
    if len(values) < 2 or normalized == VIDEO_ORDER_MANUAL:
        return values

    if normalized == VIDEO_ORDER_LEGACY_FOLDER_ALTERNATING:
        return folder_aware_order(values, rng=rng, seed=seed)

    if normalized == VIDEO_ORDER_RANDOM:
        if rng is None:
            rng = random.Random(seed) if seed is not None else random.Random()
        # Shuffle clips before choosing folders. This keeps the random mode a
        # permutation of the current pool while folder-aware selection only
        # constrains adjacency and never changes within-folder queue order.
        shuffled = randomize_order(values, rng)
        return folder_aware_order(shuffled, rng=rng)

    if normalized == VIDEO_ORDER_ALPHABETICAL:
        ordered = sorted(
            values,
            key=lambda item: (
                str(item.path.name).casefold(),
                str(item.path.expanduser().resolve()).casefold(),
            ),
        )
    else:
        # Natural order is numeric-aware within each source folder. Keeping the
        # folder key first means the subsequent alternator can retain each
        # folder queue exactly while choosing among folders fairly.
        ordered = sorted(
            values,
            key=lambda item: (
                media_source_folder(item),
                natural_sort_key(str(item.path.name)),
                str(item.path.expanduser().resolve()).casefold(),
            ),
        )
    return folder_aware_order(ordered, rng=rng, seed=seed)


# Descriptive alias for callers that do not need to know the historical module
# name. Both names intentionally share the exact implementation.
effective_video_order = order_media_for_video_order


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


def required_selection_length(
    durations: list[float],
    target_duration: float,
    transition_duration: float,
    fps: float,
    duration_fit_mode: str = "cut",
    max_stretch_percent: float = 10.0,
    prefixes: list[float] | None = None,
) -> int:
    """1.3.0: ausgewählte Clip-Anzahl inkl. Smart Stretch — reine Dauer-Mathematik.

    Spiegelt exakt die Präfix-Auswahl von
    :func:`timeline.fit_media_to_duration` für den Material-reicht-Fall: bei
    ``stretch`` kann der finale Clip des Präfixes minimal gedehnt werden —
    dann genügt eventuell ein Clip weniger (kürzeste zusammenhängende
    Auswahl, keine Sliver-Clips). Liegt die nötige Dehnung über dem Limit,
    gilt wie im Render das normale Kürzen (gleiche Anzahl wie ``cut``).
    ``prefixes`` erlaubt das Wiederverwenden einer bereits berechneten
    Präfix-Liste (keine erneute O(n)-Transition-Berechnung).
    """
    if not durations:
        return 0
    if target_duration <= 0:
        return len(durations)
    minimum = max(0.12, 3.0 / max(fps, 1.0))
    clamped = [max(value, minimum) for value in durations]
    if prefixes is None or len(prefixes) != len(clamped):
        prefixes = prefix_durations(clamped, transition_duration, fps)
    covered = next(
        (index + 1 for index, value in enumerate(prefixes) if value >= target_duration - 1e-6),
        len(clamped),
    )
    if duration_fit_mode != "stretch" or covered <= 1:
        return covered
    limit = max(0.0, min(50.0, float(max_stretch_percent))) / 100.0
    shorter_count = covered - 1
    deficit = target_duration - prefixes[shorter_count - 1]
    if deficit <= 0:
        return covered
    # Spiegelt timeline.fit_media_to_duration: natural_last ist die
    # geschwindigkeitsskalierte Timeline-Dauer des letzten Clips des
    # kürzeren Präfixes; die Dehnung wird relativ dazu begrenzt.
    natural_last = clamped[shorter_count - 1]
    if deficit / max(natural_last, 1e-9) <= limit + 1e-9:
        return shorter_count
    return covered


def compute_pool_status(
    media: list[MediaInfo],
    target_duration: float,
    transition_duration: float,
    fps: float,
    short_video_mode: str = "hold",
    duration_fit_mode: str = "cut",
    max_stretch_percent: float = 10.0,
    playback_rate: float = 1.0,
    folder_aware: bool = True,
    *,
    video_order_mode: str | None = None,
    video_order_rng: random.Random | None = None,
    video_order_seed: int | None = None,
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

    1.3.0: ``playback_rate`` skaliert die Timeline-Dauern wie der Render
    (Global Video Speed); ``duration_fit_mode``/``max_stretch_percent``
    spiegeln die Smart-Stretch-Auswahl.
    """
    total = len(media)
    target = max(0.0, float(target_duration or 0.0))
    if total == 0:
        return PoolStatus(0, 0, 0, 0, target, MODE_FULL, False)
    if target <= 0:
        return PoolStatus(total, total, total, 0, 0.0, MODE_FULL, True)

    minimum = max(0.12, 3.0 / max(fps, 1.0))
    rate = max(0.5, min(2.0, float(playback_rate or 1.0)))
    # Identisch zur Render-Timeline: _source_copy setzt die Timeline-Dauer auf
    # max(minimum, source/rate) — nicht max(source, minimum)/rate.
    if video_order_mode is not None:
        active_media = order_media_for_video_order(
            media, video_order_mode, rng=video_order_rng, seed=video_order_seed,
        )
    else:
        active_media = folder_aware_order(media) if folder_aware else list(media)
    durations = [max(minimum, item.duration / rate) for item in active_media]
    # Ein einziger O(n)-Lauf; „benötigt“ wird aus derselben Liste abgeleitet
    # (keine zweite Transition-Berechnung, keine erneute Sortierung).
    prefixes = prefix_durations(durations, transition_duration, fps)
    if prefixes and prefixes[-1] >= target - 1e-6:
        required = next(
            (index + 1 for index, value in enumerate(prefixes) if value >= target - 1e-6),
            total,
        )
        if duration_fit_mode == "stretch" and required > 1:
            required = required_selection_length(
                durations, target, transition_duration, fps,
                duration_fit_mode, max_stretch_percent, prefixes=prefixes,
            )
        return PoolStatus(total, required, required, total - required, target, MODE_EXACT, True)

    mode = MODE_LOOP if short_video_mode == "loop" else MODE_HOLD
    return PoolStatus(total, total, total, 0, target, mode, False)


def select_required_media(
    media: list[MediaInfo],
    target_duration: float,
    transition_duration: float,
    fps: float,
    short_video_mode: str = "hold",
    duration_fit_mode: str = "cut",
    max_stretch_percent: float = 10.0,
    playback_rate: float = 1.0,
    folder_aware: bool = True,
    *,
    video_order_mode: str | None = None,
    video_order_rng: random.Random | None = None,
    video_order_seed: int | None = None,
) -> tuple[list[MediaInfo], list[str]]:
    """Liefert exakt die Clips, die die FFmpeg-Pipeline verarbeiten soll.

    Deckt das Material das Ziel, ist das Ergebnis der kürzeste geordneter
    Präfix (letzter Clip gekürzt oder — bei Duration Fit Mode „Stretch“ —
    minimal gedehnt); ungenutzte Pool-Dateien sind enthalten *nicht*. Ist das
    Ziel nicht deckbar, übernimmt :func:`timeline.fit_media_to_duration`
    (Hold Last Frame bzw. Full-Timeline-Loop) und verwendet alle Clips der
    aktiven Reihenfolge.
    """
    from .timeline import fit_media_to_duration

    return fit_media_to_duration(
        media, target_duration, transition_duration, fps, short_video_mode,
        duration_fit_mode=duration_fit_mode,
        max_stretch_percent=max_stretch_percent,
        playback_rate=playback_rate,
        folder_aware=folder_aware,
        video_order_mode=video_order_mode,
        video_order_rng=video_order_rng,
        video_order_seed=video_order_seed,
    )
