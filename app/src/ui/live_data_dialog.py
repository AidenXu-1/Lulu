"""Local CSV dashboard using Qt painting; opening it never queries Douyin."""
from datetime import datetime
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel,
                              QPushButton, QSizePolicy, QTabWidget, QVBoxLayout, QWidget)

from . import theme as T
from .control_foundation import (LuluButton, LuluIconButton, LuluSegmentedControl,
                                 PaperTextureFrame)
from .live_comments_panel import LiveCommentsPanel
from ..core.live_recording.contracts import recording_message

METRICS = {
    'viewer_count': ('在线人数', '每 5 分钟采样，可能遗漏短暂高峰。'),
    'like_count': ('点赞', '平台点赞次数，可含重复点赞。'),
    'cumulative_view_count': ('累计观看', '平台累计值，可含重复访问，非去重人数；近似值保留原文。'),
}

def clock(value):
    value = max(0, int(value))
    return f'{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}'

class ElidedLabel(QLabel):
    """A single line whose full text remains available to accessibility and hover."""
    def __init__(self, value='', parent=None):
        super().__init__(value, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, value):
        super().setText(value)
        self.setToolTip(value)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        value = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(self.rect(), self.alignment() | Qt.AlignmentFlag.AlignVCenter, value)

class LiveDataChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples, self.metric_key, self.selected = [], 'viewer_count', None
        self.setMinimumHeight(100)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('直播数据趋势，左右方向键查看采样值')
        self.plot = QRectF()
        self.coordinates = []
        self.path_count = 0
        self.empty_message = '暂无直播数据\n旧录像不会补录历史数据'

    def set_data(self, samples, metric):
        selected_at = self.samples[self.selected]['at'] if self.selected is not None and self.selected < len(self.samples) else None
        self.samples, self.metric_key = samples, metric
        self.selected = next((i for i, sample in enumerate(samples) if sample['at'] == selected_at), None)
        self._accessible_sample()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(QFont(self.font().family(), 10))
        c = T.consumer_theme()
        values = [s[self.metric_key] for s in self.samples if s[self.metric_key] is not None]
        self.coordinates, self.path_count = [], 0
        self.plot = QRectF()
        if not values:
            painter.setPen(QColor(c.ink_muted))
            raw_samples = [s for s in self.samples if self.metric_key == 'cumulative_view_count'
                           and s.get('cumulative_view_count_raw')]
            raw = ''
            if self.metric_key == 'cumulative_view_count':
                raw = (self.samples[self.selected].get('cumulative_view_count_raw', '') if self.selected is not None else
                       raw_samples[-1]['cumulative_view_count_raw'] if raw_samples else '')
            message = ('平台累计观看：' + raw + '\n平台仅提供近似值或下限，暂无精确曲线' if raw else
                       '这次采样未取得精确值' if raw_samples else
                       '这项数据暂未返回\n有数据后将在这里显示' if self.samples else self.empty_message)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             message)
            return
        top = max(1, max(values))
        # Readable grid levels without implying a larger measured peak.
        scale = 10 ** max(0, len(str(top)) - 2)
        step = max(1, ((top + 4 * scale - 1) // (4 * scale)) * scale)
        top = step * 4
        label_width = max(54, painter.fontMetrics().horizontalAdvance(f'{top:,}') + 3)
        self.plot = QRectF(label_width + 10, 12, max(1, self.width() - label_width - 46), max(1, self.height() - 51))
        start, end = self.samples[0]['at'], self.samples[-1]['at']
        span = max(300, end - start)
        painter.setPen(QPen(QColor(c.line), 1, Qt.PenStyle.DashLine))
        for i in range(5):
            y = self.plot.bottom() - self.plot.height() * i / 4
            painter.drawLine(QPointF(self.plot.left(), y), QPointF(self.plot.right(), y))
            painter.setPen(QColor(c.ink_muted))
            painter.drawText(QRectF(0, y - 10, label_width, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f'{step * i:,}')
            painter.setPen(QPen(QColor(c.line), 1, Qt.PenStyle.DashLine))
        painter.setPen(QColor(c.ink_muted))
        ticks = 3 if self.width() < 650 else 5
        for i in range(ticks):
            x = self.plot.left() + self.plot.width() * i / (ticks - 1)
            painter.drawText(QRectF(x - 36, self.plot.bottom() + 12, 72, 22), Qt.AlignmentFlag.AlignCenter,
                             datetime.fromtimestamp(start + span * i / (ticks - 1)).strftime('%m-%d' if span >= 86400 else '%H:%M'))
        runs, run, last = [], [], None
        for s in self.samples:
            number = s[self.metric_key]
            point = None if number is None else QPointF(self.plot.left() + (s['at'] - start) / span * self.plot.width(),
                         self.plot.bottom() - number / top * self.plot.height())
            self.coordinates.append(point)
            if point is None or (last is not None and s['at'] - last > 450):
                if run:
                    runs.append(run)
                run = []
            if point is not None:
                run.append(point)
            last = s['at']
        if run:
            runs.append(run)
        self.path_count = len(runs)
        gradient = QLinearGradient(0, self.plot.top(), 0, self.plot.bottom())
        fill = QColor(c.accent)
        fill.setAlpha(28)
        gradient.setColorAt(0, fill)
        fill.setAlpha(0)
        gradient.setColorAt(1, fill)
        for points in runs:
            line = QPainterPath(points[0])
            for point in points[1:]:
                line.lineTo(point)
            area = QPainterPath(line)
            area.lineTo(points[-1].x(), self.plot.bottom())
            area.lineTo(points[0].x(), self.plot.bottom())
            area.closeSubpath()
            painter.fillPath(area, gradient)
            painter.setPen(QPen(QColor(c.accent), 2.5))
            painter.drawPath(line)
            if len(self.samples) <= 120 or len(points) == 1:
                painter.setBrush(QColor(c.surface))
                for point in points:
                    painter.drawEllipse(point, 2.8, 2.8)
        if self.selected is not None and self.selected < len(self.samples):
            s = self.samples[self.selected]
            x = self.plot.left() + (s['at'] - start) / span * self.plot.width()
            painter.setPen(QPen(T.consumer_qcolor(c.accent_hairline), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, self.plot.top()), QPointF(x, self.plot.bottom()))
            point = self.coordinates[self.selected]
            if point:
                painter.setPen(QPen(QColor(c.accent), 2.5))
                painter.setBrush(QColor(c.surface))
                painter.drawEllipse(point, 5, 5)
            number = s[self.metric_key]
            raw = s.get('cumulative_view_count_raw', '') if self.metric_key == 'cumulative_view_count' else ''
            value = f'{number:,}' if number is not None else raw or '未返回数据'
            tip = datetime.fromtimestamp(s['at']).strftime('%m-%d %H:%M') + '\n' + value
            tip_width = max(160, max(painter.fontMetrics().horizontalAdvance(line) for line in tip.splitlines()) + 24)
            box = QRectF(min(max(6, x + 14), self.width() - tip_width - 6), self.plot.top() + 10, tip_width, 66)
            painter.setPen(QColor(c.line))
            painter.setBrush(QColor(c.surface))
            painter.drawRoundedRect(box, 12, 12)
            painter.setPen(QColor(c.ink))
            painter.drawText(box.adjusted(12, 8, -12, -8), Qt.AlignmentFlag.AlignVCenter, tip)

    def mouseMoveEvent(self, event):
        if self.samples and self.plot.width() > 0 and self.plot.contains(event.position()):
            start, end = self.samples[0]['at'], self.samples[-1]['at']
            when = start + (event.position().x() - self.plot.left()) / self.plot.width() * max(300, end - start)
            self.selected = min(range(len(self.samples)), key=lambda i: abs(self.samples[i]['at'] - when))
            self._accessible_sample()
            self.update()

    def _accessible_sample(self):
        if self.selected is None:
            self.setAccessibleDescription('')
            return
        sample = self.samples[self.selected]
        value = sample[self.metric_key]
        raw = sample.get('cumulative_view_count_raw', '') if self.metric_key == 'cumulative_view_count' else ''
        self.setAccessibleDescription(datetime.fromtimestamp(sample['at']).strftime('%m-%d %H:%M') + '，' +
                                      METRICS[self.metric_key][0] + '，' + (str(value) if value is not None else raw or '未返回数据'))

    def leaveEvent(self, event):
        if not self.hasFocus():
            self.selected = None
            self._accessible_sample()
            self.update()

    def keyPressEvent(self, event):
        if self.samples and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 1 if event.key() == Qt.Key.Key_Right else -1
            self.selected = max(0, min(len(self.samples) - 1, (self.selected if self.selected is not None else 0) + step))
            self._accessible_sample()
            self.update()
            return
        super().keyPressEvent(event)

class LiveDataDialog(QDialog):
    def __init__(self, data, parent=None, *, controller=None):
        super().__init__(parent)
        self.recording_id = data['id']
        self.metric_key = 'viewer_count'
        self.data, self.anchor = {}, time.monotonic()
        self.stale = False
        self.setObjectName('liveDataDialog')
        self.setWindowTitle((data['name'] or '主播') + ' · 直播记录')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(560, 480)
        available = self.screen().availableGeometry()
        self.resize(min(960, available.width() - 40), min(720, available.height() - 60))
        c = T.consumer_theme()
        scroll_ink = QColor(c.ink_muted)
        scroll_handle = f'rgba({scroll_ink.red()},{scroll_ink.green()},{scroll_ink.blue()},0.24)'
        # An opaque paper tint prevents the native header's gray showing through.
        header_fill = QColor(c.surface).darker(102).name()
        self.setStyleSheet(f'''
            QDialog#liveDataDialog {{background:{c.canvas};color:{c.ink};font-family:"{c.font_body}";font-size:13px;}}
            QDialog#liveDataDialog QLabel {{background:transparent;border:0;color:{c.ink};font-family:"{c.font_body}";}}
            QDialog#liveDataDialog QLabel[secondary="true"] {{color:{c.ink_muted};font-size:12px;}}
            QDialog#liveDataDialog QLabel#recordEyebrow {{color:{c.accent};font-size:11px;font-weight:600;}}
            QDialog#liveDataDialog QLabel#recordHeading {{font-family:"{c.font_display}";font-size:26px;font-weight:600;}}
            QDialog#liveDataDialog QLabel#recordState {{background:{c.secondary};color:{c.ink_soft};border-radius:5px;padding:4px 9px;font-size:11px;}}
            QDialog#liveDataDialog QLabel#recordState[hasGap="true"] {{color:{c.task_group_warning};}}
            QDialog#liveDataDialog QFrame#chartCard,
            QDialog#liveDataDialog QFrame#commentOverview,
            QDialog#liveDataDialog QFrame#commentReadingCard {{background:{c.surface};border:1px solid {c.line};border-radius:{c.radius_surface}px;}}
            QDialog#liveDataDialog QLabel#commentOverviewTitle {{font-size:13px;font-weight:600;}}
            QDialog#liveDataDialog QLabel#commentTimelineNote {{font-size:11px;color:{c.ink_muted};}}
            QDialog#liveDataDialog QLabel#commentVideoState {{font-size:12px;color:{c.ink_soft};}}
            QDialog#liveDataDialog QLabel#commentVideoState[hasGap="true"] {{color:{c.task_group_warning};}}
            QDialog#liveDataDialog QTabWidget::pane {{border:0;background:transparent;}}
            QDialog#liveDataDialog QStackedWidget {{background:transparent;border:0;}}
            QDialog#liveDataDialog QFrame[luluControl="segment"],
            QDialog#liveDataDialog QFrame#metricSwitch {{background:{c.secondary};border:0;border-radius:8px;}}
            QDialog#liveDataDialog QPushButton[luluControl="segmentItem"],
            QDialog#liveDataDialog QFrame#metricSwitch QPushButton {{background:transparent;border:1px solid transparent;
                border-radius:6px;padding:0 14px;color:{c.ink_muted};font-size:13px;font-weight:500;}}
            QDialog#liveDataDialog QPushButton[luluControl="segmentItem"]:checked,
            QDialog#liveDataDialog QFrame#metricSwitch QPushButton:checked {{background:{c.surface};border-color:{c.line};color:{c.accent};font-weight:600;}}
            QDialog#liveDataDialog QPushButton[luluControl="segmentItem"]:enabled:hover,
            QDialog#liveDataDialog QFrame#metricSwitch QPushButton:enabled:hover {{background:{c.secondary_hover};border-color:{c.line};color:{c.accent};}}
            QDialog#liveDataDialog QPushButton[luluControl="segmentItem"]:enabled:pressed,
            QDialog#liveDataDialog QFrame#metricSwitch QPushButton:enabled:pressed {{background:{c.accent_wash};border-color:{c.accent};}}
            QDialog#liveDataDialog QPushButton[luluControl="button"] {{background:{c.secondary};color:{c.ink_soft};
                border:1px solid transparent;border-radius:8px;padding:0 12px;font-size:12px;font-weight:500;}}
            QDialog#liveDataDialog QPushButton[luluControl="button"]:enabled:hover {{background:{c.secondary_hover};color:{c.ink};}}
            QDialog#liveDataDialog QPushButton[luluControl="button"]:enabled:pressed {{background:{c.accent_wash};border-color:{c.line};}}
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="primary"] {{background:{c.accent};color:white;font-weight:600;}}
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="primary"]:enabled:hover {{background:{c.accent_hover};color:white;}}
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="primary"]:enabled:pressed {{background:{c.accent};color:white;border-color:{c.ink};}}
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="primary"]:disabled {{background:{c.secondary};color:{c.ink_muted};border-color:transparent;}}
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="icon"],
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="tertiary"] {{background:transparent;}}
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="icon"]:enabled:hover,
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="tertiary"]:enabled:hover {{background:{c.secondary_hover};}}
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="icon"]:enabled:pressed,
            QDialog#liveDataDialog QPushButton[luluControl="button"][role="tertiary"]:enabled:pressed {{background:{c.accent_wash};border-color:{c.line};}}
            QDialog#liveDataDialog QPushButton:focus {{border:1px solid {c.ink_muted};}}
            QDialog#liveDataDialog QPushButton:disabled {{background:transparent;color:{c.ink_muted};}}
            QDialog#liveDataDialog QPushButton::menu-indicator {{width:0;}}
            QDialog#liveDataDialog QLineEdit[luluControl="textField"] {{background:{c.surface};color:{c.ink};
                border:1px solid {c.line};border-radius:8px;padding:0 10px;font-family:"{c.font_body}";font-size:13px;
                selection-background-color:{c.accent_wash};}}
            QDialog#liveDataDialog QLineEdit[luluControl="textField"]:focus {{border-color:{c.accent};}}
            QDialog#liveDataDialog QTableWidget#commentReadingList {{background:transparent;border:0;outline:0;
                selection-background-color:transparent;selection-color:{c.ink};}}
            QDialog#liveDataDialog QTableWidget#commentReadingList::item:selected {{background:transparent;color:{c.ink};}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QHeaderView::section {{background:{header_fill};color:{c.ink_muted};
                border:0;border-bottom:1px solid {c.line};padding-left:12px;padding-right:12px;font-family:"{c.font_body}";font-size:12px;font-weight:500;}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QHeaderView::section:first {{border-top-left-radius:6px;}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QHeaderView::section:last {{border-top-right-radius:6px;}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QTableCornerButton::section {{background:transparent;border:0;}}
            QDialog#liveDataDialog QLabel#commentSummary {{font-size:14px;font-weight:600;}}
            QDialog#liveDataDialog QLabel#commentState {{font-size:11px;background:{c.secondary};color:{c.ink_muted};padding:3px 8px;border-radius:4px;}}
            QDialog#liveDataDialog QLabel#commentState[kind="active"] {{color:{c.accent};background:{c.accent_wash};}}
            QDialog#liveDataDialog QLabel#commentState[kind="warning"] {{color:{c.task_group_warning};background:{T.AMBER_WASH};}}
            QDialog#liveDataDialog QLabel#commentState[kind="error"] {{color:{c.danger};}}
            QDialog#liveDataDialog QLabel#commentTimeNote,
            QDialog#liveDataDialog QLabel#commentPageLabel {{font-size:11px;color:{c.ink_muted};}}
            QDialog#liveDataDialog QLabel#commentConnectionNote {{font-size:11px;color:{c.ink_muted};background:{c.secondary};padding:8px 12px;border-radius:6px;}}
            QDialog#liveDataDialog QLabel#commentNotice {{font-size:12px;color:{c.accent};}}
            QDialog#liveDataDialog QLabel#commentEmptyGlyph {{background:{c.accent_wash};border-radius:12px;}}
            QDialog#liveDataDialog QLabel#commentEmptyTitle {{font-family:"{c.font_display}";font-size:18px;font-weight:600;}}
            QDialog#liveDataDialog QLabel#commentEmptyDetail {{font-size:12px;color:{c.ink_muted};}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QScrollBar:vertical {{background:transparent;width:8px;margin:38px 0 6px 0;}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QScrollBar::handle:vertical {{background:{scroll_handle};border:0;
                border-radius:3px;min-height:32px;margin:0 1px;}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QScrollBar::handle:vertical:hover {{background:{c.accent_hairline};}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QScrollBar::add-line:vertical,
            QDialog#liveDataDialog QTableWidget#commentReadingList QScrollBar::sub-line:vertical {{height:0;border:0;background:transparent;}}
            QDialog#liveDataDialog QTableWidget#commentReadingList QScrollBar::add-page:vertical,
            QDialog#liveDataDialog QTableWidget#commentReadingList QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # A reading dialog keeps the shell's paper character with a quieter grain.
        paper = PaperTextureFrame(strength=c.paper_texture_opacity * .08, base_color=c.canvas)
        outer.addWidget(paper)
        layout = QVBoxLayout(paper)
        self.paper_layout = layout
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)
        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        heading = QHBoxLayout()
        self.title = ElidedLabel()
        self.title.setObjectName('recordHeading')
        self.title.setMinimumHeight(34)
        heading.addWidget(self.title, 1)
        heading.addStretch()
        self.record_state = QLabel()
        self.record_state.setObjectName('recordState')
        heading.addWidget(self.record_state)
        close = LuluIconButton('x', accessible_name='关闭直播记录', tooltip='关闭直播记录')
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        heading.addWidget(close)
        header_layout.addLayout(heading)
        context = QHBoxLayout()
        context.setSpacing(12)
        self.subtitle = ElidedLabel()
        self.subtitle.setProperty('secondary', True)
        self.session_label = ElidedLabel()
        self.session_label.setProperty('secondary', True)
        self.session_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        context.addWidget(self.session_label)
        context.addWidget(self.subtitle, 1)
        header_layout.addLayout(context)
        layout.addWidget(header)
        self.navigation = LuluSegmentedControl([('data', '数据走势'), ('comments', '弹幕记录')],
                                               accessible_name='直播记录内容')
        self.navigation.setFixedWidth(236)
        for button in self.navigation.buttons():
            button.setFixedHeight(32)
        layout.addWidget(self.navigation)
        self.tabs = QTabWidget()
        data_page = QWidget()
        self.tabs.addTab(data_page, '数据走势')
        self.comments_panel = LiveCommentsPanel(data, controller)
        self.tabs.addTab(self.comments_panel, '弹幕记录')
        self.tabs.tabBar().hide()
        self.navigation.selectionChanged.connect(lambda value: self.tabs.setCurrentIndex(0 if value == 'data' else 1))
        self.tabs.currentChanged.connect(lambda index: self.navigation.set_value('data' if index == 0 else 'comments'))
        self.tabs.currentChanged.connect(lambda index: self.record_state.setVisible(index == 0))
        layout.addWidget(self.tabs, 1)
        layout = QVBoxLayout(data_page)
        layout.setContentsMargins(0, 8, 0, 0)
        metrics = QHBoxLayout()
        metrics.setSpacing(16)
        self.stat_values = []
        for caption in ('已录时长', '采样最高人数', '最近在线人数', '采集记录'):
            column = QVBoxLayout()
            label = QLabel(caption)
            label.setProperty('secondary', True)
            column.addWidget(label)
            value = ElidedLabel('—')
            value.setStyleSheet('font-size:28px;font-weight:600;')
            column.addWidget(value)
            metrics.addLayout(column, 1)
            self.stat_values.append(value)
        layout.addLayout(metrics)
        card = QFrame()
        card.setObjectName('chartCard')
        body = QVBoxLayout(card)
        body.setContentsMargins(18, 14, 18, 12)
        body.setSpacing(8)
        chart_heading = QHBoxLayout()
        chart_heading.setSpacing(12)
        self.metric_title = QLabel()
        self.metric_title.setStyleSheet('font-size:16px;font-weight:600;')
        chart_heading.addWidget(self.metric_title)
        self.definition = ElidedLabel()
        self.definition.setProperty('secondary', True)
        chart_heading.addWidget(self.definition, 1)
        switches = QFrame()
        switches.setObjectName('metricSwitch')
        switch_layout = QHBoxLayout(switches)
        switch_layout.setContentsMargins(4, 4, 4, 4)
        switch_layout.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons = {}
        for key, (caption, _) in METRICS.items():
            button = QPushButton(caption)
            button.setAutoDefault(False)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, metric=key: self.select_metric(metric))
            self.group.addButton(button)
            switch_layout.addWidget(button)
            self.buttons[key] = button
        chart_heading.addWidget(switches)
        body.addLayout(chart_heading)
        self.chart = LiveDataChart()
        body.addWidget(self.chart, 1)
        footnote = QHBoxLayout()
        label = QLabel('每 5 分钟采集一次')
        label.setProperty('secondary', True)
        footnote.addWidget(label)
        self.records_label = QLabel()
        self.records_label.setProperty('secondary', True)
        footnote.addWidget(self.records_label, 1, Qt.AlignmentFlag.AlignRight)
        body.addLayout(footnote)
        layout.addWidget(card, 1)
        self.notice = ElidedLabel()
        self.notice.setProperty('secondary', True)
        layout.addWidget(self.notice)
        footer = QHBoxLayout()
        self.file_label = QLabel()
        self.file_label.setProperty('secondary', True)
        footer.addWidget(self.file_label, 1)
        self.folder_button = LuluButton('打开数据文件夹', role='secondary', icon='folder')
        self.folder_button.setAutoDefault(False)
        self.folder_button.clicked.connect(self.open_folder)
        footer.addWidget(self.folder_button)
        layout.addLayout(footer)
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self.tick_clock)
        self.finished.connect(lambda _: self.clock_timer.stop())
        self.update_data(data)

    def update_data(self, data):
        if data == self.data and not self.stale:
            self.anchor = time.monotonic()
            self.tick_clock()
            return
        self.data, self.anchor = data, time.monotonic()
        self.stale = False
        self.comments_panel.update_recording(data)
        self.title.setText(data['name'] or '主播')
        self.subtitle.setText(data.get('title') or '平台暂未返回直播标题')
        start = datetime.fromtimestamp(data['started']).strftime('%m-%d %H:%M')
        end = datetime.fromtimestamp(data['ended']).strftime('%H:%M') if data['ended'] is not None else recording_message(data)
        self.session_label.setText(f'{start} 至 {end}')
        self.record_state.setText(recording_message(data))
        self.record_state.setProperty('hasGap', data.get('gaps', '[]') != '[]')
        self.record_state.style().unpolish(self.record_state)
        self.record_state.style().polish(self.record_state)
        peak, recent = data.get('peak_viewers'), data.get('last_viewers')
        self.stat_values[1].setText(f'{peak:,}' if peak is not None else '—')
        self.stat_values[2].setText(f'{recent:,}' if recent is not None else data.get('last_viewers_raw') or '—')
        self.stat_values[3].setText('—' if data.get('read_error') else str(data.get('sample_count', 0)))
        notice = data.get('data_error') or ('显示最近 10,000 条；完整数据保存在 CSV。' if data.get('truncated') else
            '部分记录不完整，已跳过并保留原文件；可打开文件夹检查。' if data.get('invalid_count') else '')
        self.notice.setText(notice)
        self.notice.setVisible(bool(notice))
        self.folder_button.setEnabled(bool(data.get('folder')))
        self.file_label.setText('直播数据.csv\n与本场录像一起保存在本机' if data.get('folder') else
                               '数据文件暂不可用\n请检查录像保存位置' if data.get('read_error') else
                               '暂无数据文件\n数据保存后可在这里查看' if data['ended'] is None else
                               '暂无数据文件\n旧录像不会补录历史数据')
        self.chart.empty_message = ('直播数据暂时无法读取\n原文件保留，请检查保存位置' if data.get('read_error') else
                                    '还没有采集记录\n数据保存后将在这里显示' if data['ended'] is None else
                                    '这场录像没有直播数据\n旧录像不会补录历史数据')
        self.select_metric(self.metric_key)
        self.tick_clock()
        if data.get('clock_running'):
            self.clock_timer.start()
        else:
            self.clock_timer.stop()

    def select_metric(self, key):
        self.metric_key = key
        self.buttons[key].setChecked(True)
        caption, definition = METRICS[key]
        self.metric_title.setText(caption)
        self.definition.setText(definition)
        self.chart.set_data(self.data.get('samples', []), key)
        valid = sum(sample[key] is not None for sample in self.data.get('samples', []))
        raw_count = sum(sample[key] is None and bool(sample.get('cumulative_view_count_raw'))
                        for sample in self.data.get('samples', [])) if key == 'cumulative_view_count' else 0
        missing = len(self.data.get('samples', [])) - valid - raw_count
        self.records_label.setText('数据暂不可读' if self.data.get('read_error') else
                                  (f'{valid} 条精确记录 · {raw_count} 条平台原文' if raw_count else f'{valid} 条有效记录') +
                                  (f' · {missing} 条缺失' if missing else ''))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'stat_values'):
            compact = self.height() < 620
            small = self.height() < 520
            margin_y = 8 if small else 12 if compact else 20
            self.paper_layout.setContentsMargins(20 if compact else 28, margin_y,
                                                 20 if compact else 28, margin_y)
            self.paper_layout.setSpacing(8 if small else 12 if compact else 16)
            self.comments_panel.layout().setSpacing(6 if small else 8 if compact else 12)
            self.comments_panel.timeline.setFixedHeight(84 if compact else 104)
            size = 20 if self.width() < 640 else 24 if self.width() < 800 else 28
            for label in self.stat_values:
                label.setStyleSheet(f'font-size:{size}px;font-weight:600;')

    def tick_clock(self):
        elapsed = time.monotonic() - self.anchor if self.data.get('clock_running') else 0
        # Local data is refreshed every 10 seconds. A missing reply must not
        # turn a disconnected recorder into an indefinitely running UI clock.
        if elapsed >= 15 or self.stale:
            self.mark_stale()
            return
        self.stat_values[0].setText(clock(self.data.get('duration', 0) + elapsed))

    def mark_stale(self):
        self.stale = True
        self.clock_timer.stop()
        self.stat_values[0].setText(clock(self.data.get('duration', 0)))
        self.notice.setText('数据暂未更新，时长停在最后确认值；连接恢复后自动更新。')
        self.notice.setVisible(True)

    def open_folder(self):
        path = self.data.get('folder')
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
