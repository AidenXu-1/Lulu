"""Recent session results, with file actions attached to the affected audio."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import dialogs, icons
from .control_foundation import adopt_button, adopt_dialog
from .widgets import ElideLabel


def _record_labels(record):
    title = str(record.get('title') or Path(record.get('output_path') or '').stem)
    adjusted = record.get('summary') == '局部调整 · 原版保留'
    preview = title.startswith('选段试听')
    kind = '局部调整' if adjusted else '选段试听' if preview else '完整配音'
    if adjusted:
        title = '局部调整后的配音'
    elif record.get('voice_label'):
        title = str(record['voice_label'])
    details = []
    try:
        generated = datetime.fromisoformat(str(record.get('generated_at') or ''))
        details.append(generated.strftime('%m月%d日 %H:%M'))
    except ValueError:
        pass  # Older local adjustments have no reliable generation timestamp.
    duration = str(record.get('duration_label') or '')
    if duration:
        details.append(duration[:-1] + ' 秒' if duration.endswith('s') else duration)
    if preview:
        details.append('未入库')
    elif adjusted:
        details.append('原版保留')
    return title or '配音结果', kind, ' · '.join(details)


class VoiceHistoryDialog(QDialog):
    def __init__(self, workbench):
        super().__init__(workbench.page)
        self.page = workbench.page
        self.workbench = workbench
        adopt_dialog(self, accessible_name='配音历史记录')
        self.setWindowTitle('历史记录')
        self.setMinimumWidth(560)
        self.setMaximumWidth(880)
        self.setMinimumHeight(340)
        c = self.page._consumer
        self.setStyleSheet(f'''
            QDialog {{background:{c.surface};color:{c.ink};}}
            QLabel {{background:transparent;color:{c.ink};font-size:13px;}}
            QLabel[muted="true"] {{color:{c.ink_muted};font-size:12px;}}
            QLabel#historyHeading {{font-family:'Songti SC';font-size:22px;font-weight:600;}}
            QLabel#historyName {{font-size:14px;font-weight:600;}}
            QLabel#historyBadge {{color:{c.accent};font-size:11px;}}
            QLabel#historyIcon {{background:{c.accent_wash};border-radius:10px;}}
            QFrame#historyRow {{background:{c.surface};border:1px solid {c.line};border-radius:12px;}}
            QFrame#historyRow[current="true"] {{background:{c.accent_wash};border-color:{c.accent};}}
            QFrame#historyDivider {{background:{c.line};border:0;}}
            QScrollArea, QWidget#historyList {{background:transparent;border:0;}}
            QScrollBar:vertical {{background:transparent;width:6px;margin:0;}}
            QScrollBar::handle:vertical {{background:{c.line};border-radius:3px;min-height:28px;}}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{background:transparent;}}
            QPushButton {{background:transparent;color:{c.ink};border:1px solid transparent;
                border-radius:8px;padding:0 12px;font-size:13px;}}
            QPushButton[role="secondary"] {{border-color:{c.line};background:{c.surface};}}
            QPushButton:hover {{background:{c.accent_wash};color:{c.accent};}}
            QPushButton:pressed {{border-color:{c.accent};}}
            QPushButton:focus {{border:2px solid {c.accent};}}
            QPushButton:disabled {{color:{c.ink_muted};}}
        ''')
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(18)
        header = QVBoxLayout()
        header.setSpacing(6)
        heading = QHBoxLayout()
        title = QLabel('最近生成')
        title.setObjectName('historyHeading')
        heading.addWidget(title)
        heading.addStretch()
        heading.addWidget(self._label(f'{len(self.page._indextts2_results)} 条记录', muted=True))
        header.addLayout(heading)
        hint = self._label('保留本次打开的最近 5 条结果，已保存的配音可在音频库查看。', muted=True)
        hint.setWordWrap(True)
        header.addWidget(hint)
        root.addLayout(header)

        self.list_area = QScrollArea()
        self.list_area.setWidgetResizable(True)
        self.list_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_area.setAccessibleName('最近生成的音频')
        content = QWidget()
        content.setObjectName('historyList')
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 0, 8, 0)
        rows.setSpacing(10)
        self.record_rows = []
        for record in self.page._indextts2_results:
            row = self._record_row(dict(record))
            rows.addWidget(row)
            self.record_rows.append(row)
        if not self.record_rows:
            empty = QVBoxLayout()
            empty.setContentsMargins(0, 26, 0, 26)
            icon = QLabel()
            icon.setPixmap(icons.pixmap('waveform', c.accent, 32))
            icon.setAlignment(Qt.AlignCenter)
            empty.addWidget(icon)
            empty.addWidget(self._label('还没有生成记录', centered=True))
            empty.addWidget(self._label('去音频库，查看以前保存的配音。', muted=True, centered=True))
            rows.addLayout(empty)
        rows.addStretch()
        self.list_area.setWidget(content)
        root.addWidget(self.list_area, 1)

        divider = QFrame()
        divider.setObjectName('historyDivider')
        divider.setFixedHeight(1)
        root.addWidget(divider)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.library_button = self._button('音频库', self._open_library, 'layers')
        footer.addWidget(self.library_button)
        self.import_button = None
        if self.page._index_version == '2.5':
            self.import_button = self._button('导入音频调整', self._import_audio, 'plus')
            self.import_button.setToolTip('选择已有的 WAV 音频，重新制作其中一段。')
            footer.addWidget(self.import_button)
        footer.addStretch()
        self.close_button = self._button('关闭', self.reject, role='secondary')
        self.close_button.setMinimumWidth(76)
        footer.addWidget(self.close_button)
        root.addLayout(footer)
        self.resize(680, min(650, max(340, 200 + len(self.record_rows) * 96)))
        self.close_button.setFocus(Qt.OtherFocusReason)

    def _label(self, text, *, muted=False, centered=False):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setProperty('muted', muted)
        if centered:
            label.setAlignment(Qt.AlignCenter)
            label.setWordWrap(True)
        return label

    def _button(self, text, callback, icon=None, role='tertiary'):
        button = adopt_button(QPushButton(text), role=role, accessible_name=text)
        button.setFixedHeight(36)
        button.setAutoDefault(False)
        button.setCursor(Qt.PointingHandCursor)
        if icon:
            button.setIcon(QIcon(icons.pixmap(icon, self.page._consumer.accent, 16)))
        button.clicked.connect(callback)
        return button

    def _record_row(self, record):
        title, kind, details = _record_labels(record)
        path = str(record.get('output_path') or '')
        available = bool(path and Path(path).is_file())
        current = path == self.page._indextts2_last_output
        row = QFrame()
        row.setObjectName('historyRow')
        row.setProperty('current', current)
        row.setFixedHeight(86)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)
        emblem = QLabel()
        emblem.setObjectName('historyIcon')
        emblem.setPixmap(icons.pixmap('waveform', self.page._consumer.accent, 22))
        emblem.setAlignment(Qt.AlignCenter)
        emblem.setFixedSize(40, 40)
        layout.addWidget(emblem)
        text = QVBoxLayout()
        text.setSpacing(5)
        name = ElideLabel(title)
        name.setTextFormat(Qt.PlainText)
        name.setObjectName('historyName')
        name.setToolTip(str(record.get('title') or title) + '\n' + path)
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text.addWidget(name)
        meta = ElideLabel(' · '.join(filter(None, (kind, details, '' if available else '文件已移动或删除'))))
        meta.setProperty('muted', True)
        meta.setToolTip(meta.text())
        text.addWidget(meta)
        layout.addLayout(text, 1)
        if current:
            badge = self._label('当前结果')
            badge.setObjectName('historyBadge')
            layout.addWidget(badge)
        view = self._button('查看结果', lambda: self._choose(record), role='secondary')
        view.setAccessibleName('查看结果：' + title)
        view.setToolTip('回到配音页查看和播放这份结果')
        view.setEnabled(available)
        layout.addWidget(view)
        reveal = self._button('', lambda: self.page._reveal_indextts2_output(record), 'folder')
        reveal.setFixedWidth(36)
        reveal.setStyleSheet('QPushButton {padding:0;}')
        reveal.setAccessibleName('在访达中显示：' + title)
        reveal.setToolTip('在访达中显示这份音频')
        reveal.setEnabled(available)
        layout.addWidget(reveal)
        return row

    def _choose(self, record):
        if not Path(record.get('output_path') or '').is_file():
            dialogs.alert(self, '找不到音频', '这份音频可能已移动或删除，请到音频库检查。')
            return
        self.page._select_indextts2_result(record)
        self.accept()

    def _open_library(self):
        self.accept()
        self.page._open_voice_library()

    def _import_audio(self):
        self.accept()
        self.workbench._open_finished_audio()
