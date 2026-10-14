"""Lulu 自有消费级选择浮层，避免系统原生下拉菜单割裂主题。"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QTimer, Signal, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QPainterPath,
    QPalette,
    QRegion,
    QTransform,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme as T
from .control_foundation import PaperTextureFrame, _repolish


_DEFAULT_OPTION_ROW_HEIGHT = 42
_PAPER_OPTION_ROW_HEIGHT = 36
_PAPER_OPTION_SPACING = 1
_PAPER_POPUP_PADDING = 4
_ACTION_ROW_HEIGHT = 38
_ACTION_POPUP_PADDING = 5
_POPUP_ANCHOR_GAP = 7


def _settle_popup_below_trigger(
    popup: QWidget,
    trigger: QWidget,
) -> None:
    """Re-apply the anchor after the native popup window has been placed."""
    if not popup.isVisible() or not trigger.isVisible():
        return
    anchor = trigger.mapToGlobal(
        QPoint(0, trigger.height() + _POPUP_ANCHOR_GAP)
    )
    screen = QGuiApplication.screenAt(anchor)
    if screen is not None:
        available = screen.availableGeometry()
        anchor.setX(
            min(
                max(available.left() + 6, anchor.x()),
                available.right() - popup.width() - 6,
            )
        )
    popup.move(anchor)


def _schedule_popup_anchor_settle(
    popup: QWidget,
    trigger: QWidget,
) -> None:
    # Cocoa may finalize a Qt.Popup's native frame after show(); settle once
    # immediately and once after the next frame without keeping a timer alive.
    for delay_ms in (0, 32):
        QTimer.singleShot(
            delay_ms,
            lambda popup=popup, trigger=trigger: _settle_popup_below_trigger(
                popup,
                trigger,
            ),
        )


class _OptionRow(QPushButton):
    def __init__(self, value: str, label: str, popup: "_ConsumerSelectPopup"):
        super().__init__(popup)
        self.value = value
        self.label = label
        self.setFixedHeight(
            _PAPER_OPTION_ROW_HEIGHT
            if popup.owner.paper_popup
            else _DEFAULT_OPTION_ROW_HEIGHT
        )
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setProperty("selected", False)
        self.setProperty("active", False)
        self.setProperty("hovered", False)
        self.clicked.connect(lambda: popup.choose(self.value))

    def sync(self, *, selected: bool, active: bool) -> None:
        self.setProperty("selected", selected)
        self.setProperty("active", active)
        self.setText(("✓  " if selected else "    ") + self.label)
        _repolish(self)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._set_hovered(False)
        super().leaveEvent(event)

    def _set_hovered(self, hovered: bool) -> None:
        if bool(self.property("hovered")) == hovered:
            return
        self.setProperty("hovered", hovered)
        _repolish(self)
        self.update()


class _ConsumerSelectPopup(QFrame):
    def __init__(self, owner: "ConsumerSelect"):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.owner = owner
        self.setObjectName("consumerSelectPopup")
        if owner.consumer_theme_scope is not None:
            self.setProperty("consumerThemeScope", owner.consumer_theme_scope)
        # 默认分支继续使用完全不透明的原生窗口表面，避免底层文字穿透。
        # 只有显式 paper_popup 的调用方使用透明宿主 + 真圆角纸张内卡。
        self.setAttribute(Qt.WA_TranslucentBackground, owner.paper_popup)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, not owner.paper_popup)
        self.setAutoFillBackground(not owner.paper_popup)
        self.setWindowOpacity(1.0)
        self.setFocusPolicy(Qt.StrongFocus)
        self.rows: list[_OptionRow] = []
        self._active_index = 0
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(
            0 if owner.paper_popup else 6,
            0 if owner.paper_popup else 6,
            0 if owner.paper_popup else 6,
            0 if owner.paper_popup else 6,
        )
        self._layout.setSpacing(0)
        self._paper_card: PaperTextureFrame | None = None
        content_parent: QWidget = self
        content_layout = self._layout
        if owner.paper_popup:
            consumer = owner.consumer_theme()
            self._paper_card = PaperTextureFrame(
                self,
                strength=(
                    consumer.paper_texture_opacity
                    if consumer is not None
                    else 0.0
                ),
                corner_radius=owner.popup_radius,
                layer="floating",
            )
            self._paper_card.setObjectName("consumerSelectPaper")
            self._paper_card.setAttribute(Qt.WA_StyledBackground, True)
            self._layout.addWidget(self._paper_card)
            content_parent = self._paper_card
            content_layout = QVBoxLayout(self._paper_card)
            content_layout.setContentsMargins(
                _PAPER_POPUP_PADDING,
                _PAPER_POPUP_PADDING,
                _PAPER_POPUP_PADDING,
                _PAPER_POPUP_PADDING,
            )
            content_layout.setSpacing(0)
        self.scroll = QScrollArea(content_parent)
        self.scroll.setObjectName("consumerSelectScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setAttribute(Qt.WA_TranslucentBackground, True)
        self.scroll.setAutoFillBackground(False)
        self.scroll.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setStyleSheet(
            "QScrollArea#consumerSelectScroll{background:transparent;border:none;}"
            "QScrollArea#consumerSelectScroll>QWidget>QWidget{background:transparent;}"
        )
        self._rows_host = QWidget()
        self._rows_host.setObjectName("consumerSelectRowsHost")
        self._rows_host.setAttribute(Qt.WA_StyledBackground, True)
        self._rows_host.setAutoFillBackground(False)
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(
            _PAPER_OPTION_SPACING if owner.paper_popup else 2
        )
        self.scroll.setWidget(self._rows_host)
        content_layout.addWidget(self.scroll)
        self.rebuild_rows()
        self.refresh_theme()

    def rebuild_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rows = []
        for value, label in self.owner.options:
            row = _OptionRow(value, label, self)
            self.rows.append(row)
            self._rows_layout.addWidget(row)
        self._active_index = max(0, self.owner.index_of(self.owner.currentData()))
        self._sync_rows()

    def refresh_theme(self) -> None:
        consumer = self.owner.consumer_theme()
        if consumer is not None:
            if self.owner.paper_popup:
                palette = self.palette()
                palette.setColor(QPalette.Window, QColor(0, 0, 0, 0))
                palette.setColor(QPalette.Base, QColor(0, 0, 0, 0))
                self.setPalette(palette)
                if self._paper_card is not None:
                    self._paper_card.set_paper_texture_strength(
                        consumer.paper_texture_opacity
                    )
                self.setStyleSheet(
                    "QFrame#consumerSelectPopup{background:transparent;border:none;}"
                    f"PaperTextureFrame#consumerSelectPaper{{"
                    f"background:{consumer.surface_floating};"
                    f"border:1px solid {consumer.accent_hairline};"
                    f"border-radius:{self.owner.popup_radius}px;}}"
                    f"QPushButton{{background:transparent;color:{consumer.ink};border:none;"
                    f"border-radius:{self.owner.option_radius}px;"
                    "text-align:left;padding:0 10px;font-size:12px;}"
                    f"QPushButton[active=\"true\"]{{background:{consumer.secondary_hover};"
                    f"color:{consumer.ink};}}"
                    f"QPushButton:hover,QPushButton[hovered=\"true\"]{{"
                    f"background:{consumer.secondary_hover};color:{consumer.accent};font-weight:600;}}"
                    f"QPushButton[selected=\"true\"]{{background:{consumer.accent_wash};"
                    f"color:{consumer.accent};font-weight:600;}}"
                    f"QPushButton[selected=\"true\"]:hover,"
                    f"QPushButton[selected=\"true\"][hovered=\"true\"]{{"
                    f"background:{consumer.secondary_hover};color:{consumer.ink};}}"
                    f"QPushButton:pressed{{background:{consumer.secondary_hover};color:{consumer.ink};}}"
                    "QWidget#consumerSelectRowsHost{background:transparent;}"
                    f"QScrollBar:vertical{{background:transparent;width:6px;margin:3px 0;}}"
                    f"QScrollBar::handle:vertical{{background:{consumer.popup_scroll_handle};"
                    "border-radius:3px;min-height:24px;}"
                    "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
                    "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
                )
                self._rows_host.setStyleSheet(self.styleSheet())
                self._update_rounded_mask()
                return
            palette = self.palette()
            palette.setColor(QPalette.Window, QColor(consumer.surface_floating))
            palette.setColor(QPalette.Base, QColor(consumer.surface_floating))
            self.setPalette(palette)
            self.setStyleSheet(
                f"QFrame#consumerSelectPopup{{background:{consumer.surface_floating};"
                f"border:1px solid {consumer.line};border-radius:{self.owner.popup_radius}px;}}"
                f"QPushButton{{background:transparent;color:{consumer.ink};border:none;"
                f"border-radius:{self.owner.option_radius}px;"
                "text-align:left;padding:0 12px;font-size:12px;}"
                f"QPushButton[active=\"true\"]{{background:{consumer.secondary_hover};"
                f"color:{consumer.ink};}}"
                f"QPushButton:hover,QPushButton[hovered=\"true\"]{{"
                f"background:{consumer.secondary_hover};color:{consumer.accent};font-weight:600;}}"
                f"QPushButton[selected=\"true\"]{{background:{consumer.accent_wash};"
                f"color:{consumer.accent};font-weight:600;}}"
                f"QPushButton[selected=\"true\"]:hover,"
                f"QPushButton[selected=\"true\"][hovered=\"true\"]{{"
                f"background:{consumer.secondary_hover};color:{consumer.ink};}}"
                f"QPushButton:pressed{{background:{consumer.secondary_hover};color:{consumer.ink};}}"
                f"QScrollBar:vertical{{background:transparent;width:8px;margin:4px 1px;}}"
                f"QScrollBar::handle:vertical{{background:{consumer.popup_scroll_handle};"
                "border-radius:4px;min-height:28px;}"
                "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
                "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
            )
            self._rows_host.setStyleSheet(self.styleSheet())
            return
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(T.CARD))
        palette.setColor(QPalette.Base, QColor(T.CARD))
        self.setPalette(palette)
        self.setStyleSheet(
            f"QFrame#consumerSelectPopup{{background:{T.CARD};border:1px solid {T.LINE};"
            f"border-radius:{self.owner.popup_radius}px;}}"
            f"QPushButton{{background:transparent;color:{T.INK};border:none;"
            f"border-radius:{self.owner.option_radius}px;"
            "text-align:left;padding:0 12px;font-size:12px;}"
            f"QPushButton[active=\"true\"]{{background:{T.BG_DEEP};color:{T.INK};}}"
            f"QPushButton:hover,QPushButton[hovered=\"true\"]{{background:{T.BG_DEEP};"
            f"color:{T.TERRA};font-weight:600;}}"
            f"QPushButton[selected=\"true\"]{{background:{T.TERRA_WASH};color:{T.TERRA};font-weight:600;}}"
            f"QPushButton[selected=\"true\"]:hover,QPushButton[selected=\"true\"][hovered=\"true\"]{{"
            f"background:{T.TERRA_SOFT};color:{T.INK};}}"
            f"QPushButton:pressed{{background:{T.TERRA_SOFT};color:{T.INK};}}"
        )
        self._rows_host.setStyleSheet(self.styleSheet())

    def show_for(
        self,
        trigger: QPushButton,
        *,
        anchor_ready: bool = False,
    ) -> None:
        if not self.rows:
            return
        if not anchor_ready and self.owner._request_anchor_visibility():
            QTimer.singleShot(
                0,
                lambda: self.show_for(trigger, anchor_ready=True),
            )
            return
        self.refresh_theme()
        self._clear_hover_state()
        self._active_index = max(0, self.owner.index_of(self.owner.currentData()))
        self._sync_rows()
        anchor = trigger.mapToGlobal(
            QPoint(0, trigger.height() + _POPUP_ANCHOR_GAP)
        )
        screen = QGuiApplication.screenAt(anchor)
        available = screen.availableGeometry() if screen is not None else None
        if self.owner.paper_popup:
            metrics = QFontMetrics(trigger.font())
            content_width = max(
                (metrics.horizontalAdvance(label) for _, label in self.owner.options),
                default=0,
            ) + 48
            width = max(trigger.width(), min(max(content_width, 144), 320))
        else:
            width = max(trigger.width(), self.sizeHint().width(), 220)
        if available is not None:
            width = min(width, max(trigger.width(), available.width() - 12))
        row_height = (
            _PAPER_OPTION_ROW_HEIGHT
            if self.owner.paper_popup
            else _DEFAULT_OPTION_ROW_HEIGHT
        )
        spacing = _PAPER_OPTION_SPACING if self.owner.paper_popup else 2
        chrome_height = (
            _PAPER_POPUP_PADDING * 2 if self.owner.paper_popup else 12
        )
        wanted_rows = min(len(self.rows), self.owner.max_visible_rows)
        wanted_height = wanted_rows * row_height + max(0, wanted_rows - 1) * spacing
        theme = self.owner.consumer_theme()
        theme_max = theme.popup_max_height if theme is not None else 260
        theme_max = max(theme_max, wanted_height + chrome_height)
        below = (
            available.bottom() - anchor.y() - 6
            if available is not None
            else theme_max
        )
        if below < row_height + chrome_height:
            self.owner._set_popup_space_error(True)
            self.owner.trigger.setChecked(False)
            return
        self.owner._set_popup_space_error(False)
        rows_height = min(wanted_height, theme_max - chrome_height, below - chrome_height)
        full_rows_height = (
            len(self.rows) * row_height + max(0, len(self.rows) - 1) * spacing
        )
        self._rows_host.setFixedHeight(full_rows_height)
        self.setFixedWidth(width)
        self.scroll.setFixedHeight(max(row_height, rows_height))
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded
            if rows_height < full_rows_height
            else Qt.ScrollBarAlwaysOff
        )
        if self.owner.paper_popup:
            self.setFixedHeight(max(row_height, rows_height) + chrome_height)
        else:
            self.adjustSize()
        self._update_rounded_mask()
        if available is not None:
            anchor.setX(
                min(
                    max(available.left() + 6, anchor.x()),
                    available.right() - width - 6,
                )
            )
        self.move(anchor)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.PopupFocusReason)
        self.owner.trigger.setChecked(True)
        _schedule_popup_anchor_settle(self, trigger)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._update_rounded_mask()

    def _update_rounded_mask(self) -> None:
        if self.owner.paper_popup:
            self.clearMask()
            if self._paper_card is None:
                return
            if self._paper_card.width() <= 0 or self._paper_card.height() <= 0:
                return
            path = QPainterPath()
            rect = QRectF(self._paper_card.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path.addRoundedRect(
                rect,
                self.owner.popup_radius,
                self.owner.popup_radius,
            )
            self._paper_card.setMask(QRegion(path.toFillPolygon().toPolygon()))
            return
        if not self.owner.clip_popup_corners or self.width() <= 0 or self.height() <= 0:
            self.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self.owner.popup_radius, self.owner.popup_radius)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def choose(self, value: str) -> None:
        self.owner.set_value(value, emit=True)
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key_Escape:
            self.close()
            event.accept()
            return
        if event.key() in (Qt.Key_Up, Qt.Key_Down):
            if not self.rows:
                event.accept()
                return
            step = -1 if event.key() == Qt.Key_Up else 1
            self._active_index = (self._active_index + step) % len(self.rows)
            self._sync_rows()
            self.scroll.ensureWidgetVisible(self.rows[self._active_index])
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self.rows:
                self.choose(self.rows[self._active_index].value)
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._clear_hover_state()
        self.owner.trigger.setChecked(False)
        super().hideEvent(event)

    def _clear_hover_state(self) -> None:
        for row in self.rows:
            row._set_hovered(False)

    def _sync_rows(self) -> None:
        current = self.owner.currentData()
        for index, row in enumerate(self.rows):
            row.sync(selected=row.value == current, active=index == self._active_index)


class ConsumerSelect(QWidget):
    """带主题化浮层、键盘语义和稳定值 API 的互斥选择控件。"""

    value_changed = Signal(str)

    def __init__(
        self,
        options: Iterable[tuple[str, str]],
        *,
        value: str,
        accessible_name: str,
        corner_radius: int = 10,
        popup_radius: int = 12,
        clip_popup_corners: bool = False,
        paper_popup: bool = False,
        max_visible_rows: int = 6,
        allow_empty: bool = False,
        empty_text: str = "暂无可选项",
        consumer_theme_scope: str | None = None,
        consumer_theme_name: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        if consumer_theme_scope not in (None, "coreWorkspace"):
            raise ValueError(f"unsupported ConsumerSelect theme scope: {consumer_theme_scope}")
        if consumer_theme_name is not None and consumer_theme_scope is None:
            raise ValueError("consumer_theme_name requires consumer_theme_scope")
        if paper_popup and consumer_theme_scope is None:
            raise ValueError("paper_popup requires consumer_theme_scope")
        self.options = tuple((str(item_value), str(label)) for item_value, label in options)
        self.allow_empty = bool(allow_empty)
        self.empty_text = str(empty_text)
        if not self.options and not self.allow_empty:
            raise ValueError("ConsumerSelect requires at least one option")
        self._value = (
            value
            if self.index_of(value) >= 0
            or (self.allow_empty and value == "")
            else (self.options[0][0] if self.options else "")
        )
        self.max_visible_rows = max(1, int(max_visible_rows))
        self.corner_radius = max(6, int(corner_radius))
        self.popup_radius = max(8, int(popup_radius))
        self.option_radius = max(6, self.popup_radius - 7)
        self.clip_popup_corners = bool(clip_popup_corners)
        self.paper_popup = bool(paper_popup)
        self.consumer_theme_scope = consumer_theme_scope
        self.consumer_theme_name = consumer_theme_name
        if consumer_theme_scope is not None:
            self.setProperty("consumerThemeScope", consumer_theme_scope)
        self.setAccessibleName(accessible_name)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumSize(192, 38)
        self.setMaximumHeight(38)

        self.trigger = QPushButton(self)
        if consumer_theme_scope is not None:
            self.trigger.setProperty("consumerThemeScope", consumer_theme_scope)
        self.trigger.setCheckable(True)
        # Mouse clicks should not leave a persistent blue focus ring after the
        # popup closes; Tab navigation still receives a visible focus state.
        self.trigger.setFocusPolicy(Qt.TabFocus)
        self.trigger.setCursor(Qt.PointingHandCursor)
        self.trigger.setAccessibleName(accessible_name)
        self.trigger.setIcon(QIcon(icons.pixmap("caret_down", T.INK_FAINT, 14)))
        self.trigger.setLayoutDirection(Qt.RightToLeft)
        self.trigger.clicked.connect(self._toggle_popup)
        self.trigger.installEventFilter(self)
        self._popup = None
        self._popup_space_error = False
        self._sync_trigger()
        self.refresh_theme()

    @property
    def popup(self):
        # Most page controls never open their menu. Keep the current value and
        # options in the owner; materialize the popup only on first use.
        if self._popup is None:
            self._popup = _ConsumerSelectPopup(self)
        return self._popup

    def refresh_theme(self) -> None:
        consumer = self.consumer_theme()
        if consumer is not None:
            self.trigger.setIcon(QIcon(icons.pixmap("caret_down", consumer.ink_muted, 14)))
            self.trigger.setStyleSheet(
                f"QPushButton{{background:{consumer.surface_focal};color:{consumer.ink};"
                f"border:1px solid {consumer.line};border-radius:{self.corner_radius}px;"
                "text-align:left;padding:0 14px;font-size:12px;}"
                f"QPushButton:hover{{background:{consumer.accent_wash};"
                f"border-color:{consumer.accent_hairline};}}"
                f"QPushButton:pressed,QPushButton:checked{{background:{consumer.surface_work};"
                f"border-color:{consumer.accent_hairline};}}"
                f"QPushButton:focus{{border:2px solid {consumer.accent};}}"
                f"QPushButton:disabled{{background:{consumer.surface_work};"
                f"color:{consumer.ink_muted};border-color:{consumer.line};}}"
            )
            if self._popup is not None:
                self._popup.refresh_theme()
            return
        self.trigger.setIcon(QIcon(icons.pixmap("caret_down", T.INK_FAINT, 14)))
        self.trigger.setStyleSheet(
            f"QPushButton{{background:{T.CARD};color:{T.INK};border:1px solid {T.LINE};"
            f"border-radius:{self.corner_radius}px;text-align:left;padding:0 14px;font-size:12px;}}"
            f"QPushButton:hover{{background:{T.TERRA_WASH};border-color:{T.TERRA_SOFT};}}"
            f"QPushButton:pressed,QPushButton:checked{{background:{T.BG_DEEP};border-color:{T.TERRA_SOFT};}}"
            f"QPushButton:focus{{border:2px solid {T.TERRA_SOFT};}}"
            f"QPushButton:disabled{{background:{T.BG_DEEP};color:{T.INK_FAINT};border-color:{T.LINE_SOFT};}}"
        )
        if self._popup is not None:
            self._popup.refresh_theme()

    def consumer_theme(self) -> T.ConsumerTheme | None:
        """Return the opt-in semantic theme; the default branch stays byte-for-byte legacy."""
        if self.consumer_theme_scope is None:
            return None
        return T.consumer_theme(self.consumer_theme_name or T.ACTIVE.name)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.trigger.setGeometry(self.rect())
        super().resizeEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.trigger and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Down, Qt.Key_Up, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                self.show_popup()
                event.accept()
                return True
            if event.key() == Qt.Key_Escape and self._popup is not None and self._popup.isVisible():
                self.popup.close()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt API
        super().setEnabled(enabled)
        self.trigger.setEnabled(enabled)
        if not enabled and self._popup is not None:
            self._popup.close()

    def set_value(self, value: str, *, emit: bool = False) -> bool:
        normalized = str(value)
        if self.index_of(normalized) < 0 and not (
            self.allow_empty and normalized == ""
        ):
            return False
        changed = normalized != self._value
        self._value = normalized
        self._sync_trigger()
        if self._popup is not None:
            self._popup._sync_rows()
        if emit and changed:
            self.value_changed.emit(self._value)
            self.currentIndexChanged.emit(self.currentIndex())
        return True

    def set_options(
        self,
        options: Iterable[tuple[str, str]],
        *,
        value: str | None = None,
        emit: bool = False,
    ) -> None:
        normalized = tuple((str(item_value), str(label)) for item_value, label in options)
        if not normalized and not self.allow_empty:
            raise ValueError("ConsumerSelect requires at least one option")
        previous = self._value
        self.options = normalized
        requested = previous if value is None else str(value)
        self._value = (
            requested
            if self.index_of(requested) >= 0
            or (self.allow_empty and requested == "")
            else (self.options[0][0] if self.options else "")
        )
        if self._popup is not None:
            self._popup.close()
            self._popup.rebuild_rows()
        self._sync_trigger()
        if emit and self._value != previous:
            self.value_changed.emit(self._value)
            self.currentIndexChanged.emit(self.currentIndex())

    def currentData(self):  # noqa: N802 - mirror the small read-only part of QComboBox API
        return self._value

    def currentText(self) -> str:  # noqa: N802
        if not self.options or (self.allow_empty and self._value == ""):
            return self.empty_text
        return dict(self.options)[self._value]

    def index_of(self, value: str) -> int:
        return next((index for index, item in enumerate(self.options) if item[0] == value), -1)

    currentIndexChanged = Signal(int)

    def count(self) -> int:
        return len(self.options)

    def currentIndex(self) -> int:  # noqa: N802
        return self.index_of(self._value)

    def itemData(self, index: int):  # noqa: N802
        return self.options[index][0]

    def itemText(self, index: int) -> str:  # noqa: N802
        return self.options[index][1]

    def findData(self, value) -> int:  # noqa: N802
        return self.index_of(str(value))

    def findText(self, text: str) -> int:  # noqa: N802
        return next(
            (index for index, item in enumerate(self.options) if item[1] == str(text)),
            -1,
        )

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if 0 <= int(index) < len(self.options):
            self.set_value(self.options[int(index)][0], emit=True)

    def clear(self) -> None:
        self.set_options((), value="", emit=False)

    def addItem(self, label: str, value=None) -> None:  # noqa: N802
        normalized = str(label if value is None else value)
        options = self.options + ((normalized, str(label)),)
        requested = self._value
        if not requested and not self.allow_empty:
            requested = normalized
        self.set_options(options, value=requested, emit=False)

    def show_popup(self) -> None:
        if self.isEnabled() and self.options:
            self.popup.show_for(self.trigger)

    def _toggle_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.close()
        else:
            self.show_popup()

    def _sync_trigger(self) -> None:
        text = self.currentText()
        self.trigger.setText(text)
        if not self._popup_space_error:
            self.trigger.setToolTip(text)
            self.trigger.setAccessibleDescription(f"当前选择：{text}")

    def _set_popup_space_error(self, failed: bool) -> None:
        self._popup_space_error = bool(failed)
        if failed:
            self.trigger.setToolTip("窗口下方空间不足")
            self.trigger.setAccessibleDescription("窗口下方空间不足")
        else:
            self._sync_trigger()

    def _request_anchor_visibility(self) -> bool:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                viewport = parent.viewport()
                viewport_rect = QRect(
                    viewport.mapToGlobal(QPoint(0, 0)),
                    viewport.size(),
                ).adjusted(0, 8, 0, -8)
                control_rect = QRect(
                    self.mapToGlobal(QPoint(0, 0)),
                    self.size(),
                )
                if viewport_rect.contains(control_rect):
                    return False
                parent.ensureWidgetVisible(self, 0, 8)
                return True
            parent = parent.parentWidget()
        return False


class _ConsumerActionRow(QFrame):
    """动作浮层的一行；文字区和文件夹区拥有完全独立的点击语义。"""

    def __init__(
        self,
        key: str,
        label: str,
        popup: "_ConsumerActionPopup",
    ) -> None:
        super().__init__(popup._paper_card)
        self.key = key
        self.label = label
        self.setObjectName("consumerActionRow")
        self.setFixedHeight(_ACTION_ROW_HEIGHT)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(2)

        self.open_button = QPushButton(label, self)
        self.open_button.setObjectName("consumerActionOpen")
        self.open_button.setCursor(Qt.PointingHandCursor)
        self.open_button.setFocusPolicy(Qt.StrongFocus)
        self.open_button.setAccessibleName(f"打开{label}位置")
        self.open_button.clicked.connect(lambda: popup.activate_row(self.key))
        self.open_button.installEventFilter(popup.owner)
        layout.addWidget(self.open_button, 1)

        self.folder_button = QPushButton(self)
        self.folder_button.setObjectName("consumerActionFolder")
        self.folder_button.setCursor(Qt.PointingHandCursor)
        self.folder_button.setFocusPolicy(Qt.StrongFocus)
        self.folder_button.setFixedSize(30, 30)
        self.folder_button.clicked.connect(lambda: popup.activate_button(self.key))
        self.folder_button.installEventFilter(popup.owner)
        layout.addWidget(self.folder_button, 0, Qt.AlignVCenter)

    def set_context(self, path: str, *, configurable: bool) -> None:
        normalized = str(path)
        self.open_button.setToolTip(normalized)
        self.open_button.setAccessibleDescription(normalized)
        if configurable:
            self.folder_button.setAccessibleName(f"设置{self.label}今后保存位置")
            self.folder_button.setToolTip(f"设置今后保存位置\n{normalized}")
            self.folder_button.setAccessibleDescription(
                f"设置今后保存位置；当前为 {normalized}"
            )
        else:
            self.folder_button.setAccessibleName(f"打开{self.label}位置")
            self.folder_button.setToolTip(normalized)
            self.folder_button.setAccessibleDescription(normalized)


class _ConsumerActionPopup(QFrame):
    """固定动作清单，不承载互斥选择、勾选态或滚动容器。"""

    def __init__(self, owner: "ConsumerActionPopup") -> None:
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.owner = owner
        self.setObjectName("consumerActionPopup")
        self.setProperty("consumerThemeScope", "coreWorkspace")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._paper_card = PaperTextureFrame(
            self,
            strength=owner.consumer_theme().paper_texture_opacity,
            corner_radius=owner.popup_radius,
            layer="floating",
        )
        self._paper_card.setObjectName("consumerActionPaper")
        self._paper_card.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self._paper_card)

        content = QVBoxLayout(self._paper_card)
        content.setContentsMargins(
            _ACTION_POPUP_PADDING,
            _ACTION_POPUP_PADDING,
            _ACTION_POPUP_PADDING,
            _ACTION_POPUP_PADDING,
        )
        content.setSpacing(0)
        self.rows: list[_ConsumerActionRow] = []
        for key, label in owner.options:
            row = _ConsumerActionRow(key, label, self)
            self.rows.append(row)
            content.addWidget(row)
        self.setFixedSize(
            owner.popup_width,
            _ACTION_POPUP_PADDING * 2 + _ACTION_ROW_HEIGHT * len(self.rows),
        )
        self.refresh_theme()

    def refresh_theme(self) -> None:
        consumer = self.owner.consumer_theme()
        self._paper_card.set_paper_texture_strength(consumer.paper_texture_opacity)
        self.setStyleSheet(
            "QFrame#consumerActionPopup{background:transparent;border:none;}"
            f"PaperTextureFrame#consumerActionPaper{{background:{consumer.surface_floating};"
            f"border:1px solid {consumer.accent_hairline};"
            f"border-radius:{self.owner.popup_radius}px;}}"
            "QFrame#consumerActionRow{background:transparent;border:none;border-radius:7px;}"
            f"QFrame#consumerActionRow:hover{{background:{consumer.accent_wash};}}"
            f"QPushButton#consumerActionOpen{{background:transparent;color:{consumer.ink};"
            "border:none;border-radius:7px;text-align:left;padding:0 10px;font-size:12px;}"
            f"QPushButton#consumerActionOpen:hover{{background:{consumer.accent_wash};"
            f"color:{consumer.accent};font-weight:600;}}"
            f"QPushButton#consumerActionOpen:pressed{{background:{consumer.secondary_hover};}}"
            f"QPushButton#consumerActionOpen:focus{{border:2px solid {consumer.accent};}}"
            f"QPushButton#consumerActionFolder{{background:transparent;color:{consumer.ink_soft};"
            "border:1px solid transparent;border-radius:7px;}"
            f"QPushButton#consumerActionFolder:hover{{background:{consumer.secondary_hover};"
            f"color:{consumer.accent};border-color:transparent;}}"
            f"QPushButton#consumerActionFolder:pressed{{background:{consumer.accent_wash};}}"
            f"QPushButton#consumerActionFolder:focus{{border:2px solid {consumer.accent};}}"
        )
        icon = QIcon(icons.pixmap("folder", consumer.ink_soft, 15))
        for row in self.rows:
            row.folder_button.setIcon(icon)
        self._update_rounded_mask()

    def show_for(
        self,
        trigger: QPushButton,
        *,
        keyboard: bool,
        anchor_ready: bool = False,
    ) -> None:
        if not anchor_ready and self.owner._request_anchor_visibility():
            QTimer.singleShot(
                0,
                lambda: self.show_for(
                    trigger,
                    keyboard=keyboard,
                    anchor_ready=True,
                ),
            )
            return
        self.refresh_theme()
        anchor = trigger.mapToGlobal(
            QPoint(0, trigger.height() + _POPUP_ANCHOR_GAP)
        )
        screen = QGuiApplication.screenAt(anchor)
        available = screen.availableGeometry() if screen is not None else None
        if available is not None:
            below = available.bottom() - anchor.y() - 6
            if below < self.height():
                self.owner._set_popup_space_error(True)
                self.owner.trigger.setChecked(False)
                return
            anchor.setX(
                min(
                    max(available.left() + 6, anchor.x()),
                    available.right() - self.width() - 6,
                )
            )
        self.owner._set_popup_space_error(False)
        self.move(anchor)
        self.show()
        self.raise_()
        self.activateWindow()
        self.owner.trigger.setChecked(True)
        self.owner._sync_trigger_icon()
        if keyboard and self.rows:
            self.rows[0].open_button.setFocus(Qt.PopupFocusReason)
        else:
            self.setFocus(Qt.MouseFocusReason)
        _schedule_popup_anchor_settle(self, trigger)

    def activate_row(self, key: str) -> None:
        self.close()
        self.owner.row_activated.emit(key)

    def activate_button(self, key: str) -> None:
        self.close()
        self.owner.button_activated.emit(key)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.owner.trigger.setChecked(False)
        self.owner._sync_trigger_icon()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._update_rounded_mask()

    def _update_rounded_mask(self) -> None:
        self.clearMask()
        if self._paper_card.width() <= 0 or self._paper_card.height() <= 0:
            return
        path = QPainterPath()
        rect = QRectF(self._paper_card.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path.addRoundedRect(rect, self.owner.popup_radius, self.owner.popup_radius)
        self._paper_card.setMask(QRegion(path.toFillPolygon().toPolygon()))


class ConsumerActionPopup(QWidget):
    """消费级动作浮层；固定入口不会被任一行改写成“当前值”。"""

    row_activated = Signal(str)
    button_activated = Signal(str)

    def __init__(
        self,
        actions: Iterable[tuple[str, str]],
        *,
        accessible_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.options = tuple((str(key), str(label)) for key, label in actions)
        if not self.options:
            raise ValueError("ConsumerActionPopup requires at least one action")
        self.popup_width = 172
        self.popup_radius = 12
        self._popup_space_error = False
        self.setProperty("consumerThemeScope", "coreWorkspace")
        self.setAccessibleName(accessible_name)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(96, 38)

        self.trigger = QPushButton(accessible_name, self)
        self.trigger.setProperty("consumerThemeScope", "coreWorkspace")
        self.trigger.setCheckable(True)
        self.trigger.setFocusPolicy(Qt.TabFocus)
        self.trigger.setCursor(Qt.PointingHandCursor)
        self.trigger.setAccessibleName(accessible_name)
        self.trigger.setLayoutDirection(Qt.RightToLeft)
        self.trigger.clicked.connect(self._toggle_popup)
        self.trigger.installEventFilter(self)
        self.popup = _ConsumerActionPopup(self)
        self.refresh_theme()

    def consumer_theme(self) -> T.ConsumerTheme:
        return T.consumer_theme(T.ACTIVE.name)

    def refresh_theme(self) -> None:
        consumer = self.consumer_theme()
        self.trigger.setStyleSheet(
            f"QPushButton{{background:{consumer.surface_focal};color:{consumer.ink};"
            f"border:1px solid {consumer.line};border-radius:10px;"
            "text-align:left;padding:0 10px;font-size:12px;}"
            f"QPushButton:hover{{background:{consumer.accent_wash};"
            f"border-color:{consumer.accent_hairline};}}"
            f"QPushButton:pressed,QPushButton:checked{{background:{consumer.surface_work};"
            f"border-color:{consumer.accent_hairline};}}"
            f"QPushButton:focus{{border:2px solid {consumer.accent};}}"
        )
        self._sync_trigger_icon()
        self.popup.refresh_theme()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.trigger.setGeometry(self.rect())
        super().resizeEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.KeyPress:
            if watched is self.trigger and event.key() in (
                Qt.Key_Down,
                Qt.Key_Return,
                Qt.Key_Enter,
                Qt.Key_Space,
            ):
                self.show_popup(keyboard=True)
                event.accept()
                return True
            if event.key() == Qt.Key_Escape and self.popup.isVisible():
                self.popup.close()
                self.trigger.setFocus(Qt.PopupFocusReason)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def show_popup(self, *, keyboard: bool = False) -> None:
        if self.isEnabled():
            self.popup.show_for(self.trigger, keyboard=keyboard)
            self._sync_trigger_icon()

    def _toggle_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.close()
        else:
            self.show_popup()

    def set_action_context(self, key: str, path: str, *, configurable: bool) -> None:
        row = next((row for row in self.popup.rows if row.key == str(key)), None)
        if row is None:
            raise KeyError(key)
        row.set_context(str(path), configurable=bool(configurable))

    def _set_popup_space_error(self, failed: bool) -> None:
        self._popup_space_error = bool(failed)
        if failed:
            self.trigger.setToolTip("窗口下方空间不足")
            self.trigger.setAccessibleDescription("窗口下方空间不足")
        else:
            self.trigger.setToolTip("查看或设置数据位置")
            self.trigger.setAccessibleDescription("查看或设置数据位置")

    def _sync_trigger_icon(self) -> None:
        consumer = self.consumer_theme()
        pixmap = icons.pixmap("caret_down", consumer.ink_muted, 14)
        if self.popup.isVisible():
            pixmap = pixmap.transformed(
                QTransform().rotate(180),
                Qt.SmoothTransformation,
            )
        self.trigger.setIcon(QIcon(pixmap))

    def _request_anchor_visibility(self) -> bool:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                viewport = parent.viewport()
                viewport_rect = QRect(
                    viewport.mapToGlobal(QPoint(0, 0)),
                    viewport.size(),
                ).adjusted(0, 8, 0, -8)
                control_rect = QRect(
                    self.mapToGlobal(QPoint(0, 0)),
                    self.size(),
                )
                if viewport_rect.contains(control_rect):
                    return False
                parent.ensureWidgetVisible(self, 0, 8)
                return True
            parent = parent.parentWidget()
        return False
