"""Paper document-source controls used by the speech workspaces."""
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QSizePolicy, QLabel
from . import theme as T, icons
from .control_foundation import PaperTextureFrame
from .widgets import FileDropArea
_SOURCE_SLOT_HEIGHT = 130
_SOURCE_CORNER_RADIUS = 16.0

def _mix_paper_color(first: str | QColor, second: str | QColor, second_weight: float):
    """Blend two consumer-theme colours for custom-painted paper layers."""
    left = QColor(first) if isinstance(first, QColor) else T.consumer_qcolor(first)
    right = QColor(second) if isinstance(second, QColor) else T.consumer_qcolor(second)
    weight = max(0.0, min(1.0, float(second_weight)))
    return T.consumer_qcolor(
        "#{:02X}{:02X}{:02X}".format(
            round(left.red() * (1.0 - weight) + right.red() * weight),
            round(left.green() * (1.0 - weight) + right.green() * weight),
            round(left.blue() * (1.0 - weight) + right.blue() * weight),
        )
    )


class PaperEnvelopePicker(QFrame):
    """Responsive cotton-paper envelope with a fixed-size centre tie.

    The body and tiled fibres grow horizontally while the cord and bow keep their
    proportions, so opening either side panel never stretches the focal detail.
    """

    def __init__(self, consumer, parent=None):
        super().__init__(parent)
        self._consumer = consumer
        self.setObjectName("documentPaperEnvelope")
        self.setProperty("sourceVisual", "responsivePaperEnvelope")
        self.setProperty("sourceMaterial", "blueCottonPaperNaturalGrain")
        self.setProperty("sourceStyle", "referencePaperEnvelope")
        self.setProperty("sourceScaling", "stretchBodyFixedCentreTie")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(_SOURCE_SLOT_HEIGHT)
        self.setStyleSheet(
            "QFrame#documentPaperEnvelope { background: transparent; border: none; }"
        )

    def _envelope_path(self, body: QRectF) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(body, _SOURCE_CORNER_RADIUS, _SOURCE_CORNER_RADIUS)
        return path

    def _draw_natural_paper_grain(self, painter: QPainter, body: QRectF) -> None:
        """Build the reference image's fine, irregular cotton-paper surface."""
        painter.save()
        painter.setClipPath(self._envelope_path(body))
        texture = PaperTextureFrame.texture_pixmap()
        if not texture.isNull():
            # Keep the scanned fibres visible without flattening the envelope blue.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SoftLight)
            painter.setOpacity(0.28)
            painter.drawTiledPixmap(self.rect(), texture)

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setOpacity(1.0)
        dark_fibre = T.consumer_qcolor(self._consumer.ink)
        dark_fibre.setAlpha(12)
        light_fibre = T.consumer_qcolor(self._consumer.paper_edge)
        light_fibre.setAlpha(34)

        # A deterministic irregular field gives resize-stable natural noise.
        # No regular dot grid is used: the reference reads as pressed paper,
        # rather than halftone or a vector line drawing.
        left = round(body.left()) + 4
        top = round(body.top()) + 3
        width = max(1, round(body.width()) - 8)
        height = max(1, round(body.height()) - 6)
        seed = 0x5EED91
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(max(180, round(body.width() * body.height() / 74))):
            seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
            x = left + (seed % width)
            seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
            y = top + (seed % height)
            seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
            painter.setBrush(light_fibre if seed & 1 else dark_fibre)
            radius = 0.22 + ((seed >> 8) % 5) * 0.055
            painter.drawEllipse(QPointF(x + 0.25, y + 0.2), radius, radius)

            if index % 9 == 0:
                length = 1.5 + ((seed >> 13) % 8) * 0.34
                rise = (((seed >> 17) % 7) - 3) * 0.16
                fibre_pen = QPen(light_fibre if index % 18 else dark_fibre, 0.48)
                fibre_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(fibre_pen)
                painter.drawLine(QPointF(x, y), QPointF(x + length, y + rise))
                painter.setPen(Qt.PenStyle.NoPen)
        painter.restore()

    def _draw_cord(self, painter: QPainter, body: QRectF) -> None:
        centre_x = body.center().x()
        cord_y = body.top() + body.height() * 0.58
        cord = _mix_paper_color(
            self._consumer.paper_edge,
            self._consumer.ink_soft,
            0.24,
        )
        cord_shadow = T.consumer_qcolor(self._consumer.ink)
        cord_shadow.setAlpha(42)

        def draw_with(pen: QPen) -> None:
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(body.left() + 9.0, cord_y), QPointF(body.right() - 9.0, cord_y))
            painter.drawLine(
                QPointF(centre_x, body.top() + 5.0),
                QPointF(centre_x, body.bottom() - 5.0),
            )
            bow = QPainterPath(QPointF(centre_x, cord_y))
            bow.cubicTo(
                centre_x - 14.0, cord_y - 16.5,
                centre_x - 40.0, cord_y - 16.0,
                centre_x - 32.0, cord_y + 0.5,
            )
            bow.cubicTo(
                centre_x - 25.0, cord_y + 10.0,
                centre_x - 8.0, cord_y + 6.0,
                centre_x, cord_y,
            )
            bow.cubicTo(
                centre_x + 14.0, cord_y - 16.5,
                centre_x + 40.0, cord_y - 16.0,
                centre_x + 32.0, cord_y + 0.5,
            )
            bow.cubicTo(
                centre_x + 25.0, cord_y + 10.0,
                centre_x + 8.0, cord_y + 6.0,
                centre_x, cord_y,
            )
            painter.drawPath(bow)
            left_tail = QPainterPath(QPointF(centre_x - 2.5, cord_y + 3.0))
            left_tail.cubicTo(
                centre_x - 7.0, cord_y + 13.0,
                centre_x - 15.0, cord_y + 23.0,
                centre_x - 17.0, cord_y + 29.0,
            )
            right_tail = QPainterPath(QPointF(centre_x + 2.5, cord_y + 3.0))
            right_tail.cubicTo(
                centre_x + 7.0, cord_y + 13.0,
                centre_x + 15.0, cord_y + 23.0,
                centre_x + 17.0, cord_y + 29.0,
            )
            painter.drawPath(left_tail)
            painter.drawPath(right_tail)
            painter.setBrush(pen.color())
            painter.drawEllipse(QPointF(centre_x, cord_y), 2.05, 2.05)
            painter.drawEllipse(QPointF(centre_x - 17.0, cord_y + 29.0), 1.35, 1.0)
            painter.drawEllipse(QPointF(centre_x + 17.0, cord_y + 29.0), 1.35, 1.0)

        shadow_pen = QPen(cord_shadow, 1.90)
        shadow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        shadow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        draw_with(shadow_pen)
        cord_pen = QPen(cord, 1.04)
        cord_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        cord_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        draw_with(cord_pen)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        if self.width() < 180 or self.height() < 72:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Fill almost to the widget edge so the section paper cannot show as
            # a pale ring around the envelope. The half-pixel inset is only for
            # antialiasing; there is no decorative outline in the resting state.
            body = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
            base = _mix_paper_color(
                self._consumer.surface_focal,
                self._consumer.accent,
                0.47,
            )
            top = _mix_paper_color(
                self._consumer.surface_focal,
                self._consumer.accent,
                0.39,
            )
            lower = _mix_paper_color(
                base,
                self._consumer.ink,
                0.055,
            )
            gradient = QLinearGradient(body.topLeft(), body.bottomLeft())
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(0.52, base)
            gradient.setColorAt(1.0, lower)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(body, _SOURCE_CORNER_RADIUS, _SOURCE_CORNER_RADIUS)

            painter.save()
            painter.setClipPath(self._envelope_path(body))

            cord_y = body.top() + body.height() * 0.58
            flap = QPainterPath(QPointF(body.left() + 1.5, body.top() + 1.5))
            flap.lineTo(body.right() - 1.5, body.top() + 1.5)
            flap.lineTo(body.right() - 1.5, cord_y - 24.0)
            flap.cubicTo(
                body.right() - 2.0, cord_y - 9.0,
                body.right() - 18.0, cord_y - 5.0,
                body.right() - 46.0, cord_y - 4.0,
            )
            flap.cubicTo(
                body.center().x() + body.width() * 0.22, cord_y - 2.6,
                body.center().x() - body.width() * 0.22, cord_y - 2.6,
                body.left() + 46.0, cord_y - 4.0,
            )
            flap.cubicTo(
                body.left() + 18.0, cord_y - 5.0,
                body.left() + 2.0, cord_y - 9.0,
                body.left() + 1.5, cord_y - 24.0,
            )
            flap.closeSubpath()
            flap_gradient = QLinearGradient(body.topLeft(), QPointF(body.left(), cord_y))
            flap_gradient.setColorAt(0.0, _mix_paper_color(top, self._consumer.paper_edge, 0.10))
            flap_gradient.setColorAt(1.0, _mix_paper_color(base, self._consumer.ink, 0.035))
            fold_edge = T.consumer_qcolor(self._consumer.accent)
            fold_edge.setAlpha(76)
            painter.setPen(QPen(fold_edge, 0.62))
            painter.setBrush(flap_gradient)
            painter.drawPath(flap)

            # The reference has a soft tactile shadow below the flap, with no
            # oversized lower-body triangles competing with the content.
            flap_shadow = T.consumer_qcolor(self._consumer.ink)
            flap_shadow.setAlpha(22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(flap_shadow)
            painter.drawRoundedRect(
                QRectF(body.left() + 38.0, cord_y - 3.4, body.width() - 76.0, 2.2),
                1.1,
                1.1,
            )

            seam = T.consumer_qcolor(self._consumer.accent)
            seam.setAlpha(34)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(seam, 0.45))
            painter.drawLine(
                QPointF(body.left() + 2.5, cord_y - 2.0),
                QPointF(body.left() + 2.5, body.bottom() - 11.0),
            )
            painter.drawLine(
                QPointF(body.right() - 2.5, cord_y - 2.0),
                QPointF(body.right() - 2.5, body.bottom() - 11.0),
            )

            self._draw_natural_paper_grain(painter, body)
            self._draw_cord(painter, body)
            painter.restore()

            # Keyboard focus still needs a visible state, but it sits fully
            # inside the paper instead of leaving a second line at the corners.
            if self.hasFocus():
                focus_edge = T.consumer_qcolor(self._consumer.accent_hairline_active)
                focus_rect = QRectF(body).adjusted(1.25, 1.25, -1.25, -1.25)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(focus_edge, 1.2))
                painter.drawRoundedRect(
                    focus_rect,
                    _SOURCE_CORNER_RADIUS - 1.25,
                    _SOURCE_CORNER_RADIUS - 1.25,
                )
        finally:
            painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().focusOutEvent(event)


class PaperFileDropArea(FileDropArea):
    """Existing upload behaviour rendered as an elastic blue-bound paper sheet."""

    def __init__(self, on_click, on_drop, consumer, sub: str, *, title: str):
        super().__init__(on_click, on_drop, sub=sub, title=title)
        self._consumer = consumer
        self.setObjectName("documentPaperDropArea")
        self.setProperty("uploadVisual", "responsiveBlueBoundPaper")
        self.setProperty("uploadMaterial", "cottonPaper")
        self.setFixedHeight(_SOURCE_SLOT_HEIGHT)
        self.setStyleSheet(
            "QFrame#documentPaperDropArea { background: transparent; border: none; }"
        )
        glyph = self.glyph.layout().itemAt(0).widget()
        if isinstance(glyph, QLabel):
            glyph.setPixmap(icons.pixmap("download", consumer.accent, 20))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            paper = QRectF(1.5, 1.5, self.width() - 3.0, self.height() - 3.0)
            hovered = self.underMouse() or self.hasFocus()
            fill = T.consumer_qcolor(self._consumer.surface_focal)
            edge = T.consumer_qcolor(
                self._consumer.accent_hairline_active if hovered else self._consumer.accent_hairline
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(paper, _SOURCE_CORNER_RADIUS, _SOURCE_CORNER_RADIUS)
            texture = PaperTextureFrame.texture_pixmap()
            if not texture.isNull():
                clip = QPainterPath()
                clip.addRoundedRect(paper, _SOURCE_CORNER_RADIUS, _SOURCE_CORNER_RADIUS)
                painter.save()
                painter.setClipPath(clip)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                painter.setOpacity(min(0.20, self._consumer.paper_texture_opacity * 1.6 + 0.03))
                painter.drawTiledPixmap(self.rect(), texture)
                painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(edge, 1.35 if self.hasFocus() else 0.86))
            painter.drawRoundedRect(paper, _SOURCE_CORNER_RADIUS, _SOURCE_CORNER_RADIUS)
        finally:
            painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().leaveEvent(event)

