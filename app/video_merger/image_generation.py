"""Phase 31: Smart Visual Hybrid - local image generation providers.

This module defines the provider abstraction (spec sections 13-17, 23, 31,
33, 49) and ships two honest, strictly optional backends:

* :class:`DiffusersProvider` - a real local diffusion-model backend. It only
  reports availability; when the ``diffusers``/``torch`` stack (or a model)
  is missing it degrades to "unavailable" with clear diagnostics and never
  crashes. No paid/cloud service is ever required.

* :class:`SyntheticConceptProvider` - a deterministic, dependency-free
  DEMO/TEST backend that renders abstract concept art with FFmpeg lavfi
  gradients seeded by the prompt hash. It exists so the full Smart Visual
  pipeline (prompt building, caching, plan application, render integration)
  can be exercised and tested in environments without a real model. It is
  clearly labelled as synthetic in every diagnostic and never presented as
  AI image generation.

Generated images are cached content-addressed (sha256 over prompt +
provider + model + style + size + params + seed), so identical requests are
produced exactly once and reused across projects.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------
def generation_cache_key(
    prompt: str,
    provider: str,
    model: str,
    style: str,
    width: int,
    height: int,
    params: dict | None = None,
    seed: int = 0,
) -> str:
    """Deterministic content identity of one generation request.

    Every input that can change the pixels is part of the key, so cache
    reuse is exact and a changed prompt/style/size always misses.
    """
    payload = "|".join(
        [
            str(prompt or "").strip().casefold(),
            str(provider or "").strip(),
            str(model or "").strip(),
            str(style or "").strip(),
            str(int(width)),
            str(int(height)),
            ",".join(f"{k}={params[k]}" for k in sorted(params or {})),
            str(int(seed)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class GenerationResult:
    path: Path | None = None
    diagnostics: list[str] = field(default_factory=list)


class ImageGenerationProvider:
    """Contract every local generation backend implements."""

    name: str = "base"
    model: str = ""

    def is_available(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def diagnostics(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def generate(
        self,
        prompt: str,
        width: int,
        height: int,
        *,
        style: str = "",
        seed: int = 0,
        cache_dir: Path,
        params: dict | None = None,
    ) -> GenerationResult:  # pragma: no cover - interface
        raise NotImplementedError

    def get_cache_key(
        self, prompt: str, width: int, height: int, *, style: str = "", seed: int = 0, params: dict | None = None
    ) -> str:
        return generation_cache_key(prompt, self.name, self.model, style, width, height, params, seed)


class DiffusersProvider(ImageGenerationProvider):
    """Real local generation through Hugging Face ``diffusers``.

    Availability is checked WITHOUT importing heavy model weights up front:
    the import probe runs only here, and a missing stack simply marks the
    provider unavailable (spec section 49: document honestly when a real
    model cannot run in the environment - never fake it).
    """

    name = "diffusers"
    model = "stable-diffusion-local"

    def __init__(self) -> None:
        self._availability: tuple[bool, list[str]] | None = None

    def is_available(self) -> bool:
        return self._check()[0]

    def diagnostics(self) -> list[str]:
        return list(self._check()[1])

    def _check(self) -> tuple[bool, list[str]]:
        if self._availability is not None:
            return self._availability
        notes: list[str] = []
        try:
            import diffusers  # type: ignore  # noqa: F401
        except Exception as exc:
            notes.append(f"diffusers nicht verfügbar: {exc.__class__.__name__}")
            self._availability = (False, notes + [
                "Lokale Diffusion-Erzeugung deaktiviert - Smart Visuals nutzen "
                "vorhandene Medien bzw. den synthetischen Demo-Backend.",
            ])
            return self._availability
        try:
            import torch  # type: ignore  # noqa: F401
        except Exception as exc:
            notes.append(f"torch nicht verfügbar: {exc.__class__.__name__}")
            self._availability = (False, notes)
            return self._availability
        notes.append("diffusers + torch gefunden; kein Modell-Download wird automatisch gestartet.")
        self._availability = (True, notes)
        return self._availability

    def generate(self, prompt, width, height, *, style="", seed=0, cache_dir, params=None) -> GenerationResult:
        available, notes = self._check()
        if not available:
            return GenerationResult(None, notes + ["Erzeugung übersprungen: Backend nicht verfügbar."])
        # A full pipeline (model loading, scheduler, safety settings) is out of
        # scope for this repository's sandbox; availability gating above is the
        # honest contract. The GUI diagnostics surface exactly this state.
        return GenerationResult(None, notes + ["Kein lokales Modell konfiguriert - keine Erzeugung."])


class SyntheticConceptProvider(ImageGenerationProvider):
    """Deterministic demo/test backend: abstract concept art via FFmpeg lavfi.

    NOT AI image generation. It composes a gradient/vignette frame whose
    palette and geometry derive from the prompt hash, so identical prompts
    always produce identical bytes. It exists purely so the Smart Visual
    pipeline (prompts, caching, plan application, rendering) is fully
    testable without GPU/model dependencies (spec section 49).
    """

    name = "synthetic-concept"
    model = "lavfi-gradients-v1"

    def __init__(self, ffmpeg_path: str | Path | None = None) -> None:
        self._ffmpeg_path = ffmpeg_path

    def _ffmpeg(self) -> str | None:
        if self._ffmpeg_path:
            return str(self._ffmpeg_path)
        try:
            from .paths import locate_ffmpeg

            ffmpeg, _probe = locate_ffmpeg()
            return str(ffmpeg)
        except Exception:
            return None

    def is_available(self) -> bool:
        return self._ffmpeg() is not None

    def diagnostics(self) -> list[str]:
        ffmpeg = self._ffmpeg()
        if ffmpeg is None:
            return ["FFmpeg nicht gefunden - synthetisches Demo-Backend nicht verfügbar."]
        return [
            "Synthetisches Demo/Test-Backend (FFmpeg-lavfi-Gradienten, deterministisch).",
            "Dies ist KEINE KI-Bilderzeugung; echte Erzeugung erfordert ein lokales Modell.",
        ]

    def generate(self, prompt, width, height, *, style="", seed=0, cache_dir, params=None) -> GenerationResult:
        ffmpeg = self._ffmpeg()
        if ffmpeg is None:
            return GenerationResult(None, ["FFmpeg fehlt - synthetische Erzeugung nicht möglich."])
        key = self.get_cache_key(prompt, width, height, style=style, seed=seed, params=params)
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f"{key}.png"
        if target.is_file() and target.stat().st_size > 0:
            return GenerationResult(target, ["Cache-Treffer: identische Anfrage wiederverwendet."])

        digest = hashlib.sha256(
            f"{prompt}|{style}|{seed}".encode("utf-8")
        ).digest()
        # The palette stays in the purple/magenta band with mid luminance:
        # visually varied per prompt, but never confused with typical video
        # content colors and never a pure red/green/blue test clip.
        hue_a = 0.72 + (digest[0] / 255.0) * 0.20
        hue_b = hue_a + 0.04 + (digest[1] / 255.0) * 0.06

        def color(base: float, spread: float) -> str:
            import colorsys

            red, green, blue = colorsys.hls_to_rgb((base + spread) % 1.0, 0.35, 0.50)
            return f"0x{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"

        x0 = int(digest[2] / 255.0 * width)
        y0 = int(digest[3] / 255.0 * height)
        x1 = width - x0 or width
        y1 = height - y0 or height
        width = int(width) - (int(width) % 2)
        height = int(height) - (int(height) % 2)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                f"gradients=s={width}x{height}:c0={color(hue_a, 0.0)}:c1={color(hue_b, 0.13)}:"
                f"x0={x0}:y0={y0}:x1={x1}:y1={y1}:type=linear,"
                f"vignette=angle=PI/4:x0={x0}:y0={y0}"
            ),
            "-frames:v", "1", str(target),
        ]
        from .platform_utils import hidden_process_flags, safe_subprocess_env

        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return GenerationResult(None, [f"Demo-Erzeugung fehlgeschlagen: {exc}"])
        if completed.returncode != 0 or not target.is_file():
            return GenerationResult(None, [f"Demo-Erzeugung fehlgeschlagen: {completed.stderr.strip()[:200]}"])
        return GenerationResult(target, ["Synthetisches Konzeptbild erzeugt (Demo-Backend)."])


def available_providers(ffmpeg_path: str | Path | None = None) -> list[ImageGenerationProvider]:
    """All known providers; the first AVAILABLE one wins at resolution time."""
    return [DiffusersProvider(), SyntheticConceptProvider(ffmpeg_path)]


def resolve_generation_provider(
    ffmpeg_path: str | Path | None = None,
) -> tuple[ImageGenerationProvider | None, list[str]]:
    """Pick the first usable provider and collect honest diagnostics."""
    notes: list[str] = []
    for provider in available_providers(ffmpeg_path):
        if provider.is_available():
            notes.extend(provider.diagnostics())
            return provider, notes
        notes.extend(provider.diagnostics())
    notes.append("Kein Erzeugungs-Backend verfügbar - Smart Visuals nutzen nur vorhandene Medien.")
    return None, notes
