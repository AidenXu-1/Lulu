"""Bounded, asynchronous local waveform preview; never alters source audio."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QSlider
from . import theme as T
from .audio_waveform import AudioWaveform


class VoiceWaveSlider(QSlider):
    seekRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setMinimumWidth(60)
        self.setFixedHeight(44)
        self.setRange(0, 0)
        self.peaks = []
        self._path = ""
        self.decoder = AudioWaveform(self)
        self.decoder.changed.connect(self._waveform_changed)
        self._keyboard_focus = False
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.setStyleSheet("QSlider {border:0;background:transparent;}")
        QApplication.instance().aboutToQuit.connect(self.decoder.shutdown)
        self.valueChanged.connect(self.update)

    def stop_decode(self):
        self.decoder.shutdown()

    def load_file(self, path):
        self._path = str(path or "")
        self.decoder.load_file(self._path)

    def _waveform_changed(self):
        self.peaks = self.decoder.peaks
        self.setToolTip("点击或拖动波形定位；方向键微调" if self.peaks else self.decoder.description)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        c = T.consumer_theme(T.ACTIVE.name)
        # A decorative empty state must never impersonate an actual file.
        values = self.peaks or ([0] * 100 if self._path else [.08 + .32 * abs(math.sin(i * .71)) for i in range(100)])
        count = max(2, self.width() // 5)
        if len(values) > count:
            values = [max(values[i * len(values) // count:(i + 1) * len(values) // count]) for i in range(count)]
        progress = self.value() / self.maximum() if self.maximum() else 0
        for index, amplitude in enumerate(values):
            x = 3 + index * (self.width() - 6) / max(1, len(values) - 1)
            height = max(2, amplitude * (self.height() - 12))
            color = QColor(c.accent if self.peaks and index / len(values) <= progress else c.ink_muted)
            if self.peaks and index / len(values) > progress:
                color.setAlpha(100)
            if not self.peaks:
                color.setAlpha(75 if not self._path else 120)
            painter.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(int(x), int((self.height() - height) / 2), int(x), int((self.height() + height) / 2))
        if self.peaks and self.maximum():
            x = 3 + progress * (self.width() - 6)
            painter.setPen(QPen(QColor(c.accent), 1.2))
            painter.drawLine(int(x), 4, int(x), self.height() - 4)
        if self.hasFocus() and self._keyboard_focus:
            # Keyboard feedback without framing the audio as an input field.
            painter.setPen(QPen(QColor(c.accent), 1))
            painter.drawLine(3, self.height() - 2, self.width() - 3, self.height() - 2)

    def _seek(self, event):
        if self.maximum():
            self.setValue(round(max(0, min(1, (event.position().x() - 3) / max(1, self.width() - 6))) * self.maximum()))
            self.seekRequested.emit(self.value())

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
        super().focusInEvent(event)
        self.update()

    def keyPressEvent(self, event):
        self._keyboard_focus = True
        super().keyPressEvent(event)
        self.seekRequested.emit(self.value())

    def wheelEvent(self, event):
        # Scrolling the page must never unexpectedly seek audio.
        event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._keyboard_focus = False
            self.setFocus(Qt.MouseFocusReason)
            self.setSliderDown(True)
            self._seek(event)
            event.accept()

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self._seek(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._seek(event)
            self.setSliderDown(False)
            event.accept()
