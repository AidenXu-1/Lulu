"""Native Lulu live workspace: bounded tables, explicit media actions."""
from datetime import datetime
import json
from pathlib import Path
import time

from PySide6.QtCore import QEvent, QRegularExpression, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QRegularExpressionValidator
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog, QFileDialog, QFrame, QHeaderView,
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import icons
from .control_foundation import PaperTextureFrame
from .live_data_dialog import LiveDataDialog
from .live_workspace_controls import CommentsSwitch, WorkspaceDialog, WorkspaceTab, note
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
            QDialog#liveSegmentsDialog QPushButton:enabled:hover{background:#f5f1e7;border-color:#8aafba;}
            QDialog#liveSegmentsDialog QPushButton:enabled:pressed{background:#e3eceb;}
            QDialog#liveSegmentsDialog QPushButton:focus{border-color:#437f96;}
            QDialog#liveSegmentsDialog QPushButton[primary="true"]{background:#437f96;border-color:#437f96;color:#fffdf7;}
            QDialog#liveSegmentsDialog QPushButton[primary="true"]:enabled:hover{background:#376d82;border-color:#376d82;}
            QDialog#liveSegmentsDialog QPushButton[primary="true"]:enabled:pressed{background:#2e5e71;border-color:#2e5e71;}
            QDialog#liveSegmentsDialog QPushButton:disabled{background:#f3efe6;border-color:#e6ddcf;color:#aaa196;}
            QDialog#liveSegmentsDialog QPushButton#closeSegments{background:transparent;border-color:transparent;
                padding:0;font-size:22px;color:#877d70;}
            QDialog#liveSegmentsDialog QPushButton#closeSegments:enabled:hover{background:#eee8dd;}
            QDialog#liveSegmentsDialog QPushButton#closeSegments:enabled:pressed{background:#e3eceb;}
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
    login_state_changed = Signal()
    def __init__(self, controller, *, transcribe, parent=None, collection_busy=None):
        super().__init__(parent)
        self.controller, self.transcribe = controller, transcribe
        self.collection_busy = collection_busy or (lambda: False)
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
        self.pending_hosts = {}
        self._pausing_all = False
        self.pending_comments = None
        self._row_keys = None
        self._deferred_data = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_deferred)
        self.setStyleSheet('''
            QWidget#liveRecordingPage{background:transparent;color:#36332e;font-size:14px;}
            QWidget#liveRecordingPage QLabel{background:transparent;border:0;}
            QWidget#liveRecordingPage QLineEdit{background:#f8f5ed;border:1px solid #e7dfd1;border-radius:7px;padding:8px 11px;color:#36332e;font-size:13px;}
            QWidget#liveRecordingPage QLineEdit:focus{border-color:#77aab7;}
            QWidget#liveRecordingPage QPushButton{background:#fffdf8;border:1px solid #e3d9c9;border-radius:7px;padding:7px 12px;color:#36332e;font-size:13px;}
            QWidget#liveRecordingPage QPushButton:enabled:hover{border-color:#b6c5bd;background:#f5f0e6;}
            QWidget#liveRecordingPage QPushButton:focus{border-color:#437f96;outline:none;}
            QWidget#liveRecordingPage QPushButton:enabled:pressed{background:#e3eceb;}
            QWidget#liveRecordingPage QPushButton[primary="true"]{background:#437f96;color:#fffdf7;border-color:#437f96;}
            QWidget#liveRecordingPage QPushButton[primary="true"]:enabled:hover{background:#376d82;border-color:#376d82;}
            QWidget#liveRecordingPage QPushButton[primary="true"]:enabled:pressed{background:#2e5e71;border-color:#2e5e71;}
            QWidget#liveRecordingPage QPushButton:disabled{color:#aaa090;}
            QWidget#liveRecordingPage QPushButton[quiet="true"]{background:transparent;border:1px solid transparent;padding:4px 8px;color:#8a7f6e;}
            QWidget#liveRecordingPage QPushButton[quiet="true"]:enabled:hover{background:#f5f0e6;border-color:#b6c5bd;color:#36332e;}
            QWidget#liveRecordingPage QPushButton[quiet="true"]:enabled:pressed{background:#e3eceb;}
            QWidget#liveRecordingPage QPushButton[quiet="true"]:focus{border-color:#77aab7;}
            QWidget#liveRecordingPage QPushButton[pending="true"]{background:#f0ece2;color:#908371;}
            QWidget#liveRecordingPage QPushButton[monitoring="true"]{background:#edf4f3;color:#427c8a;border-color:#c7d9da;}
            QWidget#liveRecordingPage QPushButton[monitoring="true"]:enabled:hover{background:#deebea;border-color:#8aafba;}
            QWidget#liveRecordingPage QPushButton[monitoring="true"]:enabled:pressed{background:#cbdedd;}
            QFrame#liveTabs,QFrame#liveMetrics{background:transparent;border:0;}
            QWidget#liveRecordingPage QLabel[statLabel="true"]{color:#8a8073;font-size:11px;}
            QWidget#liveRecordingPage QLabel[statValue="true"]{color:#36332e;font-size:21px;font-weight:500;}
            QWidget#liveRecordingPage QTableWidget{background:transparent;alternate-background-color:#f8f3e9;border:0;gridline-color:#eee6d9;outline:none;}
            QWidget#liveRecordingPage QTableWidget::item{padding:0;}
            QWidget#liveRecordingPage QPushButton[liveRemove="true"]{background:transparent;border:1px solid transparent;padding:0;font-size:19px;color:#a69a88;}
            QWidget#liveRecordingPage QPushButton[liveRemove="true"]:enabled:hover{background:#f5e9df;color:#a65d4e;}
            QWidget#liveRecordingPage QPushButton[liveRemove="true"]:enabled:pressed{background:#ead6c9;}
            QWidget#liveRecordingPage QHeaderView::section{background:#f6f1e7;border:0;border-top:1px solid #e8e0d3;border-bottom:1px solid #e8e0d3;padding:8px;color:#928570;font-size:12px;}
            QWidget#liveRecordingPage QHeaderView{background:transparent;}
            QWidget#liveRecordingPage QPushButton[livePager="true"]{border-radius:5px;padding:0 5px;background:transparent;border-color:transparent;}
            QWidget#liveRecordingPage QPushButton[livePager="true"]:enabled:hover{background:#f5f0e6;border-color:#b6c5bd;}
            QWidget#liveRecordingPage QPushButton[livePager="true"]:enabled:pressed{background:#e3eceb;}
            QWidget#liveRecordingPage QPushButton[livePager="true"]:focus{border-color:#77aab7;}
            QWidget#liveRecordingPage QLineEdit#livePageNumber{background:transparent;border:1px solid transparent;border-radius:4px;padding:0 2px;font-size:11px;}
            QWidget#liveRecordingPage QLineEdit#livePageNumber:focus{background:#fffdf8;border-color:#77aab7;}
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 24)
        layout.setSpacing(20)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(6)
        title = QLabel('直播录制')
        title.setStyleSheet('font-family:"Songti SC","STSong";font-size:28px;font-weight:600;')
        heading.addWidget(title)
        subtitle = note('开启监控，开播后自动保存到本机。')
        subtitle.setWordWrap(False)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.metrics = QFrame()
        self.metrics.setObjectName('liveMetrics')
        self.metrics.setFixedWidth(340)
        stats = QHBoxLayout(self.metrics)
        stats.setContentsMargins(0, 0, 0, 0)
        stats.setSpacing(18)
        self.stats = []
        for index, caption in enumerate(('监控中', '录制中', '可用空间')):
            if index:
                divider = QFrame()
                divider.setFixedSize(1, 32)
                divider.setStyleSheet('background:#e1d8c8;border:0;')
                stats.addWidget(divider)
            group = QVBoxLayout()
            group.setSpacing(5)
            label = QLabel(caption)
            label.setProperty('statLabel', True)
            value = QLabel('—')
            value.setProperty('statValue', True)
            group.addWidget(label)
            group.addWidget(value)
            stats.addLayout(group, 1)
            self.stats.append(value)
        header.addWidget(self.metrics)
        layout.addLayout(header)
        self.panel = PaperTextureFrame(strength=.025, corner_radius=13, base_color='#fffdf8')
        workspace = QVBoxLayout(self.panel)
        workspace.setContentsMargins(0, 0, 0, 0)
        workspace.setSpacing(0)
        nav = QWidget()
        nav.setFixedHeight(58)
        tabs = QHBoxLayout(nav)
        tabs.setContentsMargins(20, 0, 16, 0)
        tabs.setSpacing(8)
        self.tab_bar = QFrame()
        self.tab_bar.setObjectName('liveTabs')
        segment = QHBoxLayout(self.tab_bar)
        segment.setContentsMargins(0, 0, 0, 0)
        segment.setSpacing(16)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.host_tab = WorkspaceTab('主播监控  0')
        self.record_tab = WorkspaceTab('录像库  0')
        self.host_tab.clicked.connect(lambda: self._tab('hosts'))
        self.record_tab.clicked.connect(lambda: self._tab('recordings'))
        for tab_button in (self.host_tab, self.record_tab):
            self.tab_group.addButton(tab_button)
            segment.addWidget(tab_button)
        self.host_tab.setChecked(True)
        tabs.addWidget(self.tab_bar)
        tabs.addStretch()
        self.comments_toggle = CommentsSwitch()
        self.comments_toggle.setToolTip('记住选择，从下一场录制起生效；已开始的场次保持当前选择。')
        self.comments_toggle.setEnabled(False)
        self.comments_toggle.clicked.connect(self._comments_changed)
        tabs.addWidget(self.comments_toggle)
        divider = QFrame()
        divider.setFixedSize(1, 15)
        divider.setStyleSheet('background:#e8e0d3;border:0;')
        tabs.addWidget(divider)
        self.storage_button = button('保存位置 ⌄', self._storage_menu)
        self.storage_button.setProperty('quiet', True)
        self.storage_button.setIcon(QIcon(icons.pixmap('folder', '#8a7f6e', 15)))
        tabs.addWidget(self.storage_button)
        self.settings_button = button('', self._settings)
        self.settings_button.setProperty('quiet', True)
        self.settings_button.setIcon(QIcon(icons.pixmap('gear', '#8a7f6e', 17)))
        self.settings_button.setFixedSize(32, 34)
        self.settings_button.setAccessibleName('抖音连接')
        self.settings_button.setToolTip('抖音连接 · 访问受限时设置')
        tabs.addWidget(self.settings_button)
        workspace.addWidget(nav)
        self.toolbars = QStackedWidget()
        self.toolbars.setFixedHeight(62)
        host_toolbar = QWidget()
        row = QHBoxLayout(host_toolbar)
        row.setContentsMargins(20, 12, 20, 12)
        row.setSpacing(10)
        self.host_search = QLineEdit()
        self.host_search.setPlaceholderText('搜索主播名称或链接')
        self.host_search.setAccessibleName('搜索主播名称或链接')
        self.host_search.setMaximumWidth(320)
        self.host_filter = FilterButton([('全部状态', 'all'), ('直播中', 'live'), ('未直播', 'offline'), ('无法确认', 'unknown')])
        self.host_filter.setFixedWidth(108)
        row.addWidget(self.host_search, 1)
        row.addWidget(self.host_filter)
        row.addStretch()
        self.pause_all = button('暂停全部', self._pause_all)
        self.pause_all.setAccessibleName('暂停全部监控')
        row.addWidget(self.pause_all)
        self.add_button = button('＋ 添加主播', self._show_add, primary=True)
        row.addWidget(self.add_button)
        self.toolbars.addWidget(host_toolbar)
        records_toolbar = QWidget()
        row = QHBoxLayout(records_toolbar)
        row.setContentsMargins(20, 12, 20, 12)
        row.setSpacing(10)
        self.record_search = QLineEdit()
        self.record_search.setPlaceholderText('搜索录像的主播名称')
        self.record_search.setMaximumWidth(320)
        self.record_filter = FilterButton([('全部日期', 'all'), ('今天', 'today'), ('最近 7 天', 'week'), ('最近 30 天', 'month')])
        self.record_filter.setFixedWidth(108)
        row.addWidget(self.record_search, 1)
        row.addWidget(self.record_filter)
        row.addStretch()
        self.toolbars.addWidget(records_toolbar)
        workspace.addWidget(self.toolbars)
        self.table = QTableWidget()
        self.table.setItemDelegate(LiveRowDelegate(self.table))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.horizontalHeader().setFixedHeight(39)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(100)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        workspace.addWidget(self.table, 1)
        self.empty = QLabel('还没有主播\n添加抖音链接，开启监控后自动录制。', self.table)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet('color:#9a8e7b;font-size:13px;background:transparent;')
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty.resize(400, 80)
        self.table.installEventFilter(self)
        self.table.viewport().installEventFilter(self)
        foot = QWidget()
        foot.setFixedHeight(40)
        footer = QHBoxLayout(foot)
        footer.setContentsMargins(20, 0, 20, 0)
        footer.setSpacing(8)
        self.count = QLabel('共 0 位主播')
        self.count.setStyleSheet('font-size:11px;color:#978a75;')
        footer.addWidget(self.count)
        footer.addStretch()
        self.footer_hint = note('●  录像保存在本机')
        self.footer_hint.setStyleSheet('font-size:10px;color:#a09480;')
        footer.addWidget(self.footer_hint)
        self.pagination = QWidget()
        pager = QHBoxLayout(self.pagination)
        pager.setContentsMargins(0, 0, 0, 0)
        pager.setSpacing(3)
        self.previous = button('‹', lambda: self._page(-1))
        self.next = button('›', lambda: self._page(1))
        self.previous.setAccessibleName('上一页')
        self.next.setAccessibleName('下一页')
        self.jump = QLineEdit('1')
        self.jump.setObjectName('livePageNumber')
        self.jump.setFixedSize(28, 26)
        self.jump.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.jump.setMaxLength(6)
        self.jump.setValidator(QRegularExpressionValidator(QRegularExpression('[0-9]{0,6}'), self.jump))
        self.jump.setAccessibleName('跳转页码')
        self.jump.setToolTip('输入页码后按回车')
        self.jump_button = button('跳转', self._jump)
        self.jump_button.hide()
        self.page_total = note('/ 1')
        self.page_total.setStyleSheet('font-size:11px;color:#a09480;')
        for control in (self.previous, self.next):
            control.setProperty('livePager', True)
            control.setFixedSize(26, 26)
        for control in (self.previous, self.jump, self.page_total, self.next):
            pager.addWidget(control)
        footer.addWidget(self.pagination)
        workspace.addWidget(foot)
        layout.addWidget(self.panel, 1)
        self.storage = QLabel('录像保存位置\n影片 / Lulu直播录制')
        self.storage.hide()
        self._build_add_dialog()
        self._build_access_dialog()
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
            edit.setFixedHeight(35)
            edit.textChanged.connect(self._filter_changed)
        for select in (self.host_filter, self.record_filter):
            select.setFixedHeight(35)
            select.changed.connect(self._filter_changed)
        self.jump.textEdited.connect(self._edit_page)
        self.jump.returnPressed.connect(self._jump)
        controller.snapshot.connect(self.render)
        controller.error.connect(self._error)
        self.export_timer = QTimer(self)
        self.export_timer.setInterval(1000)
        self.export_timer.timeout.connect(self._poll_exports)
        self.export_timer.start()

    def _build_add_dialog(self):
        self.add_dialog = WorkspaceDialog('添加主播', '开播后自动录制', self)
        content = self.add_dialog.content
        content.addWidget(note('粘贴抖音个人主页或直播间链接。添加后，可以随时开启或暂停监控。'))
        content.addWidget(QLabel('抖音链接'))
        self.source = QLineEdit()
        self.source.setMaxLength(4096)
        self.source.setPlaceholderText('https://v.douyin.com/…')
        self.source.setAccessibleName('添加主播链接')
        self.source.returnPressed.connect(self._add)
        content.addWidget(self.source)
        self.add_status = note('')
        content.addWidget(self.add_status)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button('取消', self.add_dialog.reject))
        self.confirm_add = button('添加主播', self._add, primary=True)
        row.addWidget(self.confirm_add)
        content.addLayout(row)
        self._adding = False

    def _show_add(self):
        self.add_status.clear()
        self.add_dialog.show()
        self.source.setFocus()

    def _build_access_dialog(self):
        self.credentials = WorkspaceDialog('抖音登录', '', self)
        self.credentials.title.setStyleSheet('font-size:21px;font-weight:600;')
        self._configured = None
        self._credential_pending = False
        self._login_generation = 0
        self._login_cancelled = False
        content = self.credentials.content
        self.access_description = note('直播提示需要登录或访问受限时，再登录账号。')
        content.addWidget(self.access_description)
        self.access_main = QWidget()
        main = QVBoxLayout(self.access_main)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(13)
        row = QHBoxLayout()
        row.addWidget(note('登录状态'))
        row.addStretch()
        self.connection_title = QLabel('未登录')
        self.connection_title.setStyleSheet('font-size:12px;color:#746855;')
        row.addWidget(self.connection_title)
        main.addLayout(row)
        row = QHBoxLayout()
        self.browser_login = button('登录账号', self._browser_login, primary=True)
        self.delete_credential = button('清除登录', lambda: self._credential(delete=True))
        row.addWidget(self.browser_login)
        row.addWidget(self.delete_credential)
        row.addStretch()
        main.addLayout(row)
        self.login_note = note('在打开的浏览器中扫码即可，无需填写 Cookie。')
        main.addWidget(self.login_note)
        self.advanced_toggle = button('手动填写 Cookie', self._toggle_advanced)
        self.advanced_toggle.setProperty('quiet', True)
        main.addSpacing(8)
        main.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        content.addWidget(self.access_main)
        self.advanced = QWidget()
        manual = QVBoxLayout(self.advanced)
        manual.setContentsMargins(0, 0, 0, 0)
        manual.setSpacing(12)
        manual.addWidget(QLabel('登录 Cookie'))
        row = QHBoxLayout()
        self.credential = QLineEdit()
        self.credential.setEchoMode(QLineEdit.EchoMode.Password)
        self.credential.setMaxLength(16384)
        self.credential.setPlaceholderText('粘贴 Cookie 内容，不含 Cookie:')
        self.credential.setAccessibleName('抖音登录 Cookie')
        self.credential.returnPressed.connect(self._credential)
        row.addWidget(self.credential, 1)
        self.cookie_eye = button('显示', self._toggle_cookie_visibility)
        row.addWidget(self.cookie_eye)
        manual.addLayout(row)
        help_button = button('如何获取 Cookie？', self._toggle_cookie_help)
        help_button.setProperty('quiet', True)
        manual.addWidget(help_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.cookie_help = note('1. 用 Chrome 打开抖音官网，登录自己的账号。\n2. 按 ⌥⌘I，打开 Network（网络），刷新网页。\n3. 选择 douyin.com 请求，在 Headers（标头）→ Request Headers 中找到 Cookie。\n4. 复制完整内容，不含 Cookie:，粘贴到这里。请勿分享给他人。')
        self.cookie_help.setStyleSheet('background:#f5f0e7;border-radius:7px;padding:12px;font-size:11px;color:#94816a;')
        manual.addWidget(self.cookie_help)
        row = QHBoxLayout()
        row.addWidget(button('返回', lambda: self._set_access_view('main')))
        row.addStretch()
        self.save_credential = button('保存 Cookie', self._credential, primary=True)
        row.addWidget(self.save_credential)
        manual.addLayout(row)
        content.addWidget(self.advanced)
        self.clear_page = QWidget()
        clear = QVBoxLayout(self.clear_page)
        clear.setContentsMargins(0, 0, 0, 0)
        clear.addWidget(note('下次需要登录时，重新登录即可。\n已采集资料、录像和日常浏览器的登录保留。'))
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button('取消', lambda: self._set_access_view('main')))
        self.confirm_clear = button('清除登录', lambda: self._save_credential(''), primary=True)
        self.confirm_clear.setStyleSheet('''
            QPushButton{background:#a25e4e;color:#fffdf7;border-color:#a25e4e;}
            QPushButton:enabled:hover{background:#8f5143;border-color:#8f5143;}
            QPushButton:enabled:pressed{background:#7a4438;border-color:#7a4438;}
            QPushButton:disabled{background:#f3efe6;color:#aaa090;border-color:#e8e0d4;}
        ''')
        row.addWidget(self.confirm_clear)
        clear.addLayout(row)
        content.addWidget(self.clear_page)
        self.credential_status = note('')
        content.addWidget(self.credential_status)
        self.credentials.finished.connect(self._access_closed)
        self._set_access_view('main')

    def _set_access_view(self, view):
        self._access_view = view
        self.access_main.setVisible(view == 'main')
        self.advanced.setVisible(view == 'manual')
        self.clear_page.setVisible(view == 'clear')
        if view != 'manual':
            self.credential.clear()
            self.credential.setEchoMode(QLineEdit.EchoMode.Password)
            self.cookie_eye.setText('显示')
            self.cookie_help.hide()
        self.credentials.title.setText({'main': '抖音登录', 'manual': '填写 Cookie', 'clear': '清除抖音登录？'}[view])
        self.access_description.setText({'main': '直播提示需要登录或访问受限时，再登录账号。',
            'manual': '粘贴你自己抖音账号的 Cookie，用于直播访问。',
            'clear': '清除 Lulu 为主页采集和直播录制记住的抖音登录。'}[view])
        self.credential_status.clear()
        self.credentials._fit()

    def _toggle_advanced(self):
        if not self._access_busy():
            self._set_access_view('manual')

    def _toggle_cookie_visibility(self):
        masked = self.credential.echoMode() == QLineEdit.EchoMode.Password
        self.credential.setEchoMode(QLineEdit.EchoMode.Normal if masked else QLineEdit.EchoMode.Password)
        self.cookie_eye.setText('隐藏' if masked else '显示')

    def _toggle_cookie_help(self):
        self.cookie_help.setVisible(not self.cookie_help.isVisible())
        self.credentials._fit()

    def _access_closed(self, *_):
        self._login_generation += 1
        self.credential.clear()
        worker = self.controller.login_worker
        if worker:
            self._login_cancelled = True
            worker.requestInterruption()

    def _access_busy(self):
        return bool(self.collection_busy() or self.data.get('active') or self.pending_hosts
            or self._credential_pending or not self.controller.connected or self.controller.closing)

    def login_state(self):
        data = self.data.get('login', {})
        if data.get('partial') or data.get('error'):
            label = '未完全清除' if data.get('partial') else '登录状态待检查'
        elif data.get('source') == 'browser' and data.get('browser') and data.get('cookie'):
            label = '已登录'
        elif data.get('cookie') or self.data.get('credential'):
            label = '已保存 Cookie'
        elif data.get('browser'):
            label = '已保存浏览器登录'
        else:
            label = '未登录'
        return label, bool(data.get('browser') or data.get('cookie') or data.get('partial') or data.get('error') or data.get('profile') or self.data.get('credential'))

    def _access_controls(self):
        busy, worker = self._access_busy(), self.controller.login_worker
        label, saved = self.login_state()
        self.browser_login.setEnabled(not busy and not self._login_cancelled)
        self.browser_login.setText('正在取消…' if worker and self._login_cancelled else '取消登录' if worker else '重新登录' if label != '未登录' else '登录账号')
        self.connection_title.setText('等待扫码登录' if worker else label)
        self.save_credential.setEnabled(not busy and not worker)
        self.delete_credential.setEnabled(saved and not busy and not worker)
        self.confirm_clear.setEnabled(not busy and not worker)
        self.advanced_toggle.setEnabled(not busy and not worker)
        self.credential.setEnabled(not busy and not worker)
        self.login_note.setText('请在打开的浏览器中扫码，完成后会自动更新。' if worker else
            '请先停止采集和直播监控，再更改登录。' if busy else '在打开的浏览器中扫码即可，无需填写 Cookie。')

    def _browser_login(self):
        if self.controller.login_worker:
            self._login_cancelled = True
            self._login_generation += 1
            self.controller.login_worker.requestInterruption()
            self._access_controls()
            return
        if not self.browser_login.isEnabled() or self._access_busy():
            return
        from .live_login import LiveLoginRequest
        self._login_cancelled = False
        self.credential_status.clear()
        self._login_generation += 1
        generation = self._login_generation
        worker = LiveLoginRequest(self.controller)
        self.controller.login_worker = worker
        self.controller.activity_changed.emit()
        self._access_controls()
        def current():
            return (generation == self._login_generation and self.credentials.isVisible()
                    and not worker.isInterruptionRequested() and not self.controller.closing)
        def received(value):
            if current():
                self._save_credential(value, source='browser')
        def notice(text):
            if current():
                self.credential_status.setText(text)
        def finished():
            self.controller.login_worker = None
            self._login_cancelled = False
            self.controller.activity_changed.emit()
            worker.deleteLater()
            self._access_controls()
            self.controller._finish_stop()
        worker.received.connect(received)
        worker.notice.connect(notice)
        worker.finished.connect(finished)
        worker.start()

    def _storage_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet('QMenu{background:#fffdf7;color:#716552;border:1px solid #e4dbca;border-radius:9px;padding:6px;}QMenu::item{padding:9px 18px;}QMenu::item:selected{background:#e8f1f1;}')
        title = menu.addAction('录像保存位置')
        title.setEnabled(False)
        path = menu.addAction(self.data.get('directory', '影片 / Lulu直播录制'))
        path.setEnabled(False)
        menu.addSeparator()
        menu.addAction('在访达中打开', self._open_folder)
        menu.addAction('更改保存位置', self._directory)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.storage_button.mapToGlobal(self.storage_button.rect().bottomLeft()))
        self._storage_popup = menu

    @staticmethod
    def _control_property(control, name, value):
        if control.property(name) != value:
            control.setProperty(name, value)
            control.style().unpolish(control)
            control.style().polish(control)
            control.update()

    def _render_host_actions(self):
        if not self._row_keys or self._row_keys[0] != 'hosts':
            return
        for row, value in enumerate(self.data.get('rows', [])):
            if row >= self.table.rowCount():
                continue
            cell = self.table.cellWidget(row, 6)
            if not cell:
                continue
            control = cell.findChild(QPushButton)
            hid = value['id']
            pending = hid in self.pending_hosts
            enabled = bool(value['enabled'])
            control.setText(('启动中…' if self.pending_hosts[hid] else '暂停中…') if pending
                            else '暂停监控' if enabled else '开始监控')
            control.setEnabled(not pending and not self._pausing_all)
            self._control_property(control, 'pending', pending)
            self._control_property(control, 'monitoring', enabled and not pending)
            self.table.cellWidget(row, 0).findChild(QPushButton).setEnabled(not pending)
        self.pause_all.setEnabled((bool(self.data.get('monitoring')) or any(self.pending_hosts.values()))
                                  and not self._pausing_all)
        self.pause_all.setVisible(bool(self.data.get('monitoring')) or any(self.pending_hosts.values()) or self._pausing_all)
        self.pause_all.setText('暂停中…' if self._pausing_all else '暂停全部')

    def _toggle_host(self, hid):
        self._discard_deferred()
        if hid in self.pending_hosts or self._pausing_all:
            return
        value = next((row for row in self.data.get('rows', []) if row['id'] == hid), None)
        if not value:
            return
        target = not bool(value['enabled'])
        if target and (self.controller.login_worker or self._credential_pending):
            self._message('请先完成或取消抖音登录，再开始监控。')
            return
        self.pending_hosts[hid] = target
        self._render_host_actions()
        def failed():
            if self.pending_hosts.get(hid) is not target:
                return
            self.pending_hosts.pop(hid, None)
            self._render_host_actions()
        def changed(_):
            if self.pending_hosts.get(hid) is not target:
                return
            self._confirmed_intents({hid: target})
            failed()
            self.refresh()
        self.controller.call('start' if target else 'pause', host_id=hid, callback=changed, failure=failed)

    def _pause_all(self):
        self._discard_deferred()
        if self._pausing_all:
            return
        self._pausing_all = True
        self.pending_hosts = {hid: False for hid in self.pending_hosts}
        self.pending_hosts.update({row['id']: False for row in self.data.get('rows', []) if row.get('enabled')})
        affected = set(self.pending_hosts)
        self._render_host_actions()
        def finished(success=False):
            self._pausing_all = False
            if success:
                self._confirmed_intents({hid: False for hid in affected}, pause_all=True)
            for hid in affected:
                self.pending_hosts.pop(hid, None)
            self._render_host_actions()
            if success:
                self.refresh()
        self.controller.call('pause_all', callback=lambda _: finished(True), failure=finished)

    def _confirmed_intents(self, changes, *, pause_all=False):
        # Service replies confirm monitor intent. Refresh its visible text and
        # totals together; recording completion still comes from Core snapshots.
        if self.tab != 'hosts' or not self._row_keys or self._row_keys[0] != 'hosts':
            return
        delta = stopped_clocks = 0
        for index, row in enumerate(self.data.get('rows', [])):
            if row['id'] not in changes:
                continue
            target = changes[row['id']]
            delta += int(target) - int(bool(row['enabled']))
            row['enabled'] = int(target)
            running = bool(row.get('clock_running'))
            if not target:
                row['clock_running'] = False
                stopped_clocks += int(running)
            if target and running:
                continue  # Existing recording facts still belong to the latest snapshot.
            progress = '监控中' if target else '正在保存最后一段' if running else '已暂停'
            row['progress_message'] = progress
            cell = self.table.item(index, 4)
            self.clock_cells = [(item, seconds) for item, seconds in self.clock_cells if item is not cell]
            cell.setData(Qt.ItemDataRole.UserRole, progress)
            cell.setText((duration(row['duration']) if row['duration'] else '—') + '\n' + progress)
            cell.setToolTip(progress)
        self.data['monitoring'] = 0 if pause_all else max(0, self.data['monitoring'] + delta)
        self.data['recording'] = 0 if pause_all else max(0, self.data['recording'] - stopped_clocks)
        self.stats[0].setText(f"{self.data['monitoring']} 位")
        self.stats[1].setText(f"{self.data['recording']} 位")

    def _sync_comments(self):
        self.comments_toggle.blockSignals(True)
        self.comments_toggle.setChecked(self.pending_comments if self.pending_comments is not None
                                       else self.data.get('comments_enabled', True))
        self.comments_toggle.setEnabled(self.pending_comments is None and self.controller.connected and not self.controller.closing)
        self.comments_toggle.blockSignals(False)

    def _comments_changed(self, enabled):
        self._discard_deferred()
        if self.pending_comments is not None:
            return
        self.pending_comments = bool(enabled)
        self._sync_comments()
        def failed():
            self.pending_comments = None
            self._sync_comments()
        def changed(reply):
            self.data['comments_enabled'] = reply['enabled']
            failed()
            self._message('已'+('开启' if reply['enabled'] else '关闭')+'弹幕保存，从下一场录制生效')
            self.refresh()
        self.controller.call('comments_setting', enabled=bool(enabled), callback=changed, failure=failed)

    def _open_room(self, hid):
        value = next((row for row in self.data.get('rows', []) if row['id'] == hid), None)
        if value and value.get('live_url'):
            QDesktopServices.openUrl(QUrl(value['live_url']))

    def _render_deferred(self):
        pending, self._deferred_data = self._deferred_data, None
        if pending is not None and pending[0] == self.tab:
            self.render(pending[1])

    def _discard_deferred(self):
        self._deferred_data = None
        self._render_timer.stop()

    def _filter_changed(self, *_):
        self._reset_page_input()
        self.pages[self.tab] = 1
        self.search_timer.start()

    def _message(self, text):
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def _error(self, text):
        if self.add_dialog.isVisible():
            self.add_status.setText(text)
        if self.credentials.isVisible():
            self.credential_status.setText(text)
        if self.detail_dialog and self.detail_dialog.isVisible():
            self.detail_dialog.hint.setText(text)
            self.detail_dialog.set_busy(False)
        self._message(text)

    def _settings(self, action=None):
        self._set_access_view('main')
        self._credential_state(bool(self.data.get('credential')), force=True)
        self.credentials.show()
        self.credentials.raise_()
        self.credentials.activateWindow()
        self.credentials.close_button.setFocus()
        if action == 'clear':
            self._credential(delete=True)
        elif action == 'login':
            self._browser_login()

    def _credential_state(self, configured, *, force=False):
        changed = configured != self._configured
        self._configured = configured
        self._access_controls()
        if changed or force:
            self.login_state_changed.emit()

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
        self._discard_deferred()
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
        if self._adding:
            return
        source = self.source.text().strip()
        if source:
            self._adding = True
            self.confirm_add.setText('添加中…')
            self.confirm_add.setEnabled(False)
            def failed():
                self._adding = False
                self.confirm_add.setText('添加主播')
                self.confirm_add.setEnabled(True)
            def added(_):
                failed()
                self._added(source)
            self.controller.call('add', callback=added, failure=failed, source=source)
        else:
            self.add_status.setText('请先粘贴抖音主页或直播间链接')

    def _added(self, source):
        self._reset_page_input()
        if self.source.text().strip() == source:
            self.source.clear()
        self.host_search.clear()
        self.host_filter.select('all', '全部状态')
        self.pages['hosts'] = 1
        self.add_dialog.accept()
        self._tab('hosts')

    def _directory(self):
        chosen = QFileDialog.getExistingDirectory(self, '选择录像保存位置', self.data.get('directory', str(Path.home() / 'Movies')))
        if chosen:
            self.controller.call('directory', path=chosen, callback=lambda _: self.refresh())

    def _open_folder(self):
        path = self.data.get('directory')
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _credential(self, delete=False):
        if self._access_busy() or self.controller.login_worker:
            self.credential_status.setText('请先停止采集和直播监控，再更改登录。')
            return
        if delete:
            self._set_access_view('clear')
            return
        value = self.credential.text().strip()
        if not value or value.lower().startswith('cookie:') or '\n' in value or '\r' in value:
            self.credential_status.setText('请粘贴 Cookie 内容，不含 Cookie: 和换行。')
            return
        self._save_credential(value)

    def _save_credential(self, value, source='manual'):
        if self._access_busy():
            return
        self._credential_pending = True
        self.credential_status.setText('正在保存登录信息…' if value else '正在清除登录…')
        self._access_controls()
        self.controller.activity_changed.emit()
        def failed():
            self._credential_pending = False
            self._access_controls()
            self.controller.activity_changed.emit()
            self.refresh()
        def saved(reply):
            failed()
            self.credential.clear()
            configured = bool(reply['configured'])
            self.data['credential'] = configured
            self.data['login'] = reply.get('login', {})
            self._set_access_view('main')
            self._credential_state(configured, force=True)
            self._message('登录信息已保存' if configured else '抖音登录已清除')
        self.controller.call('credential', value=value, source=source,
            callback=saved, failure=failed)

    def render(self, data):
        if (QApplication.mouseButtons() != Qt.MouseButton.NoButton
                or any(control.isDown() for control in self.table.findChildren(QPushButton))):
            self._deferred_data = (self.tab, data)
            self._render_timer.start(30)
            return
        self._deferred_data = None
        self._render_timer.stop()
        self.data = data
        self.clock_cells = []
        self.clock_anchor = time.monotonic()
        self.pages[self.tab] = data['page']
        self.host_tab.setText(f"主播监控  {data['hosts']}")
        self.record_tab.setText(f"录像库  {data['recordings']}")
        free = f"{data['free_bytes'] / 1024**3:.1f} GB" if data['free_bytes'] is not None else '—'
        for label, text in zip(self.stats, (f"{data['monitoring']} 位", f"{data['recording']} 位",
                free)):
            label.setText(text)
        self.pause_all.setEnabled(data['monitoring'] > 0)
        self._credential_state(bool(data['credential']))
        self._sync_comments()
        if data['disk_warning']:
            self._message('可用空间低于 5 GB；低于 2 GB 会暂停监控，已存录像保留。')
        self.storage.setText('录像保存位置\n' + data['directory'])
        self.storage_button.setToolTip('录像保存在 ' + data['directory'])
        self.count.setText(f"共 {data['total']} {'位主播' if self.tab == 'hosts' else '场录像'}")
        self.page_total.setText('/ ' + str(data['pages']))
        self.footer_hint.setVisible(data['pages'] == 1)
        self.previous.setEnabled(data['page'] > 1)
        self.next.setEnabled(data['page'] < data['pages'])
        self.pagination.setVisible(data['pages'] > 1)
        if not self._page_draft:
            self.jump.setText(str(data['page']))
        scrollbar = self.table.verticalScrollBar()
        scroll = scrollbar.value()
        keys = (self.tab, tuple(row['id'] for row in data['rows']))
        rebuild = keys != self._row_keys
        headers = ['', '主播', '直播状态', '最近检查', '录制进度', '直播间', '操作'] if self.tab == 'hosts' else ['主播', '录制时间', '已录时长', '文件', '状态', '直播记录', '操作']
        if rebuild:
            self.clock_cells = []
            self.table.clearContents()
            self.table.setColumnCount(len(headers))
            self.table.setHorizontalHeaderLabels(headers)
            self.table.setRowCount(len(data['rows']))
            self._row_keys = keys
        self.empty.setVisible(not data['rows'])
        self.empty.setText(('还没有主播\n添加抖音链接，开启监控后自动录制。' if self.tab == 'hosts' and not data['hosts'] else
                            '还没有录像\n开始监控后，开播时自动保存到本机。' if self.tab == 'recordings' and not data['recordings'] else
                            '没有符合当前条件的内容\n试试其他名称、日期或状态。'))
        self._resize_columns()
        for row, value in enumerate(data['rows']):
            self.table.setRowHeight(row, 74)
            if self.tab == 'hosts':
                hid = value['id']
                if rebuild:
                    remove = button('×', lambda checked=False, id=hid: self._remove_host(id))
                    remove.setProperty('liveRemove', True)
                    remove.setFixedWidth(30)
                    remove.setAccessibleName('移除主播，保留录像')
                    remove.setToolTip('移除主播并停止监控，已存录像保留')
                    self.table.setCellWidget(row, 0, row_controls(remove))
                source_label = value['source'].removeprefix('https://')
                live_state = value.get('live_state', 'unknown')
                progress = value.get('progress_message', '')
                if value.get('comments_message'):
                    progress += '\n' + value['comments_message']
                text = [(value['name'] or '待识别主播') + '\n' + source_label,
                        LIVE_MESSAGES.get(live_state, LIVE_MESSAGES['unknown']), stamp(value['checked']),
                        (duration(value['duration']) if value['duration'] else '—') + ('\n' + progress if progress else '')]
                for column, item in enumerate(text, 1):
                    cell = self._table_cell(row, column, item)
                    cell.setToolTip(value['source'] if column == 1 else item)
                self._style_status(self.table.item(row, 2), live_state)
                self.table.item(row, 2).setToolTip('最近一次检查的直播结果；暂停监控后不再更新')
                self.table.item(row, 4).setData(Qt.ItemDataRole.UserRole, progress)
                self.table.item(row, 4).setToolTip(progress + '\n本场已写入 ' + file_size(value.get('bytes', 0)) +
                    '\n录制中每秒计时，保存后以文件实际时长为准' +
                    ('\n\n' + value['error_message'] if value.get('error_message') else ''))
                if value.get('clock_running'):
                    self.clock_cells.append((self.table.item(row, 4), value['duration']))
                if rebuild:
                    link_button = button('打开 ↗', lambda checked=False, id=hid: self._open_room(id))
                    link_button.setProperty('quiet', True)
                    self.table.setCellWidget(row, 5, row_controls(link_button))
                    toggle = button('开始监控', lambda checked=False, id=hid: self._toggle_host(id))
                    toggle.setMinimumWidth(104)
                    self.table.setCellWidget(row, 6, row_controls(toggle))
                link_button = self.table.cellWidget(row, 5).findChild(QPushButton)
                link_button.setText('打开 ↗' if value['live_url'] else '待识别')
                link_button.setEnabled(bool(value['live_url']))
            else:
                status = recording_message(value)
                text = [value['name'] or '待识别主播', stamp(value['started']), duration(value['duration']),
                        f"{value['n']} 段 · {value['bytes'] / 1024**3:.2f} GB", status]
                for column, item in enumerate(text):
                    cell = self._table_cell(row, column, item)
                    cell.setToolTip(item)
                self._style_status(self.table.item(row, 4), 'live' if value.get('clock_running') else 'warning' if status != '已保存' else 'saved')
                if value.get('clock_running'):
                    self.clock_cells.append((self.table.item(row, 2), value['duration']))
                if rebuild:
                    view_data = button('查看', lambda checked=False, id=value['id']:
                        self.controller.call('data', recording_id=id, callback=self._data_detail))
                    view_data.setAccessibleName('查看本场直播记录')
                    view_data.setToolTip('查看数据走势和弹幕记录')
                    self.table.setCellWidget(row, 5, row_controls(view_data))
                    view = button('查看分段', lambda checked=False, id=value['id']:
                        self.controller.call('detail', recording_id=id, callback=self._detail))
                    remove = button('删除', lambda checked=False, id=value['id']: self._delete_recording(id))
                    remove.setProperty('deleteRecordingId', value['id'])
                    self.table.setCellWidget(row, 6, row_controls(view, remove))
                remove = self.table.cellWidget(row, 6).findChildren(QPushButton)[1]
                blocked = value.get('delete_blocked', '') or ('本场录像正在录制，结束后才能删除' if value['ended'] is None else '')
                remove.setEnabled(not blocked and value['id'] not in self.deleting)
                remove.setAccessibleName('将本场录像移入废纸篓')
                remove.setToolTip(blocked or '将本场录像、直播数据和弹幕记录移入系统废纸篓')
            for column in range(self.table.columnCount()):
                cell = self.table.item(row, column)
                if cell is not None:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        scrollbar.setValue(scroll)
        if self.tab == 'hosts':
            self._render_host_actions()
        if self.clock_cells:
            if not self.clock_timer.isActive():
                self.clock_timer.start()
        else:
            self.clock_timer.stop()

    def _table_cell(self, row, column, text):
        cell = self.table.item(row, column)
        if cell is None:
            cell = QTableWidgetItem()
            self.table.setItem(row, column, cell)
        if cell.text() != text:
            cell.setText(text)
        return cell

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
            self._message('本场录像、直播数据和弹幕记录已移入系统废纸篓' if reply['trashed'] else
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
        dialog = LiveDataDialog(data, self, controller=self.controller)
        self.data_dialog = dialog
        dialog.tabs.currentChanged.connect(lambda _: self._refresh_data() if self.data_dialog is dialog else None)
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
        # Keep scene metadata fresh on either tab, including the video's final
        # save state after pause. The comment panel refreshes its own rows.
        if self.controller.data_worker:
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
