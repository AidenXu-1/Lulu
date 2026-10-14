"""The approved document-first IndexTTS layout, reusing existing business actions."""
import math
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox, QDialog, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget, QStackedWidget,
)

from . import dialogs, icons
from .consumer_select import ConsumerSelect
from .control_foundation import LuluCheckBox, adopt_button
from .voice_wave_slider import VoiceWaveSlider
from . import voice_result_style as result_style
from .widgets import ElideLabel


class _ReferenceDrop(QWidget):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.setAcceptDrops(True)
        self.setAccessibleName("情绪参考音频，可拖入本机文件")

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.callback(urls[0].toLocalFile())
            event.acceptProposedAction()


class _FollowTexture(QWidget):
    """Quiet decorative motif, never an audio preview or a seek target."""

    def __init__(self, accent, parent=None):
        super().__init__(parent)
        self.accent = accent
        self.setMinimumWidth(40)
        self.setFixedHeight(46)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        count = max(2, self.width() // 6)
        for i in range(count):
            position = i / (count - 1)
            height = 4 + 28 * abs(math.sin(i * .71) * math.cos(i * .19))
            color = QColor(self.accent)
            color.setAlpha(round(60 * min(1, position * 5, (1 - position) * 5)))
            painter.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap))
            x = round(3 + position * (self.width() - 6))
            painter.drawLine(x, round((46 - height) / 2), x, round((46 + height) / 2))


class IndexTTS25Workbench(QWidget):
    """Owns presentation only; VoicePage keeps generation, storage and cancellation."""

    def __init__(self, page):
        super().__init__(page)
        from . import voice_page as module
        self.page = page
        self._voice_path = ""
        self._reference_path = ""
        self.players = []
        self.audio_outputs = []
        self.random_emotion = False
        self._settings_busy = False
        self._media_factory = module
        self.setObjectName("indexApprovedWorkbench")
        c = page._consumer
        self.setStyleSheet("""
            QFrame#indexCard {background:SURFACE;border:1px solid LINE;border-radius:13px;}
            QLabel#indexTitle {font-family:'Songti SC';font-size:18px;font-weight:600;}
            QLabel#indexHint {color:MUTED;font-size:11px;}
            QFrame#indexFollow {background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 WASH,stop:1 SURFACE);border:1px solid LINE;border-radius:10px;}
            QFrame#indexFollowEmblem {background:SURFACE;border:1px solid LINE;border-radius:23px;}
            QPushButton#indexRound {background:ACCENT;border:0;border-radius:22px;color:white;}
        """.replace("SURFACE", c.surface).replace("LINE", c.line).replace("MUTED", c.ink_muted).replace("WASH", c.accent_wash).replace("ACCENT", c.accent))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addWidget(self._editor())
        root.addWidget(self._sound())
        root.addWidget(self._emotion())
        root.addWidget(self._actions())
        root.addWidget(self._result())
        QWidget.setTabOrder(page.btn_indextts2_export, page.btn_indextts2_output_dir)
        QWidget.setTabOrder(self.emotion_settings_button, self.advanced_settings_button)
        QWidget.setTabOrder(self.advanced_settings_button, page.btn_indextts2_preview)
        QWidget.setTabOrder(page.btn_indextts2_preview, page.btn_indextts2_generate)
        self.players.append(page.indextts2_player)
        for player in self.players:
            player.playbackStateChanged.connect(lambda state, own=player: self._exclusive(own, state))
        self.sync_voice()
        self.sync_reference()
        self.sync_mode()
        self.sync_text()
        # A compact cancellation dock remains reachable even when a long
        # document or the result is scrolled out of view.
        self.busy_dock = QWidget(page)
        busy_row = QHBoxLayout(self.busy_dock)
        busy_row.setContentsMargins(24, 6, 24, 6)
        busy_row.addWidget(self._label("正在本机生成，文案和上次结果会保留"), 1)
        self.busy_cancel = self._button("取消生成", page.btn_indextts2_cancel.click)
        busy_row.addWidget(self.busy_cancel)
        page.layout().addWidget(self.busy_dock)
        self.busy_dock.hide()

    def _card(self):
        card = QFrame()
        card.setObjectName("indexCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(9)
        return card, layout

    def _label(self, text, *, title=False):
        label = QLabel(text)
        label.setObjectName("indexTitle" if title else "indexHint")
        return label

    def _button(self, text, callback, icon=None):
        button = QPushButton(text)
        adopt_button(button, role="secondary", accessible_name=text)
        button.setFixedHeight(36)
        if icon:
            button.setIcon(QIcon(icons.pixmap(icon, self.page._consumer.accent, 15)))
        else:
            button.setIcon(QIcon())
        button.setStyleSheet("QPushButton {text-align:center;padding-left:10px;padding-right:10px;}")
        button.clicked.connect(callback)
        return button

    def _editor(self):
        p = self.page
        st = p._src["indextts2"]
        card, layout = self._card()
        bar = QHBoxLayout()
        st["buttons"]["library"].setText("文稿库")
        bar.addWidget(st["segment"])
        self.selection_hint = ElideLabel("")
        self.selection_hint.setObjectName("indexHint")
        self.selection_hint.setAlignment(Qt.AlignCenter)
        self.selection_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bar.addWidget(self.selection_hint, 1)
        self.clear = self._button("清空", self._clear_text)
        # Keep the slot even when clear is hidden, so pronunciation never moves.
        clear_slot = QWidget()
        clear_slot.setFixedWidth(78)
        clear_layout = QHBoxLayout(clear_slot)
        clear_layout.setContentsMargins(0, 0, 0, 0)
        clear_layout.addWidget(self.clear)
        bar.addWidget(clear_slot)
        bar.addWidget(p.btn_indextts2_glossary)
        p.btn_indextts2_glossary.setFixedHeight(36)
        self.view_text = self._button("查看原文", self._view_text)
        bar.addWidget(self.view_text)
        st["picked_view"].hide()
        layout.addLayout(bar)
        for key in ("src_stack", "text_edit", "drop_area", "picked_card", "pick_empty"):
            st[key].setFixedHeight(152)
        c = p._consumer
        p.indextts2_text_edit.setStyleSheet(f"QPlainTextEdit{{font-size:15px;padding:9px 12px;border:1px solid {c.line};border-radius:9px;background:{c.surface};}}")
        layout.addWidget(st["src_stack"])
        p.indextts2_text_length_hint.hide()
        p.indextts2_text_edit.selectionChanged.connect(self.sync_text)
        p.indextts2_text_edit.textChanged.connect(self.sync_text)
        for button in st["buttons"].values():
            button.clicked.connect(self.sync_text)
        return card

    def _clear_text(self):
        if dialogs.confirm(self.page, "清空文案", "清空本次输入？原始文稿和文件不会被删除。"):
            self.page.indextts2_text_edit.clear()

    def _view_text(self):
        dlg = QDialog(self.page)
        dlg.setWindowTitle("查看配音原文")
        dlg.resize(760, 560)
        layout = QVBoxLayout(dlg)
        editor = QPlainTextEdit(self.page.indextts2_text_edit.toPlainText())
        editor.setReadOnly(True)
        layout.addWidget(editor)
        def preview_selection():
            cursor = editor.textCursor()
            source_cursor = self.page.indextts2_text_edit.textCursor()
            source_cursor.setPosition(cursor.selectionStart())
            source_cursor.setPosition(cursor.selectionEnd(), cursor.MoveMode.KeepAnchor)
            self.page.indextts2_text_edit.setTextCursor(source_cursor)
            dlg.accept()
            self.page.btn_indextts2_preview.click()
        preview = self._button("试听选段", preview_selection, "play")
        preview.setEnabled(False)
        editor.selectionChanged.connect(lambda: preview.setEnabled(editor.textCursor().hasSelection() and not self.page._indextts2_gate_reason()))
        layout.addWidget(preview)
        layout.addWidget(self._button("关闭", dlg.accept))
        dlg.exec()

    def sync_text(self):
        p = self.page
        custom = p._src["indextts2"]["mode"] == "custom"
        self.clear.setVisible(custom)
        self.clear.setEnabled(bool(p.indextts2_text_edit.toPlainText()))
        selected = p.indextts2_text_edit.textCursor().selectedText().strip()
        from ..core.synthesis.indextts2_text_guard import count_text_chars
        count = count_text_chars(p.indextts2_text_edit.toPlainText())
        message = f"配音正文 {count} 字 · " + (f"已选 {count_text_chars(selected)} 字" if selected else "选中文字可试听")
        self.selection_hint.setText(message)
        self.selection_hint.setToolTip(message)
        self.view_text.setEnabled(count > 0)
        p._update_indextts2_generate_state()

    def _new_player(self, label):
        player = self._media_factory.QMediaPlayer(self.page)
        output = self._media_factory.QAudioOutput(self.page)
        self.audio_outputs.append(output)
        player.setAudioOutput(output)
        self.players.append(player)
        button = self._button("", lambda: player.pause() if player.playbackState() == type(player).PlayingState else player.play())
        button.setObjectName("indexRound")
        button.setAccessibleName(label)
        button.setFixedSize(44, 44)
        wave = VoiceWaveSlider(self)
        wave.setAccessibleName(label + "进度")
        clock = self._label("0:00 / 0:00")
        def sync(state=None):
            icon = "pause" if player.playbackState() == type(player).PlayingState else "play"
            button.setIcon(QIcon(icons.pixmap(icon, "#ffffff", 18)))
        player.durationChanged.connect(lambda duration: wave.setRange(0, duration))
        player.durationChanged.connect(lambda duration: clock.setText(f"{self._media_factory._time_label(player.position())} / {self._media_factory._time_label(duration)}"))
        player.positionChanged.connect(lambda position: wave.setValue(position) if not wave.isSliderDown() else None)
        player.positionChanged.connect(lambda position: clock.setText(f"{self._media_factory._time_label(position)} / {self._media_factory._time_label(player.duration())}"))
        player.playbackStateChanged.connect(sync)
        if hasattr(player, "errorOccurred"):
            player.errorOccurred.connect(lambda *_: clock.setText("无法播放此音频"))
        wave.seekRequested.connect(player.setPosition)
        wave.setSingleStep(1000)
        wave.setPageStep(5000)
        sync()
        return player, button, wave, clock

    def _exclusive(self, own, state):
        if state == type(own).PlayingState:
            for player in self.players:
                if player is not own and player.playbackState() == type(player).PlayingState:
                    player.pause()

    def _sound(self):
        p = self.page
        card, layout = self._card()
        self.sound_grid = QGridLayout()
        self.sound_grid.setHorizontalSpacing(20)
        voice = QWidget()
        vl = QVBoxLayout(voice)
        vl.setContentsMargins(0, 0, 0, 0)
        self.voice_label = self._label("我的音色")
        vl.addWidget(self.voice_label)
        vr = QHBoxLayout()
        self.voice_player, self.voice_play, self.voice_wave, self.voice_clock = self._new_player("试听所选音色")
        vr.addWidget(self.voice_play)
        refs = p._voice_sel[id(p.indextts2_voice_combo)]
        refs["select_host"].setFixedWidth(205)
        vr.addWidget(refs["select_host"])
        vr.addWidget(self.voice_wave, 1)
        vr.addWidget(self.voice_clock)
        vl.addLayout(vr)
        language = QWidget()
        ll = QVBoxLayout(language)
        ll.setContentsMargins(0, 0, 0, 0)
        self.language_label = self._label("朗读语言")
        self.language_label.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.language_label)
        combo = p.indextts2_language_combo
        choices = [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]
        self.language_select = ConsumerSelect(choices, value=combo.currentData(), accessible_name="朗读语言", consumer_theme_scope="coreWorkspace", paper_popup=True)
        self.language_select.setMinimumWidth(140)
        self.language_select.setFixedHeight(44)
        self.language_select.value_changed.connect(lambda value: combo.setCurrentIndex(combo.findData(value)))
        combo.currentIndexChanged.connect(lambda _index: self.language_select.set_value(combo.currentData(), emit=False))
        self.language_select.setToolTip("按所选语言朗读，不会翻译文案")
        ll.addWidget(self.language_select)
        speed = QWidget()
        sl = QVBoxLayout(speed)
        sl.setContentsMargins(0, 0, 0, 0)
        self.speed_label = self._label("语速")
        self.speed_label.setAlignment(Qt.AlignCenter)
        sl.addWidget(self.speed_label)
        sr = QHBoxLayout()
        p.indextts2_speed_slider.setRange(50, 200)
        sr.addWidget(p.indextts2_speed_slider, 1)
        self.speed_value = QDoubleSpinBox()
        self.speed_value.setRange(.5, 2)
        self.speed_value.setDecimals(2)
        self.speed_value.setSingleStep(.05)
        self.speed_value.setSuffix(" ×")
        self.speed_value.setValue(1)
        self.speed_value.setFixedWidth(86)
        self.speed_value.setFixedHeight(44)
        self.speed_value.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.speed_value.setAlignment(Qt.AlignCenter)
        self.speed_value.setStyleSheet(f"QDoubleSpinBox{{border:1px solid {p._consumer.line};border-radius:8px;padding:4px;background:{p._consumer.surface};}}")
        self.speed_value.setAccessibleName("语速倍数")
        self.speed_value.valueChanged.connect(lambda value: p.indextts2_speed_slider.setValue(round(value * 100)))
        p.indextts2_speed_slider.valueChanged.connect(lambda value: self.speed_value.setValue(value / 100))
        sr.addWidget(self.speed_value)
        sl.addLayout(sr)
        for label in (self.voice_label, self.language_label, self.speed_label):
            label.setFixedHeight(20)
        for column in (vl, ll, sl):
            column.setSpacing(8)
        for row in (vr, sr):
            row.setContentsMargins(0, 0, 0, 0)
        # All three labels and control rows share the same baseline.
        for cell in (voice, language, speed):
            cell.setFixedHeight(80)
        self.sound_cells = (voice, language, speed)
        for i, cell in enumerate(self.sound_cells):
            self.sound_grid.addWidget(cell, 0, i)
        self.sound_grid.setColumnStretch(0, 1)
        language.setFixedWidth(146)
        speed.setFixedWidth(220)
        layout.addLayout(self.sound_grid)
        return card

    def _emotion(self):
        p = self.page
        card, layout = self._card()
        self.emotion_card = card
        head = self.emotion_head = QGridLayout()
        self.emotion_title = self._label("• 情绪表达", title=True)
        head.addWidget(self.emotion_title, 0, 0)
        self.emotion_hint = ElideLabel("")
        self.emotion_hint.setObjectName("indexHint")
        self.emotion_hint.setAlignment(Qt.AlignCenter)
        self.emotion_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        head.addWidget(self.emotion_hint, 0, 1)
        head.setColumnStretch(1, 1)
        self.emotion_tabs = QWidget()
        tabs = QHBoxLayout(self.emotion_tabs)
        tabs.setContentsMargins(0, 0, 0, 0)
        tabs.setSpacing(3)
        for key, name in (("follow", "跟随音色"), ("emoaudio", "参考音频"), ("vector", "精细调节"), ("emotext", "文字描述")):
            button = p.indextts2_emotion_mode_buttons[key]
            button.setText(name)
            button.setFixedSize(78, 34)
            tabs.addWidget(button)
        head.addWidget(self.emotion_tabs, 0, 2)
        p.indextts2_emotion_mode_buttons["template"].hide()
        layout.addLayout(head)
        body = self.emotion_body = QGridLayout()
        body.setVerticalSpacing(6)
        stack = p.indextts2_emotion_stack
        stack.setFixedHeight(108)
        # Replace presentation of follow/reference/vector while preserving all
        # engine input objects and the legacy template data for traceability.
        follow = QFrame()
        follow.setObjectName("indexFollow")
        fl = QHBoxLayout(follow)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(14)
        emblem = QFrame()
        emblem.setObjectName("indexFollowEmblem")
        emblem.setFixedSize(46, 46)
        emblem_layout = QHBoxLayout(emblem)
        emblem_layout.setContentsMargins(0, 0, 0, 0)
        emblem_layout.addWidget(icons.icon_label("waveform", p._consumer.accent, 26), 0, Qt.AlignCenter)
        fl.addWidget(emblem)
        copy = QVBoxLayout()
        copy.setSpacing(6)
        title = QLabel("跟随原声，自然表达")
        title.setStyleSheet("font-size:16px;font-weight:600")
        copy.addWidget(title)
        self.follow_name = ElideLabel("")
        self.follow_name.setObjectName("indexHint")
        self.follow_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        copy.addWidget(self.follow_name)
        fl.addLayout(copy, 1)
        fl.setAlignment(copy, Qt.AlignVCenter)
        self.follow_texture = _FollowTexture(p._consumer.accent)
        fl.addWidget(self.follow_texture, 2)
        self.follow_play = self._button("试听原音", lambda: self.voice_play.click(), "play")
        fl.addWidget(self.follow_play)
        def sync_follow_play(state=None):
            playing = self.voice_player.playbackState() == type(self.voice_player).PlayingState
            self.follow_play.setText("暂停原音" if playing else "试听原音")
            self.follow_play.setIcon(QIcon(icons.pixmap("pause" if playing else "play", p._consumer.accent, 16)))
        self.voice_player.playbackStateChanged.connect(sync_follow_play)
        reference = _ReferenceDrop(p._load_indextts2_emotion_audio)
        rl = QHBoxLayout(reference)
        rl.setContentsMargins(0, 0, 0, 0)
        self.ref_player, self.ref_play, self.ref_wave, self.ref_clock = self._new_player("试听情绪参考")
        rl.addWidget(self.ref_play)
        self.ref_name = ElideLabel("拖入一段情绪参考音频")
        self.ref_name.setFixedWidth(150)
        rl.addWidget(self.ref_name)
        rl.addWidget(self.ref_wave, 1)
        rl.addWidget(self.ref_clock)
        self.ref_pick = self._button("选择音频", p._pick_indextts2_emotion_audio, "folder")
        rl.addWidget(self.ref_pick)
        self.ref_record = self._button("录制参考", p._record_indextts2_emotion_audio, "mic")
        rl.addWidget(self.ref_record)
        self.ref_remove = self._button("移除", p._clear_indextts2_emotion_audio)
        rl.addWidget(self.ref_remove)
        vector = QWidget()
        gl = QGridLayout(vector)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setHorizontalSpacing(18)
        for i, slider in enumerate(p.indextts2_advanced_sliders):
            cell = QVBoxLayout()
            labels = QHBoxLayout()
            labels.addWidget(self._label(self._media_factory.VECTOR_DISPLAY_LABELS.get(self._media_factory.EMOTION_VECTOR_LABELS[i], self._media_factory.EMOTION_VECTOR_LABELS[i])))
            labels.addStretch(1)
            labels.addWidget(p.indextts2_advanced_value_labels[i])
            cell.addLayout(labels)
            cell.addWidget(slider)
            gl.addLayout(cell, i // 4, i % 4)
        for index, replacement in ((0, follow), (1, reference), (3, vector)):
            previous = stack.widget(index)
            stack.removeWidget(previous)
            previous.setParent(p)
            previous.hide()
            stack.insertWidget(index, replacement)
        body.addWidget(stack, 0, 0)
        body.setColumnStretch(0, 1)
        self.strength = QWidget()
        self.strength.setFixedWidth(205)
        strength = self.strength_grid = QGridLayout(self.strength)
        strength.setContentsMargins(14, 8, 0, 8)
        strength.setSpacing(4)
        self.strength_label = self._label("表达强度")
        strength.addWidget(self.strength_label, 0, 0)
        p.btn_indextts2_clear_vector.setFixedHeight(24)
        strength.addWidget(p.btn_indextts2_clear_vector, 0, 1, Qt.AlignRight)
        p.indextts2_weight_slider.setRange(0, 100)
        strength.addWidget(p.indextts2_weight_slider, 1, 0, 1, 2)
        strength.addWidget(p.indextts2_weight_value, 2, 1, Qt.AlignRight)
        body.addWidget(self.strength, 0, 1)
        layout.addLayout(body)
        p.indextts2_emotion_text_edit.setPlaceholderText("例如：温柔、带一点期待。")
        return card

    def sync_mode(self):
        p = self.page
        mode = p._indextts2_emotion_mode
        if mode not in ("follow", "emoaudio", "vector", "emotext"):
            mode = "vector" if mode == "template" else "follow"
            p._indextts2_emotion_mode = mode
        p.indextts2_emotion_stack.setCurrentIndex({"follow": 0, "emoaudio": 1, "vector": 3, "emotext": 4}[mode])
        p.indextts2_emotion_mode_buttons[mode].setChecked(True)
        narrow = bool(self.property("narrow"))
        self.strength.setVisible(mode != "follow")
        self.emotion_body.removeWidget(p.indextts2_emotion_stack)
        self.emotion_body.addWidget(p.indextts2_emotion_stack, 0, 0, 1, 2 if narrow or mode == "follow" else 1)
        p.indextts2_emotion_stack.setFixedHeight(158 if narrow and mode == "follow" else 108)
        for child in self.strength.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
            child.setVisible(mode != "follow")
        p.btn_indextts2_clear_vector.setVisible(mode == "vector")
        hint = {"follow": "保留原声的语气", "emoaudio": "模仿另一段录音的语气", "vector": "调高想要的情绪，原声也会影响效果", "emotext": "写一句想要的语气（实验）"}.get(mode, "")
        if hasattr(self, 'emotion_settings_button'):
            self.sync_emotion_settings()
        self.emotion_hint.setText(hint)
        self.emotion_hint.setToolTip(hint)

    def sync_voice(self):
        voice = self.page._selected_indextts2_voice()
        path = str(voice.reference_audio_path) if voice else ""
        self.voice_play.setEnabled(bool(path and Path(path).is_file()))
        self.follow_play.setEnabled(self.voice_play.isEnabled())
        self.follow_name.setText(f"情绪来自 {voice.name}" if voice else "请先选择或添加音色")
        self.follow_name.setToolTip(self.follow_name.text())
        if path != self._voice_path:
            self._voice_path = path
            self.voice_player.stop()
            self.voice_player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
            self.voice_wave.load_file(path)

    def sync_reference(self):
        path = self.page._indextts2_emotion_audio_path
        present = bool(path)
        self.ref_name.setText(Path(path).name if present else "拖入一段情绪参考音频")
        self.ref_name.setToolTip(path)
        self.ref_pick.setText("更换音频" if present else "选择音频")
        self.ref_remove.setVisible(present)
        self.ref_record.setVisible(not present)
        self.ref_play.setVisible(present)
        self.ref_clock.setVisible(present)
        if path != self._reference_path:
            self._reference_path = path
            self.ref_player.stop()
            self.ref_player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
            self.ref_wave.load_file(path)

    def _actions(self):
        p = self.page
        wrap = QWidget()
        # Let the page shrink before reflow moves the desktop actions to two rows.
        wrap.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        self.actions_grid = QGridLayout()
        self.actions_grid.setSpacing(8)
        directory = QWidget()
        dr = QHBoxLayout(directory)
        dr.setContentsMargins(0, 0, 0, 0)
        dr.setSpacing(4)
        p.btn_indextts2_output_dir.setMaximumWidth(160)
        p.btn_indextts2_output_dir.setMinimumWidth(90)
        self.emotion_settings_button = self._button("情绪自由", self._emotion_settings)
        self.emotion_settings_button.setFixedSize(148, 38)
        dr.addWidget(self.emotion_settings_button)
        dr.addStretch(1)
        self.sync_emotion_settings()
        p.btn_indextts2_preview.setText("试听选段")
        p.btn_indextts2_preview.setToolTip("选中 200 字以内先试听，使用当前设置，不自动入库。全文听感可能略有差别。")
        p.btn_indextts2_preview.setAccessibleName("试听选中文字")
        p.btn_indextts2_generate.setText("生成全文")
        preview_slot = QWidget()
        preview_row = QHBoxLayout(preview_slot)
        preview_row.setContentsMargins(0, 0, 0, 0)
        preview_row.addWidget(p.btn_indextts2_preview)
        preview_row.addWidget(p.btn_indextts2_cancel)
        self.advanced_settings_button = self._button("高级设置", self._generation_settings)
        self.action_cells = (self._button("音色管理", lambda: p._open_voice_picker(p.indextts2_voice_combo), "layers"),
                             p.btn_indextts2_release_bg, directory, self.advanced_settings_button,
                             preview_slot, p.btn_indextts2_generate)
        self.advanced_settings_button.setFixedWidth(148)
        self.advanced_settings_button.installEventFilter(self)
        self.sync_settings()
        self._settings_mouse = False
        p.btn_indextts2_output_dir.setFixedHeight(38)
        for cell in self.action_cells:
            cell.setFixedHeight(38)
        for cell in self.action_cells[:1]:
            cell.setMaximumWidth(112)
        for button in (p.btn_indextts2_release_bg, p.btn_indextts2_preview, p.btn_indextts2_cancel, p.btn_indextts2_generate):
            button.setFixedHeight(38)
            button.setFixedWidth(108 if button is not p.btn_indextts2_generate else 126)
        for i, widget in enumerate(self.action_cells):
            self.actions_grid.addWidget(widget, 0, i)
        self.actions_grid.setColumnStretch(2, 1)
        layout.addLayout(self.actions_grid)
        self.status_stack = QStackedWidget()
        self.status_stack.setFixedHeight(26)
        self.status_stack.addWidget(p.indextts2_status)
        self.status_stack.addWidget(p.indextts2_gate_hint)
        p.indextts2_gate_hint.setStyleSheet("QLabel {border:0;background:transparent;padding:0;}")
        p.indextts2_gate_hint.setMinimumWidth(0)
        layout.addWidget(self.status_stack)
        layout.addWidget(p.indextts2_progress)
        return wrap

    def _result(self):
        p = self.page
        card, layout = self._card()
        result_style.card_style(card,layout,p._consumer)
        head = QHBoxLayout()
        head.addWidget(self._label("本次结果", title=True))
        self.studio_button = self._button('局部调整', self._segment_studio)
        self.studio_button.setToolTip('调整当前成品中的一段，原版保留。')
        self.studio_button.hide()

        self.history_button = self._button("历史记录", self._history)
        head.addStretch()
        self.history_button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        head.addWidget(self.history_button)
        result_style.action_style(self.history_button,p._consumer)
        layout.addLayout(head)
        p.indextts2_result_empty.setFixedHeight(44)
        def clear_layout(old):
            while old.count():
                item = old.takeAt(0)
                if item.widget():
                    item.widget().hide()
                elif item.layout():
                    clear_layout(item.layout())
        clear_layout(p.indextts2_result_empty.layout())
        self.empty_text = self._label(result_style.EMPTY_TEXT)
        p.indextts2_result_empty.layout().addWidget(self.empty_text)
        layout.addWidget(p.indextts2_result_empty)
        old_summary = p.indextts2_result_summary
        p.indextts2_result_summary = ElideLabel(old_summary.text())
        p.indextts2_result_summary.setToolTip(old_summary.text())
        p.indextts2_result_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        p.indextts2_result_summary.hide()
        old_summary.hide()
        bar = p.indextts2_player_bar
        # Preserve all owned player callbacks; replace just its progress control.
        previous = p.indextts2_player_slider
        wave = VoiceWaveSlider(self)
        wave.setAccessibleName("本次结果播放进度")
        p.indextts2_player_row.replaceWidget(previous, wave)
        previous.hide()
        p.indextts2_player_slider = wave
        wave.sliderPressed.connect(p._on_indextts2_slider_pressed)
        wave.sliderReleased.connect(p._on_indextts2_slider_released)
        wave.seekRequested.connect(p.indextts2_player.setPosition)
        wave.setSingleStep(1000)
        wave.setPageStep(5000)
        bar.setStyleSheet("QFrame {border:0;background:transparent;}")
        clear_layout(bar.layout())
        result_row = QHBoxLayout()
        play = p.btn_indextts2_play
        play.setObjectName("indexRound")
        play.setFixedSize(44, 44)
        play.setText("")
        play.setIcon(QIcon(icons.pixmap("play", "#ffffff", 18)))
        play.setAccessibleName("播放本次结果")
        result_row.addWidget(play)
        head.insertWidget(1, p.indextts2_result_summary, 1)
        result_row.addWidget(wave, 4)
        result_row.addWidget(p.indextts2_time_label)
        p.btn_indextts2_export.setText("下载音频")
        p.btn_indextts2_export.setAccessibleName("下载音频")
        p.btn_indextts2_export.setIcon(QIcon(icons.pixmap("download", p._consumer.accent, 16)))
        p.btn_indextts2_export.setFixedWidth(108)
        actions=QHBoxLayout();actions.setSpacing(10)
        actions.addWidget(self.studio_button);actions.addStretch()
        actions.addWidget(p.btn_indextts2_export);actions.addWidget(p.btn_indextts2_output_dir)
        result_style.action_style(self.studio_button,p._consumer)
        result_style.action_style(p.btn_indextts2_export,p._consumer,download=True)
        result_style.action_style(p.btn_indextts2_output_dir,p._consumer,location=True)
        p.btn_indextts2_output_dir.set_path(p.cfg.tts_output_dir)
        p.btn_indextts2_output_dir.setAccessibleName("音频下载位置")
        p.btn_indextts2_output_dir.setAccessibleDescription("选择下载副本的保存文件夹")
        bar.layout().setContentsMargins(0,0,0,0);bar.layout().setSpacing(14)
        result_row.setContentsMargins(0,0,0,0);result_row.setSpacing(12)
        bar.layout().addLayout(result_row);bar.layout().addLayout(actions)
        result_style.player_style(play,wave,p.indextts2_time_label,p._consumer)
        # Less frequent file/navigation actions remain available, without
        # competing with playback and export on the main result row.
        for button in (p.btn_indextts2_reveal, p.btn_indextts2_library):
            button.hide()
        for widget in (play, wave, p.indextts2_time_label, p.btn_indextts2_export, p.btn_indextts2_output_dir):
            widget.show()
        layout.addWidget(bar)
        return card

    def _history(self):
        from .voice_history import VoiceHistoryDialog
        dialog = VoiceHistoryDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def reflow(self):
        # Keep desktop proportions stable; a narrow window uses a second sound
        # row, without giving any of the emotion modes a different height.
        narrow = self.width() < 880
        toolbar_narrow = self.width() < 1050
        version = self.page._index_version
        if self.property("narrow") == narrow and self.property("toolbarNarrow") == toolbar_narrow and self.property("layoutVersion") == version:
            return
        self.setProperty("narrow", narrow)
        self.setProperty("toolbarNarrow", toolbar_narrow)
        self.setProperty("layoutVersion", version)
        self.follow_texture.setVisible(not narrow)
        for cell in self.action_cells:
            self.actions_grid.removeWidget(cell)
        for i, cell in enumerate(self.action_cells):
            if toolbar_narrow:
                self.actions_grid.addWidget(cell, i // 3, i % 3)
            else:
                self.actions_grid.addWidget(cell, 0, i)
        self.actions_grid.setColumnStretch(2, 1)
        self.actions_grid.setColumnStretch(3, 0)
        self.ref_name.setVisible(not narrow)
        for cell in self.sound_cells:
            self.sound_grid.removeWidget(cell)
        modern = version == "2.5"
        for cell in self.sound_cells[1:]:
            cell.setVisible(modern)
        if modern:
            for i, cell in enumerate(self.sound_cells):
                self.sound_grid.addWidget(cell, 0 if i == 0 or not narrow else 1, i if not narrow else max(0, i - 1), 1, 2 if narrow and i == 0 else 1)
        else:
            self.sound_grid.addWidget(self.sound_cells[0], 0, 0, 1, 3)
        self.voice_clock.setVisible(not narrow)
        self.emotion_head.removeWidget(self.emotion_tabs)
        self.emotion_head.addWidget(self.emotion_tabs, 1 if narrow else 0, 0 if narrow else 2, 1, 3 if narrow else 1, Qt.AlignRight)
        self.emotion_body.removeWidget(self.strength)
        self.emotion_body.addWidget(self.strength, 1 if narrow else 0, 0 if narrow else 1, 1, 2 if narrow else 1, Qt.AlignRight)
        self.strength.setFixedHeight(44 if narrow else 108)
        self.strength.setFixedWidth(440 if narrow else 205)
        controls = (self.strength_label, self.page.indextts2_weight_slider,
                    self.page.indextts2_weight_value, self.page.btn_indextts2_clear_vector)
        for widget in controls:
            self.strength_grid.removeWidget(widget)
        if narrow:
            for i, widget in enumerate(controls):
                self.strength_grid.addWidget(widget, 0, i)
        else:
            for widget, row, col, span in ((controls[0], 0, 0, 1), (controls[3], 0, 1, 1), (controls[1], 1, 0, 2), (controls[2], 2, 1, 1)):
                self.strength_grid.addWidget(widget, row, col, 1, span)
        self.sync_mode()

    def stop_playback(self):
        for player in self.players:
            player.stop()

    def shutdown(self):
        self.stop_playback()
        for slider in (self.voice_wave, self.ref_wave, self.page.indextts2_player_slider):
            slider.stop_decode()

    def _segment_studio(self):
        if self.page._index_version != '2.5' or self._settings_busy:
            return
        if not self.page._indextts2_last_output:
            return
        self._adjust_audio(self.page._indextts2_last_output)

    def _open_finished_audio(self):
        if self.page._index_version != '2.5' or self._settings_busy:
            return
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, '打开需要调整的成品',
            self.page.cfg.tts_output_dir, 'WAV 成品 (*.wav)')
        if path:
            self._adjust_audio(path)

    def _adjust_audio(self, path):
        from .local_adjustment import LocalAdjustmentDialog
        for player in self.players:
            player.pause()
        dialog = LocalAdjustmentDialog(self.page, path)
        dialog.saved.connect(self._adjustment_saved)
        dialog.exec()
        dialog.deleteLater()

    def _adjustment_saved(self, result):
        p = self.page
        from datetime import datetime
        record = {'output_path': result['path'], 'summary': '局部调整 · 原版保留',
                  'generated_at': datetime.now().isoformat(timespec='seconds'),
                  'duration_label': f"{result['duration']:.1f}s" if result.get('duration') is not None else '',
                  'default_filename': Path(result['path']).name}
        p._add_indextts2_result(record)
        p._select_indextts2_result(record)

    def sync_adjustment_entry(self):
        modern = self.page._index_version == '2.5'
        path = self.page._indextts2_last_output
        self.studio_button.setVisible(bool(modern and path and Path(path).is_file()))
        self.studio_button.setEnabled(not self._settings_busy)

    def sync_busy(self, busy):
        self._settings_busy = busy
        self.sync_emotion_settings()
        self.advanced_settings_button.setEnabled(not busy)
        if hasattr(self, 'studio_button'):
            self.sync_adjustment_entry()
        reason = self.page._indextts2_gate_reason()
        self.status_stack.setCurrentIndex(1 if reason and not busy else 0)
        if hasattr(self, "busy_dock"):
            self.busy_dock.setVisible(busy and self.page.panel_stack.currentIndex() == 0)
            self.busy_cancel.setEnabled(self.page.btn_indextts2_cancel.isEnabled())
        self.page.indextts2_progress.setVisible(busy)

    def sync_emotion_settings(self):
        active = self.page._indextts2_emotion_mode in ('vector', 'emotext')
        state = '开启' if self.random_emotion else '关闭'
        enabled = active and not self._settings_busy
        hint = ('对应官方「情感随机采样」。点击了解和设置。' if active
                else '选择「精细调节」或「文字描述」后可用。')
        if active and self._settings_busy:
            hint = '当前任务结束后可设置。'
        button = self.emotion_settings_button
        button.setVisible(active)
        button.setEnabled(enabled)
        button.setText('情绪自由 · ' + state)
        button.setToolTip(hint)
        button.setAccessibleName('情绪自由设置，' + (state if active else '当前模式不可用'))
        button.setAccessibleDescription(hint)
        button.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)

    def _emotion_settings(self):
        if not self.emotion_settings_button.isEnabled():
            return
        from .indextts2_settings import IndexTTSEmotionDialog
        dialog = IndexTTSEmotionDialog(self.page, self.random_emotion,
            mode=self.page._indextts2_emotion_mode, model_version=self.page._index_version)
        if dialog.exec() == QDialog.Accepted:
            self.random_emotion = dialog.enabled_choice.isChecked()
            self.sync_emotion_settings()
        dialog.deleteLater()

    def sync_settings(self):
        from ..core.synthesis.indextts2_worker import validate_index_generation
        version = self.page._index_version
        self.settings_key = 'indextts25_generation_options' if version == '2.5' else 'indextts2_generation_options'
        raw = getattr(self.page.cfg, self.settings_key, {})
        name = 'IndexTTS 2.5' if version == '2.5' else 'IndexTTS 2'
        try:
            values = validate_index_generation(raw, version)
            state = '已调整' if values else '默认'
        except ValueError:
            state = '需检查'
        button = self.advanced_settings_button
        button.setText('高级设置 · ' + state)
        button.setToolTip(name + '：调整生成参数，通常保持默认。')
        button.setAccessibleName(name + ' 高级设置，' + state)

    def _generation_settings(self):
        button = self.advanced_settings_button
        if not button.isEnabled():
            return
        from .indextts2_settings import IndexTTS2SettingsDialog
        self.sync_settings()
        key = self.settings_key
        previous = getattr(self.page.cfg, key, {})
        dialog = IndexTTS2SettingsDialog(self.page, previous,
            animate=self._settings_mouse, model_version=self.page._index_version)
        # Save before closing; on failure retain both the original config and draft.
        def apply():
            values = dialog.values()
            setattr(self.page.cfg, key, values)
            try:
                saved = self.page.cfg.save() is not False
            except Exception:
                saved = False
            if not saved:
                setattr(self.page.cfg, key, previous)
                dialog.show_error('设置未能保存，请重试。当前生成设置未改变。')
                return
            self.sync_settings()
            dialog.accept()
        dialog.apply_button.clicked.disconnect()
        dialog.apply_button.clicked.connect(apply)
        dialog.exec()
        dialog.deleteLater()

    def eventFilter(self, watched, event):
        if watched is self.advanced_settings_button:
            if event.type() == QEvent.MouseButtonPress:
                self._settings_mouse = True
            elif event.type() == QEvent.KeyPress:
                self._settings_mouse = False
        return super().eventFilter(watched, event)
