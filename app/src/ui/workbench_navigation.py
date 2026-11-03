"""One interruptible selection surface for workbench and library destinations."""
from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QVBoxLayout

from .indextts2_settings import reduced_motion


class WorkbenchNavigation(QFrame):
    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.setObjectName('workbenchNavigation')
        self.theme = theme
        self.rows = {}
        self.current_index = None
        self.indicator = QRectF()
        self.animation_allowed = True
        self.keyboard_focus = None
        # Read the system preference once, never launch a process in a click.
        self.motion_enabled = not reduced_motion()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(240)
        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(QPointF(.23, 1), QPointF(.32, 1), QPointF(1, 1))
        self.animation.setEasingCurve(curve)
        self.animation.valueChanged.connect(self._move_indicator)

    def add_row(self, index, row):
        self.rows[index] = row
        self.layout().addWidget(row)
        row.installEventFilter(self)

    def eventFilter(self, row, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            self.animation_allowed = True
            self.keyboard_focus = None
            self.update()
        elif event.type() == QEvent.Type.KeyPress:
            self.animation_allowed = False
            self.keyboard_focus = row
            self.update()
        elif event.type() == QEvent.Type.FocusIn:
            self.keyboard_focus = row if event.reason() in (
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason) else None
            self.update()
        elif event.type() == QEvent.Type.FocusOut and self.keyboard_focus is row:
            self.keyboard_focus = None
            self.update()
        return super().eventFilter(row, event)

    def _move_indicator(self, value):
        self.indicator = QRectF(value)
        self.update()

    def select(self, index):
        row = self.rows.get(index)
        if row is None:
            self.animation.stop()
            self.current_index = None
            self.indicator = QRectF()
            self.update()
            return
        self.layout().activate()
        target = QRectF(row.geometry()).adjusted(.5, .5, -.5, -.5)
        if self.current_index == index and self.animation.endValue() == target:
            return
        self.current_index = index
        self.animation.stop()
        if not self.motion_enabled or not self.animation_allowed or not self.isVisible() or self.indicator.isEmpty():
            self._move_indicator(target)
        else:
            # Continue from the currently painted position on rapid clicks.
            self.animation.setStartValue(self.indicator)
            self.animation.setEndValue(target)
            self.animation.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.current_index in self.rows:
            self.layout().activate()
            self.animation.stop()
            self._move_indicator(QRectF(self.rows[self.current_index].geometry()).adjusted(.5, .5, -.5, -.5))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.indicator.isEmpty():
            # A flat translucent mark lets the sidebar paper show through.
            # Keep the shared motion and marker without a raised button edge.
            pill = self.indicator.adjusted(1, 1, -1, -1)
            radius = 10
            painter.setPen(Qt.PenStyle.NoPen)
            wash = QColor(self.theme.accent); wash.setAlphaF(.07)
            painter.setBrush(wash)
            painter.drawRoundedRect(pill, radius, radius)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.theme.accent))
            marker = QRectF(pill.left()+6, pill.center().y()-7, 2.5, 14)
            painter.drawRoundedRect(marker, 1.25, 1.25)
        if self.keyboard_focus is not None and self.keyboard_focus.hasFocus():
            focus = QRectF(self.keyboard_focus.geometry()).adjusted(2.5, 2.5, -2.5, -2.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(self.theme.accent), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(focus, 10, 10)
