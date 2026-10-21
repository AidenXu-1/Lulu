"""Bounded comment counts on a receive-time axis; no media seek is implied."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme as T


def elapsed_clock(value):
    value = max(0, int(value))
    return (f'{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}'
            if value >= 3600 else f'{value // 60:02d}:{value % 60:02d}')


class CommentTimeline(QWidget):
    selectionChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bins, self.duration, self.selected_range = [], 0, None
        self.searching, self.hovered, self.plot = False, None, QRectF()
        self.has_status = False
        self.setFixedHeight(128)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('弹幕时间总览')
        self.setAccessibleDescription('点击时段筛选弹幕；左右方向键切换时段，Esc 查看全部。斜纹表示连接或保存不完整。')

    def set_data(self, summary, *, searching=False, selected_range=None):
        self.bins = summary.get('bins', [])
        self.duration = summary.get('duration', 0)
        self.has_status = summary.get('has_status', False)
        self.searching, self.selected_range = searching, selected_range
        self.hovered = None
        self.setEnabled(bool(self.bins))
        self.update()

    def select(self, index):
        if not self.bins:
            return
        item = self.bins[index]
        self.selected_range = (item['start'], item['end'])
        description = self.description(index)
        self.setAccessibleDescription(description)
        self.selectionChanged.emit(self.selected_range)
        self.update()

    def clear_selection(self):
        self.selected_range = None
        self.selectionChanged.emit(None)
        self.update()

    def description(self, index):
        item = self.bins[index]
        text = f"{elapsed_clock(item['start'])} 至 {elapsed_clock(item['end'])}，已收到 {item['count']:,} 条"
        if self.searching:
            text += f"，匹配 {item['matching']:,} 条"
        if item['interrupted']:
            text += '；该时段连接或保存不完整，未收到的数量无法确定'
        return text

    def index_at(self, position):
        if not self.bins or self.plot.width() <= 0 or not self.plot.left() <= position.x() <= self.plot.right():
            return None
        when = (position.x() - self.plot.left()) / self.plot.width() * self.duration
        return min(len(self.bins) - 1, max(0, int(when / self.bins[0]['end'])))

    def mouseMoveEvent(self, event):
        self.hovered = self.index_at(event.position())
        self.setToolTip(self.description(self.hovered) if self.hovered is not None else '')
        self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        index = self.index_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and index is not None:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.select(index)
            return
        super().mousePressEvent(event)

    def leaveEvent(self, event):
        self.hovered = None
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.selected_range is not None:
            self.clear_selection()
            event.accept()
            return
        if self.bins and event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End}:
            current = next((i for i, b in enumerate(self.bins)
                            if self.selected_range and b['start'] <= self.selected_range[0] < b['end']), None)
            if event.key() == Qt.Key.Key_Home:
                index = 0
            elif event.key() == Qt.Key.Key_End:
                index = len(self.bins) - 1
            else:
                index = 0 if current is None else current + (1 if event.key() == Qt.Key.Key_Right else -1)
            self.select(max(0, min(len(self.bins) - 1, index)))
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = T.consumer_theme()
        font = QFont(c.font_body)
        font.setPixelSize(11)
        painter.setFont(font)
        if not self.bins:
            self.plot = QRectF()
            painter.setPen(QColor(c.ink_muted))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '暂无可显示的时间记录')
            return
        peak = max(1, max(b['count'] for b in self.bins))
        label_width = max(36, painter.fontMetrics().horizontalAdvance(f'{peak:,}') + 10)
        self.plot = QRectF(label_width, 23, max(1, self.width() - label_width - 12), max(24, self.height() - 68))
        x = lambda at: self.plot.left() + at / self.duration * self.plot.width()
        painter.setPen(QColor(c.ink_muted))
        painter.drawText(QRectF(0, 0, label_width, 18), '条')
        for number in (0, peak):
            y = self.plot.bottom() - number / peak * self.plot.height()
            painter.setPen(QPen(QColor(c.line), 1))
            painter.drawLine(QPointF(self.plot.left(), y), QPointF(self.plot.right(), y))
            painter.setPen(QColor(c.ink_muted))
            painter.drawText(QRectF(0, y - 8, label_width - 8, 16),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f'{number:,}')
        for i, item in enumerate(self.bins):
            start, end = x(item['start']), x(item['end'])
            width = end - start
            selected = (self.selected_range is not None and item['start'] < self.selected_range[1]
                        and item['end'] > self.selected_range[0])
            if selected or i == self.hovered:
                painter.fillRect(QRectF(start, 17, width, self.height() - 23), T.consumer_qcolor(c.accent_wash))
            height = item['count'] / peak * self.plot.height()
            gap = min(3, width * .16)
            bar = QRectF(start + gap, self.plot.bottom() - height, max(1, width - 2 * gap), height)
            fill = QColor(c.accent)
            fill.setAlpha(70 if self.searching else 205 if selected else 155)
            painter.fillRect(bar, fill)
            if self.searching and item['matching']:
                matched = item['matching'] / peak * self.plot.height()
                painter.fillRect(QRectF(bar.left(), self.plot.bottom() - matched, bar.width(), matched), QColor(c.accent))
            if width >= 34 and item['count']:
                painter.setPen(QColor(c.ink_muted))
                painter.drawText(QRectF(start, self.plot.bottom() - height - 18, width, 16),
                                 Qt.AlignmentFlag.AlignCenter, str(item['count']))
            lane = QRectF(start, self.height() - 15, width, 7)
            painter.fillRect(lane, T.consumer_qcolor(c.secondary))
            if item['interrupted']:
                painter.fillRect(lane, QBrush(QColor(c.task_group_warning), Qt.BrushStyle.BDiagPattern))
            elif self.has_status:
                lane_fill = QColor(c.accent)
                lane_fill.setAlpha(65)
                painter.fillRect(lane, lane_fill)
        painter.setPen(QColor(c.ink_muted))
        painter.drawText(QRectF(0, self.height() - 21, label_width - 8, 18),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, '连接')
        ticks = 3 if self.width() < 640 else 5
        for i in range(ticks):
            when = self.duration * i / (ticks - 1)
            text = elapsed_clock(when)
            width = painter.fontMetrics().horizontalAdvance(text) + 4
            left = min(max(self.plot.left(), x(when) - width / 2), self.plot.right() - width)
            painter.drawText(QRectF(left, self.plot.bottom() + 4, width, 18), Qt.AlignmentFlag.AlignCenter, text)
        if self.hasFocus():
            painter.setPen(QPen(QColor(c.accent), 1, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 6, 6)
