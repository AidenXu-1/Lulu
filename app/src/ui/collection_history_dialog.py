"""主页采集任务管理浮层。"""

from __future__ import annotations

from collections.abc import Callable
from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    rename_requested = Signal(str)

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
        copy.setContentsMargins(0, 9, 0, 9)
        copy.setSpacing(4)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(6)
        self.name_label = _ElidedLabel(record.name, self)
        self.name_label.setObjectName("historyRecordName")
        self.name_label.setProperty(
            "searchMatch",
            bool(needle and needle in record.name.casefold()),
        )
        self.name_label.installEventFilter(self)
        heading.addWidget(self.name_label, 1)

        markers = _record_markers(record).split(" · ") if _record_markers(record) else []
        self.marker_labels: list[QLabel] = []
        for marker in markers:
            label = QLabel(marker, self)
            label.setObjectName("historyRecordMarker")
            label.setProperty(
                "markerKind",
                (
                    "warning"
                    if marker == "部分结果" or marker.startswith("封面失败")
                    or marker == "封面待恢复"
                    else "neutral"
                ),
            )
            label.setFixedHeight(20)
            self.marker_labels.append(label)
            heading.addWidget(label)
        copy.addLayout(heading)

        author = record.author_name or "未知作者"
        self.author_label = _ElidedLabel(author, self)
        self.author_label.setObjectName("historyRecordAuthor")
        self.author_label.setProperty(
            "searchMatch",
            bool(needle and needle in author.casefold()),
        )
        copy.addWidget(self.author_label)
        row.addLayout(copy, 1)

        self.edit_button: QPushButton | None = None
        if editable:
            self.edit_button = QPushButton("编辑", self)
            adopt_button(
                self.edit_button,
                role="tertiary",
                size="sm",
                accessible_name=f"编辑任务名称：{record.name}",
                tooltip="编辑这项任务的名称",
            )
            self.edit_button.setProperty("historyRowEdit", True)
            self.edit_button.setFixedWidth(46)
            self.edit_button.clicked.connect(
                lambda: self.rename_requested.emit(self.record_id)
            )
            row.addWidget(self.edit_button)

        meta = QVBoxLayout()
        meta.setContentsMargins(0, 9, 0, 9)
        meta.setSpacing(4)
        self.count_label = QLabel(f"{_item_count(record)} 条", self)
        self.count_label.setObjectName("historyRecordCount")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.time_label = QLabel(
            record.created_at[:16].replace("T", " "),
            self,
        )
        self.time_label.setObjectName("historyRecordTime")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        meta.addWidget(self.count_label)
        meta.addWidget(self.time_label)
        row.addLayout(meta)

    def set_checked(self, checked: bool) -> None:
        selected = bool(checked)
        self.setProperty("checked", selected)
        self.check.setProperty("checked", selected)
        self.check.setAccessibleDescription("已选择" if selected else "未选择")
        _repolish(self)
        _repolish(self.check)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if (
            watched is self.name_label
            and event.type() == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.rename_requested.emit(self.record_id)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_requested.emit(self.record_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CollectionHistoryDialog(QFrame):
    """主窗口内容区内嵌的 680×500 任务管理卡片。"""

    finished = Signal(int)
    accepted = Signal()
    rejected = Signal()

    def __init__(
        self,
        repository: CollectionHistoryRepository,
        snapshot: HistorySnapshot,
        *,
        on_changed: Callable[[HistorySnapshot], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        if parent is None:
            raise ValueError("采集任务管理必须嵌入现有 Lulu 窗口。")
        super().__init__(parent)
        self.repository = repository
        self.snapshot = snapshot
        self.on_changed = on_changed
        self._show_trash = False
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
        initial_width = 680
        initial_height = 500
        if parent is not None and parent.width() > 0 and parent.height() > 0:
            initial_width = max(320, min(680, parent.width() - 48))
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
        self.title_label = QLabel("采集任务管理")
        self.title_label.setObjectName("historyDialogTitle")
        self.title_label.setAccessibleName("采集任务管理")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)
        self.close_button = QPushButton("×")
        adopt_button(
            self.close_button,
            role="icon",
            size="md",
            accessible_name="关闭采集任务管理",
        )
        self.close_button.setFixedWidth(32)
        self.close_button.clicked.connect(self.reject)
        title_row.addWidget(self.close_button)
        layout.addLayout(title_row)

        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(10)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索任务名称 / 作者")
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

        tabs_frame = QFrame()
        tabs_frame.setObjectName("historyTabs")
        tabs = QHBoxLayout(tabs_frame)
        tabs.setContentsMargins(0, 0, 0, 1)
        tabs.setSpacing(4)
        self.active_button = QPushButton("当前任务")
        self.trash_button = QPushButton("任务回收站")
        for button in (self.active_button, self.trash_button):
            button.setCheckable(True)
            adopt_button(button, role="tertiary", size="sm", accessible_name=button.text())
            button.setProperty("historyTab", True)
            tabs.addWidget(button)
        self.active_button.setChecked(True)
        self.active_button.clicked.connect(lambda: self._set_bucket(False))
        self.trash_button.clicked.connect(lambda: self._set_bucket(True))
        tabs.addStretch(1)
        self.readonly_label = QLabel("")
        self.readonly_label.setObjectName("pageMeta")
        tabs.addWidget(self.readonly_label)
        layout.addWidget(tabs_frame)

        self.list = _HistoryListWidget()
        self.list.setObjectName("historyList")
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setSpacing(0)
        self.list.setAccessibleName("采集任务列表")
        self.list.setProperty("historyBucket", "active")
        self.list.toggle_requested.connect(self._toggle_item)
        self.list.set_all_requested.connect(self._set_all_items_checked)
        self.list.itemClicked.connect(self._toggle_item)
        self.list.itemChanged.connect(self._sync_item_row)
        layout.addWidget(self.list, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("pageMeta")
        self.legacy_item_trash_label = QLabel("")
        self.legacy_item_trash_label.setObjectName("pageMeta")
        layout.addWidget(self.legacy_item_trash_label)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.summary)
        actions.addStretch(1)
        self.move_button = QPushButton("移入回收站")
        self.purge_button = QPushButton("永久删除")
        for button in (self.move_button, self.purge_button):
            adopt_button(button, role="secondary", size="sm", accessible_name=button.text())
            actions.addWidget(button)
        self.move_button.clicked.connect(self.move_selected)
        self.purge_button.clicked.connect(self.purge_selected)
        self.move_button.setProperty("historyAction", "danger")
        self.purge_button.setProperty("historyAction", "danger")
        self.purge_button.hide()
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
            raise RuntimeError("采集任务管理缺少嵌入宿主。")
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
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        if not self.backdrop.has_scene():
            self._capture_backdrop()
        self.backdrop.setGeometry(self.rect())
        self.backdrop.lower()
        self._fit_card()
        super().showEvent(event)

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
        if not self._opened:
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
        width = max(320, min(680, self.width() - 48))
        height = max(300, min(500, self.height() - 48))
        self.card.setFixedSize(width, height)

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

    def _set_bucket(self, trash: bool) -> None:
        self._show_trash = bool(trash)
        self.title_label.setText("采集任务管理")
        self.active_button.setChecked(not trash)
        self.trash_button.setChecked(trash)
        self.move_button.setText("恢复任务" if trash else "移入任务回收站")
        self.move_button.setAccessibleName(self.move_button.text())
        self.move_button.setProperty("historyAction", "secondary" if trash else "danger")
        _repolish(self.move_button)
        self.purge_button.setVisible(trash)
        self.list.setProperty("historyBucket", "trash" if trash else "active")
        _repolish(self.list)
        self.refresh()

    def refresh(self) -> None:
        source = self.snapshot.trashed_records if self._show_trash else self.snapshot.active_records
        needle = self.search_edit.text().strip().casefold()
        filtered = tuple(
            row
            for row in source
            if not needle or needle in row.name.casefold() or needle in row.author_name.casefold()
        )
        self._visible_records = _sorted_records(filtered, self.sort_select.currentData())
        self.list.blockSignals(True)
        self.list.clear()
        for record in self._visible_records:
            item = _HistoryListItem()
            item.setData(Qt.UserRole, record.record_id)
            item.setFlags(
                item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(Qt.Unchecked)
            item.setSizeHint(QSize(0, 66))
            item.setToolTip(record.name)
            self.list.addItem(item)
            row = _HistoryRecordRow(
                record,
                needle=needle,
                editable=not self._show_trash and self.snapshot.writable,
                parent=self.list,
            )
            row.toggle_requested.connect(self._toggle_record)
            row.rename_requested.connect(self._rename_record)
            self.list.setItemWidget(item, row)
        self.list.blockSignals(False)
        total = len(source)
        self.summary.setText(f"显示 {len(self._visible_records)} / {total} 项任务")
        legacy_count = sum(
            len(record.item_trash)
            for record in self.snapshot.active_records + self.snapshot.trashed_records
        )
        self.legacy_item_trash_label.setText(
            f"检测到旧版回收作品 {legacy_count} 条，数据已只读保留。"
            if legacy_count
            else ""
        )
        self.legacy_item_trash_label.setVisible(bool(legacy_count))
        readonly = not self.snapshot.writable
        self.readonly_label.setText("只读：文件版本过高或数据异常" if readonly else "")
        for button in (self.move_button, self.purge_button):
            button.setEnabled(not readonly)
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
        row = self.list.itemWidget(item)
        if isinstance(row, _HistoryRecordRow):
            row.set_checked(item.checkState() == Qt.Checked)

    def selected_ids(self) -> list[str]:
        return [
            str(self.list.item(index).data(Qt.UserRole))
            for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.Checked
        ]

    def _apply(self, operation: Callable[[], HistorySnapshot]) -> bool:
        try:
            self.snapshot = operation()
        except HistoryError as exc:
            dialogs.alert(self, "任务未修改", self._error_text(exc.code))
            return False
        if self.on_changed is not None:
            self.on_changed(self.snapshot)
        self.refresh()
        return True

    def rename_selected(self) -> None:
        selected = self.selected_ids()
        if len(selected) != 1:
            dialogs.alert(self, "请选择一项任务", "改名时请只勾选一项任务。")
            return
        self._rename_record(selected[0])

    def _rename_record(self, record_id: str) -> None:
        if self._show_trash or not self.snapshot.writable:
            return
        record = next(
            (row for row in self.snapshot.active_records if row.record_id == record_id),
            None,
        )
        if record is None:
            return
        name = dialogs.prompt(self, "修改任务名称", text=record.name, ok="保存")
        if name is None:
            return
        expected = self.snapshot.source_hash
        self._apply(lambda: self.repository.rename(record.record_id, name, expected_source_hash=expected))

    def move_selected(self) -> None:
        selected = self.selected_ids()
        if not selected:
            dialogs.alert(self, "请选择任务", "请先勾选要处理的完整任务。")
            return
        expected = self.snapshot.source_hash
        if self._show_trash:
            self._apply(lambda: self.repository.restore_records(selected, expected_source_hash=expected))
            return
        if not dialogs.confirm(
            self,
            "移入任务回收站",
            f"将 {len(selected)} 项完整采集任务移入任务回收站？任务内作品会一起移动。",
            ok="移入任务回收站",
        ):
            return
        self._apply(lambda: self.repository.trash_records(selected, expected_source_hash=expected))

    def purge_selected(self) -> None:
        selected = self.selected_ids()
        if not selected:
            dialogs.alert(self, "请选择任务", "请先勾选要永久删除的完整任务。")
            return
        if not dialogs.confirm(
            self,
            "永久删除任务",
            f"永久删除 {len(selected)} 项完整任务？此操作无法撤销。",
            ok="永久删除",
        ):
            return
        expected = self.snapshot.source_hash
        self._apply(lambda: self.repository.purge_records(selected, expected_source_hash=expected))

    @staticmethod
    def _error_text(code: str) -> str:
        return {
            "concurrent_change": "任务文件已被其他窗口更新，请关闭并重新打开后再试。",
            "disk_full": "磁盘空间不足，原任务没有改变。",
            "permission_denied": "当前目录不可写，原任务没有改变。",
            "unsafe_path": "任务路径不安全，已停止写入。",
        }.get(code, "任务文件没有改变，请稍后重试。")
