"""1.2.4: Echte Untertitel- und Quote-Vorschau für die GUI.

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
* Quote-Karte: exakt :func:`quote.layout_quote` (dieselbe Geometrie wie der
  FFmpeg-Filtergraph)

Damit ist die Vorschau proportional identisch zum Final Render in der
gewählten Auflösung – kein dekoratives QLabel, keine FFmpeg-Render.
"""

from __future__ import annotations

from dataclasses import dataclass

from .font_manager import resolve_font
from .models import WordTiming
from .quote import (
    ATTRIBUTION_HEX,
    BACKGROUND_HEX,
    HAIRLINE_HEX,
    TEXT_HEX,
    QuoteLayout,
    layout_quote,
)
from .subtitle_presets import get_preset
from .subtitles import _font_size, _layout_words, _position


def _hex_rgb(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#").lstrip("0x").lower()
    while len(raw) < 6:
        raw = raw.zfill(6)
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


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


def preview_cue(
    text: str,
    font_key: str,
    preset_key: str,
    position: str,
    width: int,
    height: int,
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
    )


def quote_layout_for_preview(
    text: str, attribution: str, font_key: str, width: int, height: int
) -> QuoteLayout:
    """Quote-Layout exakt wie der Render (:func:`quote.layout_quote`)."""
    return layout_quote(text, attribution, font_key, width, height)


# --------------------------------------------------------------------------- #
# Qt-Canvas
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - Plattformabhängigkeit
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import (
        QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath,
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


def _word_style(
    layout: SubtitlePreviewLayout, animation: str, index: int, total: int,
) -> tuple[tuple[int, int, int], bool, bool]:
    """(Farbe, transparent?, Akzent-Outline?) für ein Wort im Demo-Fortschritt."""
    white = (247, 247, 247)
    accent = layout.accent
    if animation == "word_highlight":
        active = _demo_progress_index(animation, total)
        return (accent, False, False) if index == active else (white, False, False)
    if animation == "color_change":
        cut = _demo_progress_index(animation, total)
        return (accent, False, False) if index < cut else (white, False, False)
    if animation == "outline_highlight":
        active = _demo_progress_index(animation, total)
        return (white, False, index == active)
    if animation == "type_reveal":
        cut = _demo_progress_index(animation, total)
        return (white, False, False) if index < cut else (white, True, False)
    return (white, False, False)  # static_phrase


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

    class SubtitlePreviewCanvas(_PreviewCanvasBase):
        """Zeigt die Demo-Cue in echter Renderer-Geometrie, skaliert auf den Canvas."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._layout: SubtitlePreviewLayout | None = None
            self._animation = "static_phrase"

        def set_state(
            self,
            font_key: str,
            preset_key: str,
            position: str,
            animation: str,
            text: str,
            width: int,
            height: int,
        ) -> None:
            self._layout = preview_cue(text, font_key, preset_key, position, width, height)
            self._animation = animation
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

            for line in layout.lines:
                widths = [font_metrics.text_width(word, layout.font_size) for word in line]
                line_w = sum(widths) + (len(line) - 1) * space_w if line else 0.0
                x = rect.center().x() - line_w * scale / 2
                x = max(x, rect.left() + layout.margin_h * scale)
                if x + line_w * scale > rect.right() - layout.margin_h * scale:
                    x = rect.right() - layout.margin_h * scale - line_w * scale
                if layout.box:
                    box = QRectF(
                        x - 10 * scale, block_top - 4 * scale,
                        line_w * scale + 20 * scale, line_height * scale + 8 * scale,
                    )
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
                    painter.drawRoundedRect(box, 6 * scale, 6 * scale)
                for index, word in enumerate(line):
                    color, transparent, emphasis = _word_style(
                        layout, self._animation, flat_index, total_words
                    )
                    x0 = x + sum(widths[:index]) * scale + index * space_w * scale
                    w = widths[index] * scale
                    word_rect = QRectF(x0, block_top, max(1.0, w), line_height)
                    baseline_y = word_rect.top() + (line_height + metrics.ascent() - metrics.descent()) / 2
                    if self._animation == "outline_highlight" and emphasis:
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
                block_top += line_height
            painter.end()

    class QuotePreviewCanvas(_PreviewCanvasBase):
        """Zeigt die Quote-Karte exakt wie :func:`quote.layout_quote` sie rendert."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._layout: QuoteLayout | None = None

        def set_state(
            self, text: str, attribution: str, font_key: str, width: int, height: int,
        ) -> None:
            self._layout = quote_layout_for_preview(text, attribution, font_key, width, height)
            self.update()

        def current_layout(self) -> QuoteLayout | None:
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

            # Kartenfläche (Background + Vignette wie im Filtergraph)
            bg = _hex_rgb(BACKGROUND_HEX)
            painter.fillRect(rect, QColor(*bg))
            gradient = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * 0.62)
            gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
            gradient.setColorAt(1.0, QColor(0, 0, 0, 74))
            painter.fillRect(rect, gradient)

            if layout.hairline_x is not None and layout.hairline_y is not None:
                hair = _hex_rgb(HAIRLINE_HEX)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(*hair))
                painter.drawRect(QRectF(
                    rect.left() + layout.hairline_x * scale,
                    rect.top() + layout.hairline_y * scale,
                    (layout.hairline_width or 0) * scale,
                    max(1.0, (layout.hairline_height or 2) * scale),
                ))

            font = QFont(layout.font_family)
            font.setPixelSize(max(2, round(layout.font_size * scale)))
            font.setBold(True)
            text_color = QColor(*_hex_rgb(TEXT_HEX))
            metrics = QFontMetrics(font)
            for offset, line in enumerate(layout.lines):
                w = metrics.horizontalAdvance(line)
                x = rect.center().x() - w / 2
                y = rect.top() + (layout.line_top + offset * layout.line_height) * scale
                painter.setPen(QPen(text_color))
                painter.drawText(
                    QRectF(x, y, max(1.0, w), layout.line_height * scale),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    line,
                )

            if layout.attribution and layout.attribution_y is not None:
                attr_font = QFont(layout.font_family)
                attr_font.setPixelSize(max(2, round(layout.attribution_size * scale)))
                attr_font.setBold(True)  # Renderer zeichnet mit derselben (Bold)-Datei
                painter.setFont(attr_font)
                am = QFontMetrics(attr_font)
                w = am.horizontalAdvance(layout.attribution)
                painter.setPen(QPen(QColor(*_hex_rgb(ATTRIBUTION_HEX))))
                painter.drawText(
                    QRectF(
                        rect.center().x() - w / 2,
                        rect.top() + layout.attribution_y * scale,
                        max(1.0, w), layout.attribution_size * scale,
                    ),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    layout.attribution,
                )
            painter.end()

else:  # pragma: no cover - PySide6 fehlt

    class SubtitlePreviewCanvas(QWidget):  # type: ignore[no-redef]
        def __init__(self, parent=None) -> None:
            super().__init__()

        def set_state(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("PySide6 ist für die Untertitel-Vorschau erforderlich.")

    class QuotePreviewCanvas(QWidget):  # type: ignore[no-redef]
        def __init__(self, parent=None) -> None:
            super().__init__()

        def set_state(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("PySide6 ist für die Quote-Vorschau erforderlich.")


# --------------------------------------------------------------------------- #
# Demo-Texte
# --------------------------------------------------------------------------- #

SAMPLE_TEXTS = {
    "German": "Untertitel folgen exakt der gesprochenen Stimme.",
    "English": "Captions follow the spoken voice precisely.",
}


def sample_subtitle_text(language: str) -> str:
    return SAMPLE_TEXTS.get(language, SAMPLE_TEXTS["German"])
