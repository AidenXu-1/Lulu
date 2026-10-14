"""Compact VoxCPM2 workbench, with real single-text generation and version history."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import secrets
import shutil

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QIcon, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QDialog, QPlainTextEdit, QStackedWidget, QButtonGroup, QSpinBox,
    QDoubleSpinBox, QCheckBox, QLineEdit, QScrollArea, QSizePolicy,
)
from . import dialogs, icons
from .control_foundation import adopt_button, adopt_dialog, LuluCheckBox
from .indextts2_settings import _dialog_style, _NumberField
from .consumer_select import ConsumerSelect
from .voice_wave_slider import VoiceWaveSlider
from . import voice_result_style as result_style
from .widgets import ElideLabel
from ..core import library
from ..core.synthesis import voices
from ..core.synthesis.voxcpm2_options import DEFAULTS, MODES, validate_options, generation_inputs
from ..core.synthesis.indextts2_results import unique_output_path


class VoxCPM2Workbench(QWidget):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.mode = 'hifi'
        self.options = {mode: {**DEFAULTS, 'mode': mode} for mode in MODES}
        for mode, value in getattr(page.cfg, 'voxcpm2_generation_options', {}).items():
            if mode in MODES:
                self.options[mode].update(value)
        self.records = []
        self.current_record = None
        self.busy = False
        self.last_input = None
        self.previous_draft = None
        self.pronunciations = deepcopy(getattr(page.cfg, 'voxcpm2_pronunciations', []))
        self._voice_path = ''
        self._task = None
        self.setObjectName('voxWorkbench')
        c = page._consumer
        self.setStyleSheet(f'''
            QFrame#voxCard {{background:{c.surface};border:1px solid {c.line};border-radius:13px;}}
            QLabel#voxHeading {{font-family:'Songti SC';font-size:18px;font-weight:600;color:{c.ink};}}
            QLabel#voxHint {{font-size:11px;color:{c.ink_muted};background:transparent;}}
            QPushButton#voxMode {{border:1px solid {c.line};background:{c.surface};border-radius:7px;padding:5px 12px;}}
            QPushButton#voxMode:checked {{border-color:{c.accent};color:{c.accent};background:{c.accent_wash};}}
            QPushButton#voxRound {{background:{c.accent};border:0;border-radius:19px;color:white;}}
            QFrame#voxFollow {{border:1px solid {c.line};border-radius:10px;
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {c.accent_wash},stop:1 {c.surface});}}
            QLabel#voxSoundTitle {{font-size:15px;font-weight:600;color:{c.ink};background:transparent;}}
            QLabel#voxSoundIcon {{border:1px solid {c.line};border-radius:23px;background:{c.surface};}}
            QPlainTextEdit {{border:1px solid {c.line};border-radius:8px;background:{c.surface};color:{c.ink};padding:10px;font-size:14px;}}
        ''')
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addWidget(self._editor())
        root.addWidget(self._sound())
        root.addWidget(self._actions())
        root.addWidget(self._result())
        page.voice_combo.currentIndexChanged.connect(self.sync_voice)
        page.text_edit.textChanged.connect(self.sync_text)
        page.text_edit.selectionChanged.connect(self.sync_text)
        self.sync_mode()
        self.sync_voice()
        self.sync_text()

    def _button(self, text, fn, primary=False):
        b = QPushButton(text)
        adopt_button(b, role='primary' if primary else 'secondary', accessible_name=text)
        b.setFixedHeight(36)
        b.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        b.setAutoDefault(False)
        b.clicked.connect(fn)
        return b

    def _label(self, text, heading=False):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setObjectName('voxHeading' if heading else 'voxHint')
        label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        return label

    def _card(self):
        card = QFrame()
        card.setObjectName('voxCard')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 15, 17, 15)
        layout.setSpacing(12)
        return card, layout

    def _editor(self):
        card, layout = self._card()
        self.editor_card = card
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(9)
        st = self.page._src['voxcpm2']
        row = QHBoxLayout()
        st['buttons']['library'].setText('文稿库')
        row.addWidget(st['segment'])
        self.count = ElideLabel('')
        self.count.setObjectName('voxHint')
        self.count.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.count.setAlignment(Qt.AlignCenter)
        row.addWidget(self.count, 1)
        self.clear = self._button('清空', self.clear_text)
        slot = QWidget(); slot.setFixedWidth(78)
        slot_layout = QHBoxLayout(slot); slot_layout.setContentsMargins(0, 0, 0, 0)
        slot_layout.addWidget(self.clear)
        row.addWidget(slot)
        self.pronunciation_button = self._button('术语读音', self.edit_pronunciations)
        self.pronunciation_button.setIcon(QIcon(icons.pixmap('pen', self.page._consumer.ink_muted, 13)))
        row.addWidget(self.pronunciation_button)
        self.view_button = self._button('查看原文', self.view_text)
        row.addWidget(self.view_button)
        layout.addLayout(row)
        for key in ('src_stack','text_edit','drop_area','picked_card','pick_empty'):
            st[key].setFixedHeight(152)
        st['picked_view'].hide()
        c = self.page._consumer
        st['text_edit'].setStyleSheet(f'QPlainTextEdit{{font-size:15px;padding:9px 12px;border:1px solid {c.line};border-radius:9px;background:{c.surface};}}')
        for button in st['buttons'].values():
            button.clicked.connect(self.sync_text)
        layout.addWidget(st['src_stack'])
        return card

    def clear_text(self):
        if dialogs.confirm(self.page, '清空文案', '清空本次输入？原始文稿和文件不会被删除。'):
            self.page.text_edit.clear()

    def edit_pronunciations(self):
        from .voxcpm2_pronunciation import edit_pronunciations
        edit_pronunciations(self)

    def view_text(self):
        dialog, layout = self._dialog('配音原文')
        text = QPlainTextEdit(self.page.text_edit.toPlainText())
        text.setReadOnly(True)
        text.setMinimumHeight(220)
        layout.addWidget(text)
        def preview():
            cursor = text.textCursor()
            original = self.page.text_edit.textCursor()
            original.setPosition(cursor.selectionStart())
            original.setPosition(cursor.selectionEnd(), original.MoveMode.KeepAnchor)
            self.page.text_edit.setTextCursor(original)
            dialog.accept()
            self.generate(True)
        layout.addWidget(self._button('试听选中文字', preview))
        dialog.exec()
        dialog.deleteLater()

    def _sound(self):
        from . import voice_page as module
        from .indextts25_workbench import _FollowTexture
        p = self.page
        card, layout = self._card()
        head = QHBoxLayout()
        head.addWidget(self._label('配音方式', True))
        head.addStretch()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for mode, name in MODES.items():
            b = self._button(name, lambda checked=False, m=mode: self.change_mode(m))
            b.setObjectName('voxMode')
            b.setCheckable(True)
            self.mode_group.addButton(b)
            self.mode_buttons[mode] = b
            head.addWidget(b)
        layout.addLayout(head)
        self.body = QWidget()
        self.body.setFixedHeight(200)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0,0,0,0)
        body.setSpacing(12)
        self.voice_bar = QWidget()
        vr = QHBoxLayout(self.voice_bar)
        vr.setContentsMargins(0,0,0,0)
        self.voice_player = module.QMediaPlayer(self)
        self.voice_audio = module.QAudioOutput(self)
        self.voice_player.setAudioOutput(self.voice_audio)
        self.voice_play = self._button('', self.play_reference)
        self.voice_play.setObjectName('voxRound')
        self.voice_play.setFixedSize(38,38)
        self.voice_play.setAccessibleName('播放或暂停参考录音')
        self.voice_play.setIcon(QIcon(icons.pixmap('play','#ffffff',16)))
        vr.addWidget(self.voice_play)
        self.voice_select = p._voice_sel[id(p.voice_combo)]['select_host']
        self.voice_select.setMinimumWidth(160)
        self.voice_select.setMaximumWidth(230)
        vr.addWidget(self.voice_select, 2)
        self.voice_wave = VoiceWaveSlider(self)
        self.voice_wave.setAccessibleName('参考录音播放进度')
        self.voice_wave.seekRequested.connect(self.voice_player.setPosition)
        self.voice_player.positionChanged.connect(self.voice_wave.setValue)
        self.voice_player.durationChanged.connect(lambda duration: self.voice_wave.setRange(0,duration))
        self.voice_player.playbackStateChanged.connect(self.reference_state)
        vr.addWidget(self.voice_wave, 3)
        body.addWidget(self.voice_bar)
        self.mode_stack = QStackedWidget()
        follow = QFrame()
        follow.setObjectName('voxFollow')
        fl = QHBoxLayout(follow)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(14)
        emblem = QLabel()
        emblem.setObjectName('voxSoundIcon')
        emblem.setFixedSize(46, 46)
        emblem.setAlignment(Qt.AlignCenter)
        emblem.setPixmap(icons.pixmap('waveform', p._consumer.accent, 26))
        fl.addWidget(emblem)
        copy = QVBoxLayout()
        copy.setSpacing(6)
        copy.addStretch()
        title = QLabel('跟随原声，自然表达')
        title.setObjectName('voxSoundTitle')
        title.setStyleSheet('font-size:16px;font-weight:600;')
        title.setMinimumWidth(180)
        title.setWordWrap(True)
        copy.addWidget(title)
        self.reference_hint = self._label('选择音色后校对文字')
        self.reference_hint.setWordWrap(True)
        copy.addWidget(self.reference_hint)
        copy.addStretch()
        fl.addLayout(copy,1)
        self.follow_texture = _FollowTexture(p._consumer.accent)
        fl.addWidget(self.follow_texture,2)
        self.reference_edit = self._button('校对文字', self.edit_reference)
        self.reference_edit.setIcon(QIcon(icons.pixmap('pen', p._consumer.accent, 14)))
        self.reference_edit.setAccessibleName('查看与校对参考文字')
        self.reference_edit.setStyleSheet(self._sound_button_style())
        fl.addWidget(self.reference_edit)
        self.mode_stack.addWidget(follow)
        self.direction = QPlainTextEdit()
        self.direction.setAccessibleName('表达要求')
        self.direction.setPlaceholderText('希望怎么说？例如：轻声安慰，语气温柔。')
        self.direction.setPlainText(self.options['directed'].get('instruction',''))
        self._style_sound_input(self.direction)
        directed = QWidget()
        dl = QVBoxLayout(directed)
        dl.setContentsMargins(0,0,0,0)
        dl.setSpacing(10)
        dl.addWidget(self.direction)
        presets = QHBoxLayout()
        presets.setSpacing(8)
        self.direction_presets = {}
        for title, prompt in [('平静叙述','平静自然地叙述'),('开心分享','开心地分享，语气轻松'),('轻声安慰','轻声安慰，语气温柔'),('稍快讲述','语速稍快，清楚地讲述')]:
            b = self._button(title, lambda checked=False, value=prompt: self.apply_direction_preset(value))
            b.setCheckable(True)
            b.setFixedHeight(32)
            b.setStyleSheet(self._sound_button_style(chip=True))
            b.setToolTip(prompt)
            self.direction_presets[prompt] = b
            presets.addWidget(b)
        presets.addStretch()
        dl.addLayout(presets)
        self.direction.textChanged.connect(self.sync_direction_presets)
        self.sync_direction_presets()
        self.mode_stack.addWidget(directed)
        designed = QWidget()
        design = QVBoxLayout(designed)
        design.setContentsMargins(0,0,0,0)
        design.setSpacing(10)
        design_head = QHBoxLayout()
        name = QLabel('声音描述')
        name.setObjectName('voxSoundTitle')
        design_head.addWidget(name)
        design_head.addStretch()
        design_head.addWidget(self._label('无需参考录音'))
        design.addLayout(design_head)
        self.description = QPlainTextEdit()
        self.description.setAccessibleName('声音描述')
        self.description.setPlainText(self.options['design'].get('instruction',''))
        self.description.setPlaceholderText('例如：温暖低沉的成年男声，像朋友一样自然讲述。')
        self._style_sound_input(self.description)
        design.addWidget(self.description)
        self.mode_stack.addWidget(designed)
        body.addWidget(self.mode_stack,1)
        layout.addWidget(self.body)
        return card

    def _sound_button_style(self, chip=False):
        c = self.page._consumer
        return f'''
            QPushButton {{background:{c.surface};color:{c.ink};border:1px solid {c.line};
                border-radius:{16 if chip else 8}px;padding:0 14px;font-size:12px;font-weight:500;}}
            QPushButton:hover {{background:{c.accent_wash};border-color:{c.accent};}}
            QPushButton:checked {{background:{c.accent_wash};border-color:{c.accent};color:{c.accent};}}
            QPushButton:pressed {{background:{c.surface_alt};border-color:{c.accent};}}
            QPushButton:focus {{border:2px solid {c.accent};padding:0 13px;}}
            QPushButton:disabled {{color:{c.ink_muted};border-color:{c.line};background:{c.surface_alt};}}
        '''

    def _style_sound_input(self, editor):
        c = self.page._consumer
        editor.setTabChangesFocus(True)
        editor.setStyleSheet(f'''
            QPlainTextEdit {{background:{c.surface};color:{c.ink};border:1px solid {c.line};
                border-radius:10px;padding:12px 14px;font-size:14px;selection-background-color:{c.accent};selection-color:white;}}
            QPlainTextEdit:hover {{border-color:{c.accent_hairline};}}
            QPlainTextEdit:focus {{border-color:{c.accent};}}
            QPlainTextEdit:disabled {{color:{c.ink_muted};background:{c.surface_alt};}}
        ''')

    def apply_direction_preset(self, prompt):
        # Keep the user's previous instruction available through ordinary Undo.
        if self.direction.toPlainText() != prompt:
            cursor = self.direction.textCursor()
            cursor.beginEditBlock()
            cursor.select(QTextCursor.Document)
            cursor.insertText(prompt)
            cursor.endEditBlock()
            self.direction.setTextCursor(cursor)
        self.sync_direction_presets()

    def sync_direction_presets(self):
        text = self.direction.toPlainText().strip()
        for prompt, button in self.direction_presets.items():
            button.setChecked(text == prompt)

    def _actions(self):
        p = self.page
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0,0,0,0)
        row = QHBoxLayout()
        row.addWidget(self._button('音色管理',lambda:p._open_voice_picker(p.voice_combo)))
        self.settings_button = self._button('高级设置 · 默认',self.edit_settings)
        row.addWidget(self.settings_button)
        row.addStretch()
        self.preview_button = self._button('试听选段',lambda:self.generate(True))
        row.addWidget(self.preview_button)
        p.btn_generate.setText('生成全文')
        p.btn_generate.setIcon(QIcon())
        p.btn_generate.setFixedHeight(36)
        p.btn_cancel.setFixedHeight(36)
        row.addWidget(p.btn_generate)
        row.addWidget(p.btn_cancel)
        layout.addLayout(row)
        layout.addWidget(p.status)
        layout.addWidget(p.progress)
        p.status.setText('整段生成，满意的旧结果会保留。')
        return wrap

    def _result(self):
        p = self.page
        card, layout = self._card()
        result_style.card_style(card,layout,p._consumer)
        head = QHBoxLayout()
        head.addWidget(self._label('本次结果',True))
        self.result_meta = ElideLabel('')
        self.result_meta.setObjectName('voxHint')
        self.result_meta.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        head.addWidget(self.result_meta,1)
        history=self._button('历史记录',self.history);result_style.action_style(history,p._consumer);head.addWidget(history)
        layout.addLayout(head)
        p.voxcpm2_result_hint.setText(result_style.EMPTY_TEXT)
        layout.addWidget(p.voxcpm2_result_hint)
        bar = p.voxcpm2_player_bar
        old = p.player_slider
        self.result_wave = VoiceWaveSlider(self)
        self.result_wave.setAccessibleName('本次结果播放进度')
        p.player_slider = self.result_wave
        self.result_wave.sliderPressed.connect(p._on_slider_pressed)
        self.result_wave.sliderReleased.connect(p._on_slider_released)
        self.result_wave.seekRequested.connect(p.player.setPosition)
        old.hide()
        while bar.layout().count():
            item = bar.layout().takeAt(0)
            if item.widget():
                item.widget().hide()
        bar.setStyleSheet('QFrame {border:0;background:transparent;}')
        p.btn_play.setObjectName('voxRound')
        p.btn_play.setFixedSize(38,38)
        self._play_icon(False)
        p.player.playbackStateChanged.connect(self.result_state)
        for w in (p.btn_play,self.result_wave,p.time_label):
            bar.layout().addWidget(w,1 if w is self.result_wave else 0)
            w.show()
        bar.layout().setContentsMargins(0,0,0,0);bar.layout().setSpacing(12)
        result_style.player_style(p.btn_play,self.result_wave,p.time_label,p._consumer)
        layout.addWidget(bar)
        self.result_actions = QWidget()
        actions = QHBoxLayout(self.result_actions)
        actions.setContentsMargins(0,0,0,0)
        actions.setSpacing(10)
        self.save_voice_button = self._button('保存为音色',self.save_voice)
        actions.addWidget(self.save_voice_button)
        actions.addStretch()
        self.export_button = self._button('下载音频',self.export)
        self.export_format = ConsumerSelect([('wav','WAV'),('mp3','MP3')], value='wav', accessible_name='下载音频格式', consumer_theme_scope='coreWorkspace', paper_popup=True)
        self.export_format.setFixedWidth(84)
        actions.addWidget(self.export_format)
        actions.addWidget(self.export_button)
        p.out_dir_btn.set_path(p.cfg.tts_output_dir)
        result_style.action_style(self.export_button,p._consumer,download=True)
        result_style.action_style(p.out_dir_btn,p._consumer,location=True)
        result_style.action_style(self.save_voice_button,p._consumer)
        self.export_format.setFixedHeight(38)
        p.out_dir_btn.setAccessibleName('音频下载位置')
        actions.addWidget(p.out_dir_btn)
        layout.addWidget(self.result_actions)
        self.result_actions.hide()
        return card

    def result_state(self, state):
        from .voice_page import QMediaPlayer
        self._play_icon(state == QMediaPlayer.PlayingState)

    def _play_icon(self, playing):
        p = self.page
        if playing:
            self.voice_player.pause()
        p.btn_play.setText('')
        p.btn_play.setAccessibleName('暂停本次结果' if playing else '播放本次结果')
        p.btn_play.setIcon(QIcon(icons.pixmap('pause' if playing else 'play','#ffffff',16)))

    def sync_text(self):
        text = self.page.text_edit.toPlainText()
        selected = self.page.text_edit.textCursor().selectedText()
        self.count.setText(f'配音正文 {len(text.strip())} 字' + (f' · 已选 {len(selected)} 字' if selected else ' · 选中文字可试听'))
        self.clear.setVisible(self.page._src['voxcpm2']['buttons']['custom'].isChecked())
        self.clear.setEnabled(bool(text) and not self.busy)
        self.view_button.setEnabled(bool(text))

    def change_mode(self, mode):
        if self.busy:
            return
        self.mode = mode
        self.voice_player.stop()
        self.sync_mode()

    def sync_mode(self):
        self.mode_buttons[self.mode].setChecked(True)
        self.voice_bar.setVisible(self.mode!='design')
        self.mode_stack.setCurrentIndex(list(MODES).index(self.mode))
        self.page.btn_generate.setText('生成声音候选' if self.mode=='design' else '生成全文')
        values=self.options[self.mode]
        adjusted=any(values.get(k,DEFAULTS[k])!=DEFAULTS[k] for k in ('inference_timesteps','cfg_value','normalize'))
        self.settings_button.setText('高级设置 · '+('已调整' if adjusted else '默认'))

    def sync_voice(self):
        voice = voices.get_voice(str(self.page.voice_combo.currentData() or ''))
        path = voice.reference_audio_path if voice else ''
        self.voice_play.setEnabled(bool(path and Path(path).is_file()))
        saved = bool(voice and voice.reference_text.strip())
        self.reference_hint.setText('参考文字已保存' if saved else '参考文字待校对' if voice else '选择音色后校对文字')
        self.reference_edit.setText('查看文字' if saved else '校对文字')
        self.reference_edit.setEnabled(bool(voice))
        if path != self._voice_path:
            self._voice_path = path
            self.voice_player.stop()
            self.voice_player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
            self.voice_wave.load_file(path)

    def play_reference(self):
        from .voice_page import QMediaPlayer
        if self.voice_player.playbackState()==QMediaPlayer.PlayingState:
            self.voice_player.pause()
        else:
            self.page.player.pause()
            if self.voice_player.position()>=self.voice_player.duration()>0:
                self.voice_player.setPosition(0)
            self.voice_player.play()

    def reference_state(self,state):
        from .voice_page import QMediaPlayer
        self.voice_play.setIcon(QIcon(icons.pixmap('pause' if state==QMediaPlayer.PlayingState else 'play','#ffffff',16)))

    def _dialog(self,title):
        d=QDialog(self.page)
        adopt_dialog(d,accessible_name=title)
        d.setWindowTitle(title)
        d.setMaximumWidth(880)
        d.resize(520,380)
        c=self.page._consumer
        d.setStyleSheet(_dialog_style(c) + f"""
            QLabel#voxHeading {{font-family:'Songti SC';font-size:22px;font-weight:600;}}
            QLabel#voxHint {{color:{c.ink_muted};font-size:12px;}}
            QLabel#voxRowTitle {{font-size:14px;font-weight:600;}}
            QLabel#voxBadge {{color:{c.accent};font-size:11px;}}
            QFrame#voxCard {{background:{c.surface};border:1px solid {c.line};border-radius:12px;}}
            QFrame#voxCard[current="true"] {{background:{c.accent_wash};border-color:{c.accent};}}
            QPlainTextEdit,QLineEdit {{background:{c.surface};color:{c.ink};border:1px solid {c.line};border-radius:8px;padding:9px 12px;font-size:13px;}}
            QPlainTextEdit:focus,QLineEdit:focus {{border-color:{c.accent};}}
        """)
        d.setWindowModality(Qt.WindowModal)
        layout=QVBoxLayout(d)
        layout.setContentsMargins(22,20,22,20)
        layout.setSpacing(12)
        layout.addWidget(self._label(title,True))
        return d,layout

    def edit_reference(self):
        if self.busy:return
        voice=voices.get_voice(str(self.page.voice_combo.currentData() or ''))
        if not voice:
            dialogs.alert(self,'参考文字','请先选择音色。');return
        d,layout=self._dialog('参考录音与文字')
        layout.addWidget(self._label('请按录音准确填写。文字与这份音色一起保存。'))
        layout.addWidget(self._button('播放 / 暂停参考',self.play_reference))
        text=QPlainTextEdit(voice.reference_text)
        text.setAccessibleName('参考文字')
        layout.addWidget(text)
        error=self._label('')
        error.setWordWrap(True)
        layout.addWidget(error)
        row=QHBoxLayout()
        row.addStretch()
        row.addWidget(self._button('取消',d.reject))
        def save():
            try:voices.update_reference_text(voice.id,text.toPlainText())
            except (ValueError,OSError) as exc:error.setText(str(exc));return
            self.sync_voice();d.accept()
        row.addWidget(self._button('确认文字',save,True))
        layout.addLayout(row)
        d.exec();self.voice_player.stop();d.deleteLater()

    def edit_settings(self):
        d,layout=self._dialog('生成设置')
        d.resize(580, 430)
        values=deepcopy(self.options[self.mode])
        layout.addWidget(self._label(MODES[self.mode]+' · 仅用于之后的生成，已有音频不变。'))
        steps=QSpinBox();steps.setRange(4,30);steps.setValue(values['inference_timesteps']);steps.setAccessibleName('生成步数')
        cfg=QDoubleSpinBox();cfg.setRange(1,3);cfg.setSingleStep(.1);cfg.setValue(values['cfg_value']);cfg.setAccessibleName('条件跟随程度 CFG')
        normalize=LuluCheckBox('整理数字和日期读法');normalize.setChecked(values['normalize'])
        group, fields = self._card()
        for title,hint,widget in [
            ('生成步数', '默认 10。增加步数会延长生成时间。', steps),
            ('跟随程度', '默认 2。越高越贴近输入条件，也可能降低自然度。', cfg)]:
            row=QHBoxLayout(); words=QVBoxLayout(); words.setSpacing(5)
            name=QLabel(title); name.setObjectName('voxRowTitle'); words.addWidget(name)
            help_text=self._label(hint); help_text.setWordWrap(True); words.addWidget(help_text)
            row.addLayout(words,1); row.addSpacing(16); row.addWidget(_NumberField(widget)); fields.addLayout(row)
        layout.addWidget(group)
        group, fields = self._card(); fields.addWidget(normalize)
        hint=self._label('将数字、日期转换为读法。命中术语读音时，本次自动跳过整理。')
        hint.setWordWrap(True); fields.addWidget(hint); layout.addWidget(group)
        layout.addStretch()
        error=self._label('');layout.addWidget(error)
        def save():
            previous=deepcopy(getattr(self.page.cfg,'voxcpm2_generation_options',{}))
            values.update(inference_timesteps=steps.value(),cfg_value=cfg.value(),normalize=normalize.isChecked())
            saved=deepcopy(self.options);saved[self.mode]=values
            self.page.cfg.voxcpm2_generation_options=saved
            try:
                if self.page.cfg.save() is False:raise OSError()
            except Exception:
                self.page.cfg.voxcpm2_generation_options=previous;error.setText('设置保存失败，请重试。');return
            self.options=saved;self.sync_mode();d.accept()
        row=QHBoxLayout()
        row.addWidget(self._button('恢复默认',lambda:(steps.setValue(10),cfg.setValue(2),normalize.setChecked(False))))
        row.addStretch();row.addWidget(self._button('取消',d.reject));row.addWidget(self._button('保存设置',save,True));layout.addLayout(row)
        d.exec();d.deleteLater()

    def generation_options(self):
        options=deepcopy(self.options[self.mode])
        options['mode']=self.mode
        options['pronunciations']=deepcopy(self.pronunciations)
        options['instruction']=self.direction.toPlainText() if self.mode=='directed' else self.description.toPlainText() if self.mode=='design' else ''
        return validate_options(options)

    def generate(self,preview=False):
        from . import voice_page as module
        p=self.page
        if self.busy or (p._worker and p._worker.isRunning()):return
        if not p._guard_local_synthesis_start('voxcpm2'):return
        text=p.text_edit.textCursor().selectedText().replace('\u2029','\n') if preview else p.text_edit.toPlainText()
        if preview and (not text.strip() or len(text)>200):
            dialogs.alert(self,'试听选段','请先选中 200 字以内的文字。');return
        voice=deepcopy(voices.get_voice(str(p.voice_combo.currentData() or ''))) if self.mode!='design' else None
        try:
            options=self.generation_options()
            generation_inputs(text,voice,options)
        except ValueError as exc:
            dialogs.alert(self,'生成配音',str(exc));return
        signature=(text,voice.id if voice else '',repr(options))
        if signature==self.last_input or self.mode=='design':options['seed']=secrets.randbelow(2**32)
        self.last_input=signature
        stem=('Lulu试音_' if preview else 'Lulu配音_')+datetime.now().strftime('%Y%m%d_%H%M%S')
        path=unique_output_path(library.audio_storage_dir()/(stem+'.wav'))
        self._task={'preview':preview,'voice_name':voice.name if voice else '设计声音','source':p._source_payload_for_text(text)}
        self.voice_player.stop();p.player.pause()
        self.set_busy(True)
        self.progress('准备生成…', -1)
        p._worker=module.SynthesisWorker(text,voice.id if voice else '',str(path),'wav',engine='local_voxcpm2',engine_options=options,voice_profile=voice)
        p._worker.progress.connect(self.progress)
        p._worker.finished_ok.connect(self.done)
        p._worker.failed.connect(self.failed)
        p._worker.cancelled.connect(self.cancelled)
        p._worker.start()

    def progress(self, message, fraction):
        # The model reports phases, not a measurable percentage of completion.
        self.page.progress.setRange(0, 0)
        self.page.progress.show()
        self.page.status.setText(message)

    def set_busy(self,busy):
        self.busy=busy
        p=self.page
        p.btn_generate.setVisible(not busy);p.btn_generate.setEnabled(not busy)
        p.btn_cancel.setVisible(busy);p.btn_cancel.setEnabled(busy)
        p.progress.setVisible(busy)
        self.settings_button.setEnabled(not busy);self.preview_button.setEnabled(not busy)
        self.body.setEnabled(not busy)
        self.pronunciation_button.setEnabled(not busy)
        self.sync_text()
        for b in self.mode_buttons.values():b.setEnabled(not busy)

    def done(self,result):
        p=self.page
        options=result.meta['options']
        label=MODES[options['mode']]
        summary=f"{label} · {options['inference_timesteps']} 步 · CFG {options['cfg_value']:g}"
        task=self._task
        record={'output_path':str(result.output_path),'wav_path':str(result.wav_path),'text':result.text,
                'voice_id':result.voice_id,'voice_name':task['voice_name'],'meta':deepcopy(result.meta),
                'summary':summary,'duration':result.duration_seconds,'preview':task['preview'],
                'created_at':datetime.now().isoformat(timespec='seconds')}
        saved=True;message=''
        if not task['preview']:
            source=task['source']
            saved,message=p._save_voice_safely(result.output_path,title=f"{label} · {task['voice_name']}",
                text=result.text,engine='VoxCPM2',voice_id=result.voice_id or None,voice_name=task['voice_name'],
                duration_seconds=result.duration_seconds,config_summary=summary,meta=result.meta,
                source_type=source.get('source_type','manual'),source_id=source.get('source_id'),
                source_title=source.get('source_title'),source_text_snapshot=source.get('source_text_snapshot'),source_chain=source.get('source_chain') or [])
        self.records.insert(0,record)
        self.select_result(record)
        self.set_busy(False)
        p._set_voice_progress_complete(p.progress)
        p.status.setText(('试听完成 · 未入库' if task['preview'] else '完成 · 已保存到音频库')+f' · {result.duration_seconds:.2f} 秒' if saved else message)
        if not saved:dialogs.alert(self,'保存失败',message)
        notify=getattr(p.main,'_notify_task_completed',None)
        if notify:notify(page_index=4)

    def failed(self,message):
        self.set_busy(False)
        self.page._set_voice_progress_stopped(self.page.progress)
        self.page.status.setText('生成失败，上一份结果已保留。')
        dialogs.alert(self,'生成失败',message)

    def cancelled(self):
        self.set_busy(False)
        self.page._set_voice_progress_stopped(self.page.progress)
        self.page.status.setText('已取消，文案、设置与上一份结果保留。')

    def select_result(self,record):
        self.current_record=record
        p=self.page
        p._last_output=record['output_path']
        p._load_player(record['output_path'],record.get('duration'))
        self.result_wave.load_file(record['output_path'])
        self.result_meta.setText(record.get('summary','配音结果'))
        self.result_meta.setToolTip(self.result_meta.text())
        self.result_actions.show()
        self.save_voice_button.setVisible(record.get('meta',{}).get('mode')=='design')
        self._play_icon(False)

    def export(self):
        if not self.current_record:return
        source=Path(self.current_record['output_path'])
        if not source.is_file():dialogs.alert(self,'下载音频','文件已移动或删除。');return
        try:
            target=Path(self.page.cfg.tts_output_dir);target.mkdir(parents=True,exist_ok=True)
            fmt=self.export_format.currentData()
            output=unique_output_path((target/source.name).with_suffix('.'+fmt))
            if fmt=='mp3':
                from ..core.audio.converter import wav_to_mp3
                wav_to_mp3(source,output)
            else:
                shutil.copy2(source,output)
        except Exception as exc:dialogs.alert(self,'下载失败',str(exc));return
        self.page.main._toast('已下载到 '+str(output))

    def save_voice(self):
        record=deepcopy(self.current_record)
        if not record:return
        d,layout=self._dialog('保存为音色')
        layout.addWidget(self._label('保存选定声音及对应文字，后续可用完整克隆继续配音。'))
        name=QLineEdit('我的设计声音');name.setAccessibleName('音色名称');layout.addWidget(name)
        error=self._label('');layout.addWidget(error)
        def save():
            if not name.text().strip():error.setText('请填写音色名称。');return
            try:voice=voices.add_voice(name.text(),record.get('wav_path') or record['output_path'],record['text'])
            except (OSError,ValueError) as exc:error.setText(str(exc));return
            self.page.refresh();self.page.voice_combo.setCurrentIndex(self.page.voice_combo.findData(voice.id))
            d.accept();self.page.main._toast('音色已保存，可切换完整克隆继续使用。')
        row=QHBoxLayout();row.addStretch();row.addWidget(self._button('取消',d.reject));row.addWidget(self._button('保存音色',save,True));layout.addLayout(row)
        d.exec();d.deleteLater()

    def _draft(self):
        return (self.page.text_edit.toPlainText(), self.mode, self.direction.toPlainText(),
                self.description.toPlainText(), deepcopy(self.options),
                self.page.voice_combo.currentData(), deepcopy(self.pronunciations))

    def _restore_draft(self, draft):
        text, mode, direction, description, options, voice_id, terms = draft
        self.page._src_set_mode('voxcpm2', 'custom')
        self.page.text_edit.setPlainText(text)
        self.options = deepcopy(options); self.pronunciations = deepcopy(terms)
        self.direction.setPlainText(direction); self.description.setPlainText(description)
        index = self.page.voice_combo.findData(voice_id)
        if index >= 0: self.page.voice_combo.setCurrentIndex(index)
        self.change_mode(mode)

    def history(self):
        from .voice_page import QMediaPlayer, QAudioOutput
        d, layout = self._dialog('历史记录')
        d.resize(720, 560)
        layout.addWidget(self._label('试听比较，选出喜欢的版本。已有音频会一直保留。'))
        player = QMediaPlayer(d); output = QAudioOutput(d); player.setAudioOutput(output)
        self.voice_player.stop(); self.page.player.pause()
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(); rows = QVBoxLayout(body); rows.setContentsMargins(0, 0, 8, 0); rows.setSpacing(10)
        records = list(self.records)
        known = {r['output_path'] for r in records}
        for entry in library.list_voice_entries():
            if entry.get('engine') != 'VoxCPM2': continue
            raw = library.load_voice(entry['id'])
            if not raw or raw.get('audio_path') in known: continue
            known.add(raw['audio_path'])
            records.append({'output_path':raw['audio_path'], 'text':raw['text'], 'voice_id':raw.get('voice_id'),
                'voice_name':raw['voice_name'], 'meta':raw.get('meta') or {}, 'summary':raw['config_summary'],
                'duration':raw.get('duration_seconds'), 'created_at':raw['created_at']})
        records.sort(key=lambda r: r.get('created_at', ''), reverse=True)
        d.resize(700, min(620, max(340, 190 + len(records) * 160)))
        play_buttons = []
        def sync_playing(*_):
            playing = player.playbackState() == QMediaPlayer.PlayingState
            for button, path in play_buttons:
                active = playing and player.source().toLocalFile() == path
                button.setText('暂停' if active else '试听')
                button.setAccessibleName('暂停' if active else '试听')
                button.setIcon(QIcon(icons.pixmap('pause' if active else 'play', self.page._consumer.accent, 14)))
        player.playbackStateChanged.connect(sync_playing)
        for record in records:
            card, line = self._card(); line.setSpacing(8)
            current = bool(self.current_record and self.current_record['output_path'] == record['output_path'])
            card.setProperty('current', current)
            top = QHBoxLayout()
            kind = '选段试听 · 未入库' if record.get('preview') else '完整配音'
            badge = QLabel(kind + (' · 当前结果' if current else '')); badge.setObjectName('voxBadge'); top.addWidget(badge)
            top.addStretch()
            try: date = datetime.fromisoformat(record.get('created_at', '')).strftime('%m月%d日 %H:%M')
            except ValueError: date = ''
            duration = record.get('duration')
            top.addWidget(self._label(date + (f' · {duration:.2f} 秒' if isinstance(duration, (int, float)) else '')))
            line.addLayout(top)
            excerpt = ElideLabel(record.get('text', '').replace('\n', ' ') or '配音结果')
            excerpt.setObjectName('voxRowTitle'); excerpt.setToolTip(record.get('text', '')); line.addWidget(excerpt)
            detail = self._label((record.get('voice_name') or '设计声音') + ' · ' + record.get('summary', '配音'))
            detail.setWordWrap(True); line.addWidget(detail)
            row = QHBoxLayout(); available = Path(record['output_path']).is_file()
            def play(checked=False, r=record):
                url = QUrl.fromLocalFile(r['output_path'])
                if player.source() == url and player.playbackState() == QMediaPlayer.PlayingState:
                    player.pause()
                else:
                    if player.source() != url: player.setSource(url)
                    elif player.position() >= player.duration(): player.setPosition(0)
                    player.play()
            button = self._button('试听', play); button.setEnabled(available)
            play_buttons.append((button, record['output_path'])); row.addWidget(button)
            row.addStretch()
            def select(checked=False, r=record):
                if not Path(r['output_path']).is_file():
                    dialogs.alert(d, '音频暂不可用', '文件已移走或删除，当前结果已保留。'); return
                self.select_result(r); d.accept()
            button = self._button('查看结果', select); button.setEnabled(available and not self.busy); row.addWidget(button)
            def restore(checked=False, r=record):
                self.previous_draft = self._draft()
                self.page._src_set_mode('voxcpm2', 'custom'); self.page.text_edit.setPlainText(r.get('text', ''))
                opts = r.get('meta', {}).get('options')
                if opts:
                    self.options[opts['mode']] = deepcopy(opts)
                    self.pronunciations = deepcopy(opts.get('pronunciations', []))
                    self.change_mode(opts['mode'])
                    (self.description if opts['mode']=='design' else self.direction).setPlainText(opts.get('instruction', ''))
                index = self.page.voice_combo.findData(r.get('voice_id'))
                if index >= 0: self.page.voice_combo.setCurrentIndex(index)
                d.accept()
            button = self._button('用此文案与设置', restore); button.setEnabled(not self.busy)
            button.setToolTip('替换当前草稿；可再次打开历史记录，找回刚才的草稿。'); row.addWidget(button)
            line.addLayout(row)
            if not available: line.addWidget(self._label('音频文件已移走，可继续使用这份文案和设置。'))
            rows.addWidget(card)
        if not records:
            empty, line = self._card()
            line.addWidget(self._label('还没有配音记录', True))
            line.addWidget(self._label('生成一段配音后，就能在这里试听和比较。'))
            rows.addWidget(empty)
        rows.addStretch(); scroll.setWidget(body); layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.addWidget(self._label(f'共 {len(records)} 条'))
        if self.previous_draft:
            def recover():
                self._restore_draft(self.previous_draft); self.previous_draft = None; d.accept()
            button = self._button('找回刚才的草稿', recover); button.setEnabled(not self.busy); footer.addWidget(button)
        footer.addStretch(); footer.addWidget(self._button('关闭', d.accept)); layout.addLayout(footer)
        sync_playing()
        d.exec(); player.stop(); d.deleteLater()

    def shutdown(self):
        self.voice_player.stop()
        self.voice_wave.stop_decode()
        self.result_wave.stop_decode()
