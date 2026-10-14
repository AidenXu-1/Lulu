"""Local CSV dashboard using Qt painting; opening it never queries Douyin."""
from datetime import datetime
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel,
                              QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .control_foundation import PaperTextureFrame
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
        values = [s[self.metric_key] for s in self.samples if s[self.metric_key] is not None]
        self.coordinates, self.path_count = [], 0
        self.plot = QRectF()
        if not values:
            painter.setPen(QColor('#8b8174'))
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
        painter.setPen(QPen(QColor('#e8e0d2'), 1, Qt.PenStyle.DashLine))
        for i in range(5):
            y = self.plot.bottom() - self.plot.height() * i / 4
            painter.drawLine(QPointF(self.plot.left(), y), QPointF(self.plot.right(), y))
            painter.setPen(QColor('#8b8174'))
            painter.drawText(QRectF(0, y - 10, label_width, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f'{step * i:,}')
            painter.setPen(QPen(QColor('#e8e0d2'), 1, Qt.PenStyle.DashLine))
        painter.setPen(QColor('#8b8174'))
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
        gradient.setColorAt(0, QColor('#dfe9e8'))
        gradient.setColorAt(1, QColor(223, 233, 232, 15))
        for points in runs:
            line = QPainterPath(points[0])
            for point in points[1:]:
                line.lineTo(point)
            area = QPainterPath(line)
            area.lineTo(points[-1].x(), self.plot.bottom())
            area.lineTo(points[0].x(), self.plot.bottom())
            area.closeSubpath()
            painter.fillPath(area, gradient)
            painter.setPen(QPen(QColor('#437f96'), 2.5))
            painter.drawPath(line)
            if len(self.samples) <= 120 or len(points) == 1:
                painter.setBrush(QColor('#fffdf7'))
                for point in points:
                    painter.drawEllipse(point, 2.8, 2.8)
        if self.selected is not None and self.selected < len(self.samples):
            s = self.samples[self.selected]
            x = self.plot.left() + (s['at'] - start) / span * self.plot.width()
            painter.setPen(QPen(QColor('#9ebbc4'), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, self.plot.top()), QPointF(x, self.plot.bottom()))
            point = self.coordinates[self.selected]
            if point:
                painter.setPen(QPen(QColor('#437f96'), 2.5))
                painter.setBrush(QColor('#fffdf7'))
                painter.drawEllipse(point, 5, 5)
            number = s[self.metric_key]
            raw = s.get('cumulative_view_count_raw', '') if self.metric_key == 'cumulative_view_count' else ''
            value = f'{number:,}' if number is not None else raw or '未返回数据'
            tip = datetime.fromtimestamp(s['at']).strftime('%m-%d %H:%M') + '\n' + value
            tip_width = max(160, max(painter.fontMetrics().horizontalAdvance(line) for line in tip.splitlines()) + 24)
            box = QRectF(min(max(6, x + 14), self.width() - tip_width - 6), self.plot.top() + 10, tip_width, 66)
            painter.setPen(QColor('#e5dccc'))
            painter.setBrush(QColor('#fffdf7'))
            painter.drawRoundedRect(box, 12, 12)
            painter.setPen(QColor('#35312b'))
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
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.recording_id = data['id']
        self.metric_key = 'viewer_count'
        self.data, self.anchor = {}, time.monotonic()
        self.stale = False
        self.setObjectName('liveDataDialog')
        self.setWindowTitle((data['name'] or '主播') + ' · 直播数据')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(560, 480)
        available = self.screen().availableGeometry()
        self.resize(min(960, available.width() - 40), min(680, available.height() - 60))
        self.setStyleSheet('''
            QDialog#liveDataDialog{background:#fcf8ef;color:#35312b;font-size:14px;}
            QDialog#liveDataDialog QLabel{background:transparent;border:0;color:#35312b;}
            QDialog#liveDataDialog QLabel[secondary="true"]{color:#877d70;font-size:12px;}
            QDialog#liveDataDialog QFrame#chartCard{background:#fffdf7;border:1px solid #e7decf;border-radius:16px;}
            QDialog#liveDataDialog QFrame#metricSwitch{background:#eee9de;border:0;border-radius:11px;}
            QDialog#liveDataDialog QPushButton{background:#fffdf7;border:1px solid #e6ddcf;border-radius:10px;
                padding:8px 13px;color:#35312b;}
            QDialog#liveDataDialog QPushButton:hover{border-color:#8aafba;background:#f4f2e9;}
            QDialog#liveDataDialog QPushButton:focus{border-color:#437f96;}
            QDialog#liveDataDialog QPushButton:disabled{color:#aaa196;}
            QDialog#liveDataDialog QFrame#metricSwitch QPushButton{background:transparent;border:1px solid transparent;
                padding:7px 10px;color:#877d70;}
            QDialog#liveDataDialog QFrame#metricSwitch QPushButton:checked{background:#fffdf7;color:#437f96;font-weight:600;}
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        paper = PaperTextureFrame(strength=.055, base_color='#fcf8ef')
        outer.addWidget(paper)
        layout = QVBoxLayout(paper)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)
        heading = QHBoxLayout()
        self.title = ElidedLabel()
        self.title.setStyleSheet('font-size:22px;font-weight:600;')
        heading.addWidget(self.title, 1)
        close = QPushButton('×')
        close.setAutoDefault(False)
        close.setAccessibleName('关闭直播数据')
        close.setFixedSize(34, 34)
        close.clicked.connect(self.reject)
        heading.addWidget(close)
        header_layout.addLayout(heading)
        self.subtitle = ElidedLabel()
        self.subtitle.setProperty('secondary', True)
        header_layout.addWidget(self.subtitle)
        self.session_label = ElidedLabel()
        self.session_label.setProperty('secondary', True)
        header_layout.addWidget(self.session_label)
        layout.addWidget(header)
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
        self.folder_button = QPushButton('打开数据文件夹')
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
        self.title.setText((data['name'] or '主播') + ' · 直播数据')
        self.subtitle.setText(data.get('title') or '平台暂未返回直播标题')
        start = datetime.fromtimestamp(data['started']).strftime('%m-%d %H:%M')
        end = datetime.fromtimestamp(data['ended']).strftime('%H:%M') if data['ended'] is not None else recording_message(data)
        self.session_label.setText(f'{start} 至 {end}    ·    ' + recording_message(data))
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
