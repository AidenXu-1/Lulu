"""Finished audio first: select, audition, regenerate, compare, then save a copy."""
from pathlib import Path
import uuid

from PySide6.QtCore import QThread, Signal, Qt, QUrl, QRectF, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QIcon
from PySide6.QtMultimedia import QMediaPlayer
from .audio_output import create_audio_output as QAudioOutput
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QDoubleSpinBox, QAbstractSpinBox, QPlainTextEdit, QFileDialog, QScrollArea, QWidget, QFrame, QMessageBox)

from ..core import library
from ..core.synthesis.local_adjustment import LocalAdjustment
from ..core.synthesis.segment_project import ProjectCancelled
from . import theme as T
from . import icons
from .control_foundation import adopt_button
from .consumer_select import ConsumerSelect
from .voice_wave_slider import VoiceWaveSlider


class AdjustmentWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str, float)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancelled = False

    def run(self):
        try:
            result = self.operation(self)
            self.completed.emit(result)
        except Exception as exc:
            from ..core.synthesis.indextts2_subprocess import was_cancelled
            self.failed.emit('已取消，原版和已有候选保留。' if self.cancelled or
                             isinstance(exc, ProjectCancelled) or was_cancelled(exc) else str(exc))


class RangeWave(VoiceWaveSlider):
    selectionChanged = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection = (0, 0)
        self._drag = None
        self._moved = False
        self.setFixedHeight(92)
        self.setMouseTracking(True)

    def _finished(self, process, code):
        super()._finished(process, code)
        if self.peaks:
            self.setToolTip('拖动选择一段声音；拖动两端调整；单击定位播放点。')

    def _x(self, value):
        return 12 + value / max(1, self.maximum()) * (self.width() - 24)

    def _time(self, x):
        return round(max(0, min(1, (x - 12) / max(1, self.width() - 24))) * self.maximum())

    def _select(self, left, right):
        self.selection = (left, right)
        self.selectionChanged.emit(left, right)
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or not self.maximum():
            return
        self.setFocus(Qt.MouseFocusReason)
        self._anchor_x = event.position().x()
        self._anchor = self._time(self._anchor_x)
        self._moved = False
        left, right = self.selection
        if abs(self._anchor_x - self._x(left)) <= 12:
            self._drag = 'left'
        elif abs(self._anchor_x - self._x(right)) <= 12:
            self._drag = 'right'
        else:
            self._drag = 'range'
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._drag:
            near_handle = any(abs(event.position().x() - self._x(t)) <= 12 for t in self.selection)
            self.setCursor(Qt.SizeHorCursor if near_handle else Qt.CrossCursor)
            return
        value = self._time(event.position().x())
        self._moved |= abs(event.position().x() - self._anchor_x) > 3
        left, right = self.selection
        if self._drag == 'left':
            self._select(min(value, right - 1), right)
        elif self._drag == 'right':
            self._select(left, max(value, left + 1))
        elif self._moved and value != self._anchor:
            self._select(*sorted((self._anchor, value)))
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag:
            self.mouseMoveEvent(event)
            if self._drag == 'range' and not self._moved:
                self.setValue(self._anchor)
                self.seekRequested.emit(self._anchor)
            self._drag = None
            event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        c = T.consumer_theme(T.ACTIVE.name)
        left, right = [self._x(t) for t in self.selection]
        painter.setPen(Qt.NoPen)
        fill = QColor(c.accent); fill.setAlpha(24)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(left, 22, max(0, right - left), 62), 6, 6)
        values = self.peaks or [0] * 100
        for i, amplitude in enumerate(values):
            x = 12 + i * (self.width() - 24) / max(1, len(values) - 1)
            height = max(2, amplitude * 48)
            color = QColor(c.accent if left <= x <= right else c.ink_muted)
            if not left <= x <= right:
                color.setAlpha(95)
            painter.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(int(x), int(53 - height / 2), int(x), int(53 + height / 2))
        if not self.maximum():
            return
        for x, label in ((left, '起点'), (right, '终点')):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(c.accent))
            painter.drawRoundedRect(QRectF(x - 5, 22, 10, 62), 4, 4)
            painter.setPen(QPen(QColor('#ffffff'), 1.4))
            painter.drawLine(int(x), 44, int(x), 62)
            painter.setPen(QColor(c.accent))
            painter.drawText(QRectF(max(0, min(self.width() - 38, x - 19)), 0, 38, 20), Qt.AlignCenter, label)
        painter.setPen(QPen(QColor(c.ink), 1))
        painter.drawLine(int(self._x(self.value())), 25, int(self._x(self.value())), 80)
        if self.hasFocus() and self._keyboard_focus:
            painter.setPen(QPen(QColor(c.accent), 1))
            painter.drawLine(12, 90, self.width() - 12, 90)


class LocalAdjustmentDialog(QDialog):
    saved = Signal(object)

    def __init__(self, page, source=None, *, project=None, engine_factory=None):
        super().__init__(page)
        self.page, self.project, self.engine_factory = page, project, engine_factory
        self.worker = None
        self._revision = None
        self._exact_range = None
        self._previewing = False
        self._preview_kind = None
        self.setWindowTitle('局部调整 · IndexTTS 2.5')
        self.resize(900, 740)
        self.setMinimumSize(640, 580)
        c = page._consumer
        self.setStyleSheet(T.QSS + f"""
            QDialog {{background:{c.canvas};}}
            QWidget#localAdjustmentBody {{background:transparent;}}
            QFrame#adjustCard {{background:{c.surface};border:1px solid {c.line};border-radius:14px;}}
            QLabel {{color:{c.ink};background:transparent;border:0;}}
            QLabel#adjustHint {{color:{c.ink_muted};font-size:12px;}}
            QLabel#adjustTitle {{font-size:14px;font-weight:600;}}
            QPlainTextEdit {{background:{c.surface};color:{c.ink};border:1px solid {c.line};
                border-radius:9px;padding:10px;selection-background-color:{c.accent};}}
            QPlainTextEdit:focus {{border:1px solid {c.accent};}}
            QDoubleSpinBox {{background:{c.surface};color:{c.ink};border:1px solid {c.line};
                border-radius:8px;padding:6px;}}
            QScrollArea {{background:transparent;border:0;}}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(12)
        title = QLabel('局部调整')
        title.setStyleSheet('font-size:22px;font-weight:600;')
        outer.addWidget(title)
        self.source_label = QLabel('正在打开音频…')
        self.source_label.setObjectName('adjustHint')
        self.source_label.setWordWrap(True)
        outer.addWidget(self.source_label)
        scroll = self.scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.body = QWidget()
        self.body.setObjectName('localAdjustmentBody')
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        def card(title):
            frame = QFrame()
            frame.setObjectName('adjustCard')
            contents = QVBoxLayout(frame)
            contents.setContentsMargins(18, 14, 18, 14)
            contents.setSpacing(10)
            label = QLabel(title)
            label.setObjectName('adjustTitle')
            contents.addWidget(label)
            layout.addWidget(frame)
            return frame, contents

        _, sound = card('选中要重说的部分')
        hint = QLabel('拖出一段范围，或拖动两端调整。尽量选完整一句。')
        hint.setObjectName('adjustHint')
        hint.setWordWrap(True)
        sound.addWidget(hint)
        self.wave = RangeWave(self)
        self.wave.setAccessibleName('拖动选择要调整的声音；两端可拖动，精确时间可展开微调')
        sound.addWidget(self.wave)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self.position_changed)
        self.player.errorOccurred.connect(lambda _error, message: self.status.setText('暂时无法播放：' + message))
        self.wave.seekRequested.connect(self.seek_original)
        self.wave.selectionChanged.connect(self.range_dragged)
        row = QHBoxLayout()
        self.play_original = self.button('播放', self.play_source)
        self.player.playbackStateChanged.connect(self.playback_changed)
        row.addWidget(self.play_original)
        self.position = QLabel('0:00')
        self.position.setObjectName('adjustHint')
        row.addWidget(self.position)
        row.addStretch()
        self.range_label = QLabel('')
        self.range_label.setObjectName('adjustHint')
        row.addWidget(self.range_label)
        self.listen_range = self.button('试听选段', lambda: self.preview('range'))
        row.addWidget(self.listen_range)
        self.precise_toggle = self.button('微调时间', self.toggle_precision)
        self.precise_toggle.setCheckable(True)
        row.addWidget(self.precise_toggle)
        sound.addLayout(row)
        self.precise = QWidget()
        row = QHBoxLayout(self.precise)
        row.setContentsMargins(0, 0, 0, 0)
        self.start, self.end = self.time_input('调整起点'), self.time_input('调整终点')
        row.addWidget(QLabel('起点'))
        row.addWidget(self.start)
        row.addWidget(QLabel('终点'))
        row.addWidget(self.end)
        row.addStretch()
        self.mark_start = self.button('从播放处开始', lambda: self.start.setValue(self.wave.value() / 1000))
        self.mark_end = self.button('到播放处结束', lambda: self.end.setValue(self.wave.value() / 1000))
        row.addWidget(self.mark_start)
        row.addWidget(self.mark_end)
        sound.addWidget(self.precise)
        self.precise.hide()

        _, words = card('这段要说什么')
        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName('选中范围内要说的话')
        self.editor.setPlaceholderText('填入选中这段的原话，或改成你想说的话。')
        self.editor.setFixedHeight(72)
        words.addWidget(self.editor)
        row = QHBoxLayout()
        self.settings_hint = QLabel('')
        self.settings_hint.setObjectName('adjustHint')
        self.settings_hint.setWordWrap(True)
        row.addWidget(self.settings_hint, 1)
        self.emotion_button = self.button("情绪表达", self.edit_emotion)
        row.addWidget(self.emotion_button)
        self.generate = self.button('重新生成这段', self.generate_one, primary=True)
        row.addWidget(self.generate)
        words.addLayout(row)

        self.comparison, compare = card('听听修改效果')
        row = QHBoxLayout()
        self.versions = ConsumerSelect([], value='', allow_empty=True, empty_text='选择一个版本',
            accessible_name='局部调整候选', consumer_theme_scope='coreWorkspace')
        row.addWidget(self.versions, 1)
        self.compare_original = self.button('听原版', lambda: self.preview('original'))
        self.compare_new = self.button('听新版', lambda: self.preview('candidate'))
        row.addWidget(self.compare_original)
        row.addWidget(self.compare_new)
        compare.addLayout(row)
        self.candidate_hint = QLabel('带上前后各 1 秒，听听接得顺不顺。')
        self.candidate_hint.setObjectName('adjustHint')
        self.candidate_hint.setWordWrap(True)
        compare.addWidget(self.candidate_hint)
        self.comparison.hide()
        layout.addStretch(1)
        scroll.setWidget(self.body)
        outer.addWidget(scroll, 1)
        self.status = QLabel('')
        self.status.setObjectName('adjustHint')
        self.status.setWordWrap(True)
        self.status.setAccessibleName('局部调整状态')
        outer.addWidget(self.status)
        bottom = QHBoxLayout()
        self.cancel_button = self.button('取消生成', self.cancel)
        self.cancel_button.hide()
        bottom.addWidget(self.cancel_button)
        self.keep_original = QLabel('只替换选中部分，原音频保留。')
        self.keep_original.setObjectName('adjustHint')
        bottom.addWidget(self.keep_original, 1)
        self.close_button = self.button('关闭', self.reject)
        self.adopt_button = self.button('保存修改后的音频', self.adopt, primary=True)
        self.adopt_button.hide()
        bottom.addWidget(self.close_button)
        bottom.addWidget(self.adopt_button)
        outer.addLayout(bottom)
        self.start.valueChanged.connect(self.selection_changed)
        self.end.valueChanged.connect(self.selection_changed)
        self.editor.textChanged.connect(self.draft_changed)
        self.versions.currentIndexChanged.connect(self.select_version)
        if project:
            self.load_project()
        else:
            voice = page._selected_indextts2_voice()
            options = page._indextts2_emotion_engine_options()
            options.update(language=str(page.indextts2_language_combo.currentData() or 'ZH'),
                speed=page.indextts2_speed_slider.value() / 100,
                random_emotion=bool(page._approved_index.random_emotion and
                                    page._indextts2_emotion_mode in ('vector', 'emotext')),
                generation_options=dict(getattr(page.cfg, 'indextts25_generation_options', {})))
            parent = library.LIBRARY_DIR / 'adjustments'
            legacy = Path(page.cfg.tts_output_dir) / 'Lulu局部调整'
            self.run_operation('open', lambda w: LocalAdjustment.open_audio(parent, source, voice, options, legacy_parent=legacy))

    def button(self, text, callback, primary=False):
        button = QPushButton(text)
        adopt_button(button, role='primary' if primary else 'secondary', accessible_name=text)
        button.setMinimumHeight(36)
        button.clicked.connect(callback)
        return button

    def time_input(self, name):
        widget = QDoubleSpinBox()
        widget.setDecimals(3)
        widget.setSingleStep(.05)
        widget.setSuffix(' 秒')
        widget.setMinimumHeight(38)
        widget.setButtonSymbols(QAbstractSpinBox.NoButtons)
        widget.setAccessibleName(name)
        widget.setMinimumWidth(92)
        return widget

    def toggle_precision(self):
        self.precise.setVisible(self.precise_toggle.isChecked())

    def range_dragged(self, left, right):
        if not self.project or self.busy():
            return
        for widget, value in ((self.start, left), (self.end, right)):
            widget.blockSignals(True)
            widget.setValue(value / 1000)
            widget.blockSignals(False)
        self.selection_changed()

    def draft_changed(self):
        if self._previewing:
            self.player.stop()
            self._previewing = False
        self.refresh_actions()

    def playback_changed(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.play_original.setText('暂停' if playing else '播放')
        self.play_original.setAccessibleName('暂停试听' if playing else '播放原音频')
        self.play_original.setIcon(QIcon(icons.pixmap('pause' if playing else 'play', self.page._consumer.accent, 16)))
        for kind, button, title in (('range', self.listen_range, '试听选段'),
                                   ('original', self.compare_original, '听原版'),
                                   ('candidate', self.compare_new, '听新版')):
            active = playing and self._previewing and self._preview_kind == kind
            button.setText('暂停' if active else title)
            button.setAccessibleName('暂停' + title if active else title)
            button.setIcon(QIcon(icons.pixmap('pause' if active else 'play', self.page._consumer.accent, 14)))

    def busy(self):
        return self.worker is not None

    def load_project(self, selected=None):
        data = self.project.read()
        self._revision = data['revision']
        state = data['adjustment']
        self.source_label.setText(state['source']['name'] + f" · {state['source']['duration']:.1f} 秒")
        modes = {'follow': '跟随音色', 'emoaudio': '参考音频', 'vector': '精细调节', 'emotext': '文字描述'}
        self.settings_hint.setText('这段情绪 · ' + modes.get(data['options'].get('emotion_mode'), '跟随音色'))
        self.settings_hint.setToolTip('音色：' + data['voice']['name'] + '。可单独设置这段的情绪。')
        rate = state['source']['rate']
        for widget, value in ((self.start, state['start_frame']), (self.end, state['end_frame'])):
            widget.blockSignals(True)
            widget.setRange(0, state['source']['duration'])
            widget.setValue(value / rate)
            widget.blockSignals(False)
        self._exact_range = (state['start_frame'], state['end_frame'])
        self.editor.setPlainText(state['draft'])
        self.wave.setRange(0, round(state['source']['duration'] * 1000))
        self.wave.load_file(str(self.project.source_audio()))
        self.wave.selection = tuple(round(v / rate * 1000) for v in self._exact_range)
        self.wave.update()
        self.versions.blockSignals(True)
        self.versions.clear()
        for index, v in enumerate(data['parts'][0]['versions']):
            if 'range' in v:
                self.versions.addItem(f'第 {index + 1} 版 · {v["duration"]:.1f} 秒', v['id'])
        if selected is None:
            # Restore a matching preview choice; never adopt it automatically.
            selected = next((v['id'] for v in reversed(data['parts'][0]['versions'])
                if v.get('range') == {'start_frame': state['start_frame'], 'end_frame': state['end_frame']}
                and v['text'] == state['draft'].strip() and self.project.settings_match(v)), None)
        self.versions.setCurrentIndex(self.versions.findData(selected) if selected else -1)
        self.versions.blockSignals(False)
        self.playback_changed(self.player.playbackState())
        self.refresh_actions()

    def frames(self):
        if self._exact_range:
            return self._exact_range
        state = self.project.read()['adjustment']['source']
        return tuple(min(state['frames'], round(w.value() * state['rate'])) for w in (self.start, self.end))

    def selection_changed(self):
        if self._previewing:
            self.player.stop()
            self._previewing = False
        self._exact_range = None
        self.wave.selection = (round(self.start.value() * 1000), round(self.end.value() * 1000))
        self.wave.update()
        if self.start.value() >= self.end.value():
            self.status.setText('请把终点放在起点之后。')
        self.refresh_actions()

    def refresh_actions(self):
        ready = bool(self.project) and not self.busy()
        self.body.setEnabled(ready)
        self.adopt_button.setEnabled(False)
        self.comparison.setVisible(self.versions.count() > 0)
        self.adopt_button.setVisible(self.versions.count() > 0)
        if not ready:
            return
        left, right = self.frames()
        valid = left < right
        self.range_label.setText(f'{self.start.value():.2f}～{self.end.value():.2f} 秒')
        self.listen_range.setEnabled(valid)
        self.compare_original.setEnabled(valid)
        length = len(self.editor.toPlainText().strip())
        self.generate.setEnabled(valid and 0 < length <= 600)
        if length > 600:
            self.status.setText('这一段最多 600 字，请缩小调整范围并精简文案。')
        matches = False
        if version_id := self.versions.currentData():
            v = self.project.candidate(version_id)
            matches = (self.project.settings_match(v) and v['text'] == self.editor.toPlainText().strip() and
                       (v['range']['start_frame'], v['range']['end_frame']) == (left, right))
        self.compare_new.setEnabled(matches)
        self.adopt_button.setEnabled(matches)
        role = 'secondary' if matches else 'primary'
        if self.generate.property('role') != role:
            self.generate.setProperty('role', role)
            self.generate.style().unpolish(self.generate)
            self.generate.style().polish(self.generate)
        self.generate.setText('再试一版' if matches else '重新生成这段')
        self.generate.setAccessibleName(self.generate.text())
        self.candidate_hint.setText('带上前后各 1 秒，听听接得顺不顺。' if matches else
            '范围、文字或情绪已改动，请重新生成；也可以选回之前的版本。')

    def select_version(self):
        if self.busy() or not self.project or not self.versions.currentData():
            return
        try:
            v = self.project.candidate(self.versions.currentData())
            self.project.restore_candidate(v['id'], self._revision)
            self.load_project(v['id'])
            self.status.setText('已恢复这版的范围、文案和情绪，可试听后采用。')
        except Exception as exc:
            self.status.setText(str(exc))

    def edit_emotion(self):
        if self.busy() or not self.project:
            return
        from .local_emotion import LocalEmotionDialog
        data = self.project.read()
        ref = data['references'].get('emotion')
        dialog = LocalEmotionDialog(self, data['options'], str(self.project.file(ref['path'])) if ref else '')
        if dialog.exec() == QDialog.Accepted:
            try:
                self.project.set_selection(*self.frames(), self.editor.toPlainText(), self._revision)
                self._revision = self.project.read()['revision']
                self.project.set_emotion(dialog.values(), self._revision)
                self.player.stop()
                self._previewing = False
                self.load_project()
                self.status.setText('情绪已应用到这段，重新生成后可试听。')
            except Exception as exc:
                self.status.setText(str(exc))
        dialog.deleteLater()

    def seek_original(self, position):
        if not self.project or self.busy():
            return
        try:
            source = QUrl.fromLocalFile(str(self.project.file('original.wav')))
            if self.player.source() != source:
                self.player.setSource(source)
            self._previewing = False
            self.player.setPosition(position)
        except Exception as exc:
            self.status.setText(str(exc))

    def position_changed(self, value):
        if not self._previewing:
            self.wave.setValue(value)
        name = {'range': '选段', 'original': '原版', 'candidate': '新版'}.get(self._preview_kind, '') if self._previewing else ''
        self.position.setText(f'{name} {value // 60000}:{value // 1000 % 60:02d}')

    def play_source(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            return
        self.seek_original(self.wave.value())
        self.player.play()

    def run_operation(self, kind, operation):
        if self.busy():
            return
        self.player.stop()
        self.worker = AdjustmentWorker(operation, self)
        self.worker.completed.connect(lambda value: self.completed(kind, value))
        self.worker.failed.connect(self.status.setText)
        self.worker.progress.connect(lambda text, _value: self.status.setText(text))
        self.worker.finished.connect(self.idle)
        self.cancel_button.setVisible(kind != 'open')
        self.cancel_button.setEnabled(True)
        self.status.setText({'open': '正在读取成品…', 'generate': '正在生成这一段，原版保留…',
                             'preview': '正在准备试听…', 'adopt': '正在保存新成品…'}[kind])
        self.refresh_actions()
        self.worker.start()

    def generate_one(self):
        if self.busy() or not self.page._guard_local_synthesis_start('indextts2'):
            return
        try:
            self.project.set_selection(*self.frames(), self.editor.toPlainText(), self._revision)
            self._revision = self.project.read()['revision']
            def operation(worker):
                from ..core.synthesis.indextts2_subprocess import IndexTTS2SubprocessEngine
                engine = (self.engine_factory or IndexTTS2SubprocessEngine)()
                return self.project.generate_candidate(engine, worker.progress.emit, lambda: worker.cancelled)
            self.run_operation('generate', operation)
        except Exception as exc:
            self.status.setText(str(exc))

    def preview(self, kind):
        if self.busy():
            return
        if self._previewing and self._preview_kind == kind:
            state = self.player.playbackState()
            if state == QMediaPlayer.PlayingState:
                self.player.pause()
                return
            if state == QMediaPlayer.PausedState:
                self.player.play()
                return
        self._preview_kind = kind
        left, right = self.frames()
        version_id = self.versions.currentData() if kind == 'candidate' else None
        target = self.project.file('previews/' + uuid.uuid4().hex + '.wav')
        self.run_operation('preview', lambda w: self.project.render(target, version_id=version_id,
            start=left, end=right, context=kind != 'range', cancelled=lambda: w.cancelled))

    def adopt(self):
        if self.busy() or not self.adopt_button.isEnabled():
            return
        version_id = self.versions.currentData()
        from ..core.synthesis.indextts2_results import unique_output_path
        target = unique_output_path(library.audio_storage_dir() /
            (Path(self.project.read()['adjustment']['source']['name']).stem + '_局部调整.wav'))
        self.run_operation('adopt', lambda w: self.project.adopt(target, version_id, lambda: w.cancelled))

    def completed(self, kind, value):
        if kind == 'open':
            self.project = value
            self.load_project()
            self.status.setText('选一段声音，再填入这段要说的话。')
        elif kind == 'generate':
            self.load_project(value['id'])
            self.status.setText('新声音已生成。试听满意后，保存修改后的音频。')
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.comparison, 0, 8))
        elif kind == 'preview':
            self._previewing = True
            self.player.setSource(QUrl.fromLocalFile(value['path']))
            self.player.play()
            self.status.setText('正在试听。原成品保持不变。')
        elif kind == 'adopt':
            self._revision = self.project.read()['revision']
            data = self.project.read()
            candidate = self.project.candidate(value['version_id'])
            saved, message = self.page._save_voice_safely(value['path'],
                title=Path(value['path']).stem, text=candidate['text'], engine='IndexTTS 2.5',
                voice_name=data['voice']['name'], voice_id=data['voice']['id'],
                duration_seconds=value['duration'], config_summary='局部调整 · 原版保留',
                meta={'local_adjustment': value, 'options': candidate.get('options', data['options'])})
            self.status.setText('已保存到音频库，关闭后可在结果栏下载。' if saved else message)
            if saved and getattr(self.page.main, 'library_page', None):
                self.page.main.library_page.refresh()
            self.saved.emit(value)

    def idle(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.cancel_button.hide()
        self.refresh_actions()
        self.page._on_indextts2_lifecycle(self.page._indextts2_background_status_snapshot())

    def cancel(self):
        if self.worker:
            self.worker.cancelled = True
            self.cancel_button.setEnabled(False)
            self.status.setText('正在取消，原版和已有候选保留…')

    def reject(self):
        if self.busy():
            self.cancel()
            return
        if self.project:
            try:
                self.project.set_selection(*self.frames(), self.editor.toPlainText(), self._revision)
            except Exception as exc:
                choice = QMessageBox.warning(self, '这次修改还没保存', str(exc),
                    QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
                if choice != QMessageBox.Discard:
                    return
        self.player.stop()
        self.wave.stop_decode()
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
