"""Lulu native Qwen workbench; each mode maps to its own official model."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import secrets
import shutil
import tempfile

from PySide6.QtCore import Qt,QUrl,QSignalBlocker,QThread,Signal
from PySide6.QtGui import QIcon,QTextCursor
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QFrame,QPushButton,QComboBox,QPlainTextEdit,QStackedWidget,QButtonGroup,QProgressBar,QLineEdit,QDoubleSpinBox,QScrollArea,QSizePolicy
from . import dialogs,icons
from .consumer_select import ConsumerSelect
from .control_foundation import LuluCheckBox
from .voice_wave_slider import VoiceWaveSlider
from .widgets import ElideLabel
from . import voice_result_style as result_style
from .voxcpm2_workbench import VoxCPM2Workbench
from .indextts25_workbench import _FollowTexture
from ..core import library
from ..core.synthesis import voices,qwen3_runtime
from ..core.synthesis.qwen3_options import DEFAULTS,MODES,LANGUAGES,SPEAKERS,MODEL_NAMES,ENGINE_ID,validate_options,generation_inputs
from ..core.synthesis.indextts2_results import unique_output_path

OFFICIAL_SAMPLES = Path(__file__).resolve().parent / 'assets' / 'qwen3_samples'

class ReleaseWorker(QThread):
    completed = Signal(bool)
    def run(self):
        from ..core.synthesis.qwen3_subprocess import shutdown_persistent_worker
        self.completed.emit(shutdown_persistent_worker())

class ExportWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)
    def __init__(self,source,folder,fmt,parent=None):
        super().__init__(parent);self.source=source;self.folder=folder;self.fmt=fmt
    def run(self):
        try:
            self.folder.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='lulu_qwen_export_') as tmp:
                src=self.source
                if self.fmt=='mp3':
                    from ..core.audio.converter import wav_to_mp3
                    src=wav_to_mp3(src,Path(tmp)/'export.mp3')
                if self.isInterruptionRequested():return
                for _ in range(100):
                    dest=unique_output_path((self.folder/self.source.name).with_suffix('.'+self.fmt))
                    try:out=dest.open('xb')
                    except FileExistsError:continue
                    try:
                        with out,src.open('rb') as stream:shutil.copyfileobj(stream,out)
                    except BaseException:
                        dest.unlink(missing_ok=True);raise
                    self.completed.emit(str(dest));return
                raise OSError('无法生成不覆盖旧文件的下载名称。')
        except Exception as exc:self.failed.emit(str(exc))

class Qwen3Workbench(QWidget):
    # Reuse the already-approved Lulu card/dialog/button presentation.
    def _button(self,text,fn,primary=False):
        button=VoxCPM2Workbench._button(self,text,fn,primary)
        c=self.page._consumer
        button.setStyleSheet(f"""
            QPushButton {{font-size:13px;font-weight:500;padding:0 13px;border-radius:8px;
                background:{c.accent if primary else c.surface};color:{'#ffffff' if primary else c.ink};border:1px solid {c.accent if primary else c.line};}}
            QPushButton:hover {{border-color:{c.accent};background:{c.accent if primary else c.accent_wash};}}
            QPushButton:pressed {{border:2px solid {c.accent};padding:0 12px;}}
            QPushButton:focus {{border:2px solid {c.accent};padding:0 12px;}}
            QPushButton:checked {{background:{c.accent_wash};color:{c.accent};border-color:{c.accent};}}
            QPushButton:disabled {{background:{c.surface_alt};color:{c.ink_muted};border-color:{c.line};}}
        """)
        return button
    _label=VoxCPM2Workbench._label
    _card=VoxCPM2Workbench._card
    _dialog=VoxCPM2Workbench._dialog
    _style_sound_editor=VoxCPM2Workbench._style_sound_input

    def __init__(self,page):
        super().__init__(page)
        from . import voice_page as module
        self.page=page;self.mode='clone';self.busy=False;self.worker=None
        self.records=[];self.current_record=None;self._voice_path='';self.last_input=None;self.previous_draft=None;self.release_worker=None;self.export_worker=None
        self.options={m:{**DEFAULTS,'mode':m} for m in MODES}
        saved=getattr(page.cfg,'qwen3_generation_options',{})
        if isinstance(saved,dict):
            for m in MODES:
                if isinstance(saved.get(m),dict):
                    candidate={**self.options[m],**saved[m],'mode':m}
                    try:
                        checked=validate_options({**candidate,'instruction':candidate.get('instruction') or '声音描述'})
                        checked['instruction']=str(candidate.get('instruction') or '') if m!='clone' else ''
                        self.options[m]=checked
                    except (ValueError,TypeError):pass
        self.setObjectName('qwen3ModelPanel');self.setAccessibleName('Qwen3-TTS 配音工作台')
        c=page._consumer
        self.setStyleSheet(f"""
            QFrame#voxCard {{background:{c.surface};border:1px solid {c.line};border-radius:13px;}}
            QLabel#voxHeading {{font-family:'Songti SC';font-size:18px;font-weight:600;color:{c.ink};}}
            QLabel#voxHint {{font-size:11px;color:{c.ink_muted};background:transparent;}}
            QPushButton#qwenMode {{border:1px solid {c.line};background:{c.surface};border-radius:8px;padding:5px 12px;}}
            QPushButton#qwenMode:checked {{border-color:{c.accent};color:{c.accent};background:{c.accent_wash};}}
            QFrame#qwenFollow {{border:1px solid {c.line};border-radius:10px;background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {c.accent_wash},stop:1 {c.surface});}}
            QPlainTextEdit {{background:{c.surface};color:{c.ink};border:1px solid {c.line};border-radius:9px;padding:10px 12px;font-size:14px;}}
        """)
        root=QVBoxLayout(self);root.setContentsMargins(0,0,0,0);root.setSpacing(12)
        self.voice_combo=QComboBox(self);self.voice_combo.hide()
        self.reference_player=module.QMediaPlayer(self);self.reference_audio=module.QAudioOutput(self);self.reference_player.setAudioOutput(self.reference_audio)
        self.player=module.QMediaPlayer(self);self.audio=module.QAudioOutput(self);self.player.setAudioOutput(self.audio)
        root.addWidget(self.build_editor());root.addWidget(self.build_sound());root.addWidget(self.build_actions());root.addWidget(self.build_result())
        self.voice_combo.currentIndexChanged.connect(self.sync_voice)
        self.text_edit.textChanged.connect(self.sync_text);self.text_edit.selectionChanged.connect(self.sync_text)
        self.reference_player.playbackStateChanged.connect(lambda *_:self.sync_playback(True))
        self.player.playbackStateChanged.connect(lambda *_:self.sync_playback(False))
        self.refresh();self.change_mode('clone');self.sync_text()

    def build_editor(self):
        card,layout=self._card();self.editor_card=card
        layout.setContentsMargins(16,12,16,12);layout.setSpacing(9)
        self._source_block=self.page._build_source_block('qwen3',placeholder='输入文案，选中一句可先试听…',custom_height=152)
        self._source_block.setParent(self);self._source_block.hide()
        state=self.page._src['qwen3'];state['buttons']['library'].setText('文稿库')
        self.text_edit=state['text_edit'];self.text_edit.setAccessibleName('Qwen3 配音文案')
        row=QHBoxLayout();row.addWidget(state['segment'])
        self.count=ElideLabel('');self.count.setObjectName('voxHint');self.count.setAlignment(Qt.AlignCenter)
        self.count.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);row.addWidget(self.count,1)
        self.clear=self._button('清空',self.clear_text)
        slot=QWidget();slot.setFixedWidth(78);slot_layout=QHBoxLayout(slot);slot_layout.setContentsMargins(0,0,0,0);slot_layout.addWidget(self.clear);row.addWidget(slot)
        row.addWidget(self._button('查看原文',self.view_text));layout.addLayout(row)
        for key in ('src_stack','text_edit','drop_area','picked_card','pick_empty'):state[key].setFixedHeight(152)
        state['picked_view'].hide()
        c=self.page._consumer
        self.text_edit.setStyleSheet(f'QPlainTextEdit{{font-size:15px;padding:9px 12px;border:1px solid {c.line};border-radius:9px;background:{c.surface};}}')
        layout.addWidget(state['src_stack'])
        return card

    def build_sound(self):
        card,layout=self._card();self.sound_card=card
        row=QHBoxLayout();row.addWidget(self._label('配音方式',True))
        self.language=ConsumerSelect(list(LANGUAGES.items()),value='Chinese',accessible_name='Qwen3 配音语言',consumer_theme_scope='coreWorkspace',paper_popup=True)
        self.language.setFixedSize(112,36);self.language.setToolTip('配音语言；选择语言不会翻译文案');row.addWidget(self.language);row.addStretch()
        self.group=QButtonGroup(self);self.mode_buttons={}
        for mode,title in MODES.items():
            b=self._button(title,lambda _=False,m=mode:self.change_mode(m));b.setObjectName('qwenMode');b.setCheckable(True);self.group.addButton(b);self.mode_buttons[mode]=b;row.addWidget(b)
        layout.addLayout(row);self.stack=QStackedWidget();layout.addWidget(self.stack)
        clone=QWidget();box=QVBoxLayout(clone);box.setContentsMargins(0,0,0,0);box.setSpacing(12)
        row=QHBoxLayout();self.reference_play=self._button('',lambda:self.play(True));self.reference_play.setFixedSize(38,38);self.reference_play.setStyleSheet(self.reference_play.styleSheet()+"QPushButton {padding:0;border-radius:19px;}");row.addWidget(self.reference_play)
        selector=self.page._build_voice_selector(self.voice_combo);self.page._voice_sel[id(self.voice_combo)]['manage'].hide();selector.setMaximumWidth(250);row.addWidget(selector,2)
        self.reference_wave=VoiceWaveSlider(self);self.reference_wave.seekRequested.connect(self.reference_player.setPosition)
        self.reference_player.positionChanged.connect(self.reference_wave.setValue);self.reference_player.durationChanged.connect(lambda n:self.reference_wave.setRange(0,n));row.addWidget(self.reference_wave,3);box.addLayout(row)
        follow=QFrame();follow.setObjectName('qwenFollow');r=QHBoxLayout(follow);r.setContentsMargins(16,12,16,12)
        emblem=QLabel();emblem.setPixmap(icons.pixmap('waveform',self.page._consumer.accent,26));emblem.setFixedWidth(38);r.addWidget(emblem)
        words=QVBoxLayout();self.follow_title=QLabel('跟随原声，自然表达');self.follow_title.setStyleSheet('font-size:16px;font-weight:600;');words.addWidget(self.follow_title)
        self.reference_hint=self._label('');words.addWidget(self.reference_hint);r.addLayout(words,1);self.texture=_FollowTexture(self.page._consumer.accent);r.addWidget(self.texture,1)
        self.reference_edit=self._button('查看与校对',self.edit_reference);r.addWidget(self.reference_edit);box.addWidget(follow)
        self.no_text=LuluCheckBox('无需参考文字',parent=self);self.no_text.setChecked(bool(self.options['clone'].get('x_vector_only_mode')));self.no_text.toggled.connect(self.sync_voice);self.no_text.hide()
        self.stack.addWidget(clone)
        official=QWidget();box=QVBoxLayout(official);box.setContentsMargins(0,0,0,0)
        row=QHBoxLayout();self.speaker=self.options['official'].get('speaker','Serena');self.speaker_button=self._button('Serena · 温暖温柔',self.pick_speaker);row.addWidget(self.speaker_button);row.addStretch();box.addLayout(row)
        self.direction=QPlainTextEdit();self.direction.setAccessibleName('Qwen3 表达要求');self.direction.setPlaceholderText('希望怎么说？例如：语气轻松，像在和朋友分享。');self.direction.setFixedHeight(86);self._style_sound_editor(self.direction);box.addWidget(self.direction)
        self.direction.setPlainText(self.options['official'].get('instruction',''))
        box.addLayout(self.presets(self.direction,[('平静叙述','平静自然地叙述，语气从容。'),('开心分享','开心地分享，语气轻松。'),('轻声安慰','轻声安慰，语气温柔。'),('明快讲述','节奏明快，清楚有力地讲述。')]))
        self.stack.addWidget(official)
        design=QWidget();box=QVBoxLayout(design);box.setContentsMargins(0,0,0,0)
        box.addWidget(self._label('描述你想要的声音'))
        self.description=QPlainTextEdit();self.description.setAccessibleName('Qwen3 声音描述');self.description.setPlaceholderText('例如：成年男性，声音温暖低沉，像朋友一样自然地讲述。');self.description.setFixedHeight(100);self._style_sound_editor(self.description);box.addWidget(self.description)
        self.description.setPlainText(self.options['design'].get('instruction',''))
        box.addLayout(self.presets(self.description,[('温暖女声','年轻女性，声音温暖清澈，自然松弛地讲述。'),('沉稳男声','成年男性，声音低沉醇厚，语气沉稳自然。'),('活力分享','年轻女性，声音明亮有活力，自然地分享好消息。')]))
        self.stack.addWidget(design)
        self.description.textChanged.connect(self.sync_text)
        self.model_status=self._label('');self.model_status.setWordWrap(True);layout.addWidget(self.model_status)
        return card

    def presets(self,editor,items):
        row=QHBoxLayout();buttons={}
        for title,prompt in items:
            def apply(_=False,p=prompt):
                cursor=editor.textCursor();cursor.beginEditBlock();cursor.select(QTextCursor.Document);cursor.insertText(p);cursor.endEditBlock();editor.setTextCursor(cursor)
            b=self._button(title,apply);b.setCheckable(True);buttons[prompt]=b;row.addWidget(b)
        row.addStretch()
        editor.textChanged.connect(lambda:[b.setChecked(editor.toPlainText()==p) for p,b in buttons.items()])
        return row

    def build_actions(self):
        wrap=QWidget();box=QVBoxLayout(wrap);box.setContentsMargins(0,0,0,0);row=QHBoxLayout()
        self.manage_button=self._button('音色管理',lambda:self.page._open_voice_picker(self.voice_combo));row.addWidget(self.manage_button)
        self.settings_button=self._button('高级设置 · 默认',self.edit_settings);row.addWidget(self.settings_button)
        self.release_button=self._button('释放内存',self.release);row.addWidget(self.release_button);row.addStretch()
        self.preview_button=self._button('试听选段',lambda:self.generate(True));row.addWidget(self.preview_button)
        self.generate_button=self._button('生成全文',self.generate,True);row.addWidget(self.generate_button)
        self.cancel_button=self._button('取消生成',lambda:self.worker.request_cancel() if self.worker else None);self.cancel_button.hide();row.addWidget(self.cancel_button);box.addLayout(row)
        self.install_feature_button=self._button('安装所需功能',self.open_feature_install);box.addWidget(self.install_feature_button,0,Qt.AlignLeft)
        self.status=self._label('');self.status.setWordWrap(True);box.addWidget(self.status)
        self.progress_bar=QProgressBar();self.progress_bar.setTextVisible(False);self.progress_bar.setFixedHeight(5);self.progress_bar.hide();box.addWidget(self.progress_bar)
        return wrap

    def build_result(self):
        card,layout=self._card();row=QHBoxLayout();row.addWidget(self._label('本次结果',True));self.result_hint=ElideLabel('');row.addWidget(self.result_hint,1);history=self._button('历史记录',self.history);result_style.action_style(history,self.page._consumer);row.addWidget(history);layout.addLayout(row)
        result_style.card_style(card,layout,self.page._consumer)
        self.empty=self._label(result_style.EMPTY_TEXT);layout.addWidget(self.empty)
        self.result_body=QWidget();box=QVBoxLayout(self.result_body);box.setContentsMargins(0,0,0,0);row=QHBoxLayout()
        self.play_button=self._button('',self.play);self.play_button.setFixedSize(38,38);self.play_button.setStyleSheet(self.play_button.styleSheet()+"QPushButton {padding:0;border-radius:19px;}");row.addWidget(self.play_button)
        self.wave=VoiceWaveSlider(self);self.wave.seekRequested.connect(self.player.setPosition);row.addWidget(self.wave,1)
        self.time=self._label('0:00 / 0:00');row.addWidget(self.time);box.addLayout(row)
        self.player.durationChanged.connect(self.result_duration);self.player.positionChanged.connect(self.position)
        row=QHBoxLayout();self.save_button=self._button('保存为音色',self.save_voice);row.addWidget(self.save_button);row.addStretch()
        self.export_format=ConsumerSelect([('wav','WAV'),('mp3','MP3')],value='wav',accessible_name='下载音频格式',consumer_theme_scope='coreWorkspace',paper_popup=True);self.export_format.setFixedWidth(84);row.addWidget(self.export_format)
        self.export_button=self._button('下载音频',self.export);row.addWidget(self.export_button);self.output_dir_button=result_style.OutputLocationButton();self.output_dir_button.clicked.connect(self.page._pick_output_dir);self.output_dir_button.set_path(self.page.cfg.tts_output_dir);row.addWidget(self.output_dir_button);box.addLayout(row)
        result_style.player_style(self.play_button,self.wave,self.time,self.page._consumer)
        result_style.action_style(self.export_button,self.page._consumer,download=True)
        result_style.action_style(self.output_dir_button,self.page._consumer,location=True)
        result_style.action_style(self.save_button,self.page._consumer)
        self.export_format.setFixedHeight(38);box.setSpacing(14);row.setSpacing(10)
        layout.addWidget(self.result_body);self.result_body.hide();self.sync_playback(False);self.sync_playback(True)
        return card

    def refresh(self):
        selected=self.voice_combo.currentData()
        with QSignalBlocker(self.voice_combo):
            self.voice_combo.clear()
            for v in voices.list_voices():self.voice_combo.addItem(v.name,v.id)
            index=self.voice_combo.findData(selected)
            if index>=0:self.voice_combo.setCurrentIndex(index)
        self.page._update_voice_selector(self.voice_combo);self.sync_voice();self.sync_text()

    def sync_voice(self,*_):
        voice=voices.get_voice(str(self.voice_combo.currentData() or ''));path=voice.reference_audio_path if voice else ''
        if path!=self._voice_path:
            self._voice_path=path;self.reference_player.stop();self.reference_player.setSource(QUrl.fromLocalFile(path) if path else QUrl());self.reference_wave.load_file(path)
        self.reference_play.setEnabled(bool(path and Path(path).is_file()))
        self.reference_edit.setEnabled(bool(voice) and not self.busy)
        self.follow_title.setText('保留音色，自然朗读' if self.no_text.isChecked() else '跟随原声，自然表达')
        self.reference_hint.setText('无需参考文字' if self.no_text.isChecked() else '参考文字已保存' if voice and voice.reference_text.strip() else '参考文字待校对')
        if hasattr(self,'generate_button'):self.sync_text()

    def sync_text(self):
        if not hasattr(self,'generate_button'):return
        text=self.text_edit.toPlainText();self.count.setText(f'{len(text.strip())} 字')
        self.clear.setEnabled(bool(text) and not self.busy)
        ready,note=self.page.model_readiness(self.mode)
        from ..core.synthesis.qwen3_weights import FEATURES
        self.install_feature_button.setText('安装'+FEATURES[MODEL_NAMES[self.mode]]);self.install_feature_button.setVisible(not ready)
        self.model_status.setText(note if not ready else '');self.model_status.setVisible(not ready)
        valid=bool(text.strip()) and (self.mode!='design' or bool(self.description.toPlainText().strip()))
        if self.mode=='clone':
            voice=voices.get_voice(str(self.voice_combo.currentData() or ''))
            valid=valid and bool(voice and Path(voice.reference_audio_path).is_file() and (voice.reference_text.strip() or self.no_text.isChecked()))
        for b in (self.generate_button,self.preview_button):b.setEnabled(ready and valid and not self.busy and not (self.release_worker and self.release_worker.isRunning()));b.setToolTip('' if ready else note)

    def open_feature_install(self):
        main=self.page.main;main._switch_page(1);main.models_page.show_purpose(1,sub=0)
        main.models_page.request_feature(MODEL_NAMES[self.mode])

    def change_mode(self,mode):
        if self.busy:return
        self.mode=mode;self.mode_buttons[mode].setChecked(True);self.stack.setCurrentIndex(list(MODES).index(mode));self.stop_playback()
        self.generate_button.setText('生成声音候选' if mode=='design' else '生成全文')
        adjusted=any(self.options[mode].get(k)!=DEFAULTS[k] for k in ('temperature','repetition_penalty')) or (mode=='clone' and self.no_text.isChecked())
        self.settings_button.setText('高级设置 · '+('已调整' if adjusted else '默认'));self.sync_text()
        self.page._refit_panel()
        if hasattr(self.page,'_qwen3'):self.page._emit_model_context_changed()

    def clear_text(self):
        self.text_edit.selectAll();self.text_edit.insertPlainText('');self.text_edit.setFocus();self.page.main._toast('文案已清空，可按 ⌘Z 撤销。')

    def view_text(self):
        d,layout=self._dialog('配音原文');text=QPlainTextEdit(self.text_edit.toPlainText());text.setReadOnly(True);layout.addWidget(text);layout.addWidget(self._button('关闭',d.accept));d.exec();d.deleteLater()

    def edit_reference(self):
        voice=voices.get_voice(str(self.voice_combo.currentData() or ''))
        if not voice:return
        d,layout=self._dialog('参考录音与文字');layout.addWidget(self._button('播放 / 暂停参考',lambda:self.play(True)));text=QPlainTextEdit(voice.reference_text);layout.addWidget(text);error=self._label('');layout.addWidget(error)
        def save():
            try:voices.update_reference_text(voice.id,text.toPlainText())
            except (OSError,ValueError) as exc:error.setText(str(exc));return
            self.sync_voice();d.accept()
        row=QHBoxLayout();row.addStretch();row.addWidget(self._button('取消',d.reject));row.addWidget(self._button('保存校对',save,True));layout.addLayout(row);d.exec();self.reference_player.stop();d.deleteLater()

    def pick_speaker(self):
        from . import voice_page as module
        from .control_foundation import _repolish
        d, layout = self._dialog('选择官方音色')
        d.resize(620, 680)
        d.setMinimumSize(480, 520)
        c = self.page._consumer
        d.setStyleSheet(d.styleSheet() + f'''
            QFrame#speakerRow {{background:{c.surface};border:1px solid {c.line};border-radius:12px;}}
            QFrame#speakerRow[selected="true"] {{background:{c.accent_wash};border-color:{c.accent};}}
            QLabel#speakerCount {{color:{c.ink_muted};font-size:12px;}}
        ''')
        hint = self._label('先试听，再选择适合这段文案的声音')
        layout.addWidget(hint)
        search = QLineEdit()
        search.setPlaceholderText('搜索音色或声音特点')
        search.setAccessibleName('搜索官方音色')
        layout.addWidget(search)
        filter_row = QHBoxLayout()
        filters = QButtonGroup(d)
        filters.setExclusive(True)
        category = '全部'
        for label in ('全部', '中文', '英文', '日语', '韩语'):
            button = self._button(label, lambda _=False, value=label: change_filter(value))
            button.setCheckable(True)
            button.setChecked(label == category)
            filters.addButton(button)
            filter_row.addWidget(button)
        filter_row.addStretch()
        layout.addLayout(filter_row)
        count = self._label('9 个官方音色')
        count.setObjectName('speakerCount')
        layout.addWidget(count)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 0, 4, 0)
        rows.setSpacing(8)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        sample_player = module.QMediaPlayer(d)
        sample_audio = module.QAudioOutput(d)
        sample_player.setAudioOutput(sample_audio)
        playing = ''
        selected = self.speaker
        entries = {}
        listen_buttons = {}
        selection = QButtonGroup(d)
        selection.setExclusive(True)
        note = self._label('试听只播放内置样音，无需等待生成')
        note.setAccessibleName('样音播放状态')
        note.setWordWrap(True)
        progress = QProgressBar()
        progress.setRange(0, 1000)
        progress.setValue(0)
        progress.setTextVisible(False)
        progress.setFixedHeight(3)
        progress.hide()

        def sync(*_):
            for name, button in listen_buttons.items():
                active = playing == name and sample_player.playbackState() == module.QMediaPlayer.PlayingState
                button.setText('Ⅱ 暂停' if active else '▶ 试听')
                button.setAccessibleName(('暂停 ' if active else '试听 ') + name)
            if playing and sample_player.playbackState() == module.QMediaPlayer.StoppedState:
                note.setText('试听结束 · ' + playing)

        def failed(*_):
            nonlocal playing
            playing = ''
            sample_player.stop()
            note.setText('样音暂时无法播放，请重试。')
            progress.hide()
            sync()

        def preview(name):
            nonlocal playing
            path = OFFICIAL_SAMPLES / (name + '.wav')
            if not path.is_file():
                sample_player.stop()
                note.setText('样音文件缺失，请重新安装 Lulu 后重试。')
                progress.hide()
                return
            if playing == name and sample_player.playbackState() == module.QMediaPlayer.PlayingState:
                sample_player.pause()
                note.setText('已暂停 · ' + name)
                return
            self.stop_playback()
            for other in (self.page.player, self.page.indextts2_player, self.page._approved_vox.voice_player):
                other.pause()
            if playing != name:
                sample_player.stop()
                playing = name
                sample_player.setSource(QUrl.fromLocalFile(str(path)))
                progress.setValue(0)
            note.setText('正在试听 · ' + name + ' · ' + SPEAKERS[name])
            progress.show()
            sample_player.play()

        def choose(name):
            nonlocal selected
            selected = name
            for key, (frame, button, _) in entries.items():
                frame.setProperty('selected', key == name)
                button.setChecked(key == name)
                _repolish(frame)
            use.setText('使用 ' + name)
            use.setAccessibleName('使用 ' + name)

        def apply_filter():
            query = search.text().strip().casefold()
            visible = 0
            for name, (frame, _, language) in entries.items():
                show = (category == '全部' or category == language) and query in (name + ' ' + SPEAKERS[name]).casefold()
                frame.setVisible(show)
                visible += show
            count.setText(f'{visible} 个音色' if visible else '没有找到匹配的音色，试试其他关键词')
            empty.setVisible(not visible)

        def change_filter(value):
            nonlocal category
            category = value
            apply_filter()

        def commit():
            self.speaker = selected
            self.speaker_button.setText(selected + ' · ' + SPEAKERS[selected])
            d.accept()

        for name, description in SPEAKERS.items():
            language = '中文' if name in ('Vivian', 'Serena', 'Uncle_Fu', 'Dylan', 'Eric') else '英文' if name in ('Ryan', 'Aiden') else '日语' if name == 'Ono_Anna' else '韩语'
            frame = QFrame()
            frame.setObjectName('speakerRow')
            frame.setProperty('selected', name == selected)
            frame.setStyleSheet(f'QFrame#speakerRow {{background:{c.surface};border:1px solid {c.line};border-radius:12px;}} QFrame#speakerRow[selected="true"] {{background:{c.accent_wash};border-color:{c.accent};}}')
            row = QHBoxLayout(frame)
            row.setContentsMargins(4, 4, 12, 4)
            row.setSpacing(12)
            button = self._button(name + '\n' + description, lambda _=False, n=name: choose(n))
            button.setAccessibleName('选择 ' + name + '，' + description)
            button.setCheckable(True)
            button.setChecked(name == selected)
            button.setFixedHeight(60)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setStyleSheet(f'''QPushButton {{text-align:left;padding:6px 14px;border:0;background:transparent;color:{c.ink};font-size:13px;}}
                QPushButton:checked {{color:{c.accent};font-weight:600;}}
                QPushButton:focus {{border:1px solid {c.accent};border-radius:8px;}}
                QPushButton:hover {{background:{c.accent_wash};border-radius:8px;}}''')
            button.setText('')
            text_layout = QVBoxLayout(button)
            text_layout.setContentsMargins(14, 6, 8, 6)
            text_layout.setSpacing(3)
            title = QLabel(name.replace('_', ' '), button)
            title.setStyleSheet(f'background:transparent;color:{c.ink};font-size:15px;font-weight:600;')
            subtitle = QLabel(description, button)
            subtitle.setStyleSheet(f'background:transparent;color:{c.ink_muted};font-size:12px;')
            for label in (title, subtitle):
                label.setAttribute(Qt.WA_TransparentForMouseEvents)
                text_layout.addWidget(label)
            selection.addButton(button)
            row.addWidget(button, 1)
            listen = self._button('▶ 试听', lambda _=False, n=name: preview(n))
            listen.setFixedSize(84, 34)
            listen.setAccessibleName('试听 ' + name)
            row.addWidget(listen)
            rows.addWidget(frame)
            listen_buttons[name] = listen
            entries[name] = (frame, button, language)
        empty = self._label('换个关键词，或切回「全部」看看')
        empty.setAlignment(Qt.AlignCenter)
        rows.addWidget(empty)
        empty.hide()
        rows.addStretch()
        layout.addWidget(progress)
        layout.addWidget(note)
        footer = QHBoxLayout()
        footer.addWidget(self._button('取消', d.reject))
        footer.addStretch()
        use = self._button('使用 ' + selected, commit, True)
        use.setAccessibleName('使用 ' + selected)
        footer.addWidget(use)
        layout.addLayout(footer)
        search.textChanged.connect(apply_filter)
        sample_player.playbackStateChanged.connect(sync)
        if hasattr(sample_player, 'positionChanged'):
            sample_player.positionChanged.connect(lambda position: progress.setValue(int(position * 1000 / max(1, sample_player.duration()))))
        if hasattr(sample_player, 'errorOccurred'):
            sample_player.errorOccurred.connect(failed)
        d.finished.connect(lambda *_: sample_player.stop())
        try:
            d.exec()
        finally:
            sample_player.stop()
            sample_player.setSource(QUrl())
            d.deleteLater()

    def edit_settings(self):
        d,layout=self._dialog('生成设置');fields={}
        for key,title,low,high in [('temperature','生成变化',.1,2),('repetition_penalty','重复惩罚',1,2)]:
            row=QHBoxLayout();row.addWidget(self._label(title),1);field=QDoubleSpinBox();field.setRange(low,high);field.setSingleStep(.05);field.setValue(self.options[self.mode].get(key,DEFAULTS[key]));field.setAccessibleName(title);fields[key]=field;row.addWidget(field);layout.addLayout(row)
        clone_option=None
        if self.mode=='clone':
            clone_option=LuluCheckBox('无需参考文字');clone_option.setAccessibleName('无需参考文字');clone_option.setChecked(self.no_text.isChecked());layout.addWidget(clone_option)
            note=self._label('仅在无法提供准确录音文字时使用，克隆质量可能降低。');note.setWordWrap(True);layout.addWidget(note)
        layout.addWidget(self._label('默认值优先。已有音频不会随设置变化。'));error=self._label('');layout.addWidget(error)
        def save():
            previous=deepcopy(getattr(self.page.cfg,'qwen3_generation_options',{}));updated=deepcopy(self.options)
            updated[self.mode].update({k:f.value() for k,f in fields.items()})
            if clone_option is not None:updated['clone']['x_vector_only_mode']=clone_option.isChecked()
            self.page.cfg.qwen3_generation_options=updated
            try:
                if self.page.cfg.save() is False: raise OSError("save failed")
            except Exception:self.page.cfg.qwen3_generation_options=previous;error.setText('设置保存失败，请重试。');return
            self.options=updated
            if clone_option is not None:self.no_text.setChecked(clone_option.isChecked());self.sync_voice()
            self.change_mode(self.mode);d.accept()
        def reset():
            for k,f in fields.items():f.setValue(DEFAULTS[k])
            if clone_option is not None:clone_option.setChecked(False)
        row=QHBoxLayout();row.addWidget(self._button('恢复默认',reset));row.addStretch();row.addWidget(self._button('取消',d.reject));row.addWidget(self._button('完成',save,True));layout.addLayout(row);d.exec();d.deleteLater()

    def generation_options(self):
        return validate_options({**self.options[self.mode],'mode':self.mode,'language':self.language.currentData(),'speaker':self.speaker,'x_vector_only_mode':self.no_text.isChecked(),'instruction':self.direction.toPlainText() if self.mode=='official' else self.description.toPlainText() if self.mode=='design' else ''})

    def generate(self,preview=False):
        from . import voice_page as module
        if self.busy or not self.page._guard_local_synthesis_start('qwen3'):return
        if self.release_worker and self.release_worker.isRunning():
            self.page.main._toast('正在释放模型内存，请稍后重试。');return
        text=self.text_edit.textCursor().selectedText().replace('\u2029','\n') if preview else self.text_edit.toPlainText()
        if preview and not text.strip():dialogs.alert(self,'试听选段','请先在文案中选中文字。');return
        voice=deepcopy(voices.get_voice(str(self.voice_combo.currentData() or ''))) if self.mode=='clone' else None
        try:
            options=self.generation_options()
            generation_inputs(text,voice,options)
        except ValueError as exc:dialogs.alert(self,'生成配音',str(exc));return
        ready,note=qwen3_runtime.readiness(self.mode)
        if not ready:dialogs.alert(self,'模型尚未就绪',note);return
        signature=(text,voice.id if voice else '',repr(options))
        if signature==self.last_input or self.mode=='design':options['seed']=secrets.randbelow(2**32)
        self.last_input=signature
        out=unique_output_path(library.audio_storage_dir(preview=bool(preview))/('Lulu_Qwen_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.wav'))
        self.task={'autoplay':False,'preview':bool(preview),'voice_name':voice.name if voice else self.speaker if self.mode=='official' else '设计声音','source':self.page._source_payload_for_text(text)}
        self.stop_playback();self.set_busy(True);self.status.setText('准备生成…')
        self.worker=module.SynthesisWorker(text,voice.id if voice else '',str(out),'wav',engine=ENGINE_ID,engine_options=options,voice_profile=voice)
        self.worker.progress.connect(lambda message,_:self.status.setText(message));self.worker.finished_ok.connect(self.done);self.worker.failed.connect(self.failed);self.worker.cancelled.connect(self.cancelled);self.worker.start()

    def set_busy(self,value):
        self.busy=value;self.generate_button.setVisible(not value);self.cancel_button.setVisible(value);self.progress_bar.setVisible(value);self.progress_bar.setRange(0,0)
        for widget in (self.sound_card,self.manage_button,self.settings_button,self.release_button,self.language):widget.setEnabled(not value)
        self.editor_card.setEnabled(not value);self.text_edit.setReadOnly(value);self.sync_text()

    def done(self,result):
        summary=MODES[result.meta['mode']]+' · '+LANGUAGES[result.meta['options']['language']]
        record={'output_path':str(result.output_path),'wav_path':str(result.wav_path),'text':result.text,'voice_id':result.voice_id,'voice_name':self.task['voice_name'],'meta':deepcopy(result.meta),'summary':summary,'duration':result.duration_seconds,'preview':self.task['preview'],'created_at':datetime.now().isoformat(timespec='seconds')}
        saved=True;message=''
        if not self.task['preview']:
            source=self.task['source'];saved,message=self.page._save_voice_safely(result.output_path,title='Qwen3 · '+self.task['voice_name'],text=result.text,engine='Qwen3-TTS',voice_id=result.voice_id or None,voice_name=self.task['voice_name'],duration_seconds=result.duration_seconds,config_summary=summary,meta=result.meta,source_type=source.get('source_type','manual'),source_id=source.get('source_id'),source_title=source.get('source_title'),source_text_snapshot=source.get('source_text_snapshot'),source_chain=source.get('source_chain') or [])
        self.records.insert(0,record);self.select_result(record);self.set_busy(False)
        self.status.setText((('试听完成 · 未入库' if self.task['preview'] else '完成 · 已保存到音频库')+f' · {result.duration_seconds:.2f} 秒') if saved else message)
        if self.task.get('autoplay'):self.play()
        if not saved:dialogs.alert(self,'保存失败',message)

    def failed(self,message):self.set_busy(False);self.status.setText('生成失败，上一份结果已保留。');dialogs.alert(self,'生成失败',message)
    def cancelled(self):self.set_busy(False);self.status.setText('已取消，文案与上一份结果保留。')

    def select_result(self,record):
        self.current_record=record;self.stop_playback();self.player.setSource(QUrl.fromLocalFile(record['output_path']));self.wave.load_file(record['output_path']);self.result_hint.setText(record.get('summary',''))
        self.result_duration(0);self.empty.hide();self.result_body.show();self.save_button.setVisible(record.get('meta',{}).get('mode')=='design');self.page._refit_panel()

    def play(self,reference=False):
        from .voice_page import QMediaPlayer
        player=self.reference_player if reference else self.player;other=self.player if reference else self.reference_player
        if player.playbackState()==QMediaPlayer.PlayingState:player.pause()
        else:
            other.pause()
            for p in (self.page.player,self.page.indextts2_player,self.page._approved_vox.voice_player):p.pause()
            if player.position()>=player.duration()>0:player.setPosition(0)
            player.play()

    def sync_playback(self,reference):
        from .voice_page import QMediaPlayer
        player=self.reference_player if reference else self.player;button=self.reference_play if reference else self.play_button
        playing=player.playbackState()==QMediaPlayer.PlayingState
        button.setIcon(QIcon(icons.pixmap('pause' if playing else 'play',self.page._consumer.accent if reference else '#ffffff',18)));button.setAccessibleName(('暂停' if playing else '播放')+('参考录音' if reference else '本次结果'))

    def result_duration(self,n):
        total=n or int((self.current_record or {}).get('duration',0)*1000)
        self.wave.setRange(0,total);self.position(self.player.position())

    def position(self,n):
        self.wave.setValue(n)
        def fmt(ms):return f'{ms//60000}:{ms//1000%60:02d}'
        self.time.setText(fmt(n)+' / '+fmt(self.player.duration() or int((self.current_record or {}).get('duration',0)*1000)))
    def stop_playback(self):self.reference_player.stop();self.player.stop()

    def export(self):
        if not self.current_record:return
        path=Path(self.current_record['wav_path'])
        if not path.is_file():dialogs.alert(self,'音频暂不可用','文件已移走或删除。');return
        if self.export_worker and self.export_worker.isRunning():return
        self.export_button.setEnabled(False)
        self.export_worker=ExportWorker(path,Path(self.page.cfg.tts_output_dir),self.export_format.currentData(),self)
        self.export_worker.completed.connect(lambda target:self.page.main._toast('已下载到 '+target))
        self.export_worker.failed.connect(lambda message:dialogs.alert(self,'下载失败',message))
        self.export_worker.finished.connect(lambda:self.export_button.setEnabled(True))
        self.export_worker.start()

    def save_voice(self):
        if not self.current_record:return
        record=deepcopy(self.current_record);d,layout=self._dialog('保存为我的音色');name=QLineEdit('我的设计声音');name.setAccessibleName('音色名称');layout.addWidget(name);error=self._label('');layout.addWidget(error)
        def save():
            if not name.text().strip():error.setText('请填写音色名称。');return
            try:v=voices.add_voice(name.text(),record['wav_path'],record['text'],engine=ENGINE_ID,meta={'origin':'qwen3_voice_design','generation':deepcopy(record.get('meta',{}))})
            except (OSError,ValueError) as exc:error.setText(str(exc));return
            self.page.refresh();self.voice_combo.setCurrentIndex(self.voice_combo.findData(v.id));d.accept()
            if qwen3_runtime.readiness('clone')[0]:
                self.change_mode('clone');self.page.main._toast('音色已保存，可用新文案继续配音。')
            else:
                self.page.main._toast('音色已保存。用它读新文案时，请到 AI 大模型安装千问的声音克隆。')
        row=QHBoxLayout();row.addStretch();row.addWidget(self._button('取消',d.reject));row.addWidget(self._button('保存音色',save,True));layout.addLayout(row);d.exec();d.deleteLater()

    def history(self):
        self.stop_playback();d,layout=self._dialog('历史记录');d.resize(660,600)
        if self.previous_draft:
            recover=self._button('找回刚才的草稿',lambda:(self.restore_draft(self.previous_draft),d.accept()));recover.setEnabled(not self.busy);layout.addWidget(recover)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame);content=QWidget();rows=QVBoxLayout(content);scroll.setWidget(content);layout.addWidget(scroll)
        entries=list(self.records);seen={r['output_path'] for r in entries}
        for entry in library.list_voice_entries():
            if entry.get('engine')!='Qwen3-TTS' or entry.get('audio_path') in seen:continue
            v=library.load_voice(entry['id'])
            if not v: continue
            entries.append({'output_path':v['audio_path'],'wav_path':v['audio_path'],'text':v.get('text',''),'voice_id':v.get('voice_id',''),'voice_name':v.get('voice_name',''),'meta':v.get('meta') or {},'summary':v.get('config_summary',''),'created_at':v.get('created_at',''),'duration':v.get('duration_seconds') or 0})
        if not entries:rows.addWidget(self._label('还没有生成记录'))
        for record in entries:
            card,box=self._card();box.addWidget(self._label(record.get('summary','Qwen3 配音')));excerpt=self._label(record['text'][:120]);excerpt.setWordWrap(True);box.addWidget(excerpt);row=QHBoxLayout()
            def view(_=False,r=record):self.select_result(r);d.accept();self.play()
            b=self._button('试听结果',view);b.setEnabled(Path(record['output_path']).is_file());row.addWidget(b);row.addStretch()
            def restore(_=False,r=record):
                try:opts=validate_options(r.get('meta',{}).get('options',{}))
                except ValueError:dialogs.alert(self,'设置不可恢复','这份记录没有完整设置，原音频仍可试听。');return
                if not r.get('meta',{}).get('options'):dialogs.alert(self,'设置不可恢复','这份记录没有完整设置，原音频仍可试听。');return
                self.previous_draft=self.draft()
                self.restore_draft({'text':r['text'],'options':{**deepcopy(self.options),opts['mode']:opts},'mode':opts['mode'],'language':opts['language'],'speaker':opts['speaker'],'no_text':opts['x_vector_only_mode'],'direction':opts['instruction'] if opts['mode']=='official' else self.direction.toPlainText(),'description':opts['instruction'] if opts['mode']=='design' else self.description.toPlainText(),'voice_id':r.get('voice_id','')})
                voice=voices.get_voice(str(r.get('voice_id') or ''))
                if opts['mode']=='clone' and (not voice or voice.reference_text!=r.get('meta',{}).get('reference_text','')):
                    self.status.setText('文案与设置已恢复。参考音色已变化或缺失，请重新确认后生成。')
                d.accept()
            b=self._button('恢复文案与设置',restore);b.setEnabled(not self.busy);row.addWidget(b);box.addLayout(row);rows.addWidget(card)
        rows.addStretch();layout.addWidget(self._button('关闭',d.accept));d.exec();d.deleteLater()

    def draft(self):
        return {'text':self.text_edit.toPlainText(),'options':deepcopy(self.options),'mode':self.mode,'language':self.language.currentData(),'speaker':self.speaker,'no_text':self.no_text.isChecked(),'direction':self.direction.toPlainText(),'description':self.description.toPlainText(),'voice_id':self.voice_combo.currentData()}

    def restore_draft(self,draft):
        self.page._src_set_mode('qwen3','custom');self.text_edit.setPlainText(draft['text']);self.options=deepcopy(draft['options'])
        self.language.set_value(draft['language']);self.speaker=draft['speaker'];self.speaker_button.setText(self.speaker+' · '+SPEAKERS[self.speaker])
        self.no_text.setChecked(draft['no_text']);self.direction.setPlainText(draft['direction']);self.description.setPlainText(draft['description'])
        self.voice_combo.setCurrentIndex(self.voice_combo.findData(draft['voice_id']));self.change_mode(draft['mode'])

    def release(self):
        if self.busy or (self.release_worker and self.release_worker.isRunning()):return
        self.release_button.setEnabled(False);self.status.setText('正在释放模型内存…')
        self.release_worker=ReleaseWorker(self)
        def completed(ok):
            self.release_button.setEnabled(True)
            self.status.setText('模型内存已释放，下次生成会重新加载。' if ok else '正在生成，请先取消或等待完成。')
        self.release_worker.completed.connect(completed);self.release_worker.finished.connect(self.sync_text);self.release_worker.start();self.sync_text()

    def shutdown(self):
        if self.worker and self.worker.isRunning():self.worker.request_cancel();self.worker.wait(12000)
        if self.release_worker and self.release_worker.isRunning():self.release_worker.wait(12000)
        if self.export_worker and self.export_worker.isRunning():self.export_worker.requestInterruption();self.export_worker.wait(12000)
        self.stop_playback();self.reference_wave.stop_decode();self.wave.stop_decode()
        if not self.worker or not self.worker.isRunning():
            from ..core.synthesis.qwen3_subprocess import shutdown_persistent_worker
            shutdown_persistent_worker()
