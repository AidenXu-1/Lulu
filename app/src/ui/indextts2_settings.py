"""Version-isolated generation settings; defaults remain sparse overrides."""
import subprocess
import sys

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QButtonGroup, QDialog, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..core.synthesis.indextts2_worker import INDEX2_GENERATION_FIELDS, INDEX25_GENERATION_FIELDS, validate_index_generation
from .control_foundation import LuluCheckBox, adopt_button


FIELD_LABELS = {
    'temperature': ('变化幅度', '越高变化越多，也越容易不稳定。'),
    'top_p': ('可选表达范围', '调低会更保守，通常保持默认。'),
    'top_k': ('可选表达数量', '调低会减少变化；0 表示不限制。'),
    'repetition_penalty': ('重复抑制', '调高可减少重复，过高会不自然。'),
    'length_penalty': ('长短偏好', '不控制语速；比较多个方案时才生效。'),
    'num_beams': ('同时比较的方案数', '越多越慢、更占内存，不一定更好听。'),
    'max_text_tokens_per_segment': ('单段文字容量', '官方建议 80～200；调低会切得更碎，数值不等于字数。'),
    'max_mel_tokens': ('单段声音容量', '太低可能读不完。数值不等于秒数，建议保持默认。'),
}


def reduced_motion():
    if sys.platform != 'darwin':
        return False
    try:
        result = subprocess.run(
            ['/usr/bin/defaults', 'read', 'com.apple.universalaccess', 'reduceMotion'],
            capture_output=True, text=True, timeout=0.4,
        )
        return result.stdout.strip().lower() in ('1', 'true', 'yes')
    except (OSError, subprocess.TimeoutExpired):
        return True  # Accessibility signal unavailable: prefer no motion.


def _dialog_style(c):
    return f'''
        QDialog {{background:{c.surface};color:{c.ink};}}
        QScrollArea {{border:0;background:transparent;}}
        QScrollArea > QWidget > QWidget {{background:{c.surface};}}
        QScrollBar:vertical {{background:transparent;width:6px;margin:0;}}
        QScrollBar::handle:vertical {{background:{c.line};border-radius:3px;min-height:28px;}}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
        QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{background:transparent;}}
        QLabel {{color:{c.ink};background:transparent;}}
        QLabel[muted="true"] {{color:{c.ink_muted};font-size:12px;}}
        QFrame[example="true"] {{background:{c.surface_alt};border-radius:10px;}}
        QFrame[settingsGroup="true"] {{border:1px solid {c.line};border-radius:12px;background:{c.surface};}}
        QPushButton {{background:{c.surface};color:{c.ink};border:1px solid {c.line};border-radius:8px;padding:0 16px;font-size:13px;}}
        QPushButton:hover {{background:{c.accent_wash};}}
        QPushButton:pressed {{background:{c.accent_wash};border-color:{c.accent};}}
        QPushButton:focus {{border:2px solid {c.accent};}}
        QPushButton[role="primary"] {{background:{c.accent};color:white;border-color:{c.accent};}}
        QPushButton:disabled {{background:{c.surface_alt};color:{c.ink_muted};border-color:{c.line};}}
        QPushButton[choice="true"] {{border-radius:12px;}}
        QPushButton[choice="true"]:checked {{background:{c.accent_wash};border:2px solid {c.accent};}}
        QPushButton[choice="true"]:disabled {{background:{c.surface_alt};border:1px solid {c.line};}}
        QPushButton[choice="true"] QLabel:disabled {{color:{c.ink_muted};}}
        QLabel[selection="true"] {{color:{c.accent};font-size:20px;}}
        QFrame[numberField="true"] {{background:{c.surface};border:1px solid {c.line};border-radius:8px;}}
        QFrame[numberField="true"][focused="true"] {{border:2px solid {c.accent};}}
        QFrame[numberField="true"][inactive="true"] {{background:{c.surface_alt};}}
        QSpinBox,QDoubleSpinBox {{background:transparent;color:{c.ink};border:0;padding:0;font-size:13px;}}
        QSpinBox:disabled,QDoubleSpinBox:disabled {{color:{c.ink_muted};}}
        QPushButton[step="true"] {{background:transparent;border:0;border-radius:6px;padding:0;font-size:18px;}}
        QPushButton[step="true"]:hover {{background:{c.accent_wash};color:{c.accent};}}
        QPushButton[step="true"]:focus {{border:2px solid {c.accent};}}
        QPushButton[step="true"]:disabled {{color:{c.line};}}
    '''


class _EmotionChoice(QPushButton):
    def __init__(self, title, hint):
        super().__init__()
        adopt_button(self, accessible_name=title + '。' + hint)
        self.setProperty('choice', True)
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setFixedHeight(82)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 12, 18, 12)
        text = QVBoxLayout()
        text.setSpacing(5)
        for value, muted in ((title, False), (hint, True)):
            label = QLabel(value)
            label.setWordWrap(True)
            label.setProperty('muted', muted)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            if not muted:
                label.setStyleSheet('font-size:14px;font-weight:600;')
            text.addWidget(label)
        row.addLayout(text, 1)
        self.indicator = QLabel('○')
        self.indicator.setProperty('selection', True)
        self.indicator.setAlignment(Qt.AlignCenter)
        self.indicator.setFixedWidth(24)
        self.indicator.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(self.indicator)
        self.toggled.connect(lambda checked: self.indicator.setText('✓' if checked else '○'))


class _NumberField(QFrame):
    """Keep Qt's validated editor and keyboard steps, with clear click targets."""
    def __init__(self, spin):
        super().__init__()
        self.spin = spin
        self.setProperty('numberField', True)
        self.setFixedSize(152, 38)
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(0)
        self.minus = adopt_button(QPushButton('−'), accessible_name='减少' + spin.accessibleName())
        self.plus = adopt_button(QPushButton('+'), accessible_name='增加' + spin.accessibleName())
        for button in (self.minus, self.plus):
            button.setProperty('step', True)
            button.setFixedSize(30, 30)
            button.setAutoDefault(False)
            button.installEventFilter(self)
        spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        spin.setAlignment(Qt.AlignCenter)
        spin.setMinimumWidth(0)
        row.addWidget(self.minus)
        row.addWidget(spin, 1)
        row.addWidget(self.plus)
        self.minus.clicked.connect(lambda: self._step(-1))
        self.plus.clicked.connect(lambda: self._step(1))
        spin.valueChanged.connect(self._sync)
        spin.installEventFilter(self)
        spin.lineEdit().installEventFilter(self)
        self._sync()

    def _step(self, direction):
        if self.spin.isEnabled():
            self.spin.stepBy(direction)

    def _sync(self, *_):
        enabled = self.spin.isEnabled()
        self.minus.setEnabled(enabled and self.spin.value() > self.spin.minimum())
        self.plus.setEnabled(enabled and self.spin.value() < self.spin.maximum())
        self.setProperty('inactive', not enabled)
        self.style().unpolish(self)
        self.style().polish(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.EnabledChange and watched is self.spin:
            self._sync()
        if event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.setProperty('focused', event.type() == QEvent.FocusIn)
            self.style().unpolish(self)
            self.style().polish(self)
        return super().eventFilter(watched, event)


class IndexTTSEmotionDialog(QDialog):
    """Explain the existing random-emotion option before applying a session draft."""
    def __init__(self, page, enabled=False, *, mode='follow', model_version='2.0'):
        super().__init__(page)
        self.setWindowTitle('情绪自由')
        self.setAccessibleName('情绪自由设置')
        self.setWindowModality(Qt.WindowModal)
        c = page._consumer
        self.setStyleSheet(_dialog_style(c))
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        def label(text, muted=False):
            widget = QLabel(text)
            widget.setWordWrap(True)
            widget.setProperty('muted', muted)
            return widget
        heading = QHBoxLayout()
        title = label('情绪自由')
        title.setStyleSheet('font-size:22px;font-weight:600;')
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(label('IndexTTS 2.5' if model_version == '2.5' else 'IndexTTS 2', True))
        root.addLayout(heading)
        root.addWidget(label('官方「情感随机采样」：同一种情绪，不同说法。'))
        example = QFrame()
        example.setProperty('example', True)
        example_layout = QVBoxLayout(example)
        example_layout.setContentsMargins(14, 12, 14, 12)
        example_layout.setSpacing(8)
        example_layout.addWidget(label('比如同样是「高兴」，可以轻轻带笑，也可以更外放。'))
        example_layout.addWidget(label('开启后会尝试不同表达。情绪数值不变，听感可能不同。', True))
        root.addWidget(example)
        self.choices = QButtonGroup(self)
        self.disabled_choice = _EmotionChoice('关闭 · 更贴近原声', '推荐日常配音使用。')
        self.enabled_choice = _EmotionChoice('开启 · 多试几种表达', '适合多生成几次挑选，音色可能没那么像。')
        for choice in (self.disabled_choice, self.enabled_choice):
            self.choices.addButton(choice)
            root.addWidget(choice)
        (self.enabled_choice if enabled else self.disabled_choice).setChecked(True)
        active = mode in ('vector', 'emotext')
        current = '跟随音色' if mode == 'follow' else '参考音频'
        self.mode_note = label('用于当前情绪模式的试听和全文生成。' if active else
            f'当前是「{current}」。切到「精细调节」或「文字描述」后可设置。', True)
        root.addWidget(self.mode_note)
        root.addWidget(label('本次使用有效，重启后恢复关闭。', True))
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = adopt_button(QPushButton('取消' if active else '知道了'))
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button = adopt_button(QPushButton('应用设置'), role='primary')
        self.apply_button.clicked.connect(self.accept)
        for button in (self.cancel_button, self.apply_button):
            button.setAutoDefault(False)
            footer.addWidget(button)
        root.addLayout(footer)
        self.disabled_choice.setEnabled(active)
        self.enabled_choice.setEnabled(active)
        self.apply_button.setEnabled(active)
        self.resize(min(600, page.screen().availableGeometry().width() - 48), self.sizeHint().height())
        focus = (self.enabled_choice if enabled else self.disabled_choice) if active else self.cancel_button
        focus.setFocus()


class IndexTTS2SettingsDialog(QDialog):
    def __init__(self, page, values, *, animate=True, model_version="2.0"):
        super().__init__(page)
        self.section_title = '高级设置'
        self.model_version = model_version
        self.fields = INDEX25_GENERATION_FIELDS if model_version == '2.5' else INDEX2_GENERATION_FIELDS
        self.model_name = 'IndexTTS 2.5' if model_version == '2.5' else 'IndexTTS 2'
        self.setWindowTitle(self.model_name + ' · ' + self.section_title)
        self.setWindowModality(Qt.WindowModal)
        self.setObjectName('index2Settings')
        self.setAccessibleName(self.model_name + ' ' + self.section_title)
        self._animate = animate and not reduced_motion()
        self._closing_result = None
        self._fade = QPropertyAnimation(self, b'windowOpacity', self)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.finished.connect(self._finish_fade)
        self.controls = {}
        self._invalid_saved = False
        try:
            self._original = validate_index_generation(values, self.model_version)
        except ValueError:
            self._original = {}
            self._invalid_saved = True
        c = page._consumer
        self.setStyleSheet(_dialog_style(c))
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)
        title = QLabel(self.section_title)
        title.setStyleSheet('font-size:22px;font-weight:600;')
        heading = QHBoxLayout()
        heading.addWidget(title)
        heading.addStretch(1)
        version_label = self._note(self.model_name + ' · 独立保存')
        version_label.setWordWrap(False)
        heading.addWidget(version_label)
        root.addLayout(heading)
        root.addWidget(self._note('官方可调参数；通常保持默认，试听和全文共用。'))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 6, 0)
        body.setSpacing(12)
        self.advanced = QWidget()
        self.columns = QGridLayout(self.advanced)
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(12)
        body.addWidget(self.advanced)
        self.expression_column, self.timing_column = QWidget(), QWidget()
        expression = QVBoxLayout(self.expression_column)
        timing = QVBoxLayout(self.timing_column)
        for layout in (expression, timing):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(12)
        sampling = self._group(expression, '表达变化')
        for key in ('temperature', 'top_p', 'top_k', 'repetition_penalty', 'length_penalty'):
            self._field(sampling, key)
        limits = self._group(timing, '分段与生成耗时')
        for key in ('num_beams', 'max_text_tokens_per_segment', 'max_mel_tokens'):
            self._field(limits, key)
        self.controls['num_beams'].valueChanged.connect(
            lambda value: self.controls['length_penalty'].setEnabled(value > 1))
        pause = self._group(timing, '自动分段间隔')
        pause.addWidget(self._note('只在自动切段处加静音，不能指定停顿位置。'))
        gap_row = QHBoxLayout()
        self.custom_gap = LuluCheckBox('自定义间隔')
        gap_row.addWidget(self.custom_gap, 1)
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0, 2)
        self.gap.setDecimals(2)
        self.gap.setSingleStep(.05)
        self.gap.setSuffix(' 秒')
        self.gap.setAlignment(Qt.AlignCenter)
        self.gap.setAccessibleName('自动分段间隔，秒')
        gap_row.addWidget(_NumberField(self.gap))
        pause.addLayout(gap_row)
        pause.addWidget(self._note('关闭用默认；0 秒不额外加静音。'))
        self.custom_gap.toggled.connect(self.gap.setEnabled)
        expression.addStretch(1)
        timing.addStretch(1)
        body.addStretch(1)
        self.scroll.setWidget(content)
        root.addWidget(self.scroll, 1)
        self.message = self._note('')
        self.message.setVisible(False)
        root.addWidget(self.message)
        footer = QHBoxLayout()
        self.reset_button = adopt_button(QPushButton('恢复默认'))
        self.reset_button.clicked.connect(self.restore_defaults)
        footer.addWidget(self.reset_button)
        footer.addStretch(1)
        self.cancel_button = adopt_button(QPushButton('取消'))
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button = adopt_button(QPushButton('保存设置'), role='primary')
        self.apply_button.clicked.connect(self.accept)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        root.addLayout(footer)
        for button in (self.reset_button, self.cancel_button, self.apply_button):
            button.setAutoDefault(False)
        self._set_values(self._original)
        if self._invalid_saved:
            self.show_error('已保存的设置无效，点击保存可恢复默认。')
        available = page.screen().availableGeometry()
        self.resize(min(900, available.width() - 48), min(680, available.height() - 64))
        self._reflow()
        self.controls['temperature'].setFocus()
        # Keyboard actions are immediate, including Esc while opening.
        for widget in [self, *self.findChildren(QWidget)]:
            widget.installEventFilter(self)

    @staticmethod
    def _note(text):
        label = QLabel(text)
        label.setProperty('muted', True)
        label.setWordWrap(True)
        return label

    def _group(self, layout, title):
        frame = QFrame()
        frame.setProperty('settingsGroup', True)
        group = QVBoxLayout(frame)
        group.setContentsMargins(14, 12, 14, 12)
        group.setSpacing(10)
        label = QLabel(title)
        label.setStyleSheet('font-size:14px;font-weight:600;')
        group.addWidget(label)
        layout.addWidget(frame)
        return group

    def _field(self, group, key):
        title, hint = FIELD_LABELS[key]
        default, minimum, maximum = self.fields[key]
        row = QHBoxLayout()
        text = QVBoxLayout()
        label = QLabel(title)
        text.addWidget(label)
        text.addWidget(self._note(hint))
        text.addWidget(self._note(f'默认 {default}'))
        row.addLayout(text, 1)
        spin = QSpinBox() if isinstance(default, int) else QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        step = {'max_text_tokens_per_segment': 2, 'max_mel_tokens': 10}.get(key)
        spin.setSingleStep(step if step is not None else 1 if isinstance(default, int) else .01 if key == 'top_p' else .1)
        spin.setAlignment(Qt.AlignCenter)
        spin.setToolTip(f'官方参数：{key} · 默认 {default}')
        spin.setAccessibleName(title)
        label.setBuddy(spin)
        row.addWidget(_NumberField(spin))
        group.addLayout(row)
        self.controls[key] = spin

    def _set_values(self, values):
        for key, control in self.controls.items():
            control.setValue(values.get(key, self.fields[key][0]))
        self.custom_gap.setChecked('interval_silence' in values)
        self.gap.setValue(values.get('interval_silence', 200) / 1000)
        self.gap.setEnabled(self.custom_gap.isChecked())

    def restore_defaults(self):
        self._set_values({})
        self.show_error('已恢复默认，保存后生效。')

    def values(self):
        options = {key: control.value() for key, control in self.controls.items()
                   if control.value() != self.fields[key][0]}
        if self.custom_gap.isChecked():
            options['interval_silence'] = round(self.gap.value() * 1000)
        return validate_index_generation(options, self.model_version)

    def show_error(self, message):
        self.message.setText(message)
        self.message.show()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and isinstance(watched, QAbstractSpinBox):
            QApplication.sendEvent(self.scroll.viewport(), event)
            return True
        if event.type() == QEvent.KeyPress:
            self._animate = False
            self._fade.stop()
            if self._closing_result is not None:
                self._finish_fade()
                return True
            self.setWindowOpacity(1)
        return super().eventFilter(watched, event)

    def _reflow(self):
        narrow = self.width() < 780
        self.columns.removeWidget(self.expression_column)
        self.columns.removeWidget(self.timing_column)
        self.columns.addWidget(self.timing_column, 0, 0)
        self.columns.addWidget(self.expression_column, 1 if narrow else 0, 0 if narrow else 1)
        self.columns.setColumnStretch(0, 1)
        self.columns.setColumnStretch(1, 0 if narrow else 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'columns'):
            self._reflow()

    def closeEvent(self, event):
        event.ignore()
        self.reject()

    def showEvent(self, event):
        super().showEvent(event)
        if self._animate:
            self.setWindowOpacity(0)
            self._fade.setDuration(180)
            self._fade.setStartValue(0.)
            self._fade.setEndValue(1.)
            self._fade.start()

    def done(self, result):
        if self._closing_result is not None:
            result = self._closing_result
        if not self._animate or not self.isVisible():
            self._fade.stop()
            return super().done(result)
        self._closing_result = result
        self._fade.stop()
        self._fade.setDuration(120)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.)
        self._fade.start()

    def _finish_fade(self):
        if self._closing_result is not None:
            super().done(self._closing_result)
