"""设置中心的消费级动画开关，保留原生 CheckBox 键盘与无障碍语义。"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSequentialAnimationGroup,
    Qt,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox

from . import theme as T


def _mix(start: QColor, end: QColor, progress: float) -> QColor:
    value = max(0.0, min(1.0, float(progress)))
    return QColor.fromRgbF(
        start.redF() + (end.redF() - start.redF()) * value,
        start.greenF() + (end.greenF() - start.greenF()) * value,
        start.blueF() + (end.blueF() - start.blueF()) * value,
        start.alphaF() + (end.alphaF() - start.alphaF()) * value,
    )


class ConsumerSwitch(QCheckBox):
    """38×22 动画开关；位置、颜色和保存失败回滚共享同一过渡。"""

    ANIMATION_MS = 170
    ROLLBACK_OUT_MS = 65
    ROLLBACK_RETURN_MS = 105
    ROLLBACK_TURN = 0.38

    def __init__(self, *, accessible_name: str = "开关", parent=None):
        super().__init__(parent)
        self.setObjectName("consumerSwitch")
        self.setFixedSize(38, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(accessible_name)
        self._base_accessible_description = ""
        self._position = 1.0 if self.isChecked() else 0.0
        self._animation_target = self._position
        self.animation = QPropertyAnimation(self, b"position", self)
        self.animation.setDuration(self.ANIMATION_MS)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.finished.connect(self._finish_animation)
        self.rollback_animation = QSequentialAnimationGroup(self)
        self._rollback_out = QPropertyAnimation(self, b"position")
        self._rollback_out.setDuration(self.ROLLBACK_OUT_MS)
        self._rollback_out.setEasingCurve(QEasingCurve.OutCubic)
        self._rollback_return = QPropertyAnimation(self, b"position")
        self._rollback_return.setDuration(self.ROLLBACK_RETURN_MS)
        self._rollback_return.setEasingCurve(QEasingCurve.InOutCubic)
        self.rollback_animation.addAnimation(self._rollback_out)
        self.rollback_animation.addAnimation(self._rollback_return)
        self.rollback_animation.finished.connect(self._finish_rollback)
        self._rollback_target = self._position
        self._keyboard_focus = False
        self.toggled.connect(self._on_toggled)
        self._sync_accessible_description(self.isChecked())

    def get_position(self) -> float:
        return self._position

    def set_position(self, value: float) -> None:
        self._position = max(0.0, min(1.0, float(value)))
        self.update()

    position = Property(float, get_position, set_position)

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt API
        checked = bool(checked)
        changed = checked != self.isChecked()
        QCheckBox.setChecked(self, checked)
        # 正常情况下 toggled 会驱动动画；调用方暂时屏蔽信号时在这里补齐视觉同步。
        if changed and self.signalsBlocked():
            self._sync_accessible_description(checked)
            target = 1.0 if checked else 0.0
            if self.isVisible():
                self._animate_to(target)
            else:
                self.rollback_animation.stop()
                self.animation.stop()
                self._animation_target = target
                self.set_position(target)

    def set_checked(self, checked: bool, *, animate: bool = True) -> None:
        checked = bool(checked)
        changed = checked != self.isChecked()
        if changed:
            previous_blocked = self.blockSignals(True)
            QCheckBox.setChecked(self, checked)
            self.blockSignals(previous_blocked)
        self._sync_accessible_description(checked)
        target = 1.0 if checked else 0.0
        if animate and abs(self._position - target) > 0.0001:
            self._animate_to(target)
        else:
            self.rollback_animation.stop()
            self.animation.stop()
            self._animation_target = target
            self.set_position(target)

    def _on_toggled(self, checked: bool) -> None:
        self._sync_accessible_description(bool(checked))
        target = 1.0 if checked else 0.0
        if self.isVisible():
            self._animate_to(target)
        else:
            self.rollback_animation.stop()
            self.animation.stop()
            self._animation_target = target
            self.set_position(target)

    def _sync_accessible_description(self, checked: bool) -> None:
        state = "已开启" if checked else "已关闭"
        description = (
            f"{self._base_accessible_description}；当前{state}"
            if self._base_accessible_description
            else state
        )
        QCheckBox.setAccessibleDescription(self, description)

    def setAccessibleDescription(self, description: str) -> None:  # noqa: N802 - Qt API
        self._base_accessible_description = str(description or "")
        self._sync_accessible_description(self.isChecked())

    def hitButton(self, position) -> bool:  # noqa: N802 - Qt API
        """整块 38×22 都是点击热区，不依赖系统 CheckBox 指示器几何。"""
        return self.rect().contains(position)

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._keyboard_focus = event.reason() in {
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        }
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._keyboard_focus = False
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._keyboard_focus = False
        self.update()
        super().mousePressEvent(event)

    def _animate_to(self, target: float) -> None:
        self.rollback_animation.stop()
        self.animation.stop()
        self._animation_target = float(target)
        self.animation.setStartValue(self._position)
        self.animation.setEndValue(self._animation_target)
        self.animation.start()

    def _finish_animation(self) -> None:
        self.set_position(self._animation_target)

    def animate_rollback(self, checked: bool) -> None:
        """保存失败时先显露用户刚才的操作，再回到真实旧值。

        持久化通常会在一次点击调用栈内同步失败。此时普通动画还停在旧端点，
        直接恢复只会看起来像“按钮没反应”。这里用一段 65ms 的反向探出和
        105ms 的返回，让失败回滚既可见，也始终在 38×22 原热区内完成。
        """
        checked = bool(checked)
        previous_blocked = self.blockSignals(True)
        QCheckBox.setChecked(self, checked)
        self.blockSignals(previous_blocked)
        self._sync_accessible_description(checked)

        self.animation.stop()
        self.rollback_animation.stop()
        final = 1.0 if checked else 0.0
        current = self._position
        if final == 0.0:
            turn = max(current, self.ROLLBACK_TURN)
        else:
            turn = min(current, 1.0 - self.ROLLBACK_TURN)
        self._rollback_target = final
        self._rollback_out.setStartValue(current)
        self._rollback_out.setEndValue(turn)
        self._rollback_return.setStartValue(turn)
        self._rollback_return.setEndValue(final)
        if self.isVisible():
            self.rollback_animation.start()
        else:
            self.set_position(final)

    def _finish_rollback(self) -> None:
        self.set_position(self._rollback_target)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        progress = self._position

        if self.isEnabled():
            off_track = QColor(T.BG_DEEP)
            on_track = QColor(T.TERRA_DEEP if self.isDown() else T.TERRA)
            track = _mix(off_track, on_track, progress)
            border = _mix(QColor(T.INK_FAINT if self.underMouse() else T.LINE), on_track, progress)
            thumb = _mix(QColor(T.CARD), QColor("#FFFFFF"), progress)
        else:
            track = _mix(QColor(T.BG_DEEP), QColor(T.TERRA_SOFT), progress)
            border = QColor(T.LINE_SOFT)
            thumb = QColor(T.CARD)

        track_rect = QRectF(1, 1, 36, 20)
        painter.setPen(QPen(border, 1))
        painter.setBrush(track)
        painter.drawRoundedRect(track_rect, 10, 10)

        thumb_x = 3.0 + 16.0 * progress
        thumb_rect = QRectF(thumb_x, 3, 16, 16)
        painter.setPen(QPen(_mix(QColor(T.LINE), thumb, progress), 1))
        painter.setBrush(thumb)
        painter.drawEllipse(thumb_rect)

        if self.hasFocus() and self._keyboard_focus:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(T.INK_FAINT), 1.5))
            painter.drawRoundedRect(QRectF(0.75, 0.75, 36.5, 20.5), 10.5, 10.5)
