"""Internal Skill settings. All mutations use the same Core API as the CLI."""
from pathlib import Path

from PySide6.QtCore import QSize, QTimer, QObject, QRunnable, QThreadPool, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QScrollArea, QFrame, QStackedWidget, QTextBrowser, QListWidget,
    QFileDialog, QFormLayout, QPlainTextEdit, QCheckBox, QProgressBar,
)

from . import icons, theme as T
from .consumer_switch import ConsumerSwitch
from .control_foundation import adopt_button, adopt_scroll_area, adopt_select_field, adopt_text_field


class _Signals(QObject):
    done = Signal(bool, object)


class _Work(QRunnable):
    def __init__(self, dispatch, operation, arguments):
        super().__init__()
        self.signals = _Signals()
        self.dispatch, self.operation, self.arguments = dispatch, operation, arguments

    def run(self):
        try:
            value = self.dispatch(self.operation, **self.arguments)
        except Exception as exc:
            self.signals.done.emit(False, str(exc))
        else:
            self.signals.done.emit(True, value)


class _Preview(QTextBrowser):
    def loadResource(self, kind, url):  # noqa: N802
        # Skill text must never load remote images or arbitrary local attachments.
        return None


class LocalSkillDropZone(QPushButton):
    """One local source per inspection; URLs and text never become install requests."""
    source_dropped = Signal(str)
    rejected = Signal(str)

    def __init__(self, parent=None):
        super().__init__('拖入本地技能，或点击选择文件\n支持完整技能文件夹或 ZIP；选择 SKILL.md 时一并导入所在文件夹', parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(112)
        self.setMinimumWidth(0)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('安装本地技能，点击选择 ZIP 或 SKILL.md')
        c = T.consumer_theme(T.ACTIVE.name)
        self.setStyleSheet(f'''
            QPushButton {{ background:{c.accent_wash}; color:{c.ink};
                border:1px dashed {c.accent_hairline}; border-radius:12px;
                padding:12px; font-size:13px; }}
            QPushButton:hover, QPushButton:focus, QPushButton[dragActive="true"] {{
                border:2px solid {c.accent}; padding:11px; }}
            QPushButton:disabled {{ color:{c.ink_muted}; }}
        ''')

    @staticmethod
    def local_source(source):
        path = Path(str(source)).expanduser()
        if not path.is_absolute() or not path.exists():
            raise ValueError('请选择本机上的技能文件夹、ZIP 或 SKILL.md。')
        if path.is_file() and path.name == 'SKILL.md':
            if path.is_symlink():
                raise ValueError('请选择原始 SKILL.md 文件，暂不支持替身或符号链接。')
            return str(path.parent)
        if path.is_dir() or (path.is_file() and path.suffix.lower() == '.zip'):
            return str(path)
        raise ValueError('文件格式不支持，请选择 ZIP、SKILL.md 或技能文件夹。')

    def source_from_mime(self, mime):
        urls = mime.urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            raise ValueError('每次请拖入一个本地技能文件或文件夹。')
        return self.local_source(urls[0].toLocalFile())

    def highlight(self, active):
        self.setProperty('dragActive', active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event):
        if not self.isEnabled():
            event.ignore()
            return
        try:
            self.source_from_mime(event.mimeData())
        except ValueError as exc:
            self.rejected.emit(str(exc))
            event.ignore()
            return
        self.highlight(True)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.highlight(False)
        event.accept()

    def dropEvent(self, event):
        self.highlight(False)
        if not self.isEnabled():
            event.ignore()
            return
        try:
            source = self.source_from_mime(event.mimeData())
        except ValueError as exc:
            self.rejected.emit(str(exc))
            event.ignore()
            return
        event.acceptProposedAction()
        self.source_dropped.emit(source)


class SkillsPage(QWidget):
    def __init__(self, parent=None, *, dispatch=None):
        super().__init__(parent)
        if dispatch is None:
            from ..core.agent_workspace.client import dispatch
        self.dispatch = dispatch
        self.entries, self.pending = [], []
        self.busy = False
        self.feedback = {}
        self._activity_key = None
        self._confirming = None
        self._pending_requested = False
        self._callback = None
        self._worker = None
        self.setObjectName('internalSkillsPage')
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 6, 12, 12)
        root.setSpacing(12)
        heading = QLabel('技能')
        heading.setObjectName('pageTitle')
        root.addWidget(heading)
        note = QLabel('系统技能随 Lulu 提供。其他技能请自行下载后拖入安装，在文稿库 AI 对话中使用。关闭开关可卸载。')
        note.setWordWrap(True)
        note.setObjectName('pageMeta')
        root.addWidget(note)
        self.status = QLabel('打开此页后读取技能。')
        self.status.setWordWrap(True)
        self.status.setAccessibleName('技能操作状态')
        root.addWidget(self.status)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.list_page = QWidget()
        layout = QVBoxLayout(self.list_page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.install_button = LocalSkillDropZone(self)
        self.install_button.clicked.connect(self.choose_file)
        self.install_button.source_dropped.connect(self.inspect)
        self.install_button.rejected.connect(self.status.setText)
        layout.addWidget(self.install_button)
        self.toolbar = QBoxLayout(QBoxLayout.LeftToRight)
        self.search = QLineEdit()
        self.search.setPlaceholderText('筛选下方技能')
        adopt_text_field(self.search, accessible_name='按名称或用途筛选下方技能')
        self.search.textChanged.connect(self.render)
        self.toolbar.addWidget(self.search, 1)
        layout.addLayout(self.toolbar)
        filters = QHBoxLayout()
        self.tab = QComboBox()
        self.tab.addItems(['全部技能', '已安装', '未安装'])
        adopt_select_field(self.tab, accessible_name='安装状态筛选')
        self.tab.setMinimumContentsLength(4)
        self.tab.currentIndexChanged.connect(self.render)
        filters.addWidget(self.tab)
        self.origin = QComboBox()
        for title, value in [('全部来源', ''), ('Lulu 系统', 'system'), ('用户安装', 'user')]:
            self.origin.addItem(title, value)
        adopt_select_field(self.origin, accessible_name='技能来源筛选')
        self.origin.setMinimumContentsLength(5)
        self.origin.currentIndexChanged.connect(self.render)
        filters.addWidget(self.origin)
        filters.addStretch()
        layout.addLayout(filters)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        adopt_scroll_area(self.scroll)
        layout.addWidget(self.scroll, 1)
        self.stack.addWidget(self.list_page)
        self.detail_page = QWidget()
        self.stack.addWidget(self.detail_page)
        self._detail_layout = QVBoxLayout(self.detail_page)
        self._detail_layout.setContentsMargins(0, 0, 0, 0)
        self._detail_layout.addWidget(self.button('‹ 返回技能列表', lambda: self.stack.setCurrentIndex(0)), 0, Qt.AlignLeft)
        self.detail_title = QLabel()
        self.detail_title.setWordWrap(True)
        self.detail_title.setObjectName('sectionTitle')
        self._detail_layout.addWidget(self.detail_title)
        self.detail_info = QLabel()
        self.detail_info.setWordWrap(True)
        self.detail_info.setTextFormat(Qt.PlainText)
        self._detail_layout.addWidget(self.detail_info)
        self.file_select = QComboBox()
        adopt_select_field(self.file_select, accessible_name='查看技能文件')
        self.file_select.currentTextChanged.connect(self.read_file)
        self._detail_layout.addWidget(self.file_select)
        self.preview = _Preview()
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.anchorClicked.connect(self.open_link)
        self._detail_layout.addWidget(self.preview, 1)
        self.config_box = QWidget()
        self.config_layout = QFormLayout(self.config_box)
        self._detail_layout.addWidget(self.config_box)
        self.detail_name = None
        self.directory = ''

    def button(self, title, callback):
        button = QPushButton(title)
        button.setAccessibleName(title)
        button.setEnabled(not self.busy)
        adopt_button(button, role='secondary')
        button.clicked.connect(lambda _checked=False: callback())
        return button

    def showEvent(self, event):
        super().showEvent(event)
        if not self.busy:
            self.refresh()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.toolbar.setDirection(QBoxLayout.TopToBottom if self.width() < 520 else QBoxLayout.LeftToRight)

    def run(self, operation, callback=None, **arguments):
        if self.busy:
            return
        self.busy = True
        self._operation = operation
        self.begin_activity(operation, arguments)
        self.status.setText('正在处理…')
        self.set_busy_controls(True)
        QTimer.singleShot(0, self.render)
        self._callback = callback
        work = _Work(self.dispatch, operation, arguments)
        self._worker = work
        work.signals.done.connect(self.finished)
        QThreadPool.globalInstance().start(work)

    def begin_activity(self, operation, arguments):
        self._activity_key = None
        if operation not in {'skill_install', 'skill_uninstall', 'skill_inspect'}:
            return
        if operation == 'skill_inspect':
            source = arguments['source']
            key = 'incoming:' + source
            phase = '正在检查本地技能…'
            row = dict(name=key, title='新技能', description=source)
        else:
            key = arguments.get('name')
            row = next((r for r in self.entries if r['name'] == key), None)
            if key is None:
                row = next((r for r in self.pending if r['token'] == arguments.get('token')), {})
                key = row.get('name', 'pending:' + arguments.get('token', ''))
            row = row or dict(name=key, title=key)
            phase = '正在安装…' if operation == 'skill_install' else '正在卸载…'
        self._activity_key = key
        self.feedback[key] = dict(phase=phase, busy=True, row=row,
                                  operation=operation, arguments=dict(arguments))

    def confirm_installation(self, value):
        key, expected = self._confirming
        row = next((r for r in value['entries'] if r['name'] == key), None)
        valid = row is not None and row['installed'] == expected and not (expected and (row.get('error') or not row['enabled']))
        self.feedback[key].update(busy=False, phase=(('已安装，可以使用' if expected else '已卸载') if valid else '无法确认安装状态，请重新打开此页检查'))
        self._confirming = None

    def set_busy_controls(self, busy):
        # Keep search, filters and scrolling usable while package I/O runs.
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)
        for switch in self.findChildren(ConsumerSwitch):
            row = next((r for r in self.entries if r['name'] == switch.property('skillName')), {})
            switch.setEnabled(not busy and bool(row.get('installed') or row.get('recoverable')))
        for button in self.findChildren(QPushButton):
            if button.property('skillFolder'):
                button.setEnabled(not busy and Path(button.property('skillFolder')).is_dir())
        self.file_select.setEnabled(not busy)

    @Slot(bool, object)
    def finished(self, ok, value):
        self.busy = False
        self.set_busy_controls(False)
        callback, self._callback = self._callback, None
        self._worker = None
        key, self._activity_key = self._activity_key, None
        if not ok:
            key = key or (self._confirming[0] if self._confirming else None)
            if key and key in self.feedback:
                label = {'skill_install':'安装失败', 'skill_uninstall':'卸载失败', 'skill_inspect':'检查失败'}.get(self._operation, '状态确认失败')
                self.feedback[key].update(busy=False, phase=label + '：' + str(value))
            self._confirming = None
            self.status.setText('未完成：' + str(value) + '。可修正后重试。')
            # Return switches to their persisted state after a failed write.
            self.render()
            return
        if key:
            if self._operation in {'skill_install','skill_uninstall'}:
                self._confirming = (key, self._operation == 'skill_install')
                self.feedback[key].update(phase='正在确认安装状态…')
            else:
                self.feedback.pop(key, None)
            self.render()
        self.status.setText(value.get('message', '已完成') if isinstance(value, dict) else '已完成')
        if callback:
            callback(value)

    def refresh(self):
        self.run('skill_list', self.loaded)

    def loaded(self, value):
        if self._confirming:
            self.confirm_installation(value)
        self.entries = value['entries']
        self.directory = value['directory']
        installed = sum(r['installed'] for r in self.entries)
        self.status.setText(' · '.join([f'已安装 {installed} 项', *value.get('issues', [])]))
        self.render()
        if self._pending_requested:
            self._pending_requested = False
            self.show_drafts()

    def request_pending(self):
        if self.busy:
            self._pending_requested = True
        else:
            self.show_drafts()

    def render(self, *_args):
        old = self.scroll.verticalScrollBar().value()
        host = QWidget()
        column = QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        query = self.search.text().strip().casefold()
        candidates = list(self.entries)
        for key, feedback in self.feedback.items():
            if not any(r['name'] == key for r in candidates):
                candidates.append(dict(origin='user', installed=False, enabled=False, directory='', recoverable=False, **feedback['row']))
        rows = [r for r in candidates if (self.tab.currentIndex() == 0 or (r['installed'] if self.tab.currentIndex() == 1 else not r['installed']))
                and (not self.origin.currentData() or r['origin'] == self.origin.currentData())
                and query in (r['name'] + r.get('title', r['name']) + r.get('description', '')).casefold()]
        for row in rows:
            row = dict(row, title=row.get('title', row['name']))
            card = QFrame()
            card.setObjectName('settingsPanel')
            horizontal = QHBoxLayout(card)
            horizontal.setContentsMargins(12, 10, 12, 10)
            text = QVBoxLayout()
            title = QLabel(row['title'])
            title.setTextFormat(Qt.PlainText)
            title.setWordWrap(True)
            title.setProperty('skillName', row['name'])
            title.setToolTip(row['name'])
            text.addWidget(title, 0, Qt.AlignLeft)
            desc = QLabel(row.get('summary') or row.get('description', '')[:100])
            desc.setToolTip(row.get('description', ''))
            desc.setTextFormat(Qt.PlainText)
            desc.setWordWrap(True)
            desc.setObjectName('pageMeta')
            text.addWidget(desc)
            source = {'system':'Lulu 系统', 'user':'用户安装'}[row['origin']]
            state = '已安装' if row['installed'] else '未安装'
            if row['enabled'] and not row.get('automatic', True):
                state += ' · 仅手动指定'
            meta = QLabel(' · '.join(filter(None, [source, row.get('author') if row['origin'] != 'system' else '', state])))
            meta.setTextFormat(Qt.PlainText)
            meta.setWordWrap(True)
            meta.setObjectName('pageMeta')
            text.addWidget(meta)
            feedback = self.feedback.get(row['name'])
            if feedback:
                phase = QLabel(feedback['phase'])
                phase.setWordWrap(True)
                phase.setTextFormat(Qt.PlainText)
                phase.setProperty('skillProgress', row['name'])
                phase.setAccessibleName(row['title'] + '安装状态')
                text.addWidget(phase)
                if feedback['busy']:
                    progress = QProgressBar()
                    progress.setRange(0, 0)
                    progress.setTextVisible(False)
                    progress.setMaximumHeight(4)
                    progress.setAccessibleName(feedback['phase'])
                    text.addWidget(progress)
            horizontal.addLayout(text, 1)
            toggle = ConsumerSwitch(accessible_name=f"安装或卸载 {row['title']}")
            toggle.setProperty('skillName', row['name'])
            toggle.setChecked(row['installed'])
            toggle.setEnabled(not self.busy and bool(row['installed'] or row.get('recoverable')))
            toggle.setToolTip('关闭以卸载' if row['installed'] else '打开以重新安装')
            toggle.toggled.connect(lambda checked, row=row: self.change_installation(row, checked))
            horizontal.addWidget(toggle)
            detail = self.button('详情', lambda row=row: self.show_detail(row))
            detail.setAccessibleName(row['title'] + '的详情')
            detail.setEnabled(not self.busy and not row['name'].startswith(('incoming:', 'pending:')))
            horizontal.addWidget(detail)
            folder = self.button('', lambda row=row: self.open_directory(row['directory']))
            folder.setIcon(QIcon(icons.pixmap('folder', '#807466', 18)))
            folder.setIconSize(QSize(18, 18))
            folder.setFixedWidth(36)
            folder.setProperty('skillFolder', row['directory'])
            folder.setAccessibleName('在访达打开 ' + row['title'])
            folder.setToolTip('在访达中打开技能文件夹' if row['installed'] else '安装后可打开技能文件夹')
            folder.setEnabled(not self.busy and row['installed'])
            horizontal.addWidget(folder)
            column.addWidget(card)
        if not rows:
            empty = QLabel('没有符合条件的技能。调整筛选，或安装你自己的技能。')
            empty.setWordWrap(True)
            column.addWidget(empty)
        column.addStretch()
        self.scroll.setWidget(host)
        self.scroll.verticalScrollBar().setValue(old)

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择本地技能', '', '本地技能 (*.zip SKILL.md)')
        if path:
            self.inspect(path)

    def inspect(self, source):
        if self.busy:
            return
        try:
            source = LocalSkillDropZone.local_source(source)
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        self.feedback = {k:v for k,v in self.feedback.items() if not k.startswith('incoming:')}
        self.run('skill_inspect', self.preview_packages, source=source)

    def preview_packages(self, value):
        self.pending = value['entries']
        self.show_pending()

    def show_pending(self):
        from PySide6.QtWidgets import QDialog
        dialog = QDialog(self)
        dialog.setWindowTitle('选择要安装的技能')
        dialog.resize(560, 440)
        layout = QVBoxLayout(dialog)
        note = QLabel('安装后 Lulu 可按需读取技能说明；附带脚本不会自动执行。')
        note.setWordWrap(True)
        layout.addWidget(note)
        listing = QListWidget()
        layout.addWidget(listing, 1)
        for row in self.pending:
            listing.addItem(row['name'] + '\n' + row['description'][:180])
        detail = QLabel()
        detail.setWordWrap(True)
        detail.setTextFormat(Qt.PlainText)
        layout.addWidget(detail)
        install = self.button('安装所选技能', lambda: None)
        install.setEnabled(False)
        def selected(index):
            if index < 0:
                return
            row = self.pending[index]
            detail.setText('来源：' + row.get('source', '技能草稿') + '\n' +
                ('名称与已有技能冲突，安装时会拒绝覆盖。' if row.get('conflict') or row.get('reserved') else '完整文件已检查，可安装。'))
            install.setEnabled(not row.get('reserved'))
        listing.currentRowChanged.connect(selected)
        def accept():
            index = listing.currentRow()
            if index >= 0:
                token = self.pending[index]['token']
                dialog.accept()
                self.run('skill_install', lambda _: self.refresh(), token=token)
        install.clicked.connect(accept)
        layout.addWidget(install)
        if self.pending:
            listing.setCurrentRow(0)
        else:
            detail.setText('尚无待安装草稿。可以让 Lulu 制作技能，或先导入一个技能包。')
        dialog.exec()

    def show_drafts(self):
        self.run('skill_drafts', self.preview_packages)

    def change_installation(self, row, checked):
        # Display only persisted state; a click is a request until I/O succeeds.
        for switch in self.scroll.widget().findChildren(ConsumerSwitch):
            if switch.property('skillName') == row['name']:
                switch.blockSignals(True)
                switch.setChecked(row['installed'])
                switch.blockSignals(False)
        operation = 'skill_install' if checked else 'skill_uninstall'
        self.run(operation, lambda _: self.refresh(), name=row['name'])
        self.status.setText(('正在安装 ' if checked else '正在卸载 ') + row['title'] + '…')

    def show_detail(self, row):
        self.detail_name = row['name']
        self.detail_source = row.get('source', '')
        self.detail_directory = row['directory']
        self.detail_title.setText(row['title'])
        self.detail_info.setText('\n'.join(filter(None, [row.get('author'), row.get('version'), row.get('source'), row.get('compatibility'), row['directory']])))
        self.run('skill_details', self.detail_loaded, name=row['name'])

    def detail_loaded(self, value):
        self.stack.setCurrentIndex(1)
        self.file_select.blockSignals(True)
        self.file_select.clear()
        self.file_select.addItems(['SKILL.md'] + [f for f in value['files'] if f != 'SKILL.md'])
        self.file_select.blockSignals(False)
        self.display_file(value)
        while self.config_layout.rowCount():
            self.config_layout.removeRow(0)
        self.config_box.setVisible(self.detail_name in {'lulu-translate', 'lulu-proofread'})
        if self.detail_name == 'lulu-translate':
            language = QLineEdit(value['config'].get('language', '英语'))
            language.setMaxLength(80)
            terms = QPlainTextEdit(value['config'].get('terms', ''))
            terms.setMaximumHeight(85)
            self.config_layout.addRow('默认目标语言', language)
            self.config_layout.addRow('术语参考', terms)
            self.config_layout.addRow(self.button('保存技能偏好', lambda: self.run('skill_configure', name=self.detail_name, config=dict(language=language.text(), terms=terms.toPlainText()))))
        elif self.detail_name == 'lulu-proofread':
            strict = QCheckBox('严格保留数字、链接和原文结构')
            strict.setChecked(value['config'].get('strict', True))
            self.config_layout.addRow(strict)
            self.config_layout.addRow(self.button('保存技能偏好', lambda: self.run('skill_configure', name=self.detail_name, config=dict(strict=strict.isChecked()))))

    def read_file(self, reference):
        if reference and self.detail_name and not self.busy:
            self.run('skill_details', self.display_file, name=self.detail_name, reference=reference)

    def display_file(self, value):
        if value['reference'].lower().endswith('.md'):
            self.preview.setMarkdown(value['text'])
        else:
            self.preview.setPlainText(value['text'])

    def open_link(self, url):
        if url.scheme() == 'https' and not url.userInfo():
            QDesktopServices.openUrl(url)

    def open_directory(self, path=None):
        target = path or self.directory
        if target and Path(target).is_dir():
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(target)):
                self.status.setText('无法打开目录：' + target)
