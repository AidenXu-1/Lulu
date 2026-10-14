"""波形选段控件：画出参考音频波形，用户拖一个高亮窗口选哪一段做参考/训练片段。

- 灰色竖条 = 整段波形；橙色高亮 = 当前选中的窗口（宽度 = 时长档）；可拖动移动窗口位置。
- 可选播放头（试听时跟随）。选择变化发 selectionChanged(start_seconds)。
纯 QPainter，不依赖音频库；峰值由外部算好传入（set_peaks）。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from . import theme as T


class WaveformSelector(QWidget):
    selectionChanged = Signal(float)   # 选区起点（秒）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._peaks: list[float] = []
        self._dur = 0.0          # 总时长（秒）
        self._sel_start = 0.0    # 选区起点（秒）
        self._sel_len = 8.0      # 选区时长（秒）
        self._playhead = -1.0    # 播放头（秒）,<0 不画
        self._dragging = False
        self.setMinimumHeight(64)
        self.setCursor(Qt.PointingHandCursor)

    # ---- 数据
    def set_peaks(self, peaks: list[float], duration: float) -> None:
        self._peaks = peaks or []
        self._dur = max(duration, 0.001)
        self.update()

    def set_selection(self, start: float, length: float) -> None:
        self._sel_len = max(length, 0.1)
        self._sel_start = self._clamp_start(start)
        self.update()

    def set_playhead(self, seconds: float) -> None:
        self._playhead = seconds
        self.update()

    def selection_start(self) -> float:
        return self._sel_start

    def _clamp_start(self, s: float) -> float:
        return max(0.0, min(s, max(self._dur - self._sel_len, 0.0)))

    # ---- 交互：拖动让选区中心跟随光标
    def _x_to_time(self, x: float) -> float:
        w = max(self.width() - 2, 1)
        return (x - 1) / w * self._dur

    def mousePressEvent(self, e) -> None:
        if self._dur <= 0:
            return
        self._dragging = True
        self._move_to(e.position().x())

    def mouseMoveEvent(self, e) -> None:
        if self._dragging:
            self._move_to(e.position().x())

    def mouseReleaseEvent(self, e) -> None:
        self._dragging = False

    def _move_to(self, x: float) -> None:
        start = self._clamp_start(self._x_to_time(x) - self._sel_len / 2)
        if abs(start - self._sel_start) > 1e-4:
            self._sel_start = start
            self.update()
            self.selectionChanged.emit(self._sel_start)

    # ---- 绘制
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w = self.width()
        h = self.height()
        mid = h / 2
        gray = QColor(T.INK_FAINT)
        terra = QColor(T.TERRA)
        if not self._peaks or self._dur <= 0:
            p.setPen(QColor(T.INK_FAINT))
            p.drawText(self.rect(), Qt.AlignCenter, "选择音色后显示波形")
            return

        n = len(self._peaks)
        # 选区可能比整段还长（如 10s 档配 6s 参考音频）→ 画到边界为止，别溢出控件
        sel_end = min(self._sel_start + self._sel_len, self._dur)
        sel_x0 = 1 + (self._sel_start / self._dur) * (w - 2)
        sel_x1 = 1 + (sel_end / self._dur) * (w - 2)
        # 选区底色（浅橙圆角）
        sel_bg = QColor(terra); sel_bg.setAlpha(36)
        p.setPen(Qt.NoPen)
        p.setBrush(sel_bg)
        p.drawRoundedRect(QRectF(sel_x0, 2, max(sel_x1 - sel_x0, 2), h - 4), 6, 6)
        # 波形竖条
        bar_w = max((w - 2) / n, 1.0)
        for i, pk in enumerate(self._peaks):
            x = 1 + i * (w - 2) / n
            bh = max(pk * (h - 8), 2.0)
            in_sel = sel_x0 - 0.5 <= x <= sel_x1 + 0.5
            p.setBrush(terra if in_sel else gray)
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(x, mid - bh / 2, max(bar_w - 1.0, 1.0), bh))
        # 选区边框
        pen = p.pen(); pen.setColor(terra); pen.setWidth(0)
        border = QColor(terra); border.setAlpha(140)
        p.setBrush(Qt.NoBrush)
        p.setPen(border)
        p.drawRoundedRect(QRectF(sel_x0, 2, max(sel_x1 - sel_x0, 2), h - 4), 6, 6)
        # 播放头
        if 0 <= self._playhead <= self._dur:
            px = 1 + (self._playhead / self._dur) * (w - 2)
            p.setPen(QColor(T.INK))
            p.drawLine(int(px), 2, int(px), h - 2)
