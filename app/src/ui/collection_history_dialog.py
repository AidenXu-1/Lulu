"""主页管理采集记录浮层。"""

from __future__ import annotations

from collections.abc import Callable
from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal, QThread, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import dialogs
from .consumer_select import ConsumerSelect
from .control_foundation import (
    PaperTextureFrame,
    adopt_button,
    adopt_consumer_theme_scope,
    adopt_text_field,
)
from . import theme as T
from ..core.collect.history import (
    CollectionHistoryRepository,
    CollectionRecord,
    HistoryError,
    HistorySnapshot,
)
_SORT_OPTIONS = (
    ("newest", "最新创建"),
    ("oldest", "最早创建"),
    ("items", "作品数量"),
    ("name", "任务名称"),
)

def _item_count(record: CollectionRecord) -> int:
    return len(record.items) + len(record.item_trash)


def _record_markers(record: CollectionRecord) -> str:
    markers: list[str] = []
    if record.completion in {"partial", "cancelled"}:
        markers.append("部分结果")
    truthful = ("create_time", "duration_ms", "like", "collect", "comment", "share")
    if any(item.field_state(field) == "unknown" for item in record.items for field in truthful):
        markers.append("含未获取字段")
    items = record.items + record.item_trash
    downloaded = sum(item.cover_status == "downloaded" for item in items)
    uncertain = any(
        str(item.last_error_code or "").endswith("cover_commit_uncertain")
        for item in items
    )
    failed = sum(item.cover_status == "failed" for item in items)
    if uncertain:
        markers.append("封面待恢复")
    elif downloaded:
        markers.append(f"封面 {downloaded}/{len(items)}")
    elif failed:
        markers.append(f"封面失败 {failed}")
    return " · ".join(markers)


def _sorted_records(
    records: tuple[CollectionRecord, ...], mode: str
) -> list[CollectionRecord]:
    if mode == "oldest":
        return sorted(records, key=lambda row: row.created_at)
    if mode == "items":
        return sorted(records, key=lambda row: (_item_count(row), row.created_at), reverse=True)
    if mode == "name":
        return sorted(records, key=lambda row: (row.name.casefold(), row.created_at))
    return sorted(records, key=lambda row: row.created_at, reverse=True)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def _rgba(color: str, alpha: int) -> str:
    value = QColor(color)
    red, green, blue, _ = value.getRgb()
    return f"rgba({red},{green},{blue},{max(0, min(255, alpha))})"


_HISTORY_CHECKED_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class _HistoryBackdrop(QWidget):
    """Paint a softened snapshot so a translucent top-level never exposes desktop."""

    def __init__(
        self,
        *,
        fallback_color: QColor,
        overlay_color: QColor,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._fallback_color = fallback_color
        self._overlay_color = overlay_color
        self._softened_scene = QPixmap()
        self.setObjectName("historyBackdrop")
        self.setProperty("historyBackdropMode", "snapshot_softened")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def capture(self, source: QWidget | None) -> None:
        scene = source.grab() if source is not None and source.isVisible() else QPixmap()
        if scene.isNull():
            self._softened_scene = QPixmap()
            self.update()
            return
        reduction = QSize(
            max(1, scene.width() // 12),
            max(1, scene.height() // 12),
        )
        reduced = scene.scaled(
            reduction,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._softened_scene = reduced.scaled(
            scene.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._softened_scene.setDevicePixelRatio(scene.devicePixelRatio())
        self.update()

    def has_scene(self) -> bool:
        return not self._softened_scene.isNull()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), self._fallback_color)
        if not self._softened_scene.isNull():
            painter.drawPixmap(self.rect(), self._softened_scene)
        painter.fillRect(self.rect(), self._overlay_color)
        painter.end()
        super().paintEvent(event)


class _HistoryListItem(QListWidgetItem):
    """Keep checked truth outside Qt.CheckStateRole so no native box is painted."""

    def setCheckState(self, state: Qt.CheckState) -> None:  # noqa: N802 - Qt API
        self.setData(_HISTORY_CHECKED_ROLE, state == Qt.CheckState.Checked)

    def checkState(self) -> Qt.CheckState:  # noqa: N802 - Qt API
        return (
            Qt.CheckState.Checked
            if bool(self.data(_HISTORY_CHECKED_ROLE))
            else Qt.CheckState.Unchecked
        )


class _HistoryListWidget(QListWidget):
    """Restore checkbox keyboard semantics without Qt's native indicator."""

    toggle_requested = Signal(object)
    set_all_requested = Signal(bool)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if (
            event.key() == Qt.Key.Key_Space
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            item = self.currentItem()
            if item is not None:
                self.toggle_requested.emit(item)
                event.accept()
                return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.set_all_requested.emit(True)
            event.accept()
            return
        if (
            event.key() == Qt.Key.Key_A
            and event.modifiers()
            == (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier
            )
        ):
            self.set_all_requested.emit(False)
            event.accept()
            return
        super().keyPressEvent(event)


class _ElidedLabel(QLabel):
    """Single-line label that keeps its complete accessible text and tooltip."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.set_full_text(text)

    @property
    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self.setAccessibleName(self._full_text)
        self._sync_text()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._sync_text()
        super().resizeEvent(event)

    def _sync_text(self) -> None:
        available = max(0, self.contentsRect().width())
        shown = self.fontMetrics().elidedText(
            self._full_text,
            Qt.TextElideMode.ElideRight,
            available,
        )
        super().setText(shown)


class _HistoryRecordRow(QFrame):
    """Presentation-only row; the QListWidgetItem remains selection truth."""

    toggle_requested = Signal(str)
    rename_requested = Signal(str, str)
    move_requested = Signal(str)

    def __init__(
        self,
        record: CollectionRecord,
        *,
        needle: str,
        editable: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.record_id = record.record_id
        self.setObjectName("historyRecordRow")
        self.setProperty("historyRecordRow", True)
        self.setProperty("checked", False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(66)
        self.setAccessibleName(record.name)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 12, 0)
        row.setSpacing(10)

        self.accent = QFrame(self)
        self.accent.setObjectName("historySelectionAccent")
        self.accent.setFixedWidth(3)
        row.addWidget(self.accent)

        self.check = QLabel("✓", self)
        self.check.setObjectName("historyCheck")
        self.check.setProperty("checked", False)
        self.check.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.check.setFixedSize(22, 22)
        self.check.setAccessibleName(f"选择任务：{record.name}")
        self.check.setAccessibleDescription("未选择")
        row.addWidget(self.check)

        copy = QVBoxLayout()
        copy.setContentsMargins(0, 6, 0, 6)
        copy.setSpacing(3)
        self.name_label = _ElidedLabel(record.name, self)
        self.name_label.setObjectName("historyRecordName")
        self.name_label.setProperty("searchMatch", bool(needle and needle in record.name.casefold()))
        self.name_label.setAccessibleDescription(_record_markers(record))
        self.name_label.installEventFilter(self)
        copy.addWidget(self.name_label)
        self.name_edit = QLineEdit(record.name)
        self.name_edit.setMaxLength(120)
        self.name_edit.setAccessibleName(f"采集记录名称：{record.name}")
        self.name_edit.returnPressed.connect(self._submit_name)
        self.name_edit.installEventFilter(self)
        self.name_edit.hide()
        copy.addWidget(self.name_edit)
        author = record.author_name or "未知作者"
        self.author_label = _ElidedLabel(author, self)
        self.author_label.setObjectName("historyRecordAuthor")
        self.author_label.setProperty("searchMatch", bool(needle and needle in author.casefold()))
        copy.addWidget(self.author_label)
        row.addLayout(copy, 1)

        self.count_label = QLabel(str(_item_count(record)), self)
        self.count_label.setObjectName("historyRecordCount")
        self.count_label.setFixedWidth(46)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.count_label)
        stamp = record.created_at[:16].replace("T", " ")
        self.time_label = QLabel(stamp, self)
        self.time_label.setObjectName("historyRecordTime")
        self.time_label.setFixedWidth(124)
        row.addWidget(self.time_label)

        operations = QWidget()
        operations.setFixedWidth(136)
        actions = QHBoxLayout(operations)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        self.edit_button = QPushButton("改名")
        self.edit_button.setFixedWidth(44)
        self.move_button = QPushButton("移入废纸篓")
        for button, title in ((self.edit_button, "改名"), (self.move_button, "移入废纸篓")):
            adopt_button(button, role="tertiary", size="sm", accessible_name=f"{title}：{record.name}")
            button.setEnabled(editable)
            actions.addWidget(button)
        self.edit_button.clicked.connect(self._edit_name)
        self.move_button.clicked.connect(lambda: self.move_requested.emit(self.record_id))
        row.addWidget(operations)
        self.editable = editable
        self._stamp = stamp
        self._author = author

    def set_compact(self, compact: bool) -> None:
        self.time_label.setVisible(not compact)
        self.author_label.set_full_text(f"{self._author} · {self._stamp}" if compact else self._author)

    def _edit_name(self):
        if not self.editable:
            return
        if not self.name_edit.isHidden():
            self._submit_name()
            return
        self.name_edit.setText(self.name_label.full_text)
        self.name_label.hide()
        self.name_edit.show()
        self.edit_button.setText("保存")
        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def _submit_name(self):
        name = self.name_edit.text().strip()
        if not name:
            self.name_edit.setPlaceholderText("名称不能为空")
            self.name_edit.setFocus()
            return
        self.rename_requested.emit(self.record_id, name)

    def _cancel_name(self):
        self.name_edit.hide()
        self.name_label.show()
        self.edit_button.setText("改名")
        self.edit_button.setFocus()

    def set_checked(self, checked: bool) -> None:
        selected = bool(checked)
        self.setProperty("checked", selected)
        self.check.setProperty("checked", selected)
        self.check.setAccessibleDescription("已选择" if selected else "未选择")
        _repolish(self)
        _repolish(self.check)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.name_edit and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self._cancel_name()
            return True
        if (
            watched is self.name_label
            and event.type() == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._edit_name()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_requested.emit(self.record_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _MoveRecords(QThread):
    saved = Signal(object)
    failed = Signal(str)

    def __init__(self, repository, selected, expected, parent):
        super().__init__(parent)
        self.repository, self.selected, self.expected = repository, selected, expected

    def run(self):
        try:
            self.saved.emit(self.repository.move_records_to_system_trash(
                self.selected, expected_source_hash=self.expected,
            ))
        except HistoryError as exc:
            self.failed.emit(exc.code)
        except OSError:
            self.failed.emit("system_trash_failed")


class _HistoryFontWatcher(QObject):
    def eventFilter(self, watched, event):
        if (watched is QApplication.instance() and event.type() == QEvent.Type.DynamicPropertyChange
                and event.propertyName() == b'luluFontPercent'):
            self.parent()._font_scale_timer.start(0)
        return False


class CollectionHistoryDialog(QFrame):
    """主窗口内容区内嵌的 740×500 任务管理卡片。"""

    finished = Signal(int)
    accepted = Signal()
    rejected = Signal()

    def __init__(
        self,
        repository: CollectionHistoryRepository,
        snapshot: HistorySnapshot,
        *,
        on_changed: Callable[[HistorySnapshot], None] | None = None,
        can_move: Callable[[], bool] = lambda: True,
        parent: QWidget | None = None,
    ) -> None:
        if parent is None:
            raise ValueError("管理采集记录必须嵌入现有 Lulu 窗口。")
        super().__init__(parent)
        self.repository = repository
        self.snapshot = snapshot
        self.on_changed = on_changed
        self.can_move = can_move
        self._moving = None
        self._checked_ids = set()
        self._compact = False
        self._visible_records: list[CollectionRecord] = []
        self._previous_focus: QWidget | None = None
        self._result = 0
        self._opened = False
        self.setObjectName("collectionHistoryOverlay")
        self.setProperty("luluControl", "inlineOverlay")
        self.setProperty("historyOverlayKind", "inline")
        adopt_consumer_theme_scope(self, "coreWorkspace")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.hide()
        consumer = T.consumer_theme(T.ACTIVE.name)
        overlay_rgba = (24, 20, 18, 112)
        overlay = f"rgba({','.join(str(value) for value in overlay_rgba)})"
        self.setProperty("historyOverlayColor", overlay)
        self._history_local_qss = self._dialog_qss(consumer)
        self.setStyleSheet(T.QSS + self._history_local_qss)
        self._font_scale_timer = QTimer(self)
        self._font_scale_timer.setSingleShot(True)
        self._font_scale_timer.timeout.connect(self._sync_text_scale)
        self._font_watcher = _HistoryFontWatcher(self)
        self.backdrop = _HistoryBackdrop(
            fallback_color=QColor(consumer.surface_work),
            overlay_color=QColor(*overlay_rgba),
            parent=self,
        )
        self.backdrop.lower()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        center = QHBoxLayout()
        center.addStretch(1)
        self.card = PaperTextureFrame(
            strength=consumer.paper_texture_opacity,
            corner_radius=16,
            layer="floating",
        )
        self.card.setObjectName("historyCard")
        self.card.setProperty("luluControl", "popover")
        self.card.setProperty("surface", "card")
        self.card.setProperty("surfaceVariant", "historyDialog")
        adopt_consumer_theme_scope(self.card, "coreWorkspace")
        initial_width = 740
        initial_height = 500
        if parent is not None and parent.width() > 0 and parent.height() > 0:
            initial_width = max(320, min(740, parent.width() - 48))
            initial_height = max(300, min(500, parent.height() - 48))
        self.card.setFixedSize(initial_width, initial_height)
        T.shadow(
            self.card,
            blur=34,
            dy=10,
            alpha=max(10, consumer.popup_shadow_alpha),
        )
        center.addWidget(self.card)
        center.addStretch(1)
        outer.addLayout(center)
        outer.addStretch(1)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)
        title_rule = QFrame()
        title_rule.setObjectName("studioTitleRule")
        title_rule.setFixedSize(3, 22)
        title_row.addWidget(title_rule)
        self.title_label = QLabel("管理采集记录")
        self.title_label.setObjectName("historyDialogTitle")
        self.title_label.setAccessibleName("管理采集记录")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        self.close_button = QPushButton("×")
        adopt_button(
            self.close_button,
            role="icon",
            size="md",
            accessible_name="关闭管理采集记录",
        )
        self.close_button.setFixedWidth(32)
        self.close_button.clicked.connect(self.reject)
        title_row.addWidget(self.close_button)
        layout.addLayout(title_row)

        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(10)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索记录名称 / 作者")
        adopt_text_field(self.search_edit, accessible_name="搜索采集任务")
        self.search_edit.textChanged.connect(self.refresh)
        tools.addWidget(self.search_edit, 1)
        self.sort_select = ConsumerSelect(
            _SORT_OPTIONS,
            value="newest",
            accessible_name="采集任务排序",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        self.sort_select.setFixedWidth(150)
        self.sort_select.value_changed.connect(lambda *_: self.refresh())
        tools.addWidget(self.sort_select)
        layout.addLayout(tools)

        self.readonly_label = QLabel("")
        self.readonly_label.setObjectName("pageMeta")
        layout.addWidget(self.readonly_label)
        self.header = QFrame()
        self.header.setObjectName("historyTableHeader")
        self.header.setFixedHeight(36)
        headings = QHBoxLayout(self.header)
        headings.setContentsMargins(18, 0, 24, 0)
        headings.setSpacing(10)
        self.select_all = QCheckBox()
        self.select_all.setAccessibleName("选择当前搜索结果")
        self.select_all.setFixedWidth(22)
        self.select_all.clicked.connect(self._set_all_items_checked)
        headings.addWidget(self.select_all)
        headings.addWidget(QLabel("记录名称"), 1)
        count = QLabel("作品")
        count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        count.setFixedWidth(46)
        headings.addWidget(count)
        self.time_heading = QLabel("采集时间")
        self.time_heading.setFixedWidth(124)
        headings.addWidget(self.time_heading)
        operations = QLabel("操作")
        operations.setFixedWidth(136)
        operations.setAlignment(Qt.AlignCenter)
        headings.addWidget(operations)
        layout.addWidget(self.header)
        layout.setSpacing(7)

        self.list = _HistoryListWidget()
        self.list.setObjectName("historyList")
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.list.setSpacing(0)
        self.list.setAccessibleName("采集记录列表")
        self.list.setProperty("historyBucket", "active")
        self.list.toggle_requested.connect(self._toggle_item)
        self.list.set_all_requested.connect(self._set_all_items_checked)
        self.list.itemClicked.connect(self._toggle_item)
        self.list.itemChanged.connect(self._sync_item_row)
        layout.addWidget(self.list, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("pageMeta")

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.summary)
        actions.addStretch(1)
        self.move_button = QPushButton("移入废纸篓")
        adopt_button(self.move_button, role="secondary", size="sm")
        actions.addWidget(self.move_button)
        self.move_button.clicked.connect(self.move_selected)
        self.move_button.setProperty("historyAction", "danger")
        layout.addLayout(actions)
        self.refresh()
        self._install_focus_filter()

    @staticmethod
    def _dialog_qss(t: T.ConsumerTheme) -> str:
        return f"""
QFrame#collectionHistoryOverlay {{
    background: transparent;
}}
QFrame#collectionHistoryOverlay PaperTextureFrame#historyCard {{
    background: {t.surface_floating};
    border: {t.border_width}px solid {t.line};
    border-radius: 16px;
}}
QFrame#collectionHistoryOverlay QLabel {{
    background: transparent;
    color: {t.ink};
    font-family: "{t.font_body}";
}}
QFrame#collectionHistoryOverlay QFrame#studioTitleRule {{
    background: {t.accent};
    border: none;
    border-radius: 2px;
}}
QFrame#collectionHistoryOverlay QLabel#historyDialogTitle {{
    color: {t.ink};
    font-family: "{t.font_display}";
    font-size: 19px;
    font-weight: 650;
}}
QFrame#collectionHistoryOverlay QFrame#historyTableHeader {{
    background: {t.surface_work}; border-bottom: 1px solid {t.line}; font-weight: 650;
}}
QFrame#collectionHistoryOverlay QLabel#pageMeta {{
    color: {t.ink_muted};
    font-size: {t.type_caption}px;
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"] {{
    border-radius: {t.radius_control}px;
    font-size: {t.type_body}px;
    font-weight: 500;
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"][role="secondary"] {{
    background: {t.secondary};
    color: {t.ink_soft};
    border: {t.border_width}px solid transparent;
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"][role="secondary"]:hover {{
    background: {t.secondary_hover};
    color: {t.ink};
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"][role="icon"] {{
    background: transparent;
    color: {t.ink_muted};
    border: {t.border_width}px solid transparent;
    font-size: 20px;
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"][role="icon"]:hover {{
    background: {t.secondary_hover};
    color: {t.ink};
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"][historyAction="danger"] {{
    color: {t.danger};
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"][historyRowEdit="true"] {{
    padding: 0 8px;
}}
QFrame#collectionHistoryOverlay QPushButton[luluControl="button"]:disabled {{
    background: {t.surface_work};
    color: {t.ink_muted};
    border-color: {t.line};
}}
QFrame#collectionHistoryOverlay QLineEdit[luluControl="textField"] {{
    background: {t.surface_focal};
    color: {t.ink};
    border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_control}px;
    padding: 0 11px;
    selection-background-color: {t.accent_wash};
}}
QFrame#collectionHistoryOverlay QLineEdit[luluControl="textField"]:focus {{
    border-color: {t.accent};
}}
QFrame#collectionHistoryOverlay QFrame#historyTabs {{
    background: transparent;
    border: none;
    border-bottom: {t.border_width}px solid {t.line};
}}
QFrame#collectionHistoryOverlay QPushButton[historyTab="true"] {{
    background: transparent;
    color: {t.ink_muted};
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 0 10px;
}}
QFrame#collectionHistoryOverlay QPushButton[historyTab="true"]:hover {{
    background: {t.secondary_hover};
    color: {t.ink};
}}
QFrame#collectionHistoryOverlay QPushButton[historyTab="true"]:checked {{
    background: transparent;
    color: {t.accent};
    border-bottom: 2px solid {t.accent};
    font-weight: 650;
}}
QFrame#collectionHistoryOverlay QListWidget#historyList {{
    background: {t.surface_focal};
    color: {t.ink};
    border: {t.border_width}px solid {t.line};
    border-radius: 10px;
    outline: none;
    padding: 4px;
}}
QFrame#collectionHistoryOverlay QListWidget#historyList[historyBucket="trash"] {{
    background: {_rgba(t.danger, 18)};
    border-color: {_rgba(t.danger, 72)};
}}
QFrame#collectionHistoryOverlay QListWidget#historyList::item {{
    height: 66px;
    padding: 0;
    margin: 0;
    border: none;
    border-radius: 8px;
}}
QFrame#collectionHistoryOverlay QListWidget#historyList::item:hover {{
    background: {t.secondary_hover};
}}
QFrame#collectionHistoryOverlay QFrame#historyRecordRow {{
    background: transparent;
    border: none;
    border-radius: 8px;
}}
QFrame#collectionHistoryOverlay QFrame#historyRecordRow[checked="true"] {{
    background: {t.accent_wash};
}}
QFrame#collectionHistoryOverlay QFrame#historySelectionAccent {{
    background: transparent;
    border: none;
    border-radius: 2px;
}}
QFrame#collectionHistoryOverlay QFrame#historyRecordRow[checked="true"] QFrame#historySelectionAccent {{
    background: {t.accent};
}}
QFrame#collectionHistoryOverlay QLabel#historyCheck {{
    background: transparent;
    color: transparent;
    border: {t.border_width}px solid {t.line};
    border-radius: 5px;
    font-size: 13px;
    font-weight: 700;
}}
QFrame#collectionHistoryOverlay QLabel#historyCheck[checked="true"] {{
    background: {t.accent};
    color: {t.surface_focal};
    border-color: {t.accent};
}}
QFrame#collectionHistoryOverlay QLabel#historyRecordName {{
    color: {t.ink};
    font-size: {t.type_card}px;
    font-weight: 600;
}}
QFrame#collectionHistoryOverlay QLabel#historyRecordAuthor,
QFrame#collectionHistoryOverlay QLabel#historyRecordTime {{
    color: {t.ink_muted};
    font-size: {t.type_caption}px;
}}
QFrame#collectionHistoryOverlay QLabel#historyRecordCount {{
    color: {t.ink_soft};
    font-size: {t.type_body}px;
    font-weight: 600;
}}
QFrame#collectionHistoryOverlay QLabel[searchMatch="true"] {{
    background: {t.accent_wash};
    color: {t.accent};
    border-radius: 4px;
    padding: 1px 3px;
}}
QFrame#collectionHistoryOverlay QLabel#historyRecordMarker {{
    color: {t.ink_muted};
    background: {t.secondary};
    border: {t.border_width}px solid {t.line};
    border-radius: 6px;
    padding: 0 6px;
    font-size: {t.type_caption}px;
}}
QFrame#collectionHistoryOverlay QLabel#historyRecordMarker[markerKind="warning"] {{
    color: {T.AMBER};
    background: {T.AMBER_WASH};
    border-color: {T.AMBER};
}}
QFrame#collectionHistoryOverlay QScrollBar:vertical {{
    width: 7px;
    background: transparent;
    margin: 2px;
}}
QFrame#collectionHistoryOverlay QScrollBar::handle:vertical {{
    min-height: 32px;
    background: {t.line};
    border-radius: 3px;
}}
QFrame#collectionHistoryOverlay QScrollBar::add-line:vertical,
QFrame#collectionHistoryOverlay QScrollBar::sub-line:vertical {{
    height: 0;
    background: transparent;
}}
"""

    def open(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            raise RuntimeError("管理采集记录缺少嵌入宿主。")
        if self._opened:
            self.raise_()
            self._focus_first_control()
            return
        current_focus = QApplication.focusWidget()
        if (
            current_focus is not None
            and current_focus.window() is parent.window()
            and not self.isAncestorOf(current_focus)
        ):
            self._previous_focus = current_focus
        else:
            self._previous_focus = parent.focusWidget()
        self._result = 0
        self._opened = True
        parent.installEventFilter(self)
        self._capture_backdrop()
        self.setGeometry(parent.rect())
        self.backdrop.setGeometry(self.rect())
        self.backdrop.lower()
        self._fit_card()
        self.show()
        self.raise_()
        self._focus_first_control()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        QApplication.instance().installEventFilter(self._font_watcher)
        self._sync_text_scale()
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        if not self.backdrop.has_scene():
            self._capture_backdrop()
        self.backdrop.setGeometry(self.rect())
        self.backdrop.lower()
        self._fit_card()
        super().showEvent(event)

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self._font_watcher)
        self._font_scale_timer.stop()
        super().hideEvent(event)

    def _sync_text_scale(self):
        T._scale_dialog_text(self, QApplication.instance().property('luluFontPercent') or 100)
        self._fit_card()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.backdrop.setGeometry(self.rect())
        self.backdrop.lower()
        self._fit_card()
        super().resizeEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        parent = self.parentWidget()
        if watched is parent:
            if event.type() == QEvent.Type.Resize:
                self.setGeometry(parent.rect())
                self.backdrop.setGeometry(self.rect())
                self._fit_card()
            elif event.type() == QEvent.Type.Hide and self._opened:
                self.reject()
            return False
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                row = watched.parentWidget() if isinstance(watched, QLineEdit) else None
                if isinstance(row, _HistoryRecordRow) and watched is row.name_edit:
                    row._cancel_name()
                    return True
                self.reject()
                event.accept()
                return True
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                backwards = (
                    event.key() == Qt.Key.Key_Backtab
                    or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                )
                self._move_focus_within_overlay(not backwards)
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            backwards = (
                event.key() == Qt.Key.Key_Backtab
                or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            )
            self._move_focus_within_overlay(not backwards)
            event.accept()
            return
        super().keyPressEvent(event)

    def done(self, result: int) -> None:
        if not self._opened or self._moving is not None:
            return
        self._opened = False
        self._result = int(result)
        previous_focus = self._previous_focus
        self._previous_focus = None
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        self.hide()
        if self._result:
            self.accepted.emit()
        else:
            self.rejected.emit()
        self.finished.emit(self._result)
        if previous_focus is not None and previous_focus.isVisible():
            previous_focus.setFocus(Qt.FocusReason.OtherFocusReason)

    def accept(self) -> None:
        self.done(1)

    def reject(self) -> None:
        self.done(0)

    def result(self) -> int:
        return self._result

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        local = self.card.mapFrom(self, event.position().toPoint())
        if not self.card.rect().contains(local):
            self.reject()
            event.accept()
            return
        super().mousePressEvent(event)

    def _fit_card(self) -> None:
        width = max(320, min(740, self.width() - 48))
        height = max(300, min(500, self.height() - 48))
        self.card.setFixedSize(width, height)
        compact = width < 580
        if compact != self._compact:
            self._compact = compact
            self.time_heading.setVisible(not compact)
            for row in self.list.findChildren(_HistoryRecordRow):
                row.set_compact(compact)

    def _capture_backdrop(self) -> None:
        parent = self.parentWidget()
        self.backdrop.capture(parent)

    def _install_focus_filter(self) -> None:
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)

    def _focus_first_control(self) -> None:
        self.search_edit.setFocus(Qt.FocusReason.PopupFocusReason)

    def _move_focus_within_overlay(self, forward: bool) -> None:
        current = QApplication.focusWidget()
        cursor = current if current is not None else self
        visited: set[int] = set()
        while cursor is not None and id(cursor) not in visited:
            visited.add(id(cursor))
            cursor = (
                cursor.nextInFocusChain()
                if forward
                else cursor.previousInFocusChain()
            )
            if cursor is None:
                break
            if (
                self.isAncestorOf(cursor)
                and cursor.isVisibleTo(self)
                and cursor.isEnabled()
                and cursor.focusPolicy() != Qt.FocusPolicy.NoFocus
            ):
                cursor.setFocus(
                    Qt.FocusReason.TabFocusReason
                    if forward
                    else Qt.FocusReason.BacktabFocusReason
                )
                return
        self._focus_first_control()

    def refresh(self) -> None:
        source = self.snapshot.active_records
        self._checked_ids.intersection_update(row.record_id for row in source)
        needle = self.search_edit.text().strip().casefold()
        filtered = tuple(
            row for row in source
            if not needle or needle in row.name.casefold() or needle in row.author_name.casefold()
        )
        self._visible_records = _sorted_records(filtered, self.sort_select.currentData())
        scroll = self.list.verticalScrollBar().value()
        self.list.blockSignals(True)
        self.list.clear()
        for record in self._visible_records:
            item = _HistoryListItem()
            item.setData(Qt.UserRole, record.record_id)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            checked = record.record_id in self._checked_ids
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            item.setSizeHint(QSize(0, 66))
            item.setToolTip(record.name)
            self.list.addItem(item)
            row = _HistoryRecordRow(record, needle=needle, editable=self.snapshot.writable, parent=self.list)
            row.set_compact(self._compact)
            row.set_checked(checked)
            row.toggle_requested.connect(self._toggle_record)
            row.rename_requested.connect(self._rename_record)
            row.move_requested.connect(lambda key: self._confirm_move([key]))
            self.list.setItemWidget(item, row)
        self.list.blockSignals(False)
        self.list.verticalScrollBar().setValue(scroll)
        self.readonly_label.setText(self._error_text(self.snapshot.code)
            if self.snapshot.code == 'system_trash_pending' else "只读：文件版本过高或数据异常")
        self.readonly_label.setVisible(not self.snapshot.writable)
        self._sync_selection()
        self._install_focus_filter()

    def _toggle_item(self, item: QListWidgetItem) -> None:
        self.list.setCurrentItem(item)
        item.setCheckState(
            Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
        )

    def _set_all_items_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(state)

    def _toggle_record(self, record_id: str) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            if str(item.data(Qt.UserRole)) == record_id:
                self._toggle_item(item)
                return

    def _sync_item_row(self, item: QListWidgetItem) -> None:
        key = str(item.data(Qt.UserRole))
        checked = item.checkState() == Qt.Checked
        if checked:
            self._checked_ids.add(key)
        else:
            self._checked_ids.discard(key)
        row = self.list.itemWidget(item)
        if isinstance(row, _HistoryRecordRow):
            row.set_checked(checked)
        self._sync_selection()

    def _sync_selection(self):
        visible = {record.record_id for record in self._visible_records}
        shown = len(visible & self._checked_ids)
        hidden = len(self._checked_ids - visible)
        self.select_all.setCheckState(
            Qt.PartiallyChecked if 0 < shown < len(visible) else
            Qt.Checked if shown else Qt.Unchecked
        )
        suffix = f"（含 {hidden} 项搜索外记录）" if hidden else ""
        self.summary.setText(f"已选 {len(self._checked_ids)} 项{suffix}" if self._checked_ids else
                             f"显示 {len(visible)} / {len(self.snapshot.active_records)} 份记录")
        self.move_button.setEnabled(bool(self._checked_ids) and self.snapshot.writable)

    def selected_ids(self) -> list[str]:
        return [record.record_id for record in self.snapshot.active_records
                if record.record_id in self._checked_ids]

    def _apply(self, operation: Callable[[], HistorySnapshot]) -> bool:
        try:
            snapshot = operation()
        except HistoryError as exc:
            dialogs.alert(self, "记录未修改", self._error_text(exc.code))
            return False
        self._changed(snapshot)
        return True

    def _changed(self, snapshot):
        self.snapshot = snapshot
        if self.on_changed is not None:
            self.on_changed(snapshot)
        self.refresh()

    def _rename_record(self, record_id: str, name: str) -> None:
        if self.snapshot.writable and self.can_move():
            if self._apply(lambda: self.repository.rename(
                record_id, name, expected_source_hash=self.snapshot.source_hash,
            )):
                for row in self.list.findChildren(_HistoryRecordRow):
                    if row.record_id == record_id:
                        row.edit_button.setFocus(Qt.OtherFocusReason)
                        break

    def move_selected(self) -> None:
        self._confirm_move(self.selected_ids())

    def _confirm_move(self, selected: list[str]) -> None:
        if self._moving is not None or not selected or not self.snapshot.writable:
            return
        if not self.can_move():
            dialogs.alert(self, "暂时无法移动", "请等待当前采集完成再试。")
            return
        expected = self.snapshot.source_hash
        records = [record for record in self.snapshot.active_records if record.record_id in selected]
        names = "、".join(record.name for record in records[:3])
        if len(records) > 3:
            names += f"等 {len(records)} 份记录"
        works = sum(_item_count(record) for record in records)
        if not dialogs.confirm(
            self, "移入 macOS 废纸篓",
            f"将「{names}」移入系统废纸篓？其中有 {works} 条作品信息。"
            "已保存的音视频、封面和文稿会保留。可在废纸篓选择「放回原处」，再返回主页采集。",
            ok="移入废纸篓",
        ):
            return
        if not self.can_move():
            dialogs.alert(self, "暂时无法移动", "采集已开始，请等待任务完成再试。")
            return
        self.card.setEnabled(False)
        self.summary.setText("正在移入系统废纸篓…")
        worker = _MoveRecords(self.repository, selected, expected, self)
        self._moving = worker
        worker.saved.connect(self._changed)
        worker.failed.connect(self._move_failed)
        worker.finished.connect(self._finish_move)
        worker.start()

    def _move_failed(self, code: str):
        self._sync_selection()
        self.readonly_label.setText(self._error_text(code))
        self.readonly_label.show()

    def _finish_move(self):
        self._moving.deleteLater()
        self._moving = None
        self.card.setEnabled(True)
        self._focus_first_control()

    @staticmethod
    def _error_text(code: str) -> str:
        return {
            "system_trash_pending": "移入废纸篓尚未完成，续办记录已保留；请稍后重开采集页重试，不会再次移动已完成的包。",
            "concurrent_change": "任务文件已被其他窗口更新，请关闭并重新打开后再试。",
            "system_trash_failed": "系统废纸篓未确认移动，活动记录已保留。请检查磁盘权限后重试。",
            "disk_full": "磁盘空间不足，原任务没有改变。",
            "permission_denied": "当前目录不可写，原任务没有改变。",
            "unsafe_path": "任务路径不安全，已停止写入。",
        }.get(code, "任务文件没有改变，请稍后重试。")
