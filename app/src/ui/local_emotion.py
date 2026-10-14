"""Independent emotion draft for one replacement; main-page settings stay intact."""
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QStackedWidget, QWidget, QPlainTextEdit, QSlider, QFileDialog)

from ..core.synthesis.indextts2_presets import EMOTION_VECTOR_LABELS, normalize_emotion_vector
from .control_foundation import adopt_button

MODES = (('follow', '跟随音色'), ('emoaudio', '参考音频'), ('vector', '精细调节'), ('emotext', '文字描述'))


class LocalEmotionDialog(QDialog):
    def __init__(self, owner, options, audio_path=''):
        super().__init__(owner)
        self._consumer = owner.page._consumer
        self.setWindowTitle('这段的情绪表达')
        self.setMinimumWidth(560)
        c = self._consumer
        self.setStyleSheet(owner.styleSheet() + f"""
            QSlider::groove:horizontal {{height:4px;background:{c.line};border-radius:2px;}}
            QSlider::sub-page:horizontal {{background:{c.accent};border-radius:2px;}}
            QSlider::handle:horizontal {{background:{c.surface};border:2px solid {c.accent};
                width:14px;height:14px;margin:-6px 0;border-radius:8px;}}
            QPushButton:checked {{color:{c.accent};border:2px solid {c.accent};border-radius:8px;}}
        """)
        self.options = deepcopy(options)
        self.audio_path = audio_path
        self.random_emotion = bool(options.get('random_emotion', False))
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)
        title = QLabel('这段的情绪表达')
        title.setStyleSheet('font-size:20px;font-weight:600')
        root.addWidget(title)
        root.addWidget(QLabel('只改变重做的这段，音色和其他部分保持原样。'))
        tabs = QHBoxLayout()
        self.tabs = {}
        self.stack = QStackedWidget()
        for key, label in MODES:
            button = self.button(label, lambda _checked=False, key=key: self.set_mode(key))
            button.setCheckable(True)
            self.tabs[key] = button
            tabs.addWidget(button)
        root.addLayout(tabs)
        for text in ('沿用音色参考里的语气。', '选一段声音，让这段模仿它的情绪。',
                     '拖动想加强的情绪，总量最多 0.80。', '用一句话描述语气，例如：开心又有点惊讶。'):
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 6, 0, 6)
            layout.addWidget(QLabel(text))
            self.stack.addWidget(panel)
        self.audio_button = self.button(Path(audio_path).name if audio_path else '选择情绪参考音频', self.pick_audio)
        self.audio_button.setAccessibleName('选择情绪参考音频')
        self.stack.widget(1).layout().addWidget(self.audio_button)
        grid = QGridLayout()
        self.sliders = []
        vector = options.get('emotion_vector', [0.0] * 8)
        for i, label in enumerate(EMOTION_VECTOR_LABELS):
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 80)
            slider.setValue(round(float(vector[i]) * 100))
            slider.setAccessibleName(label)
            value = QLabel(f'{slider.value()/100:.2f}')
            slider.valueChanged.connect(lambda n, value=value: value.setText(f'{n/100:.2f}'))
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(slider, i, 1)
            grid.addWidget(value, i, 2)
            self.sliders.append(slider)
        self.stack.widget(2).layout().addLayout(grid)
        self.text = QPlainTextEdit(str(options.get('emotion_text', '')))
        self.text.setAccessibleName('这段的情绪描述')
        self.text.setPlaceholderText('例如：轻松聊天，提到意外的地方有点惊讶。')
        self.text.setFixedHeight(92)
        self.stack.widget(3).layout().addWidget(self.text)
        root.addWidget(self.stack)
        self.strength = QWidget()
        row = QHBoxLayout(self.strength)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel('情绪强度'))
        self.weight = QSlider(Qt.Horizontal)
        self.weight.setRange(owner.page.indextts2_weight_slider.minimum(), owner.page.indextts2_weight_slider.maximum())
        self.weight.setValue(round(float(options.get('emotion_weight', .8)) * 100))
        self.weight.setAccessibleName('情绪强度')
        row.addWidget(self.weight, 1)
        value = QLabel(f'{self.weight.value()}%')
        self.weight.valueChanged.connect(lambda n: value.setText(f'{n}%'))
        row.addWidget(value)
        root.addWidget(self.strength)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        footer = QHBoxLayout()
        self.freedom = self.button('情绪自由', self.edit_freedom)
        footer.addWidget(self.freedom)
        footer.addStretch()
        footer.addWidget(self.button('取消', self.reject))
        footer.addWidget(self.button('应用到这段', self.apply, primary=True))
        root.addLayout(footer)
        self.error.hide()
        self.set_mode(options.get('emotion_mode', 'follow'))

    def button(self, text, callback, primary=False):
        button = QPushButton(text)
        adopt_button(button, role='primary' if primary else 'secondary', accessible_name=text)
        button.setMinimumHeight(36)
        button.clicked.connect(callback)
        return button

    def set_mode(self, mode):
        self.mode = mode if mode in self.tabs else 'follow'
        for i, (key, _label) in enumerate(MODES):
            self.tabs[key].setChecked(key == self.mode)
            c = self._consumer
            self.tabs[key].setStyleSheet(
                f'QPushButton {{border:2px solid {c.accent if key == self.mode else c.line};'
                f'color:{c.accent if key == self.mode else c.ink};border-radius:8px;padding:6px 10px;}}')
            if key == self.mode:
                self.stack.setCurrentIndex(i)
        self.strength.setVisible(self.mode != 'follow')
        self.freedom.setVisible(self.mode in ('vector', 'emotext'))
        self.error.clear()
        self.error.hide()
        self.stack.setFixedHeight(self.stack.currentWidget().sizeHint().height())
        self.adjustSize()

    def pick_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择情绪参考音频', self.audio_path,
            '音频 (*.wav *.mp3 *.flac *.ogg)')
        if path:
            self.audio_path = path
            self.audio_button.setText(Path(path).name)
            self.audio_button.setToolTip(path)

    def edit_freedom(self):
        from .indextts2_settings import IndexTTSEmotionDialog
        dialog = IndexTTSEmotionDialog(self, self.random_emotion, mode=self.mode, model_version='2.5')
        if dialog.exec() == QDialog.Accepted:
            self.random_emotion = dialog.enabled_choice.isChecked()
        dialog.deleteLater()

    def values(self):
        result = {'emotion_mode': self.mode, 'emotion_weight': self.weight.value()/100,
                  'random_emotion': self.random_emotion if self.mode in ('vector', 'emotext') else False}
        if self.mode == 'vector':
            result['emotion_vector'] = list(normalize_emotion_vector([s.value()/100 for s in self.sliders]))
        elif self.mode == 'emotext':
            result['emotion_text'] = self.text.toPlainText().strip()
            if not result['emotion_text']:
                raise ValueError('请写下这段要表达的情绪。')
        elif self.mode == 'emoaudio':
            if not Path(self.audio_path).is_file():
                raise ValueError('请选择情绪参考音频。')
            result['emotion_audio_path'] = self.audio_path
        return result

    def apply(self):
        try:
            self.values()
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()
