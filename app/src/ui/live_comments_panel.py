"""Native local comment viewer; viewing and export never contact Douyin."""
from datetime import datetime
from html import escape
import math
from pathlib import Path
from shiboken6 import isValid
from PySide6.QtCore import QPointF, QSize, QTimer, Qt, QItemSelectionModel
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QIcon, QKeySequence,
                          QShortcut, QTextLayout, QTextOption)
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLayout, QMenu, QSizePolicy, QStackedWidget, QStyle,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import icons, theme as T
from .control_foundation import LuluButton, LuluTextField, adopt_menu
from .live_comments_timeline import CommentTimeline, elapsed_clock
from ..core.live_recording.contracts import recording_message


class _CommentDelegate(QStyledItemDelegate):
    """Three aligned columns, with plain-text wrapping only in the message body."""
    def __init__(self, parent):
        super().__init__(parent)
        self.layouts = {}
        self.body_font = QFont(T.SANS)
        self.body_font.setPixelSize(14)

    def text_layout(self, message, width):
        width = max(32, width - 24)
        key = message['message_id'], message['text'], width
        if key not in self.layouts:
            text = message['text'].replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\u2028')
            layout = QTextLayout(text, self.body_font)
            option = QTextOption()
            option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            option.setAlignment(Qt.AlignmentFlag.AlignLeft)
            layout.setTextOption(option)
            layout.beginLayout()
            height = 0
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                line.setPosition(QPointF(0, height))
                height += line.height() + 3
            layout.endLayout()
            self.layouts[key] = layout, height
        return self.layouts[key]

    def sizeHint(self, option, index):
        message = index.data(Qt.ItemDataRole.UserRole)
        if not message or index.column() != 2:
            return QSize(80, 44)
        _, height = self.text_layout(message, self.parent().columnWidth(2))
        return QSize(100, max(44, math.ceil(height) + 24))

    def paint(self, painter, option, index):
        message = index.data(Qt.ItemDataRole.UserRole)
        if not message:
            return
        c, rect = T.consumer_theme(), option.rect
        painter.save()
        painter.setClipRect(rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(T.consumer_qcolor(c.accent_wash if selected else c.secondary_hover))
            painter.drawRect(rect)
        if index.column() == 2:
            painter.setPen(QColor(c.ink))
            layout, _ = self.text_layout(message, rect.width())
            text_width = max((layout.lineAt(i).naturalTextWidth() for i in range(layout.lineCount())), default=0)
            left = rect.left() + max(12, (rect.width() - text_width) / 2)
            layout.draw(painter, QPointF(left, rect.top() + 12))
        else:
            font = QFont(T.SANS)
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor(c.ink_muted if index.column() == 0 else c.ink_soft))
            value = (elapsed_clock(message['elapsed_seconds']) if index.column() == 0 else
                     message['nickname'] or '未命名观众')
            text = painter.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, max(24, rect.width() - 24))
            painter.drawText(rect.adjusted(12, 13, -12, 0),
                             Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, text)
        divider = T.consumer_qcolor(c.line)
        divider.setAlphaF(.55)
        painter.setPen(divider)
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QColor(c.ink_muted))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(3, 2, -3, -2), 8, 8)
        painter.restore()


class _CommentList(QTableWidget):
    def __init__(self):
        super().__init__(0, 3)
        self.setItemDelegate(_CommentDelegate(self))
        self.height_timer = QTimer(self)
        self.height_timer.setSingleShot(True)
        self.height_timer.timeout.connect(self.resizeRowsToContents)
        self.horizontalHeader().sectionResized.connect(lambda *_: self.height_timer.start(0))
        self.setHorizontalHeaderLabels(['时间', '用户昵称', '弹幕内容'])
        self.horizontalHeaderItem(2).setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def size_columns(self):
        width = self.viewport().width()
        self.setColumnWidth(0, 90)
        self.setColumnWidth(1, max(108, round(width * .3)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.size_columns()
        self.itemDelegate().layouts.clear()
        self.height_timer.start(0)

class LiveCommentsPanel(QWidget):
    def __init__(self, recording, controller, parent=None):
        super().__init__(parent)
        self.recording_id, self.name = recording['id'], recording.get('name') or '主播'
        self.recording, self.time_range = recording, None
        self.controller = controller
        self.page, self.epoch = 1, 0
        self.reading, self.exporting = False, False
        self.result, self.rows = {}, []
        self.setObjectName('liveCommentsPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(12)
        save_status = QHBoxLayout()
        save_status.setSpacing(16)
        self.video_state_label, self.state_label = QLabel(), QLabel('弹幕 · 正在读取')
        self.video_state_label.setObjectName('commentVideoState')
        self.state_label.setObjectName('commentState')
        save_status.addWidget(self.video_state_label)
        save_status.addWidget(self.state_label)
        save_status.addStretch()
        layout.addLayout(save_status)
        overview = QFrame()
        overview.setObjectName('commentOverview')
        overview_layout = QVBoxLayout(overview)
        overview_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        overview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        overview_layout.setContentsMargins(14, 9, 14, 5)
        overview_layout.setSpacing(0)
        overview_heading = QHBoxLayout()
        overview_heading.setSpacing(10)
        title = QLabel('时间总览')
        title.setObjectName('commentOverviewTitle')
        self.timeline_note = QLabel('整场接收记录')
        self.timeline_note.setObjectName('commentTimelineNote')
        self.timeline_note.setMinimumWidth(0)
        self.timeline_note.setWordWrap(True)
        self.all_button = LuluButton('查看全部', role='tertiary', size='sm')
        self.all_button.setAutoDefault(False)
        self.all_button.clicked.connect(lambda: self.select_range(None))
        self.all_button.hide()
        overview_heading.addWidget(title)
        overview_heading.addWidget(self.timeline_note, 1)
        overview_heading.addWidget(self.all_button)
        overview_layout.addLayout(overview_heading)
        self.timeline = CommentTimeline()
        self.timeline.selectionChanged.connect(self.select_range)
        overview_layout.addWidget(self.timeline)
        layout.addWidget(overview)
        bar = QHBoxLayout()
        bar.setSpacing(12)
        self.search = LuluTextField(accessible_name='搜索本场弹幕', placeholder='搜索昵称或弹幕内容')
        self.search.setMaxLength(300)
        self.search.setClearButtonEnabled(True)
        self.search.addAction(QIcon(icons.pixmap('search', T.consumer_theme().ink_muted, 16)),
                              self.search.ActionPosition.LeadingPosition)
        bar.addWidget(self.search, 1)
        self.export_button = LuluButton('导出弹幕', role='primary', icon='download')
        self.export_menu = adopt_menu(QMenu(self), accessible_name='选择弹幕导出格式')
        c = T.consumer_theme()
        self.export_menu.setStyleSheet(f'''
            QMenu {{background:{c.surface};color:{c.ink};border:1px solid {c.line};border-radius:8px;padding:5px;}}
            QMenu::item {{padding:9px 16px;border-radius:5px;font-family:"{c.font_body}";font-size:13px;}}
            QMenu::item:selected {{background:{c.accent_wash};color:{c.accent};}}
            QMenu::item:disabled {{color:{c.ink_muted};}}
        ''')
        self.txt_action = self.export_menu.addAction('导出为文字文件  TXT')
        self.csv_action = self.export_menu.addAction('导出为表格文件  CSV')
        self.txt_action.triggered.connect(lambda: self.export('txt'))
        self.csv_action.triggered.connect(lambda: self.export('csv'))
        self.export_button.setMenu(self.export_menu)
        bar.addWidget(self.export_button)
        self.copy_button = LuluButton('复制选中', role='tertiary', size='sm', icon='copy')
        for button in (self.copy_button, self.export_button):
            button.setAutoDefault(False)
        self.copy_button.clicked.connect(self.copy_selected)
        layout.addLayout(bar)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.summary = QLabel('正在读取本场弹幕…')
        self.summary.setObjectName('commentSummary')
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setMinimumWidth(0)
        self.summary.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        status_row.addWidget(self.summary)
        status_row.addStretch()
        status_row.addWidget(self.copy_button)
        layout.addLayout(status_row)
        self.table = _CommentList()
        self.table.setObjectName('commentReadingList')
        self.table.setAccessibleName('本场已保存弹幕')
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setMouseTracking(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.horizontalHeader().setFixedHeight(34)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionsClickable(False)
        self.table.itemSelectionChanged.connect(self._buttons)
        self.copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self.table)
        self.copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.copy_shortcut.activated.connect(self.copy_selected)
        card = QFrame()
        card.setObjectName('commentReadingCard')
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(6, 6, 6, 6)
        self.content = QStackedWidget()
        self.content.addWidget(self.table)
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(24, 16, 24, 16)
        empty_layout.setSpacing(10)
        empty_layout.addStretch()
        glyph = QLabel()
        glyph.setObjectName('commentEmptyGlyph')
        glyph.setPixmap(icons.pixmap('summary', T.consumer_theme().accent, 26))
        glyph.setFixedSize(52, 52)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty_title, self.empty_detail = QLabel(), QLabel()
        self.empty_title.setObjectName('commentEmptyTitle')
        self.empty_detail.setObjectName('commentEmptyDetail')
        for label in (self.empty_title, self.empty_detail):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            empty_layout.addWidget(label)
        empty_layout.addStretch()
        self.content.addWidget(empty)
        card_layout.addWidget(self.content)
        layout.addWidget(card, 1)
        self.events = QLabel()
        self.events.setObjectName('commentConnectionNote')
        self.events.setTextFormat(Qt.TextFormat.PlainText)
        self.events.setWordWrap(True)
        self.events.hide()
        self.events.setParent(self)  # Full connection timestamps remain available as a tooltip.
        foot = QHBoxLayout()
        self.older = LuluButton('较早记录', role='tertiary', size='sm')
        self.newer = LuluButton('较新记录', role='tertiary', size='sm')
        self.page_label, self.notice = QLabel(), QLabel()
        self.page_label.setObjectName('commentPageLabel')
        self.notice.setObjectName('commentNotice')
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        for button in (self.older, self.newer):
            button.setAutoDefault(False)
        self.older.clicked.connect(lambda: self.turn_page(1))
        self.newer.clicked.connect(lambda: self.turn_page(-1))
        note = QLabel('时间从本场开始累计 · 按本机接收记录')
        note.setObjectName('commentTimeNote')
        note.setToolTip('接收时间不代表录像播放位置。')
        foot.addWidget(note)
        foot.addStretch()
        self.pagination = QWidget()
        pages = QHBoxLayout(self.pagination)
        pages.setContentsMargins(0, 0, 0, 0)
        pages.setSpacing(4)
        pages.addWidget(self.older)
        pages.addWidget(self.page_label)
        pages.addWidget(self.newer)
        foot.addWidget(self.pagination)
        layout.addLayout(foot)
        layout.addWidget(self.notice)
        self.timer, self.debounce = QTimer(self), QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(300)
        self.debounce.timeout.connect(self.refresh)
        self.search.textChanged.connect(self.query_changed)
        self.pagination.hide()
        self.notice.hide()
        self._buttons()
        self.update_recording(recording)

    def update_recording(self, recording):
        self.recording = recording
        self.video_state_label.setText('录像 · ' + recording_message(recording))
        self.video_state_label.setProperty('hasGap', bool(recording.get('gaps') and recording['gaps'] != '[]'))
        self.video_state_label.style().unpolish(self.video_state_label)
        self.video_state_label.style().polish(self.video_state_label)

    def select_range(self, selected):
        self.time_range = selected
        self.timeline.selected_range = selected
        self.timeline.update()
        self.all_button.setVisible(selected is not None)
        self.page = 1
        self.epoch += 1
        self.table.clearSelection()
        self.table.verticalScrollBar().setValue(0)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.search.setFocus(Qt.FocusReason.OtherFocusReason)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event):
        self.timer.stop()
        self.debounce.stop()
        self.epoch += 1
        super().hideEvent(event)

    def query_changed(self):
        self.page = 1
        self.time_range = None
        self.timeline.selected_range = None
        self.timeline.update()
        self.all_button.hide()
        self.epoch += 1
        self.debounce.start()

    def turn_page(self, step):
        self.page = max(1, min(self.result.get('pages', 1), self.page + step))
        self.epoch += 1
        self.refresh()

    def refresh(self):
        if (not self.isVisible() or self.exporting or not self.controller
                or self.controller.closing or not self.controller.connected or self.controller.comments_worker):
            return
        self.reading = True
        expected = self.epoch, self.search.text(), self.page, self.controller.session, self.time_range
        def current():
            return isValid(self) and self.isVisible() and not self.controller.closing and expected == (
                self.epoch, self.search.text(), self.page, self.controller.session, self.time_range)
        def updated(result):
            self.reading = False
            if current():
                self.show_result(result)
        def failed():
            self.reading = False
            if current():
                self.notice.setText('弹幕记录暂未更新，已有文件保留；连接恢复后会重试。')
                self.notice.show()
        ranges = dict(zip(('range_start', 'range_end'), self.time_range)) if self.time_range else {}
        self.controller.call('comments', recording_id=self.recording_id, query=self.search.text(),
                             page=self.page, callback=updated, failure=failed, **ranges)

    def show_result(self, result):
        self.result, self.page = result, result['page']
        total, matching = result['total'], result['matching']
        prefix = (f'{elapsed_clock(self.time_range[0])} 至 {elapsed_clock(self.time_range[1])} · '
                  if self.time_range else '')
        self.summary.setText('弹幕记录暂不可读' if result['read_error'] else
            prefix + (f'匹配 {matching:,} 条 · 全场 {total:,} 条' if self.search.text() else
                      f'{matching:,} 条 · 全场 {total:,} 条' if self.time_range else f'已保存 {total:,} 条弹幕'))
        overview = result.get('timeline', {})
        self.timeline.set_data(overview, searching=bool(self.search.text()), selected_range=self.time_range)
        has_gap = any(b['interrupted'] for b in overview.get('bins', []))
        selected_gap = self.time_range is not None and any(
            b['interrupted'] and b['start'] < self.time_range[1] and b['end'] > self.time_range[0]
            for b in overview.get('bins', []))
        self.timeline_note.setText((f"每 {overview['step']} 秒统计" if overview else '整场接收记录') +
            (' · 斜纹时段连接或保存不完整' if has_gap else '') +
            (' · 深色为搜索匹配' if self.search.text() else ''))
        self.timeline_note.setToolTip('末格可能不足一个统计周期；柱高表示收到条数。斜纹标记整个时间格，不代表格内每一秒都中断。')
        state = result.get('comments_state', 'legacy')
        self.state_label.setText('弹幕 · ' + ('读取失败' if result['read_error'] else {
            'connecting': '连接中', 'waiting': '等待弹幕', 'recording': '保存中',
            'reconnecting': '正在重连', 'retrying': '正在重试', 'unavailable': '暂不可用',
            'write_error': '保存失败', 'limit': '已达上限', 'ended': '已结束',
            'interrupted': '已中断', 'disabled': '未开启', 'legacy': '无记录',
        }.get(state, '已中断')))
        kind = ('error' if result['read_error'] or state == 'write_error' else
                'warning' if state in {'reconnecting', 'retrying', 'interrupted', 'limit', 'unavailable'}
                else 'active' if state in {'connecting', 'waiting', 'recording'} else 'neutral')
        if self.state_label.property('kind') != kind:
            self.state_label.setProperty('kind', kind)
            self.state_label.style().unpolish(self.state_label)
            self.state_label.style().polish(self.state_label)
        self.state_label.setToolTip(escape(result.get('comments_message', '')))
        message = result.get('error', '') or (
            ('这场录像没有弹幕记录；旧录像不会补录历史弹幕。' if result.get('comments_state') == 'legacy'
             else '本场未开启弹幕记录。' if result.get('comments_state') == 'disabled'
             else '还没有保存的弹幕。') if not result['has_file'] else
            '暂无匹配的弹幕。' if matching == 0 and self.search.text() else '')
        self.notice.setText(message)
        self.notice.hide()  # Empty/error explanations belong inside the reading surface.
        self.content.setCurrentIndex(0 if result['messages'] else 1)
        self.empty_title.setText('暂时无法读取弹幕' if result['read_error'] else
            '没有找到相关弹幕' if self.search.text() else
            '本场未开启弹幕记录' if state == 'disabled' else
            '这场没有弹幕记录' if state == 'legacy' else
            '这个时段没有保存的弹幕' if self.time_range else '还没有收到弹幕')
        self.empty_detail.setText('原文件已保留，请检查保存位置后重试。' if result['read_error'] else
            '旧录像不会补录历史弹幕。' if state == 'legacy' else
            '开启“同时保存弹幕”后，从下一场录制开始保存。' if state == 'disabled' else
            '试试其他昵称或关键词。' if self.search.text() else
            '斜纹时段可能缺少弹幕，未收到的数量无法确定。' if selected_gap else
            '可选择其他时段，或点击“查看全部”。' if self.time_range else
            '收到的新弹幕会自动出现在这里。')
        if result['messages'] != self.rows:
            selected = {self.rows[i.row()]['message_id'] for i in self.table.selectionModel().selectedRows()
                        if i.row() < len(self.rows) and self.rows[i.row()]['message_id']}
            scroll = self.table.verticalScrollBar().value()
            self.rows = result['messages']
            self.table.itemDelegate().layouts.clear()
            self.table.setUpdatesEnabled(False)
            self.table.clearSelection()
            self.table.setRowCount(len(self.rows))
            for row, message in enumerate(self.rows):
                tooltip = '\n'.join((message['received_at'], message['nickname'], message['text']))
                for column, text in enumerate((elapsed_clock(message['elapsed_seconds']),
                                               message['nickname'] or '未命名观众', message['text'])):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, message)
                    item.setToolTip('<qt>' + escape(tooltip).replace('\n', '<br>') + '</qt>')
                    self.table.setItem(row, column, item)
            self.table.resizeRowsToContents()
            for row, message in enumerate(self.rows):
                if message['message_id'] in selected:
                    self.table.selectionModel().select(self.table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            self.table.verticalScrollBar().setValue(scroll)
            self.table.setUpdatesEnabled(True)
        gaps, interrupted = [], False
        for event in result['events']:
            when = datetime.fromisoformat(event['received_at']).astimezone().strftime('%m-%d %H:%M:%S')
            if event['state'] == 'reconnecting':
                interrupted = True
                gaps.append(when + '  弹幕连接中断')
            elif interrupted and event['state'] in {'waiting', 'recording'}:
                gaps.append(when + '  弹幕连接恢复')
                interrupted = False
            elif event['state'] == 'interrupted':
                gaps.append(when + '  弹幕记录中断')
        self.events.setText('    ·    '.join(gaps[-2:]))
        self.events.setToolTip('\n'.join(gaps))
        self.timeline.setToolTip('\n'.join(gaps))
        self.page_label.setText(f"{self.page} / {result['pages']} 页")
        self.pagination.setVisible(result['pages'] > 1)
        self.older.setEnabled(self.page < result['pages'])
        self.newer.setEnabled(self.page > 1)
        self._buttons()

    def _buttons(self):
        selected = len(self.table.selectionModel().selectedRows())
        self.copy_button.setVisible(bool(selected))
        self.copy_button.setEnabled(bool(selected))
        self.copy_button.setText(f'复制选中 {selected} 条' if selected else '复制选中')
        for control in (self.export_button, self.txt_action, self.csv_action):
            control.setEnabled(bool(self.result.get('has_file')) and not self.exporting)

    def copy_selected(self):
        indices = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        lines = [f"[{self.rows[i]['received_at']}] {self.rows[i]['nickname']}：{self.rows[i]['text']}"
                 for i in indices if i < len(self.rows)]
        if lines:
            QGuiApplication.clipboard().setText('\n'.join(lines))
            self.notice.setText(f'已复制 {len(lines)} 条弹幕')
            self.notice.show()

    def export(self, format):
        if self.exporting or not self.controller or self.controller.comments_worker:
            self.notice.setText('弹幕正在读取，请稍后导出。')
            self.notice.show()
            return
        self.exporting = True
        self._buttons()
        name = self.name.replace('/', '_').replace(':', '_')[:50]
        suggestion = str(Path(self.result.get('folder') or '') / f'{name}-弹幕.{format}')
        target, _ = QFileDialog.getSaveFileName(self, '导出本场弹幕', suggestion,
                                                f"{format.upper()} 文件 (*.{format})")
        if not target:
            self.exporting = False
            self._buttons()
            return
        if not target.lower().endswith('.' + format):
            target += '.' + format
        self.export_button.set_loading(True, '正在导出…')
        epoch = self.epoch
        connection = self.controller._connection_epoch, self.controller.session
        delivered = False
        def finished(result=None):
            nonlocal delivered
            delivered = True
            self.exporting = False
            if not isValid(self):
                return
            self.export_button.set_loading(False)
            self._buttons()
            if self.isVisible() and epoch == self.epoch:
                if connection != (self.controller._connection_epoch, self.controller.session):
                    self.notice.setText('连接已变化，请检查导出位置；未自动重复导出。')
                else:
                    self.notice.setText('已导出：' + result['path'] if result else '导出未完成，原弹幕记录保留。')
                self.notice.show()
        self.controller.call('comments_export', recording_id=self.recording_id, format=format,
                             path=target, callback=finished, failure=lambda: finished())
        worker = self.controller.comments_worker
        if worker:
            # A reply from the previous connection is deliberately discarded by
            # the controller. Its actual completion still releases this panel.
            worker.finished.connect(lambda: finished() if not delivered else None)
