"""Presentation for the manual Feishu save sheet; no account or network access."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QBoxLayout, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListView, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import icons, theme as T
from .control_foundation import PaperTextureFrame


class ChoiceItemDelegate(QStyledItemDelegate):
    """Lulu menu rows: warm paper, inset highlight, and a persistent choice mark."""
    def __init__(self, combo):
        super().__init__(combo)
        self.combo = combo

    def sizeHint(self, option, index):
        return QSize(160, 36)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(5, 2, -5, -2)
        chosen = index.row() == self.combo.currentIndex()
        active = bool(option.state & (QStyle.State_Selected | QStyle.State_MouseOver))
        enabled = bool(index.flags() & Qt.ItemIsEnabled)
        theme = T.consumer_theme()
        if chosen or active:
            painter.setBrush(T.consumer_qcolor(theme.accent_wash if chosen else theme.secondary_hover))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(rect), 7, 7)
        font = option.font
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QColor(theme.accent if chosen else theme.ink if enabled else '#a49a8c'))
        text_rect = rect.adjusted(12, 0, -34, 0)
        text = painter.fontMetrics().elidedText(str(index.data() or ''), Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        if chosen:
            painter.drawPixmap(rect.right() - 26, rect.center().y() - 8, icons.pixmap('check', '#438499', 16))
        painter.restore()


class Choice(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(40)
        self.setMaxVisibleItems(7)
        view = QListView(self)
        view.setUniformItemSizes(True)
        view.setItemDelegate(ChoiceItemDelegate(self))
        view.setMouseTracking(True)
        view.setTextElideMode(Qt.ElideRight)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.setView(view)
        # macOS otherwise uses a menu whose height ignores maxVisibleItems.
        self.setStyleSheet('QComboBox {combobox-popup: 0;} QComboBox:hover {border-color:#a2b9b8;}')
        # Style the native popup before its first show. A rounded stylesheet alone
        # does not clip the opaque macOS host or its list viewport.
        popup = view.window()
        popup.setObjectName('luluChoicePopup')
        popup.setAttribute(Qt.WA_TranslucentBackground, True)
        popup.setAutoFillBackground(False)
        popup.setFrameShape(QFrame.NoFrame)
        popup.setStyleSheet('QFrame#luluChoicePopup {background:transparent;border:0;}')
        c = T.consumer_theme()
        self._paper = PaperTextureFrame(popup, strength=c.paper_texture_opacity,
                                       corner_radius=10, layer='floating', base_color=c.surface_floating)
        self._paper.setObjectName('feishuChoicePaper')
        self._paper.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._paper.setStyleSheet(f'QFrame#feishuChoicePaper {{background:transparent;border:1px solid {c.accent_hairline};border-radius:10px;}}')
        view.setStyleSheet('QListView {background:transparent;border:0;padding:6px 0;outline:0;}')
        view.setAutoFillBackground(False)
        view.viewport().setAutoFillBackground(False)
        self.currentIndexChanged.connect(lambda: self.setToolTip(self.currentText()))

    def showPopup(self):
        screen = self.screen().availableGeometry().adjusted(8, 8, -8, -8)
        width = min(self.width(), screen.width())
        view = self.view()
        view.setFixedWidth(max(1, width - 2))
        view.setMaximumHeight(min(7 * 36 + 16, screen.height() - 2))
        for index in range(self.count()):
            self.setItemData(index, self.itemText(index), Qt.ToolTipRole)
        super().showPopup()
        popup = view.window()
        height = min(popup.height(), 7 * 36 + 16, screen.height())
        below = self.mapToGlobal(QPoint(0, self.height() + 3))
        above = self.mapToGlobal(QPoint(0, -height - 3))
        y = below.y() if below.y() + height <= screen.bottom() + 1 else above.y()
        popup.setGeometry(max(screen.left(), min(below.x(), screen.right() - width + 1)),
                          max(screen.top(), min(y, screen.bottom() - height + 1)), width, height)
        self._paper.setGeometry(popup.rect())
        self._paper.lower()
        self._paper.show()
        view.scrollTo(view.currentIndex(), QListView.EnsureVisible)

    def wheelEvent(self, event):
        # Scrolling the form must not silently change a saved destination.
        if not self.view().isVisible():
            event.ignore()
            return
        super().wheelEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.drawPixmap(self.width() - 27, (self.height() - 16) // 2,
                           icons.pixmap('caret_down', '#81776a', 16))
        painter.end()


class ChoiceStrip(QWidget):
    """Small, mutually exclusive choices with native keyboard-accessible buttons."""
    currentIndexChanged = Signal(int)

    def __init__(self, items, *, cards=False, parent=None):
        super().__init__(parent)
        self.items = items
        self.index = 0
        self.buttons = []
        self.marks = []
        self.setObjectName('choiceCards' if cards else 'choiceStrip')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0) if cards else layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10 if cards else 4)
        for index, (title, value, detail, icon) in enumerate(items):
            button = QPushButton()
            button.setObjectName('formatCard' if cards else 'segment')
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setAccessibleName(title)
            button.setToolTip(detail or title)
            if cards:
                inner = QHBoxLayout(button)
                inner.setContentsMargins(15, 13, 12, 13)
                inner.setSpacing(11)
                mark = icons.icon_label(icon, '#468296', 23)
                mark.setAttribute(Qt.WA_TransparentForMouseEvents)
                inner.addWidget(mark)
                words = QVBoxLayout()
                words.setSpacing(4)
                for text, role in ((title, 'cardTitle'), (detail, 'cardDetail')):
                    label = QLabel(text)
                    label.setObjectName(role)
                    label.setAttribute(Qt.WA_TransparentForMouseEvents)
                    words.addWidget(label)
                inner.addLayout(words, 1)
                indicator = QLabel('●' if index == 0 else '○')
                indicator.setObjectName('selectionMark')
                indicator.setAttribute(Qt.WA_TransparentForMouseEvents)
                inner.addWidget(indicator)
                self.marks.append(indicator)
                button.setMinimumHeight(66)
            else:
                button.setText(title)
                button.setMinimumHeight(32)
            self.group.addButton(button, index)
            button.clicked.connect(lambda _, i=index: self.setCurrentIndex(i))
            self.buttons.append(button)
            layout.addWidget(button, 1)
        self.buttons[0].setChecked(True)

    def currentData(self):
        return self.items[self.index][1]

    def currentIndex(self):
        return self.index

    def currentText(self):
        return self.items[self.index][0]

    def setCurrentIndex(self, index):
        if not 0 <= index < len(self.items):
            return
        changed = self.index != index
        self.index = index
        self.buttons[index].setChecked(True)
        for i, mark in enumerate(self.marks):
            mark.setText('●' if i == index else '○')
        if changed:
            self.currentIndexChanged.emit(index)


class SaveSurface:
    """Layout methods used by FeishuSaveDialog; saving behavior stays in that dialog."""
    def _build_surface(self, title):
        c = T.consumer_theme()
        self.setObjectName('feishuSaveSheet')
        self.setStyleSheet(f'''
            QDialog#feishuSaveSheet {{background:#fbf8f1;color:#39362f;}}
            QDialog#feishuSaveSheet QLabel {{background:transparent;border:0;color:#39362f;font-family:"{c.font_body}";font-size:13px;}}
            QDialog#feishuSaveSheet QLabel#sheetTitle {{font-family:"{c.font_display}";font-size:26px;}}
            QDialog#feishuSaveSheet QLabel#sectionTitle {{font-size:14px;font-weight:600;}}
            QDialog#feishuSaveSheet QLabel#muted, QDialog#feishuSaveSheet QLabel#fieldLabel, QDialog#feishuSaveSheet QLabel#cardDetail {{color:#786f63;font-size:12px;}}
            QDialog#feishuSaveSheet QLabel#cardTitle {{font-weight:600;font-size:14px;}}
            QDialog#feishuSaveSheet QLabel#sourceTitle {{font-size:13px;font-weight:600;}}
            QDialog#feishuSaveSheet QLabel#selectionMark {{color:#438499;font-size:17px;}}
            QDialog#feishuSaveSheet QLabel#account {{color:#66816a;font-size:12px;}}
            QWidget#body, QWidget#fieldRow {{background:transparent;}}
            QScrollArea {{border:0;background:transparent;}}
            QScrollBar:vertical {{width:6px;background:transparent;margin:3px 0;}}
            QScrollBar::handle:vertical {{background:#cec5b6;border-radius:3px;min-height:30px;}}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{background:transparent;}}
            QFrame#rule {{background:#e6ded0;max-height:1px;border:0;}}
            QFrame#source {{background:transparent;border:0;}}
            QFrame#destination {{background:#fffdf8;border:1px solid #e3dbce;border-radius:14px;}}
            QFrame#mapping {{background:#f2f6f3;border:1px solid #dce7df;border-radius:10px;}}
            QWidget#choiceStrip {{background:#efeae0;border-radius:9px;}}
            QLineEdit, QComboBox {{background:#fffdf8;color:#39362f;border:1px solid #ded5c5;border-radius:8px;padding:8px 12px;font-size:13px;}}
            QComboBox {{padding-right:30px;}}
            QComboBox::drop-down {{border:0;width:28px;}}
            QLineEdit:focus, QComboBox:focus {{border-color:#438499;}}
            QComboBox QAbstractItemView {{background:#fffdf8;color:#39362f;selection-background-color:#e8f0ee;selection-color:#326d80;padding:5px;}}
            QPushButton {{background:#fffdf8;color:#4b453d;border:1px solid #ded5c5;border-radius:8px;padding:9px 15px;font-size:13px;}}
            QPushButton:hover {{background:#f7f4ec;border-color:#a6bfc2;}}
            QPushButton:pressed {{background:#e6eeeb;}}
            QPushButton:focus {{border:1px solid #438499;}}
            QPushButton:disabled {{color:#a59b8d;background:#f2ede3;}}
            QPushButton#formatCard {{background:#fffdf8;border:1px solid #dfd6c7;border-radius:12px;text-align:left;padding:0;}}
            QPushButton#formatCard:checked {{background:#edf3f2;border:1px solid #52899a;}}
            QDialog#feishuSaveSheet QPushButton#formatCard:checked QLabel#cardTitle {{color:#326f81;}}
            QPushButton#segment {{border:1px solid transparent;background:transparent;padding:5px 10px;color:#857c6f;}}
            QPushButton#segment:checked {{background:#fffdf8;color:#386f80;border-color:#e1d7c6;}}
            QPushButton#segment:focus {{border-color:#438499;}}
            QPushButton#quiet {{background:transparent;border:1px solid transparent;padding:7px 8px;color:#527d8b;}}
            QPushButton#quiet:hover {{background:#edf2ed;}}
            QPushButton#primary {{background:#438499;color:white;border:1px solid #438499;font-weight:600;padding:10px 20px;}}
            QPushButton#primary:hover {{background:#367386;}}
            QPushButton#primary:disabled {{background:#a1b7b7;border-color:#a1b7b7;color:#f8f7f1;}}
            QCheckBox {{color:#786f63;font-size:12px;spacing:7px;}}
            QCheckBox::indicator {{width:14px;height:14px;}}
            QPlainTextEdit {{background:#fffdf8;border:1px solid #ded5c5;border-radius:8px;padding:10px;color:#655d52;}}
        ''')
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(720, screen.width() - 48), min(680, screen.height() - 64))
        self.setMinimumSize(min(520, screen.width() - 48), min(480, screen.height() - 64))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(28, 22, 28, 18)
        logo = icons.icon_label('feishu', '#438499', 28)
        logo.setFixedSize(46, 46)
        logo.setAlignment(Qt.AlignCenter)
        logo.setStyleSheet('background:#eaf0ec;border-radius:13px;')
        head.addWidget(logo)
        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        self.heading = QLabel('存入飞书')
        self.heading.setObjectName('sheetTitle')
        title_col.addWidget(self.heading)
        self.subtitle = QLabel('选择保存形式与位置')
        self.subtitle.setObjectName('muted')
        title_col.addWidget(self.subtitle)
        head.addSpacing(4)
        head.addLayout(title_col, 1)
        self.account_label = QLabel('正在检查连接…')
        self.account_label.setObjectName('account')
        self.account_label.setMaximumWidth(190)
        head.addWidget(self.account_label)
        head.addSpacing(16)
        self.connect_button = QPushButton('连接设置')
        self.connect_button.setObjectName('quiet')
        self.connect_button.clicked.connect(self._connect)
        head.addWidget(self.connect_button)
        root.addLayout(head)
        root.addWidget(self._rule())
        self.content_layout = QHBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        root.addLayout(self.content_layout, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(lambda low, high: bar.setStyleSheet(
            'QScrollBar::handle:vertical {background:' + ('#cec5b6' if high > low else 'transparent') + ';}'))
        bar.setStyleSheet('QScrollBar::handle:vertical {background:transparent;}')
        self.scroll.viewport().setAutoFillBackground(False)
        self.body = QWidget()
        self.body.setObjectName('body')
        self.form = QVBoxLayout(self.body)
        self.form.setContentsMargins(28, 18, 28, 22)
        self.form.setSpacing(20)
        self.scroll.setWidget(self.body)
        self.content_layout.addWidget(self.scroll, 1)

        source = QFrame()
        source.setObjectName('source')
        row = QHBoxLayout(source)
        row.setContentsMargins(0, 2, 0, 0)
        row.addWidget(icons.icon_label('file' if len(self.records) == 1 else 'layers', '#8c806c', 19))
        self.source_title = QLabel(title or '已选内容')
        self.source_title.setTextFormat(Qt.PlainText)
        self.source_title.setObjectName('sourceTitle')
        self.source_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.source_title.setToolTip(title)
        row.addWidget(self.source_title, 1)
        row.addWidget(self._muted(f'{len(self.records)} ' + ('条作品' if self.context == 'homepage' else '篇文稿')))
        self.form.addWidget(source)
        self.source_strip = source

        self.format_section, col = self._section('保存为')
        self.format = ChoiceStrip([
            ('多维表格', 'table', '整理作品，筛选比较', 'workspace_grid'),
            ('飞书文档', 'document', '保留全文，阅读编辑', 'file'),
        ], cards=True)
        col.addWidget(self.format)

        self.destination_section, col = self._section('存到哪里', grouped=True)
        self.mode = ChoiceStrip([
            ('已有表格', 'existing', '', ''), ('新建表格', 'new', '', ''),
        ])
        self.mode.setMaximumWidth(270)
        self.destination_section._heading.addWidget(self.mode, 1)
        self.target = self._combo_row(col, '目标表格', [('使用其他表格…', '')])
        self.url = self._input_row(col, '表格链接', '粘贴飞书多维表格链接')
        self.read_button = QPushButton('读取')
        self.read_button.setFixedWidth(68)
        self.url._row.layout().addWidget(self.read_button)
        self.read_button.clicked.connect(self._discover)
        self.table = self._combo_row(col, '数据表', [('请先读取表格', '')])
        self.template = self._combo_row(col, '使用模板', [('选题素材库', 'topic'), ('IP调研库', 'research')])
        self.template.setCurrentIndex(1 if self.context == 'homepage' else 0)
        self.name = self._input_row(col, '名称', '给这个库起个名字')
        self.name.setText(title or '我的选题素材库')
        self.location = self._combo_row(col, '存放位置', [('云文档默认位置', {})])
        self.location.addItem('使用其他文件夹或知识库…', {'custom': True})
        self.location_url = self._input_row(col, '位置链接', '粘贴文件夹或知识库链接')
        self.template_note = self._muted('主题与开头拆解留白，方便你分析填写。')
        col.addWidget(self.template_note)

        self.mapping_card = QFrame()
        self.mapping_card.setObjectName('mapping')
        self.mapping_layout = QVBoxLayout(self.mapping_card)
        self.mapping_layout.setContentsMargins(14, 11, 14, 11)
        map_head = QHBoxLayout()
        self.mapping_title = QLabel('列对应')
        self.mapping_title.setObjectName('sectionTitle')
        map_head.addWidget(self.mapping_title)
        map_head.addStretch()
        self.edit_mapping = QPushButton('查看与调整')
        self.edit_mapping.setObjectName('quiet')
        map_head.addWidget(self.edit_mapping)
        self.mapping_layout.addLayout(map_head)
        self.mapping_summary = self._muted('先读取表格，再确认要保存的信息。')
        self.mapping_layout.addWidget(self.mapping_summary)
        self.mapping_body = QWidget()
        self.grid = QGridLayout(self.mapping_body)
        self.grid.setContentsMargins(0, 8, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(8)
        self.mapping_layout.addWidget(self.mapping_body)
        self.mapping_body.hide()
        self.edit_mapping.clicked.connect(lambda: self._set_mapping_expanded(self.mapping_body.isHidden()))
        col.addWidget(self.mapping_card)

        self.repeat_section, col = self._section('再次保存时')
        self.repeat = ChoiceStrip([
            ('更新原内容', 'update', '更新本次目标中的原记录', ''),
            ('另存一份', 'copy', '新建副本，保留原内容', ''),
        ])
        self.repeat.setMaximumWidth(300)
        self.repeat_section._heading.addWidget(self.repeat, 1)
        self.repeat_note = self._muted('保留人工分析列；飞书文档已编辑时会提示另存。')
        col.addWidget(self.repeat_note)
        self.result_details = QPlainTextEdit()
        self.result_details.setReadOnly(True)
        self.result_details.setMinimumHeight(140)
        self.result_details.setMaximumHeight(220)
        self.result_details.hide()
        self.form.addWidget(self.result_details)
        self.form.addStretch()

        root.addWidget(self._rule())
        footer = QBoxLayout(QBoxLayout.LeftToRight)
        self.footer_layout = footer
        footer.setContentsMargins(28, 14, 28, 18)
        footer.setSpacing(10)
        status_widget = QWidget()
        status_col = QVBoxLayout(status_widget)
        status_col.setContentsMargins(0, 0, 0, 0)
        status_col.setSpacing(5)
        self.status = QLabel('选择目标，确认后存入。')
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(42)
        status_col.addWidget(self.status)
        self.upload_notice = self._muted('仅上传本次所选内容，自动记住这次位置。')
        status_col.addWidget(self.upload_notice)
        footer.addWidget(status_widget, 1)
        actions_widget = QWidget()
        actions = QHBoxLayout(actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(10)
        actions.addStretch()
        footer.addWidget(actions_widget)
        self.open_button = QPushButton('打开飞书')
        self.open_button.hide()
        self.open_button.clicked.connect(self._open_result)
        actions.addWidget(self.open_button)
        self.adjust_button = QPushButton('调整选项')
        self.adjust_button.hide()
        self.adjust_button.clicked.connect(self._adjust)
        actions.addWidget(self.adjust_button)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.setObjectName('quiet')
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        self.save_button = QPushButton('存入飞书')
        self.save_button.setObjectName('primary')
        self.save_button.setMinimumWidth(128)
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.save_button)
        root.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        for control in (self.format, self.mode, self.location, self.template, self.repeat):
            control.currentIndexChanged.connect(self._layout)
        self.target.currentIndexChanged.connect(self._target_changed)
        self.url.textEdited.connect(self._url_edited)
        self.table.currentIndexChanged.connect(self._load_fields)
        if self.context in {'create', 'manage'}:
            self.mode.setCurrentIndex(1 if self.context == 'create' else 0)
            self.heading.setText('新建作品库' if self.context == 'create' else '添加已有表格')
            self.subtitle.setText('选一个模板，建立自己的内容收藏。' if self.context == 'create' else '连接你习惯的表格，保留原有整理方式。')
            self.upload_notice.setText('只创建空库，不上传素材。' if self.context == 'create' else '只记住目标，已有内容保持原样。')
            self.save_button.setText('创建空库' if self.context == 'create' else '添加到常用库')
        self._layout()

    @staticmethod
    def _rule():
        rule = QFrame()
        rule.setObjectName('rule')
        rule.setFixedHeight(1)
        return rule

    @staticmethod
    def _muted(text):
        label = QLabel(text)
        label.setObjectName('muted')
        label.setWordWrap(True)
        return label

    def _section(self, title, *, grouped=False):
        section = QFrame()
        section.setObjectName('destination' if grouped else 'fieldRow')
        col = QVBoxLayout(section)
        col.setContentsMargins(20, 16, 20, 18) if grouped else col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        heading = QBoxLayout(QBoxLayout.LeftToRight)
        heading.setSpacing(12)
        section._heading = heading
        label = QLabel(title)
        label.setObjectName('sectionTitle')
        heading.addWidget(label, 1)
        col.addLayout(heading)
        self.form.addWidget(section)
        return section, col

    def _field_row(self, layout, label, widget):
        row = QWidget()
        row.setObjectName('fieldRow')
        inner = QHBoxLayout(row)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(12)
        lab = QLabel(label)
        lab.setObjectName('fieldLabel')
        lab.setFixedWidth(70)
        lab.setBuddy(widget)
        inner.addWidget(lab)
        inner.addWidget(widget, 1)
        widget._label = lab
        widget._row = row
        widget.setAccessibleName(label)
        layout.addWidget(row)
        return widget

    def _combo_row(self, layout, label, items):
        choice = Choice()
        for text, value in items:
            choice.addItem(text, value)
        return self._field_row(layout, label, choice)

    def _input_row(self, layout, label, placeholder):
        edit = QLineEdit()
        edit.setMinimumHeight(40)
        edit.setPlaceholderText(placeholder)
        edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return self._field_row(layout, label, edit)

    def _show(self, widget, visible):
        getattr(widget, '_row', widget).setVisible(visible)

    def _layout(self, *_):
        document = self.format.currentData() == 'document'
        existing = self.mode.currentData() == 'existing' and not document
        management = self.context in {'create', 'manage'}
        self.format_section.setVisible(not management)
        self.source_strip.setVisible(not management)
        self.mode.setVisible(not document and not management)
        self._show(self.target, existing and self.target.count()>1)
        self._show(self.url, existing and self.target.currentIndex()==0)
        self._show(self.table, existing and bool(self.discovery))
        self.read_button.setText('刷新' if self.discovery else '读取')
        self.read_button.setToolTip('重新读取表格与列信息' if self.discovery else '读取链接中的表格')
        self.mapping_card.setVisible(existing and bool(self.fields))
        self._show(self.template, not document and not existing)
        self.template_note.setVisible(not document and not existing)
        for widget in (self.name, self.location):
            self._show(widget, not existing)
        self._show(self.location_url, not existing and bool((self.location.currentData() or {}).get('custom')))
        self.name._label.setText('文档名称' if document else '作品库名')
        self.repeat_section.setVisible(bool(self.records) and (existing or document))
        self.repeat_note.setText('创建一个副本，原有内容会保留。' if self.repeat.currentData() == 'copy' else '保留人工分析列；飞书文档已编辑时会提示另存。')
        self.template_note.setText('按已确认的 14 列模板新建，保留作者昵称与两类时间。' if self.template.currentData() == 'research' else '按已确认的 16 列模板新建。主题、开头拆解和标签留白；不启用飞书 AI 标签。')
        self._responsive()

    def _set_mapping_expanded(self, expanded):
        self.mapping_body.setVisible(expanded)
        self.edit_mapping.setText('收起列对应' if expanded else '查看与调整')

    def _responsive(self, *_):
        if not hasattr(self, 'footer_layout'):
            return
        # Keep actions reachable even with all partial-save recovery buttons shown.
        self.footer_layout.setDirection(QBoxLayout.TopToBottom if self.width() < 700 or self.open_button.isVisible() or self.adjust_button.isVisible() else QBoxLayout.LeftToRight)
        self.account_label.setVisible(True)
        self.account_label.setMaximumWidth(160 if self.width() >= 620 else 100)
        for section in (self.destination_section, self.repeat_section):
            section._heading.setDirection(QBoxLayout.TopToBottom if self.width() < 620 else QBoxLayout.LeftToRight)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._responsive()
