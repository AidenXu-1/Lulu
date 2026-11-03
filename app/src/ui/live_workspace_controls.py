"""Compact native controls for the approved live workspace."""
from PySide6.QtCore import QEasingCurve, QEvent, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from .control_foundation import PaperTextureFrame
from .indextts2_settings import reduced_motion


class CommentsSwitch(QCheckBox):
    def __init__(self, parent=None):
        super().__init__('保存弹幕', parent)
        self.setAccessibleName('同时保存弹幕')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(120, 34)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1 if self.isEnabled() else .6)
        if self.isEnabled() and (self.underMouse() or self.isDown()):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#e3eceb' if self.isDown() else '#f5f0e6'))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        painter.setPen(QColor('#6f685d'))
        font = self.font()
        font.setPixelSize(13)
        painter.setFont(font)
        painter.drawText(self.rect().adjusted(5, 0, -43, 0), Qt.AlignmentFlag.AlignVCenter, self.text())
        track = QRectF(self.width()-38, (self.height()-19)/2, 32, 19)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#437f96' if self.isChecked() else '#d8d1c5'))
        painter.drawRoundedRect(track, 9.5, 9.5)
        painter.setBrush(QColor('#fffdf7'))
        painter.drawEllipse(QRectF(track.left()+2+(13 if self.isChecked() else 0), track.top()+3, 13, 13))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#77aab7'), 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)


class WorkspaceTab(QPushButton):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setToolTip(text.replace('  ', '：', 1))
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(104, 34)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1 if self.isEnabled() else .6)
        if self.isEnabled() and (self.underMouse() or self.isDown()):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#dce8e5' if self.isDown() else '#edf1ec' if self.isChecked() else '#f3f0e8'))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        label, _, number = self.text().rpartition('  ')
        color = '#437f96' if self.isChecked() else '#857b6d'
        painter.setPen(QColor(color))
        font = self.font()
        font.setPixelSize(13)
        font.setBold(self.isChecked())
        painter.setFont(font)
        label_width = QFontMetricsF(font).horizontalAdvance(label)
        number_font = self.font()
        number_font.setPixelSize(10)
        number_font.setBold(False)
        number_metrics = QFontMetricsF(number_font)
        gap = 8
        badge_limit = max(21, self.width()-14-label_width-gap)
        shown_number = number_metrics.elidedText(number, Qt.TextElideMode.ElideRight, badge_limit-10)
        badge_width = max(21, number_metrics.horizontalAdvance(shown_number)+10)
        start = (self.width()-label_width-gap-badge_width)/2
        painter.drawText(QRectF(start, 0, label_width, self.height()), Qt.AlignmentFlag.AlignCenter, label)
        badge = QRectF(start+label_width+gap, (self.height()-18)/2, badge_width, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#e4efee' if self.isChecked() else '#e8e2d7'))
        painter.drawRoundedRect(badge, 5, 5)
        painter.setPen(QColor(color))
        painter.setFont(number_font)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, shown_number)
        if self.hasFocus():
            painter.setPen(QPen(QColor('#77aab7'), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text.replace('  ', '：', 1))


class WorkspaceTabBar(QFrame):
    def __init__(self, buttons, parent=None):
        super().__init__(parent)
        self.buttons = tuple(buttons)
        self.setFixedSize(212, 38)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        for button in self.buttons:
            layout.addWidget(button)
            button.toggled.connect(lambda checked, b=button: checked and self.slide_to(b))
        self.indicator_x = 2.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(200)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._set_indicator_x)

    def _set_indicator_x(self, value):
        self.indicator_x = float(value)
        self.update()

    def slide_to(self, button):
        target = float(button.x())
        self.animation.stop()
        if reduced_motion() or self.indicator_x == target:
            self._set_indicator_x(target)
            return
        self.animation.setStartValue(self.indicator_x)
        self.animation.setEndValue(target)
        self.animation.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#ded5c7'), 1))
        painter.setBrush(QColor('#e8e2d7'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 8, 8)
        selected = QRectF(self.indicator_x, 2, 104, 34)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(64, 53, 38, 10))
        painter.drawRoundedRect(selected.translated(0, 1), 7, 7)
        painter.setBrush(QColor('#fffdf8'))
        painter.drawRoundedRect(selected, 7, 7)


class WorkspaceDialog(QDialog):
    """In-window paper card, with Escape, backdrop dismissal and focus return."""
    def __init__(self, title, eyebrow, parent):
        super().__init__(parent.window())
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setObjectName('liveWorkspaceDialog')
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._previous_focus = None
        self.setStyleSheet('''
            QDialog#liveWorkspaceDialog{background:rgba(45,39,31,62);color:#36332e;font-size:13px;}
            QDialog#liveWorkspaceDialog QLabel{background:transparent;border:0;color:#36332e;}
            QDialog#liveWorkspaceDialog QLabel[secondary="true"]{color:#948570;font-size:12px;}
            QDialog#liveWorkspaceDialog QLineEdit{background:#fffef9;border:1px solid #ddd2c0;border-radius:7px;padding:10px;color:#36332e;}
            QDialog#liveWorkspaceDialog QLineEdit:focus{border-color:#77aab7;}
            QDialog#liveWorkspaceDialog QPushButton{background:#fffdf8;border:1px solid #e3d9c9;border-radius:7px;padding:8px 13px;color:#36332e;}
            QDialog#liveWorkspaceDialog QPushButton:enabled:hover{background:#f5f0e6;border-color:#b6c5bd;}
            QDialog#liveWorkspaceDialog QPushButton:enabled:pressed{background:#e3eceb;}
            QDialog#liveWorkspaceDialog QPushButton:focus{border-color:#437f96;}
            QDialog#liveWorkspaceDialog QPushButton[primary="true"]{background:#437f96;border-color:#437f96;color:#fffdf7;}
            QDialog#liveWorkspaceDialog QPushButton[primary="true"]:enabled:hover{background:#376d82;border-color:#376d82;}
            QDialog#liveWorkspaceDialog QPushButton[primary="true"]:enabled:pressed{background:#2e5e71;border-color:#2e5e71;}
            QDialog#liveWorkspaceDialog QPushButton:disabled{color:#aaa090;border-color:#e8e0d4;}
            QDialog#liveWorkspaceDialog QPushButton[quiet="true"]{background:transparent;border:0;color:#948570;padding:3px 0;text-align:left;}
            QDialog#liveWorkspaceDialog QPushButton[quiet="true"]:enabled:hover{background:#f5f0e6;color:#5d766f;}
            QDialog#liveWorkspaceDialog QPushButton[quiet="true"]:enabled:pressed{background:#e3eceb;color:#437f96;}
            QDialog#liveWorkspaceDialog QPushButton#workspaceClose{background:transparent;border:1px solid transparent;color:#8a7f6e;font-size:20px;padding:0;}
            QDialog#liveWorkspaceDialog QPushButton#workspaceClose:enabled:hover{background:#f5f0e6;color:#36332e;}
            QDialog#liveWorkspaceDialog QPushButton#workspaceClose:enabled:pressed{background:#e3eceb;}
            QDialog#liveWorkspaceDialog QPushButton#workspaceClose:focus{border-color:#437f96;}
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch()
        center = QHBoxLayout()
        center.addStretch()
        self.card = PaperTextureFrame(strength=.035, corner_radius=17, base_color='#fffcf6')
        self.card.setFixedWidth(490)
        center.addWidget(self.card)
        center.addStretch()
        outer.addLayout(center)
        outer.addStretch()
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet('QScrollArea{background:transparent;border:0;}QWidget#workspaceBody{background:transparent;}')
        self.body = QWidget()
        self.body.setObjectName('workspaceBody')
        self.scroll.setWidget(self.body)
        card_layout.addWidget(self.scroll)
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(29, 25, 29, 24)
        self.content.setSpacing(16)
        heading = QHBoxLayout()
        labels = QVBoxLayout()
        labels.setSpacing(7)
        note = QLabel(eyebrow)
        note.setStyleSheet('font-size:10px;color:#9b8a72;')
        if eyebrow:
            labels.addWidget(note)
        else:
            note.hide()
        self.title = QLabel(title)
        self.title.setStyleSheet('font-family:"Songti SC","STSong";font-size:25px;font-weight:600;')
        labels.addWidget(self.title)
        heading.addLayout(labels, 1)
        self.close_button = QPushButton('×')
        self.close_button.setObjectName('workspaceClose')
        self.close_button.setAutoDefault(False)
        self.close_button.setFixedSize(28, 28)
        self.close_button.setAccessibleName('关闭'+title)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.reject)
        heading.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.content.addLayout(heading)
        self.hide()

    def _fit(self):
        self.setGeometry(self.parentWidget().rect())
        self.card.setFixedWidth(max(320, min(490, self.width()-48)))
        self.content.invalidate()
        hint = self.content.totalHeightForWidth(self.card.width())
        if hint < 0:
            hint = self.content.sizeHint().height()
        self.card.setFixedHeight(min(max(200, self.height()-48), max(260, hint)))

    def showEvent(self, event):
        self._previous_focus = QApplication.focusWidget()
        self.parentWidget().installEventFilter(self)
        self._fit()
        self.raise_()
        super().showEvent(event)
        QTimer.singleShot(0, self._fit)
        self.close_button.setFocus()

    def hideEvent(self, event):
        self.parentWidget().removeEventFilter(self)
        super().hideEvent(event)
        try:
            if self._previous_focus and self._previous_focus.isVisible():
                self._previous_focus.setFocus()
        except RuntimeError:
            pass
        self._previous_focus = None

    def eventFilter(self, watched, event):
        if watched is self.parentWidget():
            if event.type() == QEvent.Type.Resize:
                self._fit()
            elif event.type() == QEvent.Type.Hide:
                self.reject()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if not self.card.geometry().contains(event.position().toPoint()):
            self.reject()
            event.accept()
            return
        super().mousePressEvent(event)

    def focusNextPrevChild(self, forward):
        current = QApplication.focusWidget() or self
        cursor = current
        while True:
            cursor = cursor.nextInFocusChain() if forward else cursor.previousInFocusChain()
            if cursor is current:
                return False
            if (self.isAncestorOf(cursor) and cursor.isVisible() and cursor.isEnabled()
                    and cursor.focusPolicy() & Qt.FocusPolicy.TabFocus):
                cursor.setFocus(Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason)
                return True


def note(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setProperty('secondary', True)
    label.setWordWrap(True)
    return label
