"""Native Lulu live workspace: bounded tables, explicit media actions."""
from datetime import datetime
import json
from pathlib import Path
import time

from PySide6.QtCore import QEvent, QRegularExpression, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QRegularExpressionValidator
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog, QFileDialog, QFrame, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import icons
from .control_foundation import PaperTextureFrame
from .live_data_dialog import LiveDataDialog
from ..core.live_recording.contracts import LIVE_MESSAGES, recording_message

BLUE = '#437f96'

def duration(value):
    value = max(0, int(value))
    return f'{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}'

def stamp(value):
    return datetime.fromtimestamp(value).strftime('%m-%d %H:%M:%S') if value else '—'

def file_size(value):
    return f'{value / 1024**3:.2f} GB' if value >= 1024**3 else f'{value / 1024**2:.1f} MB'

class LiveRowDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        padded = QStyleOptionViewItem(option)
        padded.rect = option.rect.adjusted(12, 0, -12, 0)
        padded.state &= ~(QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_MouseOver)
        super().paint(painter, padded, index)
        painter.save()
        painter.setPen(QColor('#eae2d5'))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()

class LiveSegmentsDialog(QDialog):
    export_requested = Signal(object)
    transcribe_requested = Signal(object)

    def __init__(self, data, parent=None):
        # An in-window overlay receives backdrop clicks; a native macOS sheet
        # blocks those clicks before Qt can dismiss it.
        super().__init__(parent.window() if parent else None,
                         Qt.WindowType.Widget if parent else Qt.WindowType.Dialog)
        self._overlay = parent is not None
        if self._overlay:
            # QDialog's constructor forces Dialog even when given Widget (0).
            # Reset after construction so this really shares the parent canvas.
            self.setWindowFlags(Qt.WindowType.Widget)
        self._previous_focus = None
        self.busy = False
        self.recording_id = data['id']
        self._data = None
        self.setObjectName('liveSegmentsDialog')
        self.setWindowTitle((data['name'] or '主播') + ' · 录像分段')
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(660, 510)
        self.setStyleSheet('''
            QDialog#liveSegmentsDialog{background:rgba(29,25,20,80);color:#35312b;font-size:14px;}
            QDialog#liveSegmentsDialog QLabel{background:transparent;border:0;color:#35312b;}
            QDialog#liveSegmentsDialog QLabel[secondary="true"]{color:#877d70;font-size:12px;}
            QDialog#liveSegmentsDialog QListWidget{background:transparent;border:0;outline:none;}
            QDialog#liveSegmentsDialog QListWidget::item{background:#fffdf7;border:1px solid #e7decf;
                border-radius:12px;margin:0 0 8px 0;}
            QDialog#liveSegmentsDialog QListWidget::item:selected{background:#e9f0ee;border-color:#8aafba;}
            QDialog#liveSegmentsDialog QListWidget::item:hover{border-color:#b6c5bd;}
            QDialog#liveSegmentsDialog QPushButton{background:#fffdf7;border:1px solid #e6ddcf;
                border-radius:10px;padding:7px 14px;color:#35312b;}
            QDialog#liveSegmentsDialog QPushButton:hover{background:#f5f1e7;border-color:#8aafba;}
            QDialog#liveSegmentsDialog QPushButton:pressed{background:#e3eceb;}
            QDialog#liveSegmentsDialog QPushButton:focus{border-color:#437f96;}
            QDialog#liveSegmentsDialog QPushButton[primary="true"]{background:#437f96;border-color:#437f96;color:#fffdf7;}
            QDialog#liveSegmentsDialog QPushButton:disabled{background:#f3efe6;border-color:#e6ddcf;color:#aaa196;}
            QDialog#liveSegmentsDialog QPushButton#closeSegments{background:transparent;border-color:transparent;
                padding:0;font-size:22px;color:#877d70;}
            QDialog#liveSegmentsDialog QPushButton#closeSegments:hover{background:#eee8dd;}
            QDialog#liveSegmentsDialog QPushButton#closeSegments:focus{border-color:#437f96;}
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch()
        center = QHBoxLayout()
        center.addStretch()
        self.card = paper = PaperTextureFrame(strength=.055, base_color='#fcf8ef', corner_radius=16)
        center.addWidget(paper)
        center.addStretch()
        outer.addLayout(center)
        outer.addStretch()
        layout = QVBoxLayout(paper)
        layout.setContentsMargins(24, 24, 24, 22)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setStyleSheet('font-size:21px;font-weight:600;')
        heading.addWidget(self.title, 1)
        self.close_button = button('×', self.reject)
        self.close_button.setObjectName('closeSegments')
        self.close_button.setAutoDefault(False)
        self.close_button.setFixedSize(34, 34)
        self.close_button.setAccessibleName('关闭录像分段')
        self.close_button.setToolTip('关闭')
        heading.addWidget(self.close_button)
        layout.addLayout(heading)
        self.summary = QLabel()
        self.summary.setProperty('secondary', True)
        layout.addWidget(self.summary)
        note = QLabel('每段约 30 分钟，直播结束或断线时会提前保存。')
        note.setProperty('secondary', True)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.warning = QLabel()
        self.warning.setStyleSheet('color:#996946;font-size:12px;')
        layout.addWidget(self.warning)
        self.listing = QListWidget()
        self.listing.setAccessibleName('录像分段')
        self.listing.setSpacing(0)
        layout.addWidget(self.listing, 1)
        self.hint = QLabel('选中已保存分段：导出视频或音频，或创建本地文案任务。')
        self.hint.setProperty('secondary', True)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        row = QHBoxLayout()
        folder = button('打开文件夹', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(data['directory'])))
        folder.setIcon(QIcon(icons.pixmap('folder', '#877d70', 16)))
        row.addWidget(folder)
        row.addStretch()
        self.export_button = button('导出', lambda: self._selected(self.export_requested))
        self.extract_button = button('提取文案', lambda: self._selected(self.transcribe_requested), primary=True)
        self.export_button.setEnabled(False)
        self.extract_button.setEnabled(False)
        row.addWidget(self.export_button)
        row.addWidget(self.extract_button)
        layout.addLayout(row)
        self.listing.currentItemChanged.connect(self._selection)
        self.update_data(data)

    def _fit_overlay(self):
        if self._overlay:
            self.setGeometry(self.parentWidget().rect())
        self.card.setFixedSize(max(320, min(660, self.width() - 48)),
                               max(300, min(510, self.height() - 48)))

    def showEvent(self, event):
        self._previous_focus = QApplication.focusWidget()
        if self._overlay:
            self.parentWidget().installEventFilter(self)
        self._fit_overlay()
        self.raise_()
        super().showEvent(event)
        self.listing.setFocus(Qt.FocusReason.PopupFocusReason)

    def hideEvent(self, event):
        if self._overlay:
            self.parentWidget().removeEventFilter(self)
        super().hideEvent(event)
        # Periodic table refresh can destroy the button that opened the card.
        try:
            if self._previous_focus and self._previous_focus.isVisible():
                self._previous_focus.setFocus(Qt.FocusReason.OtherFocusReason)
        except RuntimeError:
            pass  # Only restoring focus is optional; the window still closes.
        self._previous_focus = None

    def eventFilter(self, watched, event):
        if watched is self.parentWidget():
            if event.type() == QEvent.Type.Resize:
                self._fit_overlay()
            elif event.type() == QEvent.Type.Hide:
                self.reject()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if not self.card.geometry().contains(event.position().toPoint()):
            self.reject()
            event.accept()
            return
        super().mousePressEvent(event)

    def focusNextPrevChild(self, forward):
        # Keep keyboard navigation inside the card, including its close button.
        current = QApplication.focusWidget() or self
        cursor = current
        while True:
            cursor = cursor.nextInFocusChain() if forward else cursor.previousInFocusChain()
            if cursor is current:
                return False
            if (self.isAncestorOf(cursor) and cursor.isVisible() and cursor.isEnabled() and
                    cursor.focusPolicy() & Qt.FocusPolicy.TabFocus):
                cursor.setFocus(Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason)
                return True

    def update_data(self, data):
        if self.busy or data['id'] != self.recording_id or data == self._data:
            return
        self._data = data
        self.title.setText(data['name'] or '主播')
        self.summary.setText(stamp(data['started']) + '  ·  ' + str(len(data['segments'])) + ' 个分段  ·  ' +
                             file_size(sum(s['bytes'] for s in data['segments'])) + '  ·  ' + recording_message(data))
        gaps = json.loads(data['gaps'])
        self.warning.setText('有 ' + str(len(gaps)) + ' 次录制中断，共缺失 ' + duration(sum(b-a for a,b in gaps)))
        self.warning.setToolTip('\n'.join(f'{stamp(a)} 至 {stamp(b)}' for a,b in gaps))
        self.warning.setVisible(bool(gaps))
        current = self.listing.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole)['path'] if current else None
        scroll = self.listing.verticalScrollBar().value()
        blocked = self.listing.blockSignals(True)
        try:
            self.listing.clear()
            first_ready, chosen = None, None
            for number, segment in enumerate(data['segments'], 1):
                ready = bool(segment['closed'] and segment['valid'])
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, segment)
                item.setSizeHint(QSize(0, 96))
                if not ready:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                card = QWidget()
                content = QVBoxLayout(card)
                content.setContentsMargins(18, 16, 18, 18)
                content.setSpacing(8)
                top = QHBoxLayout()
                label = QLabel(f'分段 {number}')
                label.setStyleSheet('font-weight:600;')
                top.addWidget(label)
                top.addStretch()
                unfinished = not segment['closed'] and data['ended'] is None
                state = '已保存' if ready else (recording_message(data).replace('正在录制', '录制中') if unfinished else '无法使用')
                badge = QLabel(state)
                badge.setStyleSheet('color:' + ('#59816b' if ready else '#437f96' if unfinished else '#996946') + ';font-size:12px;')
                top.addWidget(badge)
                content.addLayout(top)
                if ready:
                    span = (stamp(segment['started']) + ' 至 ' + stamp(segment['started'] + segment['duration'])
                            if segment['started'] else '起止时间无法确认')
                    times = span + '  ·  ' + duration(segment['duration'])
                else:
                    times = ('保存完成后可导出或提取文案' if unfinished else
                             '分段未通过检查，原文件仍在文件夹中')
                bottom = QLabel(times + '  ·  ' + file_size(segment['bytes']))
                bottom.setProperty('secondary', True)
                bottom.setWordWrap(True)
                content.addWidget(bottom)
                self.listing.addItem(item)
                self.listing.setItemWidget(item, card)
                if ready and first_ready is None:
                    first_ready = item
                if ready and segment['path'] == selected:
                    chosen = item
            self.listing.setCurrentItem(chosen if selected else first_ready)
            self.listing.doItemsLayout()
            self.listing.verticalScrollBar().setValue(scroll)
        finally:
            self.listing.blockSignals(blocked)
        self._selection(self.listing.currentItem(), None)

    def _selection(self, item, _previous):
        ready = bool(not self.busy and item and item.flags() & Qt.ItemFlag.ItemIsSelectable)
        self.export_button.setEnabled(ready)
        self.extract_button.setEnabled(ready)

    def set_busy(self, busy):
        self.busy = busy
        self.listing.setEnabled(not busy)
        self._selection(self.listing.currentItem(), None)

    def _selected(self, signal):
        item = self.listing.currentItem()
        if not self.busy and item and item.flags() & Qt.ItemFlag.ItemIsSelectable:
            signal.emit(item.data(Qt.ItemDataRole.UserRole))

def button(text, callback, *, primary=False):
    value = QPushButton(text)
    value.setAutoDefault(False)
    value.setProperty('primary', primary)
    value.setCursor(Qt.CursorShape.PointingHandCursor)
    value.setMinimumHeight(34)
    value.clicked.connect(callback)
    return value

def row_controls(*buttons):
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(6, 8, 6, 8)
    row.setSpacing(4)
    row.addStretch()
    for control in buttons:
        control.setFixedHeight(34)
        row.addWidget(control, alignment=Qt.AlignmentFlag.AlignVCenter)
    row.addStretch()
    return widget

class FilterButton(QPushButton):
    changed = Signal(str)

    def __init__(self, choices, parent=None):
        super().__init__(parent)
        self.choices = choices
        self.value = choices[0][1]
        self.setMinimumSize(124, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(14, 0, 14, 0)
        self.label = QLabel(choices[0][0])
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.row.addWidget(self.label)
        self.row.addStretch()
        arrow = icons.icon_label('chevron_down', '#877d70', 14)
        arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.row.addWidget(arrow)
        self.clicked.connect(self._open)
        self.setAccessibleName(choices[0][0])

    def _open(self):
        menu = QMenu(self)
        menu.setStyleSheet('QMenu{background:#fffdf7;color:#35312b;border:1px solid #ddd3c3;'
            'border-radius:10px;padding:6px;}QMenu::item{padding:9px 24px 9px 14px;border-radius:6px;}'
            'QMenu::item:selected{background:#e3eceb;color:#437f96;}')
        for label, value in self.choices:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value == self.value)
            action.triggered.connect(lambda checked=False, v=value, text=label: self.select(v, text))
        menu.setMinimumWidth(self.width())
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def select(self, value, label):
        self.value = value
        self.label.setText(label)
        self.setAccessibleName(label)
        self.changed.emit(value)

class LiveRecordingPage(QWidget):
    def __init__(self, controller, *, transcribe, parent=None):
        super().__init__(parent)
        self.controller, self.transcribe = controller, transcribe
        self.setObjectName('liveRecordingPage')
        self.pages = {'hosts': 1, 'recordings': 1}
        self.tab = 'hosts'
        self.data = {}
        self.clock_cells = []
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._tick_durations)
        self._page_draft = False
        self._page_error = ''
        self.exporting = {}
        self.deleting = set()
        self.deleted_recordings = set()
        self.detail_dialog = None
        self.detail_timer = QTimer(self)
        self.detail_timer.setInterval(2000)
        self.detail_timer.timeout.connect(self._refresh_detail)
        self.data_dialog = None
        self.data_timer = QTimer(self)
        self.data_timer.setInterval(10000)
        self.data_timer.timeout.connect(self._refresh_data)
        self.setStyleSheet('''
            QWidget#liveRecordingPage{background:transparent;color:#35312b;font-size:14px;}
            QWidget#liveRecordingPage QLabel{background:transparent;border:0;}
            QWidget#liveRecordingPage QLineEdit{
                background:#fffdf7;border:1px solid #e6ddcf;border-radius:10px;padding:9px 14px;color:#35312b;}
            QWidget#liveRecordingPage QLineEdit:focus{border:1px solid #437f96;}
            QWidget#liveRecordingPage QPushButton{background:#fffdf7;border:1px solid #e6ddcf;
                border-radius:10px;padding:6px 12px;color:#35312b;}
            QWidget#liveRecordingPage QPushButton:hover{border-color:#8aafba;background:#f9f7ef;}
            QWidget#liveRecordingPage QPushButton:focus{border-color:#437f96;outline:none;}
            QWidget#liveRecordingPage QPushButton:pressed{background:#e3eceb;}
            QWidget#liveRecordingPage QPushButton[primary="true"]{background:#437f96;color:#fffdf7;border-color:#437f96;}
            QWidget#liveRecordingPage QPushButton:disabled{color:#aaa196;}
            QWidget#liveRecordingPage QPushButton[livePager="true"]{border-radius:8px;padding:0 12px;outline:none;}
            QWidget#liveRecordingPage QPushButton[livePager="true"]:focus{border-color:#437f96;}
            QWidget#liveRecordingPage QPushButton[livePager="true"]:pressed{background:#e3eceb;border-color:#437f96;}
            QWidget#liveRecordingPage QLineEdit#livePageNumber{border-radius:8px;padding:0 8px;}
            QWidget#liveRecordingPage QPushButton:checked{background:#e3eceb;color:#437f96;}
            QFrame#liveTabs{background:#eee8dc;border:1px solid #e6ddcf;border-radius:12px;}
            QWidget#liveRecordingPage QPushButton[liveTab="true"]{background:transparent;border:1px solid transparent;
                border-radius:9px;padding:0 18px;color:#82786b;}
            QWidget#liveRecordingPage QPushButton[liveTab="true"]:hover{background:#f7f3e9;color:#35312b;}
            QWidget#liveRecordingPage QPushButton[liveTab="true"]:checked{background:#fffdf7;color:#437f96;
                border:1px solid #e6ddcf;font-weight:600;}
            QFrame#liveMetrics{background:#f9f6ee;border:1px solid #e6ddcf;border-radius:12px;}
            QWidget#liveRecordingPage QLabel[statLabel="true"]{color:#8a8073;font-size:12px;}
            QWidget#liveRecordingPage QLabel[statValue="true"]{color:#35312b;font-size:19px;font-weight:600;}
            QWidget#liveRecordingPage QTableWidget{background:transparent;alternate-background-color:#f5f0e5;
                border:1px solid #e6ddcf;border-radius:12px;gridline-color:#eee6d9;outline:none;}
            QWidget#liveRecordingPage QTableWidget::item{padding:0;}
            QWidget#liveRecordingPage QPushButton[liveRemove="true"]{background:transparent;border:1px solid transparent;
                padding:0;font-size:20px;color:#968a7b;}
            QWidget#liveRecordingPage QPushButton[liveRemove="true"]:hover{background:#f5e9df;border-color:#e9d6c5;}
            QWidget#liveRecordingPage QHeaderView::section{background:#f9f6ee;border:0;
                border-bottom:1px solid #e6ddcf;padding:12px;color:#8a8073;}
            QWidget#liveRecordingPage QHeaderView{background:transparent;}
            QWidget#liveRecordingPage QHeaderView::section:horizontal:first{border-top-left-radius:12px;}
            QWidget#liveRecordingPage QHeaderView::section:horizontal:last{border-top-right-radius:12px;}
            QFrame#liveStorage{background:#f9f6ee;border:1px solid #e6ddcf;border-radius:12px;}
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(14)
        header = QHBoxLayout()
        title = QLabel('直播录制')
        title.setStyleSheet('font-size:26px;font-weight:600;')
        header.addWidget(title)
        header.addStretch()
        self.settings_button = button('抖音访问设置', self._settings)
        self.settings_button.setIcon(QIcon(icons.pixmap('gear', '#877d70', 16)))
        header.addWidget(self.settings_button)
        self.pause_all = button('暂停全部监控', lambda: self.controller.call('pause_all'))
        header.addWidget(self.pause_all)
        layout.addLayout(header)
        self.metrics = QFrame()
        self.metrics.setObjectName('liveMetrics')
        self.metrics.setMaximumWidth(780)
        self.metrics.setFixedHeight(78)
        stats = QHBoxLayout(self.metrics)
        stats.setContentsMargins(20, 12, 20, 12)
        stats.setSpacing(18)
        self.stats = []
        for index, title in enumerate(('监控中', '录制中', '可用空间')):
            if index:
                divider = QFrame()
                divider.setFixedSize(1, 30)
                divider.setStyleSheet('background:#e6ddcf;border:0;')
                stats.addWidget(divider)
            group = QVBoxLayout()
            group.setSpacing(3)
            caption = QLabel(title)
            caption.setProperty('statLabel', True)
            label = QLabel('—')
            label.setProperty('statValue', True)
            group.addWidget(caption)
            group.addWidget(label)
            stats.addLayout(group, 1)
            self.stats.append(label)
        layout.addWidget(self.metrics)
        tabs = QHBoxLayout()
        self.tab_bar = QFrame()
        self.tab_bar.setObjectName('liveTabs')
        self.tab_bar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        segment = QHBoxLayout(self.tab_bar)
        segment.setContentsMargins(4, 4, 4, 4)
        segment.setSpacing(2)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.host_tab = button('主播监控  0', lambda: self._tab('hosts'))
        self.record_tab = button('录像库  0', lambda: self._tab('recordings'))
        for tab_button in (self.host_tab, self.record_tab):
            tab_button.setProperty('liveTab', True)
            tab_button.setCheckable(True)
            tab_button.setFixedHeight(36)
            tab_button.setMinimumWidth(134)
            self.tab_group.addButton(tab_button)
            segment.addWidget(tab_button)
        self.host_tab.setChecked(True)
        tabs.addWidget(self.tab_bar)
        tabs.addStretch()
        layout.addLayout(tabs)
        self.toolbars = QStackedWidget()
        self.toolbars.setFixedHeight(44)
        host_toolbar = QWidget()
        row = QHBoxLayout(host_toolbar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)
        left, right = QWidget(), QWidget()
        add = QHBoxLayout(left)
        add.setContentsMargins(0, 0, 0, 0)
        add.setSpacing(8)
        self.source = QLineEdit()
        self.source.setPlaceholderText('粘贴抖音个人主页或直播间链接')
        self.source.setMaxLength(4096)
        self.source.setAccessibleName('添加主播链接')
        add.addWidget(self.source, 1)
        add.addWidget(button('＋ 添加主播', self._add, primary=True))
        self.source.returnPressed.connect(self._add)
        search = QHBoxLayout(right)
        search.setContentsMargins(0, 0, 0, 0)
        search.setSpacing(8)
        self.host_search = QLineEdit()
        self.host_search.setPlaceholderText('搜索主播名称或链接')
        self.host_search.setAccessibleName('搜索主播名称或链接')
        self.host_filter = FilterButton([('全部状态', 'all'), ('直播中', 'live'),
                                         ('未直播', 'offline'), ('无法确认', 'unknown')])
        search.addWidget(self.host_search, 1)
        search.addWidget(self.host_filter)
        row.addWidget(left, 1)
        row.addWidget(right, 1)
        self.toolbars.addWidget(host_toolbar)
        records_toolbar = QWidget()
        row = QHBoxLayout(records_toolbar)
        row.setContentsMargins(0, 0, 0, 0)
        self.record_search = QLineEdit()
        self.record_search.setPlaceholderText('搜索录像的主播名称')
        self.record_filter = FilterButton([('全部日期', 'all'), ('今天', 'today'),
                                           ('最近 7 天', 'week'), ('最近 30 天', 'month')])
        row.addWidget(self.record_search, 1)
        row.addWidget(self.record_filter)
        row.addStretch(1)
        self.toolbars.addWidget(records_toolbar)
        layout.addWidget(self.toolbars)
        self.table = QTableWidget()
        self.table.setItemDelegate(LiveRowDelegate(self.table))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(100)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        table_paper = PaperTextureFrame(strength=.04, corner_radius=12, base_color='#fcf8ef')
        paper_layout = QVBoxLayout(table_paper)
        paper_layout.setContentsMargins(0, 0, 0, 0)
        paper_layout.addWidget(self.table)
        layout.addWidget(table_paper, 1)
        self.empty = QLabel('还没有主播\n粘贴上方的抖音链接，添加后开始监控。', self.table)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet('color:#8a8073;font-size:14px;background:transparent;')
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty.resize(380, 80)
        self.table.installEventFilter(self)
        self.table.viewport().installEventFilter(self)
        footer = QHBoxLayout()
        footer.setSpacing(12)
        self.count = QLabel('暂无主播，添加链接后可开始监控')
        self.count.setMinimumHeight(36)
        footer.addWidget(self.count)
        footer.addStretch()
        self.pagination = QWidget()
        self.pagination.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        pager = QHBoxLayout(self.pagination)
        pager.setContentsMargins(0, 0, 0, 0)
        pager.setSpacing(8)
        self.previous = button('上一页', lambda: self._page(-1))
        self.next = button('下一页', lambda: self._page(1))
        self.jump = QLineEdit('1')
        self.jump.setObjectName('livePageNumber')
        self.jump.setFixedSize(72, 36)
        self.jump.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.jump.setMaxLength(6)
        self.jump.setValidator(QRegularExpressionValidator(QRegularExpression('[0-9]{0,6}'), self.jump))
        self.jump.setAccessibleName('跳转页码')
        self.jump.setAccessibleDescription('输入页码后按回车，或点击跳转')
        self.jump_button = button('跳转', self._jump)
        for control in (self.previous, self.next, self.jump_button):
            control.setProperty('livePager', True)
            control.setFixedHeight(36)
        pager.addWidget(self.previous)
        pager.addWidget(self.next)
        pager.addSpacing(8)
        for control in (QLabel('到'), self.jump, QLabel('页'), self.jump_button):
            if isinstance(control, QLabel):
                control.setStyleSheet('color:#8a8073;')
            pager.addWidget(control)
        footer.addWidget(self.pagination)
        layout.addLayout(footer)
        storage = QFrame()
        storage.setObjectName('liveStorage')
        row = QHBoxLayout(storage)
        self.storage = QLabel('录像保存位置\n影片 / Lulu直播录制')
        self.storage.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.storage, 1)
        row.addWidget(button('在访达中打开', self._open_folder))
        row.addWidget(button('更改保存位置', self._directory))
        layout.addWidget(storage)
        self.credentials = QDialog(self)
        self._configured = None
        self.credentials.setWindowTitle('抖音访问设置')
        self.credentials.setObjectName('liveAccessDialog')
        self.credentials.setWindowModality(Qt.WindowModality.WindowModal)
        self.credentials.setFixedWidth(520)
        self.credentials.setStyleSheet('QDialog#liveAccessDialog{background:#fffdf7;color:#35312b;}')
        access = QVBoxLayout(self.credentials)
        access.setContentsMargins(24, 24, 24, 24)
        access.setSpacing(16)
        heading = QLabel('抖音访问设置')
        heading.setStyleSheet('font-size:20px;font-weight:600;')
        access.addWidget(heading)
        help_text = QLabel('访问受限或要求登录时，可填写你自己的抖音登录 Cookie。')
        help_text.setWordWrap(True)
        help_text.setStyleSheet('color:#8a8073;')
        access.addWidget(help_text)
        access.addWidget(QLabel('登录 Cookie'))
        self.credential = QLineEdit()
        self.credential.setEchoMode(QLineEdit.EchoMode.Password)
        self.credential.setMaxLength(16384)
        self.credential.setPlaceholderText('粘贴你自己的抖音 Cookie')
        self.credential.setAccessibleName('抖音登录 Cookie')
        access.addWidget(self.credential)
        self.credential_status = QLabel('尚未保存登录信息')
        self.credential_status.setStyleSheet('color:#8a8073;')
        access.addWidget(self.credential_status)
        row = QHBoxLayout()
        self.delete_credential = button('删除登录信息', lambda: self._credential(delete=True))
        self.delete_credential.setAutoDefault(False)
        row.addWidget(self.delete_credential)
        row.addStretch()
        close_access = button('关闭', self.credentials.reject)
        close_access.setAutoDefault(False)
        row.addWidget(close_access)
        self.save_credential = button('保存', self._credential, primary=True)
        self.save_credential.setDefault(True)
        row.addWidget(self.save_credential)
        access.addLayout(row)
        self.credentials.finished.connect(lambda _: self.credential.clear())
        self.message = QLabel()
        self.message.setStyleSheet('color:#8a8073;')
        self.message.setWordWrap(True)
        self.message.setMaximumHeight(44)
        layout.addWidget(self.message)
        self.message.hide()
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.refresh)
        for edit in (self.host_search, self.record_search):
            edit.setMaxLength(300)
            edit.textChanged.connect(self._filter_changed)
        for select in (self.host_filter, self.record_filter):
            select.changed.connect(self._filter_changed)
        self.jump.textEdited.connect(self._edit_page)
        self.jump.returnPressed.connect(self._jump)
        controller.snapshot.connect(self.render)
        controller.error.connect(self._error)
        self.export_timer = QTimer(self)
        self.export_timer.setInterval(1000)
        self.export_timer.timeout.connect(self._poll_exports)
        self.export_timer.start()

    def _filter_changed(self, *_):
        self._reset_page_input()
        self.pages[self.tab] = 1
        self.search_timer.start()

    def _message(self, text):
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def _error(self, text):
        if self.credentials.isVisible():
            self.credential_status.setText(text)
        if self.detail_dialog and self.detail_dialog.isVisible():
            self.detail_dialog.hint.setText(text)
            self.detail_dialog.set_busy(False)
        self._message(text)

    def _settings(self):
        self._credential_state(bool(self.data.get('credential')), force=True)
        self.credentials.show()
        self.credentials.raise_()
        self.credentials.activateWindow()
        self.credential.setFocus()

    def _credential_state(self, configured, *, force=False):
        if configured == self._configured and not force:
            return
        self._configured = configured
        self.delete_credential.setEnabled(configured)
        self.credential_status.setText('已保存在本机，可更换或删除' if configured else '尚未保存登录信息')

    def eventFilter(self, watched, event):
        if watched is self.table and event.type() == QEvent.Type.Resize:
            self.empty.move(max(0,(self.table.width()-self.empty.width())//2),max(54,(self.table.height()-80)//2))
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._resize_columns()
        return super().eventFilter(watched,event)

    def _resize_columns(self):
        if not self.table.columnCount():
            return
        if self.tab == 'hosts':
            weights, minimums = (0, 30, 12, 19, 17, 10, 12), (44, 170, 96, 155, 142, 92, 112)
        else:
            weights, minimums = (23, 19, 12, 14, 10, 7, 15), (126, 148, 104, 118, 100, 74, 172)
        if self.table.columnCount() != len(weights):
            return
        available = max(0, self.table.viewport().width() - sum(minimums))
        widths = [minimum + available * weight // 100 for minimum, weight in zip(minimums, weights)]
        widths[-1] += max(0, self.table.viewport().width() - sum(widths))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for column, width in enumerate(widths):
            self.table.setColumnWidth(column, width)

    def _tab(self, tab):
        self._reset_page_input()
        self.tab = tab
        self.host_tab.setChecked(tab == 'hosts')
        self.record_tab.setChecked(tab == 'recordings')
        self.toolbars.setCurrentIndex(0 if tab == 'hosts' else 1)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.controller.params = None

    def refresh(self):
        self.controller.view(tab=self.tab, query=self.host_search.text() if self.tab == 'hosts' else self.record_search.text(),
            filter=self.host_filter.value if self.tab == 'hosts' else self.record_filter.value,
            page=self.pages[self.tab])

    def _page(self, direction):
        self._reset_page_input()
        self.pages[self.tab] = min(self.data.get('pages', 1), max(1, self.pages[self.tab] + direction))
        self.refresh()

    def _reset_page_input(self):
        self._page_draft = False
        if self._page_error and self.message.text() == self._page_error:
            self._message('')
        self._page_error = ''

    def _edit_page(self, _):
        self._reset_page_input()
        self._page_draft = True

    def _jump(self):
        value = int(self.jump.text()) if self.jump.text() else 0
        pages = self.data.get('pages', 1)
        if not 1 <= value <= pages:
            self._page_error = f'请输入 1 至 {pages} 之间的页码'
            self._message(self._page_error)
            self.jump.setFocus()
            self.jump.selectAll()
            return
        self._reset_page_input()
        self.pages[self.tab] = value
        self.jump.setText(str(value))
        self.refresh()

    def _add(self):
        source = self.source.text().strip()
        if source:
            self.controller.call('add', callback=lambda _: self._added(source), source=source)

    def _added(self, source):
        self._reset_page_input()
        if self.source.text().strip() == source:
            self.source.clear()
        self.host_search.clear()
        self.host_filter.select('all', '全部状态')
        self.pages['hosts'] = 1
        self.refresh()

    def _directory(self):
        chosen = QFileDialog.getExistingDirectory(self, '选择录像保存位置', self.data.get('directory', str(Path.home() / 'Movies')))
        if chosen:
            self.controller.call('directory', path=chosen, callback=lambda _: self.refresh())

    def _open_folder(self):
        path = self.data.get('directory')
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _credential(self, delete=False):
        value = '' if delete else self.credential.text().strip()
        if not delete and not value:
            self.credential_status.setText('请先输入自己的抖音 Cookie')
            return
        def saved(reply):
            self.credential.clear()
            configured = bool(reply['configured'])
            self.data['credential'] = configured
            self._credential_state(configured, force=True)
            self._message('登录信息已保存' if configured else '登录信息已删除')
        self.controller.call('credential', value=value,
            callback=saved)

    def render(self, data):
        self.data = data
        self.clock_cells = []
        self.clock_anchor = time.monotonic()
        self.pages[self.tab] = data['page']
        self.host_tab.setText(f"主播监控  {data['hosts']}")
        self.record_tab.setText(f"录像库  {data['recordings']}")
        free = f"{data['free_bytes'] / 1024**3:.1f} GB" if data['free_bytes'] is not None else '未读取磁盘'
        for label, text in zip(self.stats, (f"{data['monitoring']} 位", f"{data['recording']} 位",
                free)):
            label.setText(text)
        self.pause_all.setEnabled(data['monitoring'] > 0)
        self._credential_state(bool(data['credential']))
        if data['disk_warning']:
            self._message('可用空间低于 5 GB；低于 2 GB 会暂停监控，已存录像保留。')
        self.storage.setText('录像保存位置\n' + data['directory'])
        self.count.setText(f"共 {data['total']} {'位主播' if self.tab == 'hosts' else '场录像'}" +
            (f"    ·    {data['page']} / {data['pages']} 页" if data['pages'] > 1 else ''))
        self.previous.setEnabled(data['page'] > 1)
        self.next.setEnabled(data['page'] < data['pages'])
        self.pagination.setVisible(data['pages'] > 1)
        if not self._page_draft:
            self.jump.setText(str(data['page']))
        scrollbar = self.table.verticalScrollBar()
        scroll = scrollbar.value()
        self.table.clearContents()
        headers = ['', '主播', '直播状态', '最近检查', '录制进度', '直播间', '操作'] if self.tab == 'hosts' else ['主播', '录制时间', '已录时长', '文件', '状态', '直播数据', '操作']
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(data['rows']))
        self.empty.setVisible(not data['rows'])
        self.empty.setText(('还没有主播\n粘贴上方的抖音链接，添加后开始监控。' if self.tab == 'hosts' and not data['hosts'] else
                            '还没有录像\n开始监控后，开播时自动保存到本机。' if self.tab == 'recordings' and not data['recordings'] else
                            '没有符合当前条件的内容\n试试其他名称、日期或状态。'))
        self._resize_columns()
        for row, value in enumerate(data['rows']):
            self.table.setRowHeight(row, 74)
            if self.tab == 'hosts':
                hid = value['id']
                remove = button('×', lambda checked=False, id=hid: self._remove_host(id))
                remove.setProperty('liveRemove', True)
                remove.setFixedWidth(30)
                remove.setAccessibleName('移除主播，保留录像')
                remove.setToolTip('移除主播并停止监控，已存录像保留')
                self.table.setCellWidget(row, 0, row_controls(remove))
                source_label = value['source'].removeprefix('https://')
                live_state = value.get('live_state', 'unknown')
                progress = value.get('progress_message', '')
                text = [(value['name'] or '待识别主播') + '\n' + source_label,
                        LIVE_MESSAGES.get(live_state, LIVE_MESSAGES['unknown']), stamp(value['checked']),
                        (duration(value['duration']) if value['duration'] else '—') + ('\n' + progress if progress else '')]
                for column, item in enumerate(text, 1):
                    cell = QTableWidgetItem(item)
                    cell.setToolTip(value['source'] if column == 1 else item)
                    self.table.setItem(row, column, cell)
                self._style_status(self.table.item(row, 2), live_state)
                self.table.item(row, 2).setToolTip('最近一次检查的直播结果；暂停监控后不再更新')
                self.table.item(row, 4).setData(Qt.ItemDataRole.UserRole, progress)
                self.table.item(row, 4).setToolTip(progress + '\n本场已写入 ' + file_size(value.get('bytes', 0)) +
                    '\n录制中每秒计时，保存后以文件实际时长为准' +
                    ('\n\n' + value['error_message'] if value.get('error_message') else ''))
                if value.get('clock_running'):
                    self.clock_cells.append((self.table.item(row, 4), value['duration']))
                link = value['live_url']
                if link:
                    self.table.setCellWidget(row, 5, row_controls(button('打开 ↗', lambda checked=False, url=link: QDesktopServices.openUrl(QUrl(url)))))
                else:
                    self.table.setItem(row, 5, QTableWidgetItem('待识别'))
                enabled, hid = bool(value['enabled']), value['id']
                toggle = button('暂停监控' if enabled else '开始监控', lambda checked=False, id=hid, active=enabled:
                        self.controller.call('pause' if active else 'start', host_id=id))
                toggle.setMinimumWidth(96)
                self.table.setCellWidget(row, 6, row_controls(toggle))
            else:
                status = recording_message(value)
                text = [value['name'] or '待识别主播', stamp(value['started']), duration(value['duration']),
                        f"{value['n']} 段 · {value['bytes'] / 1024**3:.2f} GB", status]
                for column, item in enumerate(text):
                    cell = QTableWidgetItem(item)
                    cell.setToolTip(item)
                    self.table.setItem(row, column, cell)
                self._style_status(self.table.item(row, 4), 'live' if value.get('clock_running') else 'warning' if status != '已保存' else 'saved')
                if value.get('clock_running'):
                    self.clock_cells.append((self.table.item(row, 2), value['duration']))
                view_data = button('查看', lambda checked=False, id=value['id']:
                    self.controller.call('data', recording_id=id, callback=self._data_detail))
                view_data.setAccessibleName('查看本场直播数据')
                view_data.setToolTip('查看本场直播数据')
                self.table.setCellWidget(row, 5, row_controls(view_data))
                view = button('查看分段', lambda checked=False, id=value['id']:
                    self.controller.call('detail', recording_id=id, callback=self._detail))
                remove = button('删除', lambda checked=False, id=value['id']: self._delete_recording(id))
                remove.setProperty('deleteRecordingId', value['id'])
                blocked = value.get('delete_blocked', '') or ('本场录像正在录制，结束后才能删除' if value['ended'] is None else '')
                remove.setEnabled(not blocked and value['id'] not in self.deleting)
                remove.setAccessibleName('将本场录像移入废纸篓')
                remove.setToolTip(blocked or '将本场录像文件夹和直播数据移入系统废纸篓')
                self.table.setCellWidget(row, 6, row_controls(view, remove))
            for column in range(self.table.columnCount()):
                cell = self.table.item(row, column)
                if cell is not None:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        scrollbar.setValue(scroll)
        if self.clock_cells:
            if not self.clock_timer.isActive():
                self.clock_timer.start()
        else:
            self.clock_timer.stop()

    def _tick_durations(self):
        if self.controller.closing:
            self.clock_timer.stop()
            return
        # Freeze a stale snapshot rather than imply recording continues after a lost Core connection.
        elapsed = min(5, max(0, time.monotonic() - self.clock_anchor))
        for cell, base in self.clock_cells:
            progress = cell.data(Qt.ItemDataRole.UserRole)
            cell.setText(duration(base + elapsed) + ('\n' + progress if progress else ''))

    @staticmethod
    def _style_status(item, state):
        color = '#59816b' if state in {'live', 'saved'} else '#437f96' if state in {'offline', 'checking', 'resolving'} else '#996946' if state not in {'paused', 'stopping'} else '#8a8073'
        item.setForeground(QColor(color))
        font = item.font()
        font.setBold(state == 'live')
        item.setFont(font)

    def _remove_host(self, hid):
        self.controller.call('remove', host_id=hid, callback=lambda _: self.refresh())

    def _delete_recording(self, rid):
        if rid in self.deleting:
            return
        self.deleting.add(rid)
        for control in self.table.findChildren(QPushButton):
            if control.property('deleteRecordingId') == rid:
                control.setEnabled(False)
        def failed():
            self.deleting.discard(rid)
            if not self.controller.closing:
                self.refresh()
        def removed(reply):
            self.deleting.discard(rid)
            self.deleted_recordings.add(rid)
            for dialog in (self.detail_dialog, self.data_dialog):
                if dialog and dialog.recording_id == rid:
                    dialog.close()
            self._message('本场录像和直播数据已移入系统废纸篓' if reply['trashed'] else
                          '已移除失效录像记录，未改动文件')
            self.refresh()
        self.controller.call('delete_recording', recording_id=rid, callback=removed, failure=failed)

    def _detail(self, data):
        if self.controller.closing or data['id'] in self.deleting | self.deleted_recordings:
            return
        if self.detail_dialog:
            self.detail_dialog.close()
            self.detail_dialog.deleteLater()
        dialog = LiveSegmentsDialog(data, self)
        self.detail_dialog = dialog
        dialog.export_requested.connect(lambda segment: self._export_segment(dialog, segment, data['name']))
        dialog.transcribe_requested.connect(lambda segment: self._transcribe_segment(dialog, segment))
        dialog.finished.connect(lambda _: self.detail_timer.stop() if self.detail_dialog is dialog else None)
        dialog.show()
        self.detail_timer.start()

    def _data_detail(self, data):
        if self.controller.closing or data['id'] in self.deleting | self.deleted_recordings:
            return
        if self.data_dialog:
            self.data_dialog.close()
            self.data_dialog.deleteLater()
        dialog = LiveDataDialog(data, self)
        self.data_dialog = dialog
        dialog.finished.connect(lambda _: self.data_timer.stop() if self.data_dialog is dialog else None)
        dialog.show()
        self.data_timer.start()

    def _refresh_data(self):
        dialog = self.data_dialog
        if not dialog or not dialog.isVisible() or self.controller.closing:
            self.data_timer.stop()
            return
        if not self.controller.connected or len(self.controller.queue) >= 10:
            return
        if ((self.controller.worker and self.controller.worker.operation == 'data') or
                any(operation == 'data' for operation, _, _ in self.controller.queue)):
            return
        def updated(reply):
            if self.data_dialog is dialog and dialog.isVisible():
                dialog.update_data(reply)
        def failed():
            if self.data_dialog is dialog and dialog.isVisible():
                dialog.mark_stale()
        self.controller.call('data', recording_id=dialog.recording_id, callback=updated, failure=failed)

    def _refresh_detail(self):
        dialog = self.detail_dialog
        if not dialog or not dialog.isVisible() or self.controller.closing:
            self.detail_timer.stop()
            return
        if dialog.busy or not self.controller.connected or len(self.controller.queue) >= 10:
            return
        # Keep at most one read outstanding; late replies must not reopen or replace a closed window.
        if ((self.controller.worker and self.controller.worker.operation == 'detail') or
                any(operation == 'detail' for operation, _, _ in self.controller.queue)):
            return
        def updated(reply):
            if self.detail_dialog is dialog and dialog.isVisible() and not dialog.busy:
                dialog.update_data(reply)
        self.controller.call('detail', recording_id=dialog.recording_id, callback=updated)

    def _transcribe_segment(self, dialog, segment):
        dialog.set_busy(True)
        def ready(reply):
            if (self.detail_dialog is not dialog or not dialog.isVisible() or
                    dialog.recording_id in self.deleting | self.deleted_recordings):
                return
            if self.transcribe(reply['path']):
                dialog.accept()
            else:
                dialog.set_busy(False)
                dialog.hint.setText('文案任务未能创建，请检查任务提示后重试。')
        self.controller.call('transcribe', segment_id=segment['id'], callback=ready)

    def _export_segment(self, dialog, segment, name):
        suggested = (name or '直播录像').replace('/', '_').replace(':', '_')[:80] + '-' + Path(segment['path']).stem + '.mp4'
        dialog.set_busy(True)
        path, selected = QFileDialog.getSaveFileName(dialog, '导出录像', str(Path(segment['path']).parent / suggested),
            'MP4 视频 (*.mp4);;MP3 音频 (*.mp3)')
        if not path:
            dialog.set_busy(False)
            return
        format = 'mp3' if '*.mp3' in selected else 'mp4'
        target = Path(path).with_suffix('.' + format)
        self.controller.call('export', segment_id=segment['id'], format=format, path=str(target),
            callback=lambda reply: self._export_started(reply, format, dialog))

    def _export_started(self, reply, format, dialog):
        self.exporting[reply['export_id']] = format
        self._message('正在导出 ' + format.upper() + '，原录像保留…')
        dialog.accept()

    def _poll_exports(self):
        if self.controller.closing:
            return
        for eid in list(self.exporting):
            self.controller.call('export_status', export_id=eid, callback=lambda reply, id=eid: self._export_done(id, reply))

    def _export_done(self, eid, reply):
        if reply['state'] == 'running' or eid not in self.exporting:
            return
        format = self.exporting.pop(eid)
        if reply['state'] == 'succeeded':
            self._message(format.upper() + ' 已导出，原录像保留')
        else:
            self._message(reply['message'])
