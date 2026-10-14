"""Lulu themed date field and calendar popup."""

from __future__ import annotations

from PySide6.QtCore import QDate, QEvent, QPoint, QRectF, QTimer, Signal, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainterPath, QRegion
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme as T
from .control_foundation import PaperTextureFrame


class _CalendarPopup(QFrame):
    def __init__(self, owner: "ConsumerDate"):
        super().__init__(
            None,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.owner = owner
        self.setObjectName("consumerCalendarPopup")
        self.setProperty("consumerThemeScope", "coreWorkspace")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._month = QDate(owner.date().year(), owner.date().month(), 1)
        self._active_date = owner.date()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.paper = PaperTextureFrame(
            self,
            strength=owner.consumer_theme().paper_texture_opacity,
            corner_radius=14,
            layer="floating",
        )
        self.paper.setObjectName("consumerCalendarPaper")
        outer.addWidget(self.paper)

        layout = QVBoxLayout(self.paper)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.previous_button = QPushButton("‹")
        self.previous_button.setAccessibleName("上一月")
        self.next_button = QPushButton("›")
        self.next_button.setAccessibleName("下一月")
        for button in (self.previous_button, self.next_button):
            button.setFixedSize(32, 30)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.previous_button.clicked.connect(lambda: self._change_month(-1))
        self.next_button.clicked.connect(lambda: self._change_month(1))
        self.month_label = QLabel()
        self.month_label.setObjectName("calendarMonth")
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.previous_button)
        header.addWidget(self.month_label, 1)
        header.addWidget(self.next_button)
        layout.addLayout(header)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(2)
        self.grid.setVerticalSpacing(2)
        for column, label in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            weekday = QLabel(label)
            weekday.setObjectName("calendarWeekday")
            weekday.setProperty("weekend", column >= 5)
            weekday.setAlignment(Qt.AlignmentFlag.AlignCenter)
            weekday.setFixedHeight(24)
            self.grid.addWidget(weekday, 0, column)
        self.day_buttons: list[QPushButton] = []
        for index in range(42):
            button = QPushButton()
            button.setObjectName("calendarDay")
            button.setFixedSize(34, 30)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, row=index: self._choose_index(row)
            )
            self.day_buttons.append(button)
            self.grid.addWidget(button, 1 + index // 7, index % 7)
        layout.addLayout(self.grid)
        self.setFixedSize(278, 286)
        self.refresh_theme()
        self._rebuild()

    def refresh_theme(self) -> None:
        theme = self.owner.consumer_theme()
        self.paper.set_paper_texture_strength(theme.paper_texture_opacity)
        self.setStyleSheet(
            "QFrame#consumerCalendarPopup{background:transparent;border:none;}"
            f"PaperTextureFrame#consumerCalendarPaper{{background:{theme.surface_floating};"
            f"border:1px solid {theme.accent_hairline};border-radius:14px;}}"
            f"QLabel#calendarMonth{{color:{theme.ink};font-size:13px;font-weight:600;}}"
            f"QLabel#calendarWeekday{{color:{theme.ink_muted};font-size:11px;}}"
            f"QLabel#calendarWeekday[weekend=\"true\"]{{color:{theme.calendar_weekend};}}"
            f"QPushButton{{background:transparent;color:{theme.ink};border:1px solid transparent;"
            "border-radius:8px;font-size:12px;}"
            f"QPushButton:hover,QPushButton[active=\"true\"]{{background:{theme.secondary_hover};"
            f"border-color:{theme.focus_ring};}}"
            f"QPushButton[selected=\"true\"]{{background:{theme.accent};color:{theme.paper_edge};"
            f"border-color:{theme.accent};font-weight:600;}}"
            f"QPushButton[today=\"true\"]{{border-color:{theme.accent_hairline_active};}}"
            f"QPushButton:disabled{{color:{theme.calendar_disabled};background:transparent;"
            "border-color:transparent;}"
        )
        self._update_mask()

    def show_for(self, trigger: QWidget) -> bool:
        self._month = QDate(self.owner.date().year(), self.owner.date().month(), 1)
        self._active_date = self.owner.date()
        self._rebuild()
        self.refresh_theme()
        anchor = trigger.mapToGlobal(QPoint(0, trigger.height() + 7))
        screen = QGuiApplication.screenAt(anchor)
        if screen is not None:
            available = screen.availableGeometry()
            if anchor.y() + self.height() > available.bottom() - 6:
                self.owner._set_space_error(True)
                return False
            anchor.setX(
                min(
                    max(available.left() + 6, anchor.x()),
                    available.right() - self.width() - 6,
                )
            )
        self.owner._set_space_error(False)
        self.move(anchor)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.PopupFocusReason)
        return True

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_mask()

    def _update_mask(self) -> None:
        if self.paper.width() <= 0 or self.paper.height() <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(self.paper.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
            14,
            14,
        )
        self.paper.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def _change_month(self, delta: int) -> None:
        self._month = self._month.addMonths(delta)
        candidate = QDate(
            self._month.year(),
            self._month.month(),
            min(self._active_date.day(), self._month.daysInMonth()),
        )
        self._active_date = candidate
        self._rebuild()

    def _rebuild(self) -> None:
        self.month_label.setText(f"{self._month.year()} 年 {self._month.month()} 月")
        first = self._month.addDays(1 - self._month.dayOfWeek())
        today = QDate.currentDate()
        selected = self.owner.date()
        for index, button in enumerate(self.day_buttons):
            value = first.addDays(index)
            in_month = value.month() == self._month.month()
            button.setText(str(value.day()))
            button.setProperty("dateValue", value.toString("yyyy-MM-dd"))
            button.setProperty("selected", value == selected)
            button.setProperty("today", value == today)
            button.setProperty("weekend", value.dayOfWeek() >= 6)
            button.setProperty("active", value == self._active_date)
            button.setEnabled(in_month)
            button.setAccessibleName(value.toString("yyyy-MM-dd"))
            button.style().unpolish(button)
            button.style().polish(button)

    def _choose_index(self, index: int) -> None:
        button = self.day_buttons[index]
        value = QDate.fromString(
            str(button.property("dateValue") or ""),
            "yyyy-MM-dd",
        )
        if button.isEnabled() and value.isValid():
            self.owner.setDate(value)
            self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self._active_date = self._active_date.addMonths(
                -1 if key == Qt.Key.Key_PageUp else 1
            )
            self._month = QDate(
                self._active_date.year(),
                self._active_date.month(),
                1,
            )
            self._rebuild()
            event.accept()
            return
        offsets = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: -7,
            Qt.Key.Key_Down: 7,
        }
        if key in offsets:
            self._active_date = self._active_date.addDays(offsets[key])
            self._month = QDate(
                self._active_date.year(),
                self._active_date.month(),
                1,
            )
            self._rebuild()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.owner.setDate(self._active_date)
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)


class ConsumerDate(QWidget):
    """Editable themed date field with a Lulu-owned calendar surface."""

    dateChanged = Signal(QDate)

    def __init__(
        self,
        value: QDate,
        *,
        accessible_name: str,
        consumer_theme_name: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("consumerDateField")
        self.setProperty("consumerThemeScope", "coreWorkspace")
        self.setProperty("focused", False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName(accessible_name)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._consumer_theme_name = consumer_theme_name
        self._date = value if value.isValid() else QDate.currentDate()
        self._space_error = False
        self.setFixedHeight(38)
        self.setMinimumWidth(132)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.editor = QLineEdit(self._date.toString("yyyy-MM-dd"))
        self.editor.setObjectName("consumerDateEditor")
        self.editor.setAccessibleName(accessible_name)
        self.editor.setInputMask("0000-00-00;_")
        self.editor.setFixedHeight(38)
        self.editor.editingFinished.connect(self._commit_editor)
        self.editor.installEventFilter(self)
        layout.addWidget(self.editor, 1)
        self.trigger = QPushButton()
        self.trigger.setObjectName("consumerDateTrigger")
        self.trigger.setAccessibleName(f"打开{accessible_name}日历")
        self.trigger.setFixedSize(32, 38)
        self.trigger.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.trigger.setCursor(Qt.CursorShape.PointingHandCursor)
        self.trigger.clicked.connect(self.show_popup)
        self.trigger.installEventFilter(self)
        layout.addWidget(self.trigger)
        self.popup = _CalendarPopup(self)
        self.refresh_theme()

    def consumer_theme(self) -> T.ConsumerTheme:
        return T.consumer_theme(self._consumer_theme_name or T.ACTIVE.name)

    def refresh_theme(self) -> None:
        theme = self.consumer_theme()
        self.trigger.setIcon(QIcon(icons.pixmap("caret_down", theme.ink_muted, 14)))
        self.setStyleSheet(
            f"QWidget#consumerDateField{{background:{theme.surface_focal};"
            f"border:1px solid {theme.line};border-radius:9px;}}"
            f"QWidget#consumerDateField[focused=\"true\"]{{border-color:{theme.focus_ring};}}"
            f"QLineEdit#consumerDateEditor{{background:transparent;color:{theme.ink};"
            "border:none;border-radius:0px;"
            "padding:0 8px;font-size:12px;selection-background-color:transparent;}"
            f"QPushButton#consumerDateTrigger{{background:transparent;color:{theme.ink};"
            f"border:none;border-left:1px solid {theme.secondary_hover};"
            "border-top-left-radius:0px;border-bottom-left-radius:0px;"
            "border-top-right-radius:9px;border-bottom-right-radius:9px;}"
            f"QPushButton#consumerDateTrigger:hover{{background:{theme.accent_wash};}}"
            f"QPushButton#consumerDateTrigger:focus{{background:{theme.accent_wash};}}"
        )
        self.popup.refresh_theme()

    def date(self) -> QDate:
        return QDate(self._date)

    def setDate(self, value: QDate) -> None:  # noqa: N802
        if not isinstance(value, QDate) or not value.isValid():
            self._restore_editor()
            return
        changed = value != self._date
        self._date = QDate(value)
        self._restore_editor()
        if changed:
            self.dateChanged.emit(QDate(self._date))

    def show_popup(self) -> None:
        if self.isEnabled():
            self.popup.show_for(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched in (self.editor, getattr(self, "trigger", None)) and event.type() in (
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        ):
            QTimer.singleShot(0, self._sync_focus_state)
        if watched in (self.editor, getattr(self, "trigger", None)) and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.show_popup()
                event.accept()
                return True
            if event.key() == Qt.Key.Key_Escape and self.popup.isVisible():
                self.popup.close()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _sync_focus_state(self) -> None:
        focused = self.editor.hasFocus() or self.trigger.hasFocus()
        if bool(self.property("focused")) == focused:
            return
        self.setProperty("focused", focused)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _commit_editor(self) -> None:
        value = QDate.fromString(self.editor.text(), "yyyy-MM-dd")
        if value.isValid():
            self.setDate(value)
        else:
            self._restore_editor()

    def _restore_editor(self) -> None:
        self.editor.setText(self._date.toString("yyyy-MM-dd"))
        if not self._space_error:
            self.editor.setToolTip("")
            self.editor.setAccessibleDescription("")

    def _set_space_error(self, failed: bool) -> None:
        self._space_error = bool(failed)
        message = "窗口下方空间不足" if failed else ""
        self.editor.setToolTip(message)
        self.editor.setAccessibleDescription(message)
