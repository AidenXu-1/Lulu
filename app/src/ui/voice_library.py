"""Shared local voice manager for the existing dubbing engines."""
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import dialogs, icons
from .control_foundation import adopt_button, adopt_dialog, adopt_scroll_area
from .indextts2_settings import _dialog_style
from .widgets import ElideLabel
from ..core.synthesis import voices


def library_style(c):
    return _dialog_style(c) + f'''
        QLineEdit,QPlainTextEdit {{background:{c.surface};color:{c.ink};border:1px solid {c.line};
            border-radius:8px;padding:9px 12px;font-size:13px;}}
        QLineEdit:focus,QPlainTextEdit:focus {{border-color:{c.accent};}}
        QLabel#libraryTitle {{font-family:'Songti SC';font-size:21px;font-weight:600;}}
        QLabel#libraryName {{font-size:14px;font-weight:600;}}
        QLabel#libraryMeta {{font-size:11px;color:{c.ink_muted};}}
        QLabel#libraryBadge {{font-size:11px;color:{c.accent};}}
        QFrame#voiceLibraryRow {{background:{c.surface};border:1px solid {c.line};border-radius:12px;}}
        QFrame#voiceLibraryRow[current="true"] {{background:{c.accent_wash};border-color:{c.accent_hairline};}}
        QPushButton#libraryPlay {{border-radius:18px;padding:0;background:{c.accent_wash};border-color:transparent;}}
        QPushButton#libraryPlay:hover {{border-color:{c.accent};}}
        QPushButton#libraryText {{border:0;background:transparent;padding:0 6px;color:{c.ink_muted};}}
        QPushButton#libraryText:hover {{color:{c.accent};background:{c.accent_wash};}}
        QPushButton#libraryText:focus {{border:1px solid {c.accent};}}
    '''


class VoiceLibraryController(QObject):
    def __init__(self, page, combo, dialog):
        super().__init__(dialog)
        from . import voice_page as module
        self.page, self.combo, self.dialog = page, combo, dialog
        self.c = page._consumer
        self.playing_id = ''
        self.play_buttons = {}
        self.rows = {}
        self.player = module.QMediaPlayer(dialog)
        self.audio = module.QAudioOutput(dialog)
        self.player.setAudioOutput(self.audio)
        self.player.playbackStateChanged.connect(self.sync_playback)
        if hasattr(self.player, 'errorOccurred'):
            self.player.errorOccurred.connect(self.playback_error)
        dialog.finished.connect(lambda *_: self.player.stop())
        dialog.surface.setStyleSheet(library_style(self.c) + f'''
            QDialog#voiceLibraryModal {{background:transparent;border:0;}}
            QFrame#voiceLibrarySurface {{background:{self.c.surface};border:1px solid {self.c.line};border-radius:18px;}}
            QLabel#voiceLibraryTitle {{font-family:'Songti SC';font-size:22px;font-weight:600;color:{self.c.ink};}}
            QLabel#voiceLibrarySub {{font-size:12px;color:{self.c.ink_muted};}}
        ''')
        lay = dialog.body_layout
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索音色')
        self.search.setAccessibleName('搜索音色')
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(38)
        lay.addWidget(self.search)
        toolbar = QHBoxLayout()
        self.count = self.label('', 'libraryMeta')
        toolbar.addWidget(self.count, 1)
        toolbar.addWidget(self.button('录制音色', lambda: self.child(self.add, True), icon='mic'))
        toolbar.addWidget(self.button('添加音色', lambda: self.child(self.add, False), primary=True, icon='plus'))
        lay.addLayout(toolbar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        adopt_scroll_area(self.scroll)
        self.body = QWidget()
        self.list_layout = QVBoxLayout(self.body)
        self.list_layout.setContentsMargins(0, 2, 0, 2)
        self.list_layout.setSpacing(8)
        self.scroll.setWidget(self.body)
        lay.addWidget(self.scroll, 1)
        self.error = self.label('', 'libraryMeta')
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)
        footer = QHBoxLayout()
        location = self.button('音色库位置', lambda: self.child(self.location), icon='folder')
        location.setObjectName('libraryText')
        footer.addWidget(location)
        footer.addStretch()
        footer.addWidget(self.button('完成', dialog.accept))
        lay.addLayout(footer)
        dialog.search_field, dialog.voice_scroll = self.search, self.scroll
        self.search.textChanged.connect(self.rebuild)
        self.rebuild()

    def label(self, text, name='libraryMeta'):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setObjectName(name)
        return label

    def button(self, text, callback, primary=False, icon=None):
        button = QPushButton(text)
        adopt_button(button, role='primary' if primary else 'secondary', accessible_name=text)
        button.setFixedHeight(34)
        button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        button.setAutoDefault(False)
        if icon:
            button.setIcon(QIcon(icons.pixmap(icon, 'white' if primary else self.c.accent, 15)))
        button.clicked.connect(callback)
        return button

    def child(self, callback, *args):
        # A file picker or edit sheet must not dismiss the manager underneath it.
        self.player.stop()
        self.dialog.setProperty('childWorkflow', True)
        try:
            return callback(*args)
        finally:
            self.dialog.setProperty('childWorkflow', False)

    def rebuild(self, *_):
        self.player.stop()
        self.playing_id = ''
        self.play_buttons.clear()
        self.rows.clear()
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        all_voices = voices.list_voices()
        query = self.search.text().strip().casefold()
        shown = [v for v in all_voices if query in v.name.casefold()]
        self.count.setText(f'{len(shown)} 个音色' if not query else f'找到 {len(shown)} 个音色')
        if not shown:
            empty = QFrame()
            box = QVBoxLayout(empty)
            box.setContentsMargins(16, 24, 16, 24)
            title = self.label('没有找到相关音色' if all_voices else '添加你的第一个音色', 'libraryName')
            title.setAlignment(Qt.AlignCenter)
            box.addWidget(title)
            hint = self.label('试试其他名称，或清空搜索' if all_voices else '导入一段录音，或直接录制')
            hint.setAlignment(Qt.AlignCenter)
            hint.setWordWrap(True)
            box.addWidget(hint)
            self.list_layout.addWidget(empty)
        selected = str(self.combo.currentData() or '')
        is_index = self.combo is self.page.indextts2_voice_combo
        for voice in shown:
            available = Path(voice.reference_audio_path).is_file()
            current = voice.id == selected
            row = QFrame()
            row.setObjectName('voiceLibraryRow')
            row.setProperty('current', current)
            rh = QHBoxLayout(row)
            rh.setContentsMargins(12, 12, 12, 12)
            rh.setSpacing(10)
            play = self.button('', lambda _=False, v=voice: self.play(v))
            play.setObjectName('libraryPlay')
            play.setFixedSize(36, 36)
            play.setEnabled(available)
            self.play_buttons[voice.id] = (play, voice.name)
            rh.addWidget(play)
            words = QVBoxLayout()
            words.setSpacing(5)
            name = ElideLabel(voice.name or '未命名音色')
            name.setObjectName('libraryName')
            name.setToolTip(voice.name)
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            words.addWidget(name)
            status = ('参考音频缺失' if not available else
                      '参考文字已保存' if voice.reference_text.strip() else
                      '可直接配音' if is_index else '完整克隆需补充参考文字')
            meta = ElideLabel(('当前使用 · ' if current else '') + status)
            meta.setObjectName('libraryBadge' if current else 'libraryMeta')
            meta.setToolTip(meta.text())
            meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            words.addWidget(meta)
            rh.addLayout(words, 1)
            edit = self.button('编辑', lambda _=False, v=voice: self.child(self.edit, v))
            edit.setAccessibleName(f'编辑音色 {voice.name}')
            edit.setObjectName('libraryText')
            rh.addWidget(edit)
            use = self.button('使用中' if current else '使用', lambda _=False, vid=voice.id: self.use(vid))
            use.setAccessibleName(f'使用音色 {voice.name}')
            use.setEnabled(available and not current)
            rh.addWidget(use)
            self.rows[voice.id] = row
            self.list_layout.addWidget(row)
        self.list_layout.addStretch()
        self.sync_playback()

    def play(self, voice):
        if not Path(voice.reference_audio_path).is_file():
            self.rebuild()
            return
        if self.playing_id == voice.id and self.player.playbackState() == type(self.player).PlayingState:
            self.player.pause()
            return
        for workbench in (getattr(self.page, '_approved_index', None), getattr(self.page, '_approved_vox', None)):
            if workbench:
                workbench.voice_player.pause()
                for player in getattr(workbench, 'players', ()):
                    player.pause()
        for player in (self.page.player, self.page.indextts2_player):
            player.pause()
        if self.playing_id != voice.id:
            self.player.stop()
            self.playing_id = voice.id
            self.player.setSource(QUrl.fromLocalFile(voice.reference_audio_path))
        elif self.player.position() >= self.player.duration() > 0:
            self.player.setPosition(0)
        self.error.hide()
        self.player.play()
        self.sync_playback()

    def sync_playback(self, *_):
        for vid, (button, name) in self.play_buttons.items():
            playing = vid == self.playing_id and self.player.playbackState() == type(self.player).PlayingState
            button.setIcon(QIcon(icons.pixmap('pause' if playing else 'play', self.c.accent, 16)))
            button.setAccessibleName(('暂停' if playing else '试听') + f'音色 {name}')
            button.setToolTip('暂停试听' if playing else '试听原音')

    def playback_error(self, *_):
        self.player.stop()
        self.error.setText('这份参考音频暂时无法播放，请检查文件是否可用。')
        self.error.show()

    def refresh_page(self):
        self.page.refresh()
        self.page._approved_vox.sync_voice()
        self.page._approved_index.sync_voice()

    def use(self, voice_id):
        voice = voices.get_voice(voice_id)
        if not voice or not Path(voice.reference_audio_path).is_file():
            self.rebuild()
            return
        self.refresh_page()
        index = self.combo.findData(voice_id)
        if index >= 0:
            self.combo.setCurrentIndex(index)
            self.page._update_voice_selector(self.combo)
            self.dialog.accept()

    def add(self, record):
        previous = {voice.id for voice in voices.list_voices()}
        if record:
            self.page._record_voice_sample(self.combo)
        else:
            self.page._new_voice(self.combo)
        if any(voice.id not in previous for voice in voices.list_voices()):
            self.search.clear()
        self.refresh_page()
        self.rebuild()

    def sheet(self, title):
        d = QDialog(self.dialog)
        adopt_dialog(d, accessible_name=title)
        d.setWindowTitle(title)
        d.setStyleSheet(library_style(self.c))
        d.resize(min(540, max(360, self.dialog.width() - 32)), 410)
        layout = QVBoxLayout(d)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        layout.addWidget(self.label(title, 'libraryTitle'))
        return d, layout

    def edit(self, voice):
        d, layout = self.sheet('编辑音色')
        layout.addWidget(self.label('音色名称'))
        name = QLineEdit(voice.name)
        name.setAccessibleName('音色名称')
        layout.addWidget(name)
        reference_row = QHBoxLayout()
        reference_row.addWidget(self.label('参考文字'), 1)
        preview = self.button('试听原音', lambda: self.play(voice), icon='play')
        preview.setEnabled(Path(voice.reference_audio_path).is_file())
        reference_row.addWidget(preview)
        layout.addLayout(reference_row)
        text = QPlainTextEdit(voice.reference_text)
        text.setAccessibleName('参考文字')
        text.setPlaceholderText('填写录音中实际说的话；Vox 完整克隆需要此项')
        text.setTabChangesFocus(True)
        layout.addWidget(text, 1)
        error = self.label('')
        error.setWordWrap(True)
        layout.addWidget(error)
        row = QHBoxLayout()
        delete = self.button('删除音色', lambda: remove())
        delete.setObjectName('libraryText')
        row.addWidget(delete)
        row.addStretch()
        row.addWidget(self.button('取消', d.reject))
        def save():
            if voice.reference_text.strip() and not text.toPlainText().strip():
                error.setText('已有参考文字不能清空；可校正为录音中的内容。')
                return
            try:
                voices.update_voice(voice.id, name=name.text(), reference_text=text.toPlainText())
            except (ValueError, OSError) as exc:
                error.setText(str(exc))
                return
            self.refresh_page()
            self.rebuild()
            d.accept()
        def remove():
            if not dialogs.confirm(d, '删除音色', f'删除「{voice.name}」及其参考录音和文字？已生成的配音会保留。', ok='删除'):
                return
            try:
                voices.delete_voice(voice.id)
            except (ValueError, OSError) as exc:
                error.setText(str(exc))
                return
            self.refresh_page()
            self.rebuild()
            d.accept()
        row.addWidget(self.button('保存', save, primary=True))
        layout.addLayout(row)
        def sync_preview(*_):
            playing = self.player.playbackState() == type(self.player).PlayingState
            preview.setText('暂停原音' if playing else '试听原音')
            preview.setIcon(QIcon(icons.pixmap('pause' if playing else 'play', self.c.accent, 15)))
        self.player.playbackStateChanged.connect(sync_preview)
        try:
            d.exec()
        finally:
            self.player.stop()
            self.player.playbackStateChanged.disconnect(sync_preview)
        d.deleteLater()

    def location(self):
        d, layout = self.sheet('音色库位置')
        path = self.label(str(voices.current_voice_library_dir()))
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(path)
        layout.addWidget(self.label('录音和参考文字保存在这个文件夹'))
        layout.addStretch()
        row = QHBoxLayout()
        row.addWidget(self.button('在 Finder 打开', self.page._open_voice_library_dir, icon='folder'))
        def change():
            if self.page._change_voice_library_dir():
                path.setText(str(voices.current_voice_library_dir()))
                self.refresh_page()
                self.rebuild()
        row.addWidget(self.button('更改位置', change))
        layout.addLayout(row)
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_row.addWidget(self.button('完成', d.accept))
        layout.addLayout(close_row)
        d.resize(d.width(), 250)
        d.exec()
        d.deleteLater()
