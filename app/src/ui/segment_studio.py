"""Local paragraph projects: review a candidate before replacing a good take."""
from pathlib import Path

from PySide6.QtCore import QThread, Signal, QUrl, Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QMediaPlayer
from .audio_output import create_audio_output as QAudioOutput
from PySide6.QtWidgets import (
    QDialog, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QPlainTextEdit, QPushButton, QProgressBar, QSplitter, QVBoxLayout, QWidget,
    QScrollArea, QFrame, QAbstractSpinBox, QLayout,
)

from ..core.synthesis.segment_project import SegmentProject, ProjectCancelled
from . import theme as T
from .consumer_select import ConsumerSelect
from .control_foundation import adopt_button
from .voice_wave_slider import VoiceWaveSlider


class ProjectWorker(QThread):
    progress = Signal(str, float)
    updated = Signal()
    failed = Signal(str)
    completed = Signal(object)

    def __init__(self, project, part_ids=(), *, destination=None, gap=.2, engine_factory=None):
        super().__init__()
        self.project, self.part_ids = project, list(part_ids)
        self.destination, self.gap = destination, gap
        self.engine_factory = engine_factory
        self.cancelled = False

    def request_cancel(self):
        # The engine observes this flag; do not cancel another client's worker.
        self.cancelled = True

    def run(self):
        try:
            if self.destination:
                result = self.project.export(self.destination, self.gap, lambda: self.cancelled)
            else:
                from ..core.synthesis.indextts2_subprocess import IndexTTS2SubprocessEngine
                engine = (self.engine_factory or IndexTTS2SubprocessEngine)()
                for index, part_id in enumerate(self.part_ids):
                    if self.cancelled:
                        raise ProjectCancelled()
                    self.project.generate(part_id, engine,
                        lambda msg, frac, i=index: self.progress.emit(
                            f'{i + 1}/{len(self.part_ids)} · {msg}', frac),
                        lambda: self.cancelled)
                    self.updated.emit()
                result = None
            self.completed.emit(result)
        except Exception as exc:
            if self.cancelled or isinstance(exc, ProjectCancelled):
                self.failed.emit('已取消。此前完成的段落和旧版本都已保留。')
            else:
                from ..core.synthesis.indextts2_subprocess import was_cancelled
                self.failed.emit('已取消，已有段落保留。' if was_cancelled(exc) else str(exc))


class SegmentStudio(QDialog):
    def __init__(self, page, *, project=None, engine_factory=None):
        super().__init__(page)
        self.page, self.project = page, project
        self.engine_factory = engine_factory
        self.worker = None
        self._part_id = None
        self._loading = False
        self._loaded_text = ''
        self._revision = None
        self._gap_project_path = None
        self._playing_export = False
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(500)
        self.save_timer.timeout.connect(self.save_edit)
        self.setWindowTitle('局部调整 · IndexTTS 2.5')
        self.resize(940, 740)
        self.setMinimumSize(640, 620)
        c = page._consumer
        self.setStyleSheet(T.QSS + f'QDialog{{background:{c.surface};}} '
            f'QPlainTextEdit,QListWidget{{background:{c.surface};color:{c.ink};border:1px solid {c.line};border-radius:9px;padding:8px;}}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel('保留好声音，只重做有问题的一段')
        title.setStyleSheet('font-size:19px;font-weight:600;')
        layout.addWidget(title)
        hint = QLabel('按完整意思分段。重做后先试听，再决定替换；段落接缝也需要听一遍。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        top = QHBoxLayout()
        self.open_button = self.button('打开项目', self.open_project)
        top.addWidget(self.open_button)
        self.folder_button = self.button('项目文件夹', self.open_folder)
        top.addWidget(self.folder_button)
        self.project_label = QLabel('尚未创建项目')
        self.project_label.setWordWrap(True)
        top.addWidget(self.project_label, 1)
        layout.addLayout(top)
        self.reference_hint = QLabel('新项目会保存参考音频副本及当前设置，方便下次继续。')
        self.reference_hint.setWordWrap(True)
        layout.addWidget(self.reference_hint)
        self.setup = QWidget()
        sl = QVBoxLayout(self.setup)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(QLabel('用空行分段，每段最多 600 字。创建后可逐段修改。'))
        self.draft = QPlainTextEdit(page.indextts2_text_edit.toPlainText())
        self.draft.setAccessibleName('分段文案，空行表示下一段')
        self.draft.setPlaceholderText('第一段完整的意思……\n\n第二段完整的意思……')
        sl.addWidget(self.draft)
        self.create_button = self.button('使用当前音色和设置创建', self.create_project, primary=True)
        sl.addWidget(self.create_button)
        layout.addWidget(self.setup, 1)
        self.body = QWidget()
        self.body.setObjectName('segmentStudioBody')
        self.body.setAttribute(Qt.WA_StyledBackground, True)
        self.body.setStyleSheet(f'QWidget#segmentStudioBody{{background:{c.surface};}}')
        bl = QVBoxLayout(self.body)
        bl.setSizeConstraint(QLayout.SetMinimumSize)
        bl.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.parts = QListWidget()
        self.parts.setAccessibleName('制作段落')
        self.parts.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.parts.setTextElideMode(Qt.ElideRight)
        self.parts.setMinimumWidth(160)
        self.parts.currentRowChanged.connect(self.part_changed)
        split.addWidget(self.parts)
        edit = QWidget()
        el = QVBoxLayout(edit)
        el.setSizeConstraint(QLayout.SetMinimumSize)
        el.setContentsMargins(8, 0, 0, 0)
        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName('本段文案')
        self.editor.setMinimumHeight(100)
        self.editor.setPlaceholderText('这一段要说的话')
        self.editor.textChanged.connect(self.draft_changed)
        el.addWidget(self.editor, 1)
        self.edit_hint = QLabel('修改会自动保存；旧版声音仍保留。')
        self.edit_hint.setWordWrap(True)
        el.addWidget(self.edit_hint)
        self.versions = ConsumerSelect([], value='', accessible_name='选择本段声音版本',
                                       allow_empty=True, empty_text='尚未生成',
                                       consumer_theme_scope='coreWorkspace')
        self.versions.setAccessibleName('选择本段声音版本')
        self.versions.currentIndexChanged.connect(self.version_changed)
        el.addWidget(self.versions)
        self.version_text = QPlainTextEdit('生成后可在这里比较每一版。')
        self.version_text.setReadOnly(True)
        self.version_text.setAccessibleName('所选版本的文案，仅供对照')
        self.version_text.setFixedHeight(70)
        el.addWidget(self.version_text)
        row = QHBoxLayout()
        self.play_button = self.button('试听这版', self.play)
        self.choose_button = self.button('使用这版文案和声音', self.choose)
        row.addWidget(self.play_button)
        row.addWidget(self.choose_button)
        el.addLayout(row)
        self.wave = VoiceWaveSlider(self)
        self.wave.setAccessibleName('当前版本播放进度')
        el.addWidget(self.wave)
        self.warning = QLabel('')
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(f'color:{c.ink_muted};font-size:12px;')
        el.addWidget(self.warning)
        split.addWidget(edit)
        split.setStretchFactor(1, 3)
        split.setSizes([180, 650])
        bl.addWidget(split, 1)
        row = QHBoxLayout()
        self.regenerate = self.button('生成本段', self.generate_one)
        self.generate_missing = self.button('生成未完成段落', self.generate_all, primary=True)
        row.addWidget(self.regenerate)
        row.addWidget(self.generate_missing)
        row.addStretch(1)
        bl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel('段落间隔'))
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0, 2)
        self.gap.setSingleStep(.05)
        self.gap.setDecimals(2)
        self.gap.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.gap.setFixedSize(92, 36)
        self.gap.setStyleSheet(f'QDoubleSpinBox{{background:{c.surface};border:1px solid {c.line};border-radius:8px;padding:5px;font-size:14px;}}')
        self.gap.setValue(.2)
        self.gap.setSuffix(' 秒')
        self.gap.setAccessibleName('合并时的段落间隔，秒')
        self.gap.setToolTip('Lulu 合并时添加的空隙，不控制句子内部的停顿。')
        self.gap_minus = self.button('−', lambda: self.gap.stepDown())
        self.gap_plus = self.button('+', lambda: self.gap.stepUp())
        self.gap_minus.setAccessibleName('减少段落间隔')
        self.gap_plus.setAccessibleName('增加段落间隔')
        for button in (self.gap_minus, self.gap_plus):
            button.setFixedWidth(36)
        row.addWidget(self.gap_minus)
        row.addWidget(self.gap)
        row.addWidget(self.gap_plus)
        row.addStretch(1)
        self.export_button = self.button('合并导出 WAV', self.export, primary=True)
        self.export_play = self.button('试听合并', self.play_export)
        self.export_play.setEnabled(False)
        row.addWidget(self.export_play)
        row.addWidget(self.export_button)
        bl.addLayout(row)
        self.body_scroll = QScrollArea()
        self.body_scroll.setFrameShape(QFrame.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setStyleSheet(f'QScrollArea{{background:{c.surface};border:0;}}')
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body_scroll.setWidget(self.body)
        layout.addWidget(self.body_scroll, 1)
        self.status = QLabel('项目保存在本机，关闭后可重新打开继续。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        bottom = QHBoxLayout()
        self.cancel_button = self.button('取消生成', self.cancel)
        self.cancel_button.hide()
        bottom.addWidget(self.cancel_button)
        bottom.addStretch(1)
        self.close_button = self.button('关闭', self.reject)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        self.player.durationChanged.connect(lambda d: self.wave.setRange(0, d))
        self.player.positionChanged.connect(self.wave.setValue)
        self.wave.sliderMoved.connect(self.player.setPosition)
        self.player.playbackStateChanged.connect(self.playback_changed)
        self.reload()

    def button(self, text, callback, primary=False):
        button = QPushButton(text)
        adopt_button(button, role='primary' if primary else 'secondary', accessible_name=text)
        button.setMinimumHeight(36)
        button.clicked.connect(callback)
        return button

    def error(self, exc):
        self.status.setText(str(exc))

    def open_folder(self):
        if self.project:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.root)))

    def open_project(self):
        if not self.save_edit():
            return
        path, _ = QFileDialog.getOpenFileName(self, '打开分段项目', self.page.cfg.tts_output_dir, 'Lulu 项目 (project.json)')
        if path:
            try:
                self.project = SegmentProject(path)
                self._part_id = None
                self.reload()
            except Exception as exc:
                self.error(exc)

    def create_project(self):
        p = self.page
        try:
            voice = p._selected_indextts2_voice()
            if voice is None:
                raise ValueError('请先在配音页选择音色，再创建项目。')
            from ..core.synthesis.indextts2_worker import validate_index_generation
            options = p._indextts2_emotion_engine_options()
            options.update(language=str(p.indextts2_language_combo.currentData() or 'ZH'),
                           speed=p.indextts2_speed_slider.value() / 100,
                           random_emotion=bool(p._approved_index.random_emotion and p._indextts2_emotion_mode in ('vector', 'emotext')),
                           generation_options=validate_index_generation(getattr(p.cfg, 'indextts25_generation_options', {}), '2.5'))
            # Resolve pronunciation rules once and keep the visible resolved draft.
            text = p.indextts2_glossary_panel.apply_to_text(self.draft.toPlainText()).text
            self.project = SegmentProject.create(Path(p.cfg.tts_output_dir) / 'Lulu分段项目', text, voice, options)
            self.reload()
            self.status.setText('项目已保存。请检查段落与文案，再开始生成。')
        except Exception as exc:
            self.error(exc)

    def reload(self):
        self.folder_button.setEnabled(self.project is not None)
        self.setup.setVisible(self.project is None)
        self.body.setVisible(self.project is not None)
        self.body_scroll.setVisible(self.project is not None)
        if not self.project:
            return
        try:
            data = self.project.read()
            self._revision = data['revision']
            if self._gap_project_path != self.project.path:
                self.gap.setValue(data['gap_seconds'])
                self._gap_project_path = self.project.path
            self.export_play.setEnabled(bool(data.get('last_export')) and not self.busy())
            self.project_label.setText(f"{data['voice']['name']} · {len(data['parts'])} 段 · 设置随项目保存")
            self.project_label.setToolTip(str(self.project.path) + '\n' + '\n'.join(r['hint'] for r in data['references'].values()))
            self.reference_hint.setText(' · '.join(r['hint'] for r in data['references'].values()))
            self.reference_hint.setVisible(any(r['duration'] > 15 for r in data['references'].values()))
            self.parts.blockSignals(True)
            self.parts.clear()
            row = 0
            for i, part in enumerate(data['parts']):
                selected = self.project.current(part)
                self.parts.addItem(f"第 {i + 1} 段 · {'已有声音' if selected else '待生成'}\n{part['text'][:18]}")
                if part['id'] == self._part_id:
                    row = i
            self.parts.setCurrentRow(row)
            self.parts.blockSignals(False)
            self._part_id = data['parts'][row]['id']
            self.load_part()
            self.export_button.setEnabled(all(self.project.current(p) for p in data['parts']) and not self.busy())
        except Exception as exc:
            self.error(exc)

    def load_part(self):
        self.save_timer.stop()
        part = self.project.part(self.project.read(), self._part_id)
        self._loading = True
        self.editor.setPlainText(part['text'])
        self._loaded_text = part['text']
        self._revision = self.project.read()['revision']
        self._loading = False
        self.versions.blockSignals(True)
        self.versions.clear()
        for i, version in reversed(list(enumerate(part['versions']))):
            self.versions.addItem(f"第 {i + 1} 版 · {version['duration']:.1f} 秒" + (' · 使用中' if version['id'] == part['selected'] else ''), version['id'])
        if part['versions']:
            self.versions.setCurrentIndex(0)
        self.versions.blockSignals(False)
        self.regenerate.setText('再生成一版' if part['versions'] else '生成本段')
        self.version_changed()

    def part_changed(self, row):
        if not self.project or row < 0:
            return
        if not self.save_edit():
            data = self.project.read()
            old = next(i for i, p in enumerate(data['parts']) if p['id'] == self._part_id)
            self.parts.blockSignals(True)
            self.parts.setCurrentRow(old)
            self.parts.blockSignals(False)
            return
        self._part_id = self.project.read()['parts'][row]['id']
        self.load_part()

    def draft_changed(self):
        if not self._loading:
            self.export_button.setEnabled(False)
            self.edit_hint.setText('正在保存文案，旧声音仍保留。')
            self.save_timer.start()

    def save_edit(self):
        self.save_timer.stop()
        if self.project and self._part_id and not self._loading:
            try:
                if self.editor.toPlainText() != self._loaded_text:
                    self.project.edit(self._part_id, self.editor.toPlainText(), self._revision)
                    self._revision = self.project.read()['revision']
                    self._loaded_text = self.editor.toPlainText()
                self.edit_hint.setText('文案已保存。旧版声音仍保留。')
                data = self.project.read()
                self.export_button.setEnabled(all(self.project.current(p) for p in data['parts']) and not self.busy())
                for i, part in enumerate(data['parts']):
                    if i < self.parts.count():
                        self.parts.item(i).setText(f"第 {i + 1} 段 · {'已有声音' if self.project.current(part) else '待生成'}\n{part['text'][:18]}")
                return True
            except Exception as exc:
                self.error(exc)
                return False
        return True

    def selected_version(self):
        if not self.project or not self._part_id:
            return None
        part = self.project.part(self.project.read(), self._part_id)
        return next((v for v in part['versions'] if v['id'] == self.versions.currentData()), None)

    def version_changed(self, *_):
        self._playing_export = False
        self.player.stop() if hasattr(self, 'player') else None
        version = self.selected_version()
        self.play_button.setEnabled(bool(version))
        self.choose_button.setEnabled(bool(version) and not self.busy())
        self.version_text.setPlainText('这版文案：' + version['text'] if version else '生成后可在这里比较每一版。')
        self.warning.setText('\n'.join(w['message'] for w in version['warnings']) if version else '')
        if hasattr(self, 'player'):
            try:
                path = self.project.checked_audio(version) if version else None
                self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
                self.wave.load_file(str(path) if path else '')
            except Exception as exc:
                self.play_button.setEnabled(False)
                self.choose_button.setEnabled(False)
                self.error(exc)

    def play(self):
        try:
            version = self.selected_version()
            if not version:
                return
            path = self.project.checked_audio(version)
            source = QUrl.fromLocalFile(str(path))
            if self.player.source() != source:
                self.player.stop()
                self._playing_export = False
                self.player.setSource(source)
                self.wave.load_file(str(path))
            self._playing_export = False
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            else:
                self.player.play()
            self.playback_changed(self.player.playbackState())
        except Exception as exc:
            self.error(exc)

    def playback_changed(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.play_button.setText('暂停这版' if playing and not self._playing_export else '试听这版')
        self.export_play.setText('暂停合并' if playing and self._playing_export else '试听合并')

    def play_export(self):
        try:
            from ..core.synthesis.segment_project import digest
            item = self.project.read().get('last_export')
            if not item or digest(item['path']) != item['sha256']:
                raise ValueError('合并音频已被移动或改动，请重新导出。')
            source = QUrl.fromLocalFile(item['path'])
            if self.player.source() != source:
                self.player.stop()
                self._playing_export = True
                self.player.setSource(source)
                self.wave.load_file(item['path'])
            self._playing_export = True
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            else:
                self.player.play()
            self.playback_changed(self.player.playbackState())
        except Exception as exc:
            self.error(exc)

    def choose(self):
        # This explicit action restores both the old text and its sound, including
        # when the current unsaved draft is temporarily empty or too long.
        self.save_timer.stop()
        try:
            version_id = self.versions.currentData()
            self.project.choose(self._part_id, version_id, self._revision)
            self.reload()
            self.versions.setCurrentIndex(self.versions.findData(version_id))
            self.status.setText('已使用这版文案和声音，其他版本仍保留。')
        except Exception as exc:
            self.error(exc)

    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    def start(self, ids=(), destination=None):
        if self.busy():
            return
        if not destination and not self.page._guard_local_synthesis_start('indextts2'):
            return
        self.player.stop()
        self.worker = ProjectWorker(self.project, ids, destination=destination, gap=self.gap.value(), engine_factory=self.engine_factory)
        self.worker.progress.connect(self.on_progress)
        self.worker.updated.connect(self.reload)
        self.worker.failed.connect(self.error)
        self.worker.completed.connect(self.completed)
        self.worker.finished.connect(self.idle)
        for widget in (self.parts, self.editor, self.regenerate, self.generate_missing, self.open_button, self.export_button, self.export_play, self.choose_button, self.gap, self.gap_minus, self.gap_plus):
            widget.setEnabled(False)
        self.cancel_button.show()
        self.cancel_button.setText('取消导出' if destination else '取消生成')
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.worker.start()

    def generate_one(self):
        if self.save_edit():
            self.start([self._part_id])

    def generate_all(self):
        if self.save_edit():
            ids = [p['id'] for p in self.project.read()['parts'] if not self.project.current(p)]
            if ids:
                self.start(ids)
            else:
                self.status.setText('每段都已有声音。需要调整时选中一段，再生成一版。')

    def export(self):
        if not self.save_edit():
            return
        target, _ = QFileDialog.getSaveFileName(self, '合并导出', str(self.project.root / 'exports' / '完整配音.wav'), 'WAV 音频 (*.wav)')
        if target:
            self.start(destination=target)

    def on_progress(self, text, value):
        self.status.setText(text)
        self.progress.setRange(0, 100 if value >= 0 else 0)
        if value >= 0:
            self.progress.setValue(int(value * 100))

    def completed(self, result):
        if result:
            self.status.setText('已导出：' + Path(result['path']).name + '。请试听段落接缝。')
            self.status.setToolTip(result['path'])
            self.player.setSource(QUrl.fromLocalFile(result['path']))
            self.wave.load_file(result['path'])
            self.play_button.setEnabled(True)
        else:
            self.status.setText('生成完成。新版本可试听，已有选择不会被替换。')

    def idle(self):
        for widget in (self.parts, self.editor, self.regenerate, self.generate_missing, self.open_button, self.gap, self.gap_minus, self.gap_plus):
            widget.setEnabled(True)
        self.cancel_button.hide()
        self.progress.hide()
        self.reload()
        self.page._on_indextts2_lifecycle(self.page._indextts2_background_status_snapshot())

    def cancel(self):
        if self.worker:
            self.worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText('正在取消，已完成的段落会保留……')

    def reject(self):
        if self.busy():
            self.cancel()
            return
        if not self.save_edit():
            return
        self.player.stop()
        self.wave.stop_decode()
        super().reject()

    def closeEvent(self, event):
        if self.busy():
            self.cancel()
            event.ignore()
        elif not self.save_edit():
            event.ignore()
        else:
            self.player.stop()
            self.wave.stop_decode()
            event.accept()
