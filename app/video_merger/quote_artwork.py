"""Validation and preparation for uploaded Quote Card artwork.

Artwork is a Stage 2 input, never a Main Video input. Raster images are passed
to FFmpeg unchanged; PDF pages are rasterized once to a deterministic,
render-time PNG. The optional PyMuPDF import is deliberately lazy so the
application can still open text-only projects on machines that have not yet
installed the PDF extra.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .errors import VideoMergerError
from .project_assets import require_asset

QUOTE_ARTWORK_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}


@dataclass(frozen=True, slots=True)
class PreparedQuoteArtwork:
    """The file and pixel dimensions consumed by the Stage 2 graph."""

    path: Path
    width: int
    height: int
    source_path: Path
    pdf_page: int | None = None


def quote_artwork_path(value: str | Path) -> Path:
    try:
        path = Path(value).expanduser().resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise VideoMergerError("Quote Artwork fehlt oder ist kein gültiger Dateipfad.") from exc
    require_asset(path, "Quote Artwork", QUOTE_ARTWORK_EXTENSIONS)
    return path


def _fitz():
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VideoMergerError(
            "PDF-Quote-Artwork benötigt PyMuPDF. Installieren Sie die "
            "Abhängigkeit aus requirements.txt und versuchen Sie es erneut."
        ) from exc
    return fitz


def pdf_page_count(value: str | Path) -> int:
    """Return the one-based selectable page count, with a clear PDF error."""
    path = quote_artwork_path(value)
    if path.suffix.casefold() != ".pdf":
        return 1
    fitz = _fitz()
    try:
        document = fitz.open(str(path))
        try:
            count = int(document.page_count)
        finally:
            document.close()
    except Exception as exc:
        raise VideoMergerError(f"PDF-Quote-Artwork konnte nicht gelesen werden: {path.name}: {exc}") from exc
    if count <= 0:
        raise VideoMergerError(f"Das PDF-Quote-Artwork enthält keine Seiten: {path.name}")
    return count


def _pdf_cache_path(path: Path, page: int, target_width: int, target_height: int, temp_dir: Path) -> Path:
    try:
        stat = path.stat()
    except OSError as exc:
        raise VideoMergerError(f"Quote-Artwork konnte nicht gelesen werden: {path.name}: {exc}") from exc
    token = hashlib.sha256(
        f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\0{page}\0{target_width}x{target_height}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return temp_dir / f"quote_artwork_{token}_p{page}.png"


def cleanup_prepared_quote_artwork(prepared: PreparedQuoteArtwork | None) -> None:
    """Remove a render-only PDF raster, never the user's source artwork.

    Raster image uploads are returned unchanged and therefore are deliberately
    not touched.  A PDF page is materialized only for the synchronous FFmpeg
    export; callers should invoke this helper in a ``finally`` block after the
    export (or after a preparation failure) so the project ``temp`` directory
    cannot become a production-artwork folder.
    """
    if prepared is None or prepared.pdf_page is None:
        return
    if prepared.path == prepared.source_path:
        return
    try:
        prepared.path.unlink(missing_ok=True)
    except OSError:
        # Cleanup must never hide the useful export/validation exception. The
        # caller may log this condition if desired.
        return


def _validate_target_dimensions(target_width: int, target_height: int) -> tuple[int, int]:
    try:
        width, height = int(target_width), int(target_height)
    except (TypeError, ValueError) as exc:
        raise VideoMergerError("Ungültige Zielabmessungen für Quote-Artwork.") from exc
    if width < 16 or height < 16 or width > 7680 or height > 7680:
        raise VideoMergerError("Ungültige Zielabmessungen für Quote-Artwork.")
    return width, height


def rasterize_pdf_page(
    value: str | Path,
    page: int,
    target_width: int,
    target_height: int,
    temp_dir: Path,
) -> PreparedQuoteArtwork:
    """Rasterize one PDF page at practical output-aware quality.

    The long edge is rendered up to two times the requested output (capped at
    7680 pixels) and at no more than 300 DPI. This keeps 1080p cards crisp,
    gives 4K cards useful source detail, and avoids unbounded memory use for
    poster-sized/vector PDFs.
    """
    path = quote_artwork_path(value)
    if path.suffix.casefold() != ".pdf":
        raise VideoMergerError(f"Kein PDF für PDF-Seitenwahl: {path.name}")
    target_width, target_height = _validate_target_dimensions(target_width, target_height)
    try:
        page = int(page)
    except (TypeError, ValueError) as exc:
        raise VideoMergerError("Die PDF-Seitennummer muss eine ganze Zahl sein.") from exc
    if page < 1:
        raise VideoMergerError("Die PDF-Seitennummer muss mindestens 1 sein.")
    fitz = _fitz()
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VideoMergerError(
            f"Temporärer Speicher für PDF-Quote-Artwork ist nicht beschreibbar: {temp_dir}"
        ) from exc
    output = _pdf_cache_path(path, page, target_width, target_height, temp_dir)
    try:
        document = fitz.open(str(path))
        try:
            if page > document.page_count:
                raise VideoMergerError(
                    f"PDF-Seite {page} existiert nicht; verfügbar sind 1–{document.page_count}."
                )
            pdf_page = document.load_page(page - 1)
            rect = pdf_page.rect
            long_edge = max(float(rect.width), float(rect.height))
            desired_long_edge = min(7680.0, max(320.0, float(max(target_width, target_height)) * 2.0))
            # PDF points are 1/72 inch. The two limits jointly provide an
            # output-aware raster without creating pathological huge images.
            scale = min(300.0 / 72.0, desired_long_edge / max(1.0, long_edge))
            scale = max(0.5, scale)
            pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            pixmap.save(str(output))
            width, height = int(pixmap.width), int(pixmap.height)
        finally:
            document.close()
    except VideoMergerError:
        output.unlink(missing_ok=True)
        raise
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise VideoMergerError(
            f"PDF-Seite {page} konnte nicht als Quote-Artwork gerastert werden: {path.name}: {exc}"
        ) from exc
    if width <= 0 or height <= 0 or not output.is_file():
        raise VideoMergerError(f"PDF-Seite {page} lieferte kein gültiges Artwork: {path.name}")
    return PreparedQuoteArtwork(output, width, height, path, page)


def prepare_quote_artwork(
    value: str | Path,
    pdf_page: int,
    target_width: int,
    target_height: int,
    temp_dir: Path,
    image_dimensions,
) -> PreparedQuoteArtwork:
    """Validate an image/PDF and return a graph-ready artwork descriptor.

    ``image_dimensions`` is supplied by the application's FFprobe analyzer;
    keeping probing outside this module makes the PDF dependency lazy and
    keeps all path handling in the existing platform-safe analyzer.
    """
    path = quote_artwork_path(value)
    target_width, target_height = _validate_target_dimensions(target_width, target_height)
    if path.suffix.casefold() == ".pdf":
        try:
            page = int(pdf_page or 1)
        except (TypeError, ValueError) as exc:
            raise VideoMergerError("Die PDF-Seitennummer muss eine ganze Zahl sein.") from exc
        return rasterize_pdf_page(path, page, target_width, target_height, temp_dir)
    try:
        width, height = image_dimensions(path)
        width, height = int(width), int(height)
    except VideoMergerError:
        raise
    except Exception as exc:
        raise VideoMergerError(
            f"Quote-Artwork konnte nicht analysiert werden: {path.name}"
        ) from exc
    if width <= 0 or height <= 0:
        raise VideoMergerError(f"Ungültige Quote-Artwork-Auflösung: {path.name}")
    return PreparedQuoteArtwork(path, width, height, path, None)
