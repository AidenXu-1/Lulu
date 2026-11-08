"""Collection-only presentation controls; business actions stay on the page."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from . import icons


class MaterialCell(QWidget):
    activated = Signal(str, str, str)
    reveal_requested = Signal(str, str)

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.key = self.kind = ''
        self._state = 'unavailable'
        self.setAutoFillBackground(False)
        self.setStyleSheet('background:transparent;')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(3)
        self.button = QPushButton()
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setFixedHeight(30)
        self.button.setStyleSheet(
            f'QPushButton{{background:transparent;border:none;border-radius:6px;color:{theme.accent};font-size:11px;}}'
            f'QPushButton:hover{{background:rgba(83,125,150,18);}}'
            f'QPushButton:disabled{{color:{theme.ink_muted};}}'
        )
        self.button.clicked.connect(lambda: self.activated.emit(self.key, self.kind, self._state))
        self.button.setContextMenuPolicy(Qt.CustomContextMenu)
        self.button.customContextMenuRequested.connect(lambda _point: self.reveal_requested.emit(self.key, self.kind))
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet(f'color:{theme.ink_soft};font-size:10px;background:transparent;')
        self.progress = QProgressBar()
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(
            f'QProgressBar{{border:none;border-radius:2px;background:{theme.line};}}'
            f'QProgressBar::chunk{{border-radius:2px;background:{theme.accent};}}'
        )
        layout.addStretch()
        layout.addWidget(self.button)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        layout.addStretch()
        self._theme = theme

    def sync(self, key, kind, title, state, fraction=None, reason=''):
        self.key, self.kind, self._state = key, kind, state
        self.setProperty('materialState', state)
        name = '音视频' if kind == 'media' else '封面'
        busy = state in {'queued', 'downloading'}
        self.button.setVisible(not busy)
        self.label.setVisible(busy)
        self.progress.setVisible(busy)
        if busy:
            known = isinstance(fraction, (int, float)) and 0 <= fraction <= 1
            if state == 'queued':
                text = '等待下载'
            elif known and fraction >= 1:
                text = '正在保存'
            else:
                text = f'{min(99, int(fraction * 100))}%' if known else '下载中'
            self.label.setText(text)
            self.label.setAccessibleName(f'{title}：{name}{text}')
            self.progress.setRange(0, 100 if known else 0)
            if known:
                self.progress.setValue(min(99, int(fraction * 100)))
            return
        ready = state == 'ready'
        unavailable = state in {'unavailable', 'image', 'privacy'}
        text = '查看' if ready else '图文' if state == 'image' else '隐私跳过' if state == 'privacy' else '不可用' if unavailable else '恢复' if state == 'recovery' else ''
        self.button.setText(text)
        self.button.setEnabled(not unavailable)
        self.button.setIcon(QIcon() if unavailable else QIcon(icons.pixmap('file' if ready else 'download', self._theme.danger if state == 'failed' else self._theme.accent, 15)))
        description = '查看' if ready else '安全恢复' if state == 'recovery' else '重试下载' if state == 'failed' else '下载'
        self.button.setAccessibleName(f'{title or key}：{description}{name}')
        self.button.setToolTip(reason or f'{description}这条作品的{name}')


class CollectionEditorPopup(QFrame):
    """A non-modal anchored editor. Nested select/calendar popups stay usable."""
    def __init__(self, owner, title, width=560):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.owner = owner
        self.setObjectName('collectionEditorPopup')
        self.setAccessibleName(title)
        self.setStyleSheet('QFrame#collectionEditorPopup{background:#faf6ec;border:1px solid #d6c9b3;border-radius:12px;}')
        self.setFixedWidth(width)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(12)
        self.heading = QLabel(title)
        self.heading.setStyleSheet('background:transparent;font-size:13px;font-weight:600;color:#665b49;')
        self.body.addWidget(self.heading)
        self._anchor = None

    def set_heading(self, title):
        self.heading.setText(title)
        self.setAccessibleName(title)

    def open_at(self, anchor):
        self._anchor = anchor
        self.adjustSize()
        position = anchor.mapToGlobal(QPoint(0, anchor.height() + 7))
        screen = QGuiApplication.screenAt(position)
        if screen:
            rect = screen.availableGeometry()
            position.setX(max(rect.left() + 8, min(position.x(), rect.right() - self.width() - 8)))
            if position.y() + self.height() > rect.bottom() - 8:
                position.setY(max(rect.top() + 8, anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 7))
        self.move(position)
        self.show()
        self.raise_()

    def showEvent(self, event):  # noqa: N802
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)

    def hideEvent(self, event):  # noqa: N802
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        if not self.isVisible():
            return False
        if event.type() == QEvent.ApplicationDeactivate:
            self.hide()
        if obj is self.owner.window() and event.type() in {QEvent.Move, QEvent.Resize, QEvent.Hide, QEvent.Close}:
            self.hide()
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape and QApplication.activePopupWidget() is None:
            self.hide()
            if self._anchor:
                self._anchor.setFocus()
            return True
        if event.type() == QEvent.MouseButtonPress and isinstance(obj, QWidget):
            nested = obj.window().windowType() == Qt.Popup
            if not nested and obj is not self and not self.isAncestorOf(obj) and obj is not self._anchor and not (self._anchor and self._anchor.isAncestorOf(obj)):
                self.hide()
        if obj is self.owner and event.type() in {QEvent.Hide, QEvent.Close}:
            self.hide()
        return False
