"""1.2.4: Echte Untertitel-Vorschau für die GUI.

Die Vorschau berechnet ihre Geometrie mit denselben Funktionen, die der
eingebrannte Renderer verwendet:

* Font-Metriken: :meth:`font_manager.ResolvedFont.text_width` (fontTools,
  dieselbe tatsächlich benutzte TTF-Datei)
* Zeilenumbrüche: :func:`subtitles._layout_words` (derselbe
  Zwei-Zeilen-Balancer wie der Render)
* Font-Größe: :func:`subtitles._font_size` (resolution-aware, selbes Ratio)
* Safe-Area: ``margin_h``/``margin_v``/Positions-Mathematik aus
  :func:`subtitles._position` (identische Werte wie in :func:`subtitles.write_ass`)
* Outline/Box/Akzent: selbe Ratios und Farben wie :func:`subtitles.write_ass`
* Add Image: the selected image is framed with the same aspect-safe
  Fit/Fill/Crop rules as the FFmpeg filter graph.

Damit ist die Vorschau proportional identisch zum Final Render in der
gewählten Auflösung – kein dekoratives QLabel, keine FFmpeg-Render.
"""

from __future__ import annotations

from dataclasses import dataclass

from .font_manager import resolve_font
from .models import WordTiming
from .subtitle_presets import get_preset
from .subtitles import _font_size, _layout_words, _position


def _ass_color(value: str) -> tuple[int, int, int]:
    """ASS ``&HAABBGGRR`` → ``(R, G, B)``."""
    raw = value.lstrip("&").replace("H", "").replace("h", "")
    while len(raw) < 8:
        raw = raw.zfill(8)
    aa, bb, gg, rr = (int(raw[i:i + 2], 16) for i in (0, 2, 4, 6))
    return rr, gg, bb


@dataclass
class SubtitlePreviewLayout:
    """Vollständig aufgelöste Vorschau-Geometrie einer Demo-Cue."""

    width: int
    height: int
    font_key: str
    font_family: str
    font_size: int
    margin_h: int
    margin_v: int
    alignment: int
    lines: list[list[str]]
    preset_key: str
    preset_label: str
    collection: str
    bold: bool
    box: bool
    accent: tuple[int, int, int]
    outline: float
    truncated: bool = False
    # 1.3.0: staged animation state for the live preview and the larger
    # dialog (both paint through the same renderer-geometry logic).
    animation: str = "static_phrase"
    active_word: int = -1  # -1 = deterministic demo progress


def preview_cue(
    text: str,
    font_key: str,
    preset_key: str,
    position: str,
    width: int,
    height: int,
    *,
    animation: str = "static_phrase",
    active_word: int = -1,
) -> SubtitlePreviewLayout:
    """Baut eine repräsentative Demo-Cue mit der echten Renderer-Logik.

    Das Wort-Budget (``preset.max_words``) und der Zwei-Zeilen-Test sind
    identisch zu :func:`subtitles.build_cues`; passt der Text nicht in zwei
    gemessene Zeilen, zeigt die Vorschau die längste Darstellungsgruppe
    (der Render würde mehrere Cues ausgeben – die Vorschau zeigt die erste).
    """
    preset = get_preset(preset_key)
    font = resolve_font(font_key)
    size = _font_size(width, height, preset)
    width_ratio = .86 if preset.collection == "long" else .90
    available_width = max(40.0, width * width_ratio)
    alignment, margin_v = _position(position, width, height, preset.collection)
    margin_h = round(width * (.07 if preset.collection == "long" else .055))
    basis = min(width, height)
    outline = max(1.0, round(basis * preset.outline_ratio, 1))
    raw = [token for token in " ".join((text or "").split()).split() if token]

    group: list[str] = []
    truncated = False
    for token in raw:
        candidate = group + [token]
        if len(candidate) > preset.max_words:
            truncated = True
            break
        stubs = [WordTiming(text=t, start=0.0, end=0.0) for t in candidate]
        if not _layout_words(stubs, font_key, size, available_width)[0]:
            if group:
                truncated = True
                break
            # Einzelnes Wort, das zu breit ist: wie der Renderer beibehalten.
            group = candidate
            truncated = True
            break
        group = candidate
    if not group:
        group = raw[:1] if raw else ["…"]

    _ok, split = _layout_words(
        [WordTiming(text=t, start=0.0, end=0.0) for t in group],
        font_key, size, available_width,
    )
    lines = [group] if split is None else [group[:split], group[split:]]
    r, g, b = _ass_color(preset.accent)
    return SubtitlePreviewLayout(
        width=width, height=height, font_key=font_key, font_family=font.family,
        font_size=size, margin_h=margin_h, margin_v=margin_v, alignment=alignment,
        lines=lines, preset_key=preset_key, preset_label=preset.label,
        collection=preset.collection, bold=bool(preset.bold), box=bool(preset.box),
        accent=(r, g, b), outline=outline, truncated=truncated,
        animation=animation, active_word=active_word,
    )


# --------------------------------------------------------------------------- #
# Qt-Canvas
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - Plattformabhängigkeit
    from PySide6.QtCore import QPointF, QRectF, QSize, Qt
    from PySide6.QtGui import (
        QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath,
        QPen, QRadialGradient,
    )
    from PySide6.QtWidgets import QSizePolicy, QWidget

    _QIMPORTS_OK = True
except Exception:  # pragma: no cover
    _QIMPORTS_OK = False
    QWidget = object  # type: ignore[assignment, misc]


def _demo_progress_index(animation: str, total: int) -> int:
    """Fester Demo-Fortschritt, damit die Vorschau die Animation erkennbar zeigt."""
    if total <= 0:
        return 0
    if animation in {"word_highlight", "outline_highlight"}:
        return max(0, min(total - 1, round(total * 0.4)))
    return max(0, min(total, round(total * 0.6)))


def word_style_for(
    layout: "SubtitlePreviewLayout", animation: str, index: int, total: int,
    active_word: int = -1,
) -> tuple[tuple[int, int, int], bool, bool]:
    """(Farbe, transparent?, Akzent-Outline?) für Wort `index`.

    Shared by the live canvas, the larger preview dialog and tests: identical
    semantics to the ASS renderer's per-word tags for the given animation and
    the active word (the canonical acoustic timeline drives it at render
    time; the preview stages a representative frame).
    """
    white = (247, 247, 247)
    accent = layout.accent
    active = (
        active_word if active_word >= 0
        else _demo_progress_index(animation, total)
    )
    active = max(0, min(total - 1 if animation in {"word_highlight", "outline_highlight"} else total, active))
    if animation == "word_highlight":
        return (accent, False, False) if index == active else (white, False, False)
    if animation == "color_change":
        return (accent, False, False) if index <= active else (white, False, False)
    if animation == "outline_highlight":
        return (white, False, index == active)
    if animation == "type_reveal":
        return (white, False, False) if index <= active else (white, True, False)
    return (white, False, False)  # static_phrase


def _word_style(
    layout: SubtitlePreviewLayout, animation: str, index: int, total: int,
) -> tuple[tuple[int, int, int], bool, bool]:
    """(Farbe, transparent?, Akzent-Outline?) für ein Wort im Demo-Fortschritt."""
    return word_style_for(layout, animation, index, total)


class _PreviewCanvasBase(QWidget):
    """Gemeinsame Skalierungs-/Hintergrund-Malerei für beide Canvas-Typen."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _video_rect(self, width: int, height: int):
        if width <= 0 or height <= 0:
            return None
        canvas = self.rect()
        if canvas.width() <= 1 or canvas.height() <= 1:
            return None
        scale = min(canvas.width() / width, canvas.height() / height)
        w = width * scale
        h = height * scale
        x = (canvas.width() - w) / 2
        y = (canvas.height() - h) / 2
        return QRectF(x, y, w, h), scale

    def _paint_backdrop(self, painter: "QPainter", rect) -> None:
        painter.fillRect(self.rect(), QColor(12, 14, 18))
        if rect is None:
            return
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(38, 46, 60))
        gradient.setColorAt(0.55, QColor(26, 31, 41))
        gradient.setColorAt(1.0, QColor(17, 20, 27))
        painter.fillRect(rect, gradient)
        painter.setPen(QPen(QColor(255, 255, 255, 28)))
        painter.drawRect(rect)


if _QIMPORTS_OK:

    def paint_subtitle_layout(painter: "QPainter", layout: SubtitlePreviewLayout,
                              rect, scale: float, animation: str,
                              active_word: int = -1) -> None:
        """Paint one preview cue in exact renderer geometry.

        1.3.0: this is THE shared painting routine — the live canvas and the
        larger preview dialog both call it, so font, size, line wrapping,
        style, position, safe area, colors/highlights and the staged
        animation are identical everywhere (Preview ≈ Final Render).
        """
        font = QFont(layout.font_family)
        font.setPixelSize(max(2, round(layout.font_size * scale)))
        font.setBold(layout.bold)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        line_height = max(1.0, metrics.height() * 1.02)
        block_height = line_height * len(layout.lines)

        # Vertikale Position identisch zu subtitles._position:
        # 2 = Bottom/Medium-Low (margin_v von unten), 5 = Middle, 8 = Top.
        if layout.alignment == 5:
            block_top = rect.top() + (rect.height() - block_height) / 2
        elif layout.alignment == 8:
            block_top = rect.top() + layout.margin_v * scale
        else:
            block_top = rect.bottom() - layout.margin_v * scale - block_height

        # Dezent gestrichelte Safe-Area
        safe_pen = QPen(QColor(255, 255, 255, 34), 1, Qt.PenStyle.DashLine)
        painter.setPen(safe_pen)
        painter.drawRect(
            rect.adjusted(
                layout.margin_h * scale, layout.margin_v * scale,
                -layout.margin_h * scale, -layout.margin_v * scale,
            )
        )

        font_metrics = resolve_font(layout.font_key)
        space_w = font_metrics.text_width(" ", layout.font_size)
        white = QColor(247, 247, 247)
        accent = QColor(*layout.accent)
        outline_color = QColor(16, 16, 16)
        total_words = sum(len(line) for line in layout.lines)
        flat_index = 0
        cursor_top = block_top

        for line in layout.lines:
            widths = [font_metrics.text_width(word, layout.font_size) for word in line]
            line_w = sum(widths) + (len(line) - 1) * space_w if line else 0.0
            x = rect.center().x() - line_w * scale / 2
            x = max(x, rect.left() + layout.margin_h * scale)
            if x + line_w * scale > rect.right() - layout.margin_h * scale:
                x = rect.right() - layout.margin_h * scale - line_w * scale
            if layout.box:
                box = QRectF(
                    x - 10 * scale, cursor_top - 4 * scale,
                    line_w * scale + 20 * scale, line_height * scale + 8 * scale,
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
                painter.drawRoundedRect(box, 6 * scale, 6 * scale)
            for index, word in enumerate(line):
                color, transparent, emphasis = word_style_for(
                    layout, animation, flat_index, total_words, active_word,
                )
                x0 = x + sum(widths[:index]) * scale + index * space_w * scale
                w = widths[index] * scale
                word_rect = QRectF(x0, cursor_top, max(1.0, w), line_height)
                baseline_y = word_rect.top() + (line_height + metrics.ascent() - metrics.descent()) / 2
                if animation == "outline_highlight" and emphasis:
                    path = QPainterPath()
                    path.addText(word_rect.left(), baseline_y, font, word)
                    painter.setPen(QPen(accent, max(1.0, layout.outline * scale * 1.8)))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)
                else:
                    fill = QColor(*color)
                    if transparent:
                        fill.setAlpha(96)
                    if layout.box:
                        # Im Box-Preset zeichnet libass ohne starken
                        # Outline-Stroke; die Vorschau entspricht dem.
                        painter.setPen(QPen(fill))
                    else:
                        if layout.outline > 0:
                            path = QPainterPath()
                            path.addText(word_rect.left(), baseline_y, font, word)
                            painter.setPen(QPen(outline_color, max(0.8, layout.outline * 1.9 * scale)))
                            painter.setBrush(Qt.BrushStyle.NoBrush)
                            painter.drawPath(path)
                        painter.setPen(QPen(fill))
                    painter.drawText(
                        word_rect,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        word,
                    )
                flat_index += 1
            cursor_top += line_height

    class SubtitlePreviewCanvas(_PreviewCanvasBase):
        """Zeigt die Demo-Cue in echter Renderer-Geometrie, skaliert auf den Canvas."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._layout: SubtitlePreviewLayout | None = None
            self._animation = "static_phrase"
            self._active_word = -1

        def set_state(
            self,
            font_key: str,
            preset_key: str,
            position: str,
            animation: str,
            text: str,
            width: int,
            height: int,
            active_word: int = -1,
        ) -> None:
            self._layout = preview_cue(
                text, font_key, preset_key, position, width, height,
                animation=animation, active_word=active_word,
            )
            self._animation = animation
            self._active_word = active_word
            self.update()

        def set_active_word(self, active_word: int) -> None:
            """Stage a different word without recomputing the layout."""
            self._active_word = active_word
            if self._layout is not None:
                self._layout.active_word = active_word
            self.update()

        def current_layout(self) -> SubtitlePreviewLayout | None:
            return self._layout

        def paintEvent(self, event) -> None:  # noqa: N802
            layout = self._layout
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            video = self._video_rect(
                layout.width if layout else 16, layout.height if layout else 9
            )
            if layout is None or video is None:
                self._paint_backdrop(painter, video[0] if video else None)
                painter.end()
                return
            rect, scale = video
            self._paint_backdrop(painter, rect)
            paint_subtitle_layout(
                painter, layout, rect, scale, self._animation, self._active_word,
            )
            painter.end()

    class ImageInsertionPreviewCanvas(_PreviewCanvasBase):
        """Live preview for the independent Image Insertion section.

        The preview uses the same contain/cover/source-crop semantics as the
        FFmpeg image branch, including zoom and deterministic look overlays.
        It intentionally does not display subtitles or any audio indication.
        """

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._image: QImage | None = None
            self._path = ""
            self._fit_mode = "fit"
            self._zoom = 100
            self._filter = "natural"
            self._width, self._height = 16, 9
            self._error = ""

        def set_image(
            self, path: str, fit_mode: str, zoom: int, filter_name: str,
            width: int, height: int,
        ) -> None:
            self._path = str(path or "").strip()
            self._fit_mode = fit_mode if fit_mode in {"fit", "fill", "crop"} else "fit"
            self._zoom = max(100, min(300, int(zoom or 100)))
            self._filter = filter_name if filter_name in {
                "natural", "cinematic", "moody", "film", "dark_editorial"
            } else "natural"
            self._width, self._height = max(16, int(width)), max(16, int(height))
            self._image = None
            self._error = ""
            if self._path:
                image = QImage(self._path)
                if image.isNull():
                    self._error = "Image preview unavailable"
                else:
                    self._image = image
            self.update()

        def _source_crop(self, image: QImage, target_ratio: float) -> QImage:
            if image.height() <= 0:
                return image
            source_ratio = image.width() / image.height()
            if abs(source_ratio - target_ratio) < 0.001:
                return image
            if source_ratio > target_ratio:
                crop_width = max(1, round(image.height() * target_ratio))
                return image.copy((image.width() - crop_width) // 2, 0, crop_width, image.height())
            crop_height = max(1, round(image.width() / target_ratio))
            return image.copy(0, (image.height() - crop_height) // 2, image.width(), crop_height)

        def _paint_filter(self, painter: "QPainter", rect) -> None:
            # Fixed translucent overlays are a preview-equivalent visual cue
            # for the fixed FFmpeg eq/colorbalance expressions. They are not
            # random and never alter source geometry.
            if self._filter == "cinematic":
                painter.fillRect(rect, QColor(24, 52, 88, 32))
                painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), rect.height() * .34), QColor(244, 180, 96, 12))
            elif self._filter == "moody":
                painter.fillRect(rect, QColor(16, 25, 48, 52))
            elif self._filter == "film":
                painter.fillRect(rect, QColor(164, 116, 65, 28))
            elif self._filter == "dark_editorial":
                painter.fillRect(rect, QColor(11, 22, 42, 68))

        def paintEvent(self, event) -> None:  # noqa: N802
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            video = self._video_rect(self._width, self._height)
            if video is None:
                self._paint_backdrop(painter, None)
                painter.end()
                return
            rect, _scale = video
            self._paint_backdrop(painter, rect)
            if self._image is None:
                if self._error:
                    painter.setPen(QPen(QColor(255, 210, 100)))
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._error)
                painter.end()
                return
            image = self._image
            target = QSize(max(1, round(rect.width() * self._zoom / 100)), max(1, round(rect.height() * self._zoom / 100)))
            target_ratio = rect.width() / max(1.0, rect.height())
            if self._fit_mode == "crop":
                image = self._source_crop(image, target_ratio)
                shown = image.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            elif self._fit_mode == "fill":
                shown = image.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            else:
                painter.fillRect(rect, QColor(0, 0, 0))
                shown = image.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = rect.left() + (rect.width() - shown.width()) / 2
            y = rect.top() + (rect.height() - shown.height()) / 2
            painter.save()
            painter.setClipRect(rect)
            painter.drawImage(QPointF(x, y), shown)
            self._paint_filter(painter, rect)
            painter.restore()
            painter.end()


else:  # pragma: no cover - PySide6 fehlt

    class SubtitlePreviewCanvas(QWidget):  # type: ignore[no-redef]
        def __init__(self, parent=None) -> None:
            super().__init__()

        def set_state(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("PySide6 ist für die Untertitel-Vorschau erforderlich.")

    class ImageInsertionPreviewCanvas(QWidget):  # type: ignore[no-redef]
        def __init__(self, parent=None) -> None:
            super().__init__()

        def set_image(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("PySide6 ist für die Image-Insertion-Vorschau erforderlich.")


# --------------------------------------------------------------------------- #
# Demo-Texte
# --------------------------------------------------------------------------- #

SAMPLE_TEXTS = {
    "German": "Untertitel folgen exakt der gesprochenen Stimme.",
    "English": "Captions follow the spoken voice precisely.",
}


def sample_subtitle_text(language: str) -> str:
    return SAMPLE_TEXTS.get(language, SAMPLE_TEXTS["German"])
