"""1.2.4: Große Video-Pools – Discovery, Required-Only-Auswahl, Order-Autorität.

Kernanforderung: Der Input-Ordner ist eine Quellbibliothek. Es werden NIE
alle Clips gerendert – nur der kürzeste geordnete Präfix, der die
Voiceover-Timeline deckt. Ungenutzte Clips tauchen weder im ``-i``-Input
noch im Filtergraph des Exports auf.
"""

from __future__ import annotations

import random
import time

import pytest

from app.video_merger.command_builder import FFmpegCommandBuilder
from app.video_merger.media_analyzer import MediaAnalyzer
from app.video_merger.models import ExportSettings, MediaInfo, ResolvedExport
from app.video_merger.video_pool import (
    MODE_EXACT,
    MODE_FULL,
    MODE_HOLD,
    MODE_LOOP,
    compute_pool_status,
    prefix_durations,
    required_prefix_length,
    select_required_media,
)
from tests.conftest import fake_media, make_clip

TARGET = 70.0
TRANSITION = 0.5
FPS = 30.0


def _pool(n: int, duration: float = 5.0) -> list[MediaInfo]:
    return [fake_media(f"clip_{i:04d}.mp4", duration=duration) for i in range(n)]


# --------------------------------------------------------------------------- #
# Required-Only-Mathematik (10 / 100 / 300 / 500 Clips)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n", [10, 100, 300, 500])
def test_pool_status_required_only_for_pool_sizes(n):
    media = _pool(n)
    status = compute_pool_status(media, TARGET, TRANSITION, FPS)

    assert status.total == n
    if n * 5.0 <= TARGET:  # Material deckt nicht -> alles wird gebraucht
        assert status.required == n and status.unused == 0
        return
    assert status.mode == MODE_EXACT
    assert 0 < status.required < n
    assert status.selected == status.required
    assert status.unused == n - status.required

    # Kürzester geordneter Präfix: ein Clip weniger reicht NICHT,
    # der gefundene Präfix reicht GENUG (Transition-Clamp inklusive).
    durations = [m.duration for m in media]
    prefixes = prefix_durations(durations, TRANSITION, FPS)
    assert prefixes[status.required - 1] >= TARGET - 1e-6
    if status.required > 1:
        assert prefixes[status.required - 2] < TARGET - 1e-6
    assert required_prefix_length(durations, TARGET, TRANSITION, FPS) == status.required


def test_300_pool_needs_14_to_16_not_300():
    """Spektrum-Beispiel: 300 verfügbar -> nur ~14-16 gerendert, 284+ unangetastet."""
    media = _pool(300)
    status = compute_pool_status(media, 70.0, TRANSITION, FPS)
    assert status.mode == MODE_EXACT
    assert 12 <= status.required <= 18, status.required
    assert status.unused >= 280, status.unused


def test_preprocessing_time_does_not_scale_with_unused_pool():
    """Gleiche Auswahl -> gleiche Vorverarbeitung, egal wie groß der Rest-Pool ist."""
    small = _pool(20)
    big = _pool(500)
    target = 70.0

    t0 = time.perf_counter()
    status_small = compute_pool_status(small, target, TRANSITION, FPS)
    small_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    status_big = compute_pool_status(big, target, TRANSITION, FPS)
    big_time = time.perf_counter() - t0

    # Beide decken das Ziel mit demselben Präfix ab.
    assert status_small.mode == status_big.mode == MODE_EXACT
    assert status_small.required == status_big.required
    # O(n)-Rechenzeit: 500 Clips dürfen nicht um Größenordlungen langsamer sein
    # (20 vs. 500 ≈ dieselbe Vorverarbeitung bei gleicher Auswahl).
    assert big_time < max(0.25, small_time * 40)


def test_selection_returns_exact_ordered_prefix_and_skips_unused():
    media = _pool(300)
    selected, _warnings = select_required_media(media, TARGET, TRANSITION, FPS)
    status = compute_pool_status(media, TARGET, TRANSITION, FPS)

    assert len(selected) == status.required
    selected_paths = [m.path.name for m in selected]
    assert selected_paths == [m.path.name for m in media[:status.required]]

    unused_paths = {m.path.name for m in media[status.required:]}
    assert not (set(selected_paths) & unused_paths)
    assert len(unused_paths) == 300 - status.required


def test_final_clip_is_trimmed_to_fit_target():
    media = _pool(300)
    selected, warnings = select_required_media(media, TARGET, TRANSITION, FPS)
    assert len(selected) >= 2
    last = selected[-1]
    # Der letzte Clip wird gekürzt: seine Timeline-Dauer ist kürzer als die
    # eigentliche Quell-Dauer (sonst wäre er zufällig exakt am Ziel gelandet).
    source = last.source_duration or last.duration
    assert last.duration < source - 0.005, (last.duration, source)
    assert any("geprüft" in w or "gekürzt" in w for w in warnings)


# --------------------------------------------------------------------------- #
# Order-Autorität: Natural / Manual / Randomized
# --------------------------------------------------------------------------- #


def test_manual_order_is_authoritative_for_selection():
    pool = _pool(300)
    manual = [pool[200], pool[5], pool[17]] + pool[18:200] + pool[201:]
    status = compute_pool_status(manual, TARGET, TRANSITION, FPS)
    selected, _w = select_required_media(manual, TARGET, TRANSITION, FPS)

    assert status.mode == MODE_EXACT
    assert len(selected) == status.required
    # Die Auswahl folgt der aktiven (manuellen) Reihenfolge – nicht der Dateinamen.
    assert [m.path.name for m in selected[:3]] == [
        "clip_0200.mp4", "clip_0005.mp4", "clip_0017.mp4",
    ]


def test_randomized_order_recalculates_immediately():
    pool = _pool(500)
    rng = random.Random(42)  # deterministischer Fisher-Yates
    shuffled = list(pool)
    rng.shuffle(shuffled)
    assert [m.path for m in shuffled] != [m.path for m in pool]

    status = compute_pool_status(shuffled, TARGET, TRANSITION, FPS)
    assert status.mode == MODE_EXACT
    assert 0 < status.required < 500
    selected, _w = select_required_media(shuffled, TARGET, TRANSITION, FPS)
    assert [m.path.name for m in selected] == [m.path.name for m in shuffled[:status.required]]


def test_short_material_falls_back_to_hold_or_loop_using_all_clips():
    media = _pool(5, duration=2.0)  # 10 s Material, Ziel 70 s
    hold = compute_pool_status(media, TARGET, TRANSITION, FPS, short_video_mode="hold")
    loop = compute_pool_status(media, TARGET, TRANSITION, FPS, short_video_mode="loop")
    assert hold.mode == MODE_HOLD and loop.mode == MODE_LOOP
    for status in (hold, loop):
        assert status.required == 5 and status.selected == 5 and status.unused == 0

    hold_media, hold_w = select_required_media(media, TARGET, TRANSITION, FPS, "hold")
    assert len(hold_media) == 5
    assert hold_media[-1].duration > media[-1].duration  # Hold Last Frame
    assert any("Hold" in w for w in hold_w)

    loop_media, loop_w = select_required_media(media, TARGET, TRANSITION, FPS, "loop")
    assert len(loop_media) > 5  # vollständige Sequenz wird wiederholt
    names = [m.path.name for m in loop_media]
    assert names[:5] == [m.path.name for m in media]  # A-B-C-D-E, dann A-B-…


def test_no_voiceover_target_renders_full_active_order():
    media = _pool(100)
    status = compute_pool_status(media, 0.0, TRANSITION, FPS)
    assert status.mode == MODE_FULL
    assert status.required == 100 and status.unused == 0


# --------------------------------------------------------------------------- #
# Ungenutzte Clips dürfen NICHT im finalen FFmpeg-Befehl stehen
# --------------------------------------------------------------------------- #


def test_final_ffmpeg_command_excludes_unused_pool_clips(tmp_path):
    """300 im Pool, 14-18 benötigt -> der Befehl kennt nur die benutzten Dateien."""
    media = _pool(300)
    selected, _w = select_required_media(media, TARGET, TRANSITION, FPS)
    status = compute_pool_status(media, TARGET, TRANSITION, FPS)
    unused_names = {m.path.name for m in media[status.required:]}

    settings = ExportSettings(
        workflow_stage="main",
        resolution="1920x1080",
        aspect="16:9",
        transition_duration=TRANSITION,
        voiceover_paths=[str(tmp_path / "voice.wav")],
        subtitle_enabled=False,
        watermark_enabled=False,
        crf=28,
        preset="fast",
        encoding="CPU",
        normalize_audio=False,
    )
    resolved = ResolvedExport(
        width=1920,
        height=1080,
        fps=FPS,
        fps_expr="30",
        effective_durations=[m.duration for m in selected],
        transitions=[TRANSITION] * max(0, len(selected) - 1),
        expected_duration=TARGET,
        warnings=[],
    )
    builder = FFmpegCommandBuilder("/nonexistent/ffmpeg")
    built = builder.build(selected, settings, resolved, tmp_path / "out.mp4")
    command_text = " ".join(built.command)

    # Kein einzelner ungenutzter Clip-Name im gesamten Befehl
    # (kein Decode, kein Filter, kein Übergang, kein Encode).
    for name in sorted(unused_names):
        assert name not in command_text, name
    # … aber jeder ausgewählte Clip genau einmal als -i-Input.
    for m in selected:
        assert command_text.count(str(m.path)) == 1, m.path
    # Die Anzahl der Video-Inputs entspricht der Auswahl, nicht dem Pool.
    input_count = command_text.count("-i ")
    assert input_count == len(selected) + 1  # +1 Voiceover
    assert built.command[0] == "/nonexistent/ffmpeg"


# --------------------------------------------------------------------------- #
# E2E: Leichtgewichtige Metadata-Discovery (ffprobe, nie Voll-Decode) + Cache
# --------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_metadata_discovery_is_lightweight_and_cached(ffmpeg_paths, tmp_path):
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "pool"
    paths = []
    for i in range(10):
        path = folder / f"video_{i:02d}.mp4"
        make_clip(ffmpeg, path, duration=0.7, color="red" if i % 2 else "blue")
        paths.append(path)

    cache = tmp_path / "media-cache.json"
    analyzer = MediaAnalyzer(ffprobe, cache_path=cache)
    t0 = time.perf_counter()
    infos = analyzer.analyze_many(paths)
    first_pass = time.perf_counter() - t0
    assert first_pass < 30, first_pass

    # Metadata vollständig, ohne Voll-Decode (ffprobe-only):
    for info, path in zip(infos, paths):
        assert info.duration == pytest.approx(0.7, abs=0.05)
        assert (info.width, info.height) == (160, 90)
        assert info.fps == pytest.approx(30.0, abs=0.5)
        assert info.video_codec
        assert info.audio.present is True
        assert info.path == path.resolve() or info.path.name == path.name

    # Zweiter Lauf: reines Stat-Cache – ffprobe wird NICHT erneut aufgerufen.
    second = MediaAnalyzer(ffprobe, cache_path=cache)
    calls = {"probe": 0}
    real_probe = second.probe_raw

    def counted_probe(path):
        calls["probe"] += 1
        return real_probe(path)

    second.probe_raw = counted_probe
    logs: list[str] = []
    t0 = time.perf_counter()
    cached = second.analyze_many(paths, log=logs.append)
    cache_pass = time.perf_counter() - t0
    assert calls["probe"] == 0
    assert sum(1 for line in logs if "Cache" in line) == 10
    assert cache_pass <= max(first_pass * 3, 0.5)  # Cache-Lauf ohne Decode
    for info, original in zip(cached, infos):
        assert info.duration == pytest.approx(original.duration)
        assert (info.width, info.height) == (original.width, original.height)


@pytest.mark.e2e
def test_discovery_handles_100_pool_with_ffprobe(ffmpeg_paths, tmp_path):
    """100 reale Dateien: Discovery liefert vollständige Metadata in vertretbarer Zeit."""
    ffmpeg, ffprobe = ffmpeg_paths
    folder = tmp_path / "pool100"
    paths = []
    for i in range(100):
        path = folder / f"c{i:03d}.mp4"
        make_clip(ffmpeg, path, size="64x36", duration=0.4, audio_rate=None)
        paths.append(path)

    analyzer = MediaAnalyzer(ffprobe, cache_path=tmp_path / "cache100.json")
    t0 = time.perf_counter()
    infos = analyzer.analyze_many(paths)
    elapsed = time.perf_counter() - t0
    assert len(infos) == 100
    assert elapsed < 60, elapsed  # nur ffprobe, kein Voll-Decode
    assert all(info.duration > 0 for info in infos)
