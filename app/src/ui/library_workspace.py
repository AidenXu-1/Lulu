"""Consumer library workspace: native virtual rows and background queries."""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt, QThread, Signal, QMimeData, QItemSelectionModel, QRect, QEvent
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QFont, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListView, QMenu, QPushButton, QStackedWidget, QVBoxLayout, QWidget, QFileDialog,
    QStyledItemDelegate, QStyle)

from ..core import library
from . import dialogs
from .library_page import _LibraryActions, _KIND_LABELS, _ENTRY_MIME, _fmt_date


class LibraryRows(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role in (Qt.DisplayRole, Qt.AccessibleTextRole):
            title = library.display_title(str(row.get("title") or "未命名"))
            date = _fmt_date(str(row.get("updated_at") or row.get("created_at") or ""))
            if row.get("kind") == "voice":
                import math
                duration = row.get("duration_seconds")
                seconds = f"{int(duration)//60}:{int(duration)%60:02}" if isinstance(duration, (float, int)) and math.isfinite(duration) and duration >= 0 else ""
                metadata = " · ".join(v for v in (str(row.get("voice_name") or ""), seconds, date,
                    "音频待重新定位" if (row.get("missing_flags") or {}).get("audio_missing") else "") if v)
            else:
                metadata = f'{date}  ·  {row.get("char_count", 0):,} 字'
                author = row.get("author")
                author = author.get("name") if isinstance(author, dict) else author
                author = author or row.get("uploader")
                if author:
                    metadata += f" · {author}"
            return f'{title}\n{row.get("text_preview") or "暂无正文预览"}\n{metadata}'
        if role == Qt.UserRole:
            return row
        if role == Qt.SizeHintRole:
            return QSize(180, 86)
        if role == Qt.ToolTipRole:
            details = [str(row.get("title") or "")]
            stats = row.get("stats")
            if isinstance(stats, dict):
                details.append(" · ".join(f'{name} {stats[key]}' for key, name in
                    (("like", "赞"), ("favorite", "收藏"), ("comment", "评论"), ("share", "分享")) if stats.get(key) is not None))
            return "\n".join(details)
        return None

    def replace(self, rows):
        if self.rows == rows:
            return
        if [r["id"] for r in self.rows] == [r["id"] for r in rows]:
            changed = [n for n, (old, new) in enumerate(zip(self.rows, rows)) if old != new]
            self.rows = rows
            for n in changed:
                self.dataChanged.emit(self.index(n), self.index(n))
        else:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()

    def flags(self, index):
        return super().flags(index) | Qt.ItemIsDragEnabled

    def mimeTypes(self):
        return [_ENTRY_MIME]

    def mimeData(self, indexes):
        mime = QMimeData()
        if indexes:
            mime.setData(_ENTRY_MIME, self.rows[indexes[0].row()]["id"].encode())
        return mime


class FolderFilter(QComboBox):
    entryDropped = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setView(FolderDropList(self))

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_ENTRY_MIME):
            event.acceptProposedAction()
            self.showPopup()


    def dropEvent(self, event):
        if self.currentData() is not None:
            self.entryDropped.emit(bytes(event.mimeData().data(_ENTRY_MIME)).decode(), self.currentData())
            event.acceptProposedAction()


class FolderDropList(QListView):
    def __init__(self, combo):
        super().__init__(combo)
        self.combo = combo
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_ENTRY_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index.data(Qt.UserRole) is not None:
            self.setCurrentIndex(index)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index.data(Qt.UserRole) is not None and event.mimeData().hasFormat(_ENTRY_MIME):
            self.combo.entryDropped.emit(bytes(event.mimeData().data(_ENTRY_MIME)).decode(), index.data(Qt.UserRole))
            self.combo.hidePopup()
            event.acceptProposedAction()


class LibraryRowDelegate(QStyledItemDelegate):
    playRequested = Signal(str)

    def __init__(self, consumer, parent=None):
        super().__init__(parent)
        self.consumer = consumer
        self.playing_id = None

    def paint(self, painter, option, index):
        row = index.data(Qt.UserRole)
        c = self.consumer
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        painter.save()
        painter.fillRect(rect, QColor(c.surface_alt if selected else c.surface))
        if selected:
            painter.fillRect(QRect(rect.left(), rect.top() + 7, 3, rect.height() - 14), QColor(c.accent))
        width = rect.width() - (62 if row.get("kind") == "voice" else 28)
        lines = str(index.data(Qt.DisplayRole)).splitlines()
        for n, line in enumerate(lines[:3]):
            font = QFont(option.font)
            font.setPointSizeF(13 if n == 0 else 11)
            font.setBold(n == 0)
            painter.setFont(font)
            painter.setPen(QColor(c.ink if n == 0 else c.ink_soft if n == 1 else c.ink_muted))
            text = painter.fontMetrics().elidedText(line, Qt.ElideRight, width)
            painter.drawText(QRect(rect.left() + 14, rect.top() + 9 + 23 * n, width, 22), Qt.AlignVCenter, text)
        if row.get("kind") == "voice":
            player = QRect(rect.right() - 42, rect.top() + 25, 28, 28)
            painter.setPen(QColor(c.accent))
            painter.drawText(player, Qt.AlignCenter, "Ⅱ" if row["id"] == self.playing_id else "▶")
        painter.setPen(QColor(c.line))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if option.state & QStyle.State_HasFocus:
            painter.setPen(QColor(c.accent))
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if (event.type() == QEvent.MouseButtonRelease and index.data(Qt.UserRole).get("kind") == "voice"
                and QRect(option.rect.right() - 46, option.rect.top(), 46, option.rect.height()).contains(event.position().toPoint())):
            self.playRequested.emit(index.data(Qt.UserRole)["id"])
            return True
        return super().editorEvent(event, model, option, index)

class LibraryQueryWorker(QThread):
    completed = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, request, options, parent=None):
        super().__init__(parent)
        self.request, self.options = request, options

    def run(self):
        try:
            rows, offset, errors = [], 0, []
            while not self.isInterruptionRequested():
                result = library.query_entries(**self.options, offset=offset, limit=500, refresh=offset == 0)
                rows.extend(result["entries"])
                errors.extend(result["errors"])
                offset = result["next_offset"]
                if offset is None:
                    break
            if self.isInterruptionRequested():
                return
            folders = result["folders"]
            self.completed.emit(self.request, {"entries": rows, "folders": folders, "errors": errors})
        except Exception as exc:
            self.failed.emit(self.request, str(exc))


class LibraryBatchWorker(QThread):
    completed = Signal(object)

    def __init__(self, ids, action, parent=None):
        super().__init__(parent)
        self.ids, self.action = ids, action

    def run(self):
        results = []
        for eid in self.ids:
            if self.isInterruptionRequested():
                results.append((eid, "操作已停止，可继续处理"))
                continue
            try:
                self.action(eid)
                results.append((eid, None))
            except Exception as exc:
                results.append((eid, str(exc)))
        self.completed.emit(results)


class LibraryWorkspace(_LibraryActions):
    def _build(self):
        self._request = 0
        self._query_worker = None
        self._batch_worker = None
        self._pending_refresh = False
        self._view_states = {}
        self._folders = []
        self._applying = False
        self._restore_scroll = None
        c = self._consumer
        self.setStyleSheet(f"""
            QWidget#root[workspacePage="library"] {{ background:{c.canvas}; }}
            QWidget {{ color:{c.ink}; }}
            QListView {{ background:{c.surface}; border:1px solid {c.line}; border-radius:10px;
                         padding:4px; outline:0; }}
            QListView::item {{ padding:8px 12px; border-bottom:1px solid {c.line}; }}
            QListView::item:selected {{ background:{c.surface_alt}; color:{c.ink}; border-left:3px solid {c.accent}; }}
            QLineEdit,QComboBox {{ padding:7px; border:1px solid {c.line}; border-radius:6px; background:{c.surface}; }}
            QPushButton {{ padding:6px 9px; border:1px solid {c.line}; border-radius:6px; background:{c.surface}; }}
            QPushButton:disabled {{ color:{c.ink_soft}; }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 14)
        root.setSpacing(12)
        header = QHBoxLayout()
        self.title_label = QLabel("素材库")
        font = QFont("Songti SC", 23)
        self.title_label.setFont(font)
        self.meta_label = QLabel("")
        header.addWidget(self.title_label)
        header.addWidget(self.meta_label)
        header.addStretch()
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("搜索标题或正文")
        self.search_field.setAccessibleName("搜索当前素材库")
        self.search_field.setMaximumWidth(320)
        self.search_field.textChanged.connect(lambda _: self._search_timer.start())
        header.addWidget(self.search_field, 1)
        root.addLayout(header)
        self.control_stack = QStackedWidget()
        self.control_stack.setFixedHeight(40)
        controls = QWidget()
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        self.folder_filter = FolderFilter()
        self.folder_filter.setAccessibleName("文件夹筛选，可拖入素材并选择目标文件夹")
        self.folder_filter.setMaximumWidth(240)
        self.folder_filter.addItem("全部文件夹", None)
        self.folder_filter.currentIndexChanged.connect(lambda _: self.refresh())
        self.folder_filter.entryDropped.connect(self._drop_move)
        self.sort_filter = QComboBox()
        for title, value in (("最近更新", "updated"), ("创建时间", "created"), ("标题", "title")):
            self.sort_filter.addItem(title, value)
        self.sort_filter.setAccessibleName("素材排序")
        self.sort_filter.currentIndexChanged.connect(lambda _: self.refresh())
        row.addWidget(self.folder_filter, 1)
        row.addWidget(self.sort_filter)
        row.addStretch()
        self.select_btn = QPushButton("选择")
        self.select_btn.clicked.connect(lambda: self._set_selecting(True))
        row.addWidget(self.select_btn)
        self.new_folder_btn = QPushButton("文件夹…")
        self.new_folder_btn.clicked.connect(self._folder_menu)
        row.addWidget(self.new_folder_btn)
        self.control_stack.addWidget(controls)
        self.toolbar = QWidget()
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.sel_count_label = QLabel()
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(self._toggle_select_all)
        bar.addWidget(self.sel_count_label)
        bar.addWidget(self.select_all_btn)
        bar.addStretch()
        self._batch_btns = []
        for title, callback in (("移动", self._batch_move), ("导出", self._batch_export),
                                ("更多", self._batch_menu), ("完成", lambda: self._set_selecting(False))):
            button = QPushButton(title)
            button.clicked.connect(callback)
            bar.addWidget(button)
            if title != "完成":
                self._batch_btns.append((button, "entry"))
        self.control_stack.addWidget(self.toolbar)
        root.addWidget(self.control_stack)
        self.list_view = QListView()
        self.list_view.setAccessibleName("素材列表，方向键预览，Command 或 Shift 多选")
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_view.setTextElideMode(Qt.ElideRight)
        self.list_view.setDragEnabled(True)
        self.model = LibraryRows(self)
        self.list_view.setModel(self.model)
        self.row_delegate = LibraryRowDelegate(c, self.list_view)
        self.row_delegate.playRequested.connect(self._play_entry)
        self.list_view.setItemDelegate(self.row_delegate)
        self.list_view.selectionModel().selectionChanged.connect(self._selection_changed)
        self.list_view.clicked.connect(self._preview_index)
        self.list_view.activated.connect(self._preview_index)
        self.list_view.selectionModel().currentChanged.connect(self._current_changed)
        self.list_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._context_menu)
        root.addWidget(self.list_view, 1)
        footer = QHBoxLayout()
        self.status_label = QLabel("正在读取…")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.play_btn = QPushButton("播放 / 暂停")
        self.play_btn.clicked.connect(self._play_current)
        self.play_btn.hide()
        footer.addWidget(self.play_btn)
        more = QPushButton("所选操作…")
        more.clicked.connect(lambda: self._context_menu(None))
        footer.addWidget(more)
        storage = QPushButton("本地资料")
        storage.setToolTip(str(library.LIBRARY_DIR))
        storage.clicked.connect(self._open_storage)
        footer.addWidget(storage)
        deleted = QPushButton("最近删除")
        deleted.clicked.connect(self._restore_deleted)
        footer.addWidget(deleted)
        root.addLayout(footer)
        self._shortcuts = []
        for key, callback in (("Ctrl+F", self.search_field.setFocus),
                              ("Ctrl+R", self.refresh),
                              ("Escape", lambda: self._set_selecting(False))):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def _set_kind(self, kind):
        if kind != self._kind:
            self._view_states[self._kind] = (self.search_field.text(), self.folder_filter.currentData(),
                self.sort_filter.currentIndex(), set(self._sel_entries), self.list_view.verticalScrollBar().value(), self._active_entry_id, self._selecting)
            self._kind = kind if kind in _KIND_LABELS else "all"
            query, folder, sort, selected, scroll, active, selecting = self._view_states.get(self._kind, ("", None, 0, set(), 0, None, False))
            self.search_field.blockSignals(True)
            self.search_field.setText(query)
            self.search_field.blockSignals(False)
            self._query = query.strip().casefold()
            self._desired_folder = folder
            self.sort_filter.blockSignals(True)
            self.sort_filter.setCurrentIndex(sort)
            self.sort_filter.blockSignals(False)
            self._sel_entries = selected
            self._active_entry_id = active
            self._selecting = selecting
            self._restore_scroll = scroll
        self.title_label.setText(_KIND_LABELS[self._kind])
        self.new_folder_btn.setVisible(self._kind != "all")
        self.play_btn.setVisible(self._kind == "voice")
        self.refresh()

    def refresh(self, *, search_only=False):
        self._request += 1
        if self._query_worker is not None and self._query_worker.isRunning():
            self._pending_refresh = True
            self._query_worker.requestInterruption()
            return
        folder = getattr(self, "_desired_folder", self.folder_filter.currentData())
        options = dict(kind=self._kind, search=self._query, folder_id=folder,
                       sort=self.sort_filter.currentData())
        self.status_label.setText("正在读取…")
        worker = LibraryQueryWorker(self._request, options, self)
        self._query_worker = worker
        worker.completed.connect(self._query_completed)
        worker.failed.connect(self._query_failed)
        worker.finished.connect(self._query_finished)
        worker.start()

    def _query_finished(self):
        worker = self._query_worker
        self._query_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._pending_refresh:
            self._pending_refresh = False
            self.refresh()

    def _query_failed(self, request, message):
        if request == self._request:
            self.status_label.setText(f"读取未完成：{message}。已显示内容保留。")

    def _query_completed(self, request, result):
        if request != self._request:
            return
        self._applying = True
        rows = result["entries"]
        scroll = self._restore_scroll if self._restore_scroll is not None else self.list_view.verticalScrollBar().value()
        self._restore_scroll = None
        selected = set(self._sel_entries)
        current = self._active_entry_id
        self.model.replace(rows)
        self._ordered_ids = [r["id"] for r in rows]
        self._entries_by_id = {r["id"]: r for r in rows}
        selection = self.list_view.selectionModel()
        selection.clearSelection()
        for n, row in enumerate(rows):
            if row["id"] in selected:
                selection.select(self.model.index(n), QItemSelectionModel.Select)
            if row["id"] == current:
                selection.setCurrentIndex(self.model.index(n), QItemSelectionModel.NoUpdate)
        self.list_view.doItemsLayout()
        self.list_view.verticalScrollBar().setValue(scroll)
        self._sel_entries = selected.intersection(self._ordered_ids)
        folder = getattr(self, "_desired_folder", self.folder_filter.currentData())
        if hasattr(self, "_desired_folder"):
            del self._desired_folder
        self._folders = result["folders"]
        self.folder_filter.blockSignals(True)
        self.folder_filter.clear()
        self.folder_filter.addItem("全部文件夹", None)
        self.folder_filter.addItem("未分类", "")
        for f in self._folders:
            self.folder_filter.addItem(f'{f["name"]}  ({f["count"]})', f["id"])
        self.folder_filter.setCurrentIndex(max(0, self.folder_filter.findData(folder)))
        self.folder_filter.blockSignals(False)
        self._applying = False
        self.meta_label.setText(f"{len(rows):,} 项")
        self.status_label.setText(f"{len(rows):,} 项" if rows else "没有匹配的素材" if self._query else "还没有素材，生成或导入后会显示在这里")
        if result["errors"]:
            self.status_label.setText(f'{len(rows):,} 项 · {len(result["errors"])} 项读取异常，原文件已保留')
        elif getattr(self, "_last_operation_message", ""):
            self.status_label.setText(self._last_operation_message)
        self._apply_selection_visuals()

    def _set_selecting(self, on):
        self._selecting = on
        if not on:
            self._sel_entries.clear()
            self._sel_folders.clear()
            self.list_view.clearSelection()
        self._apply_selection_visuals()

    def _selection_changed(self, *_):
        if self._applying:
            return
        self._sel_entries = {i.data(Qt.UserRole)["id"] for i in self.list_view.selectionModel().selectedIndexes()}
        if len(self._sel_entries) > 1:
            self._selecting = True
        self._apply_selection_visuals()

    def _apply_selection_visuals(self):
        self.control_stack.setCurrentIndex(1 if self._selecting else 0)
        self.sel_count_label.setText(f"已选 {len(self._sel_entries)} 项")
        self.select_all_btn.setText(f"全选 {len(self._ordered_ids)} 项")
        for button, _ in self._batch_btns:
            button.setEnabled(bool(self._sel_entries))

    def _toggle_select_all(self):
        self._selecting = True
        if len(self._sel_entries) == len(self._ordered_ids):
            self.list_view.clearSelection()
        else:
            self.list_view.selectAll()
        self._apply_selection_visuals()

    def _current_changed(self, current, _previous):
        if current.isValid():
            self._active_entry_id = current.data(Qt.UserRole)["id"]
            if not self._applying and self.list_view.hasFocus() and not self._selecting:
                self._preview_index(current)

    def _preview_index(self, index):
        if (index.isValid() and not self._selecting and self._on_view is not None
                and not QGuiApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.MetaModifier | Qt.ShiftModifier)):
            panel = getattr(self.window(), "panel", None)
            if panel is not None and panel._entry_id == index.data(Qt.UserRole)["id"] and getattr(self.window(), "_right_open", False):
                return
            self._view(index.data(Qt.UserRole)["id"])

    def _folder_menu(self):
        menu = QMenu(self)
        menu.addAction("新建文件夹", self._new_folder)
        folder = self.folder_filter.currentData()
        if folder:
            menu.addAction("重命名当前文件夹", lambda: self._rename_folder(folder))
            menu.addAction("删除文件夹，保留素材", lambda: self._delete_folder(folder))
            menu.addAction("移到文件夹列表最前", lambda: self._reorder_folder(folder))
            menu.addAction("调整文件夹位置…", lambda: self._choose_folder_position(folder))
        menu.exec(self.new_folder_btn.mapToGlobal(self.new_folder_btn.rect().bottomLeft()))

    def _reorder_folder(self, folder):
        try:
            library.set_folder_order([folder] + [f["id"] for f in self._folders if f["id"] != folder])
        except (OSError, ValueError, RuntimeError) as exc:
            self.status_label.setText(f"排序未保存：{exc}")
        self.refresh()

    def _choose_folder_position(self, folder):
        others = [f for f in self._folders if f["id"] != folder]
        choice = dialogs.choose(self, "调整文件夹位置", "选择新的位置",
            [f'放在「{f["name"]}」之前' for f in others] + ["放在最后"], current=0)
        if choice is None:
            return
        ids = [f["id"] for f in others]
        ids.insert(choice, folder)
        try:
            library.set_folder_order(ids)
            self.refresh()
        except (OSError, ValueError, RuntimeError) as exc:
            self.status_label.setText(f"排序未保存：{exc}")

    def _batch_menu(self):
        menu = QMenu(self)
        menu.addAction("存入 Obsidian", self._batch_obsidian)
        menu.addAction("存入飞书", self._batch_feishu)
        menu.addSeparator()
        menu.addAction("删除所选素材", self._batch_delete)
        menu.exec(self.toolbar.mapToGlobal(self.toolbar.rect().bottomRight()))

    def _context_menu(self, position):
        index = self.list_view.indexAt(position) if position is not None else self.list_view.currentIndex()
        if not index.isValid():
            return
        row = index.data(Qt.UserRole)
        eid = row["id"]
        menu = QMenu(self)
        menu.addAction("查看", lambda: self._view(eid))
        menu.addAction("重命名", lambda: self._prompt_rename(eid))
        menu.addAction("导出", lambda: self._export(eid))
        menu.addAction("移动到…", lambda: self._move_entry(eid, row["kind"]))
        menu.addAction("在访达中显示", lambda: self._reveal(eid))
        if row["kind"] == "voice":
            menu.addAction("重新定位音频文件…", lambda: self._relink(eid))
        else:
            menu.addAction("历史版本…", lambda: self._versions(eid))
        if row.get("source_url"):
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            menu.addAction("打开原链接", lambda: QDesktopServices.openUrl(QUrl(row["source_url"])))
        menu.addSeparator()
        menu.addAction("删除", lambda: self._delete(eid))
        menu.exec(self.list_view.viewport().mapToGlobal(position) if position is not None
                  else self.list_view.mapToGlobal(self.list_view.rect().center()))

    def _prompt_rename(self, eid):
        title = dialogs.prompt(self, "重命名", "标题", text=self._entry_title(eid), ok="保存")
        if title:
            self._rename_inline(eid, title)

    def _play_current(self):
        index = self.list_view.currentIndex()
        if index.isValid():
            self._play_entry(index.data(Qt.UserRole)["id"])

    def _play_entry(self, eid):
        from PySide6.QtMultimedia import QMediaPlayer
        if self._entries_by_id.get(eid, {}).get("kind") != "voice":
            return
        host = self.window()
        panel = getattr(host, "panel", None)
        if self._on_view is not None and (panel is None or getattr(panel, "_entry_id", None) != eid):
            self._view(eid)
        panel = getattr(host, "panel", None)
        if panel is not None and getattr(panel, "_entry_id", None) == eid:
            if not getattr(self, "_panel_playback_connected", False):
                panel._voice_player.playbackStateChanged.connect(lambda state: self._playback_changed(
                    panel._entry_id if state == QMediaPlayer.PlayingState else None))
                self._panel_playback_connected = True
            panel._toggle_voice_play()
        else:
            item = library.load_voice(eid)
            if not item:
                return
            if self._player.source().toLocalFile() == item.get("audio_path") and self._player.playbackState() == QMediaPlayer.PlayingState:
                self._player.pause()
                self._playback_changed(None)
            else:
                self._play_voice(item)
                self._playback_changed(eid)

    def _playback_changed(self, eid):
        self.row_delegate.playing_id = eid
        self.list_view.viewport().update()
        self.play_btn.setText("暂停" if eid else "播放")

    def _relink(self, eid):
        path, _ = QFileDialog.getOpenFileName(self, "重新定位音频", "", "音频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.aiff)")
        if path:
            try:
                library.relink_voice(eid, path)
                self.refresh()
                if self._active_entry_id == eid:
                    self._view(eid)
            except (OSError, ValueError) as exc:
                self.status_label.setText(str(exc))

    def _versions(self, eid):
        try:
            versions = library.list_versions(eid)
            if not versions:
                self.status_label.setText("这篇文稿还没有历史修改版本")
                return
            current = library.document_snapshot(eid)
            choice = dialogs.choose(self, "恢复历史版本", "恢复前也会保留当前正文，可再次恢复。",
                                    [f'{v["saved_at"]} · 版本 {v["name"].split("-")[0]}' for v in versions], current=0)
            if choice is not None:
                library.restore_version(eid, versions[choice]["name"], current["content_hash"])
                self.refresh()
                self._view(eid)
        except (OSError, ValueError, RuntimeError) as exc:
            self.status_label.setText(f"恢复未完成：{exc}")

    def _restore_deleted(self):
        records = library.list_deleted()
        if not records:
            self.status_label.setText("最近删除为空")
            return
        choice = dialogs.choose(self, "最近删除", "选择要恢复的素材（音频原文件始终保留）",
                                [f'{r["title"]} · {_fmt_date(r["deleted_at"])}' for r in records], current=0)
        if choice is not None:
            try:
                library.restore_deleted(records[choice]["token"])
                self.refresh()
            except (OSError, RuntimeError, ValueError) as exc:
                self.status_label.setText(f"恢复未完成：{exc}")

    def _start_batch(self, action, ids=None):
        if self._batch_worker is not None and self._batch_worker.isRunning():
            self.status_label.setText("当前批量操作尚未结束")
            return
        ids = self._selected_entries_ordered() if ids is None else ids
        if not ids:
            return
        worker = LibraryBatchWorker(ids, action, self)
        self._batch_worker = worker
        worker.completed.connect(self._batch_completed)
        worker.finished.connect(lambda: self._batch_finished(worker))
        self.status_label.setText(f"正在处理 {len(ids)} 项…")
        worker.start()

    def _batch_finished(self, worker):
        if self._batch_worker is worker:
            self._batch_worker = None
        worker.deleteLater()

    def _batch_completed(self, results):
        failed = {eid for eid, error in results if error}
        self._sel_entries = failed
        self._selecting = bool(failed)
        self._last_operation_message = f"已完成 {len(results) - len(failed)} 项"
        if failed:
            self._last_operation_message += f" · {len(failed)} 项未完成，已保留选择，可重试"
            self.status_label.setToolTip("\n".join(f"{self._entry_title(eid)}：{err}" for eid, err in results if err))
        self.refresh()

    def _batch_delete(self):
        if self._sel_entries and dialogs.confirm(self, "移入最近删除",
                f"将 {len(self._sel_entries)} 项移入最近删除？可以恢复，原音视频保留。"):
            self._start_batch(library.delete)

    def _batch_move(self):
        folders = list(self._folders)
        choice = dialogs.choose(self, "移动到文件夹", "选择目标文件夹",
                                ["未分类"] + [f["name"] for f in folders], current=0)
        if choice is not None:
            target = None if choice == 0 else folders[choice - 1]["id"]
            self._start_batch(lambda eid: library.set_entry_folder(eid, target))

    def _batch_export(self):
        from pathlib import Path
        import shutil
        from ..core.output.writer import new_export_file, render
        folder = QFileDialog.getExistingDirectory(self, "导出到文件夹")
        if not folder:
            return
        titles = {eid: self._entry_title(eid) for eid in self._sel_entries}

        def export(eid):
            voice = library.load_voice(eid)
            if voice:
                source = Path(voice["audio_path"])
                with source.open("rb") as source_file, new_export_file(Path(folder), source.name) as target:
                    shutil.copyfileobj(source_file, target)
            else:
                document = library.load(eid)
                if document is None:
                    raise FileNotFoundError("素材无法读取")
                title = "".join(c for c in titles[eid][:80] if c not in '/\\:*?"<>|') or "文稿"
                with new_export_file(Path(folder), title + ".md") as target:
                    target.write(document.text.encode("utf-8"))
        self._start_batch(export)

    def _batch_obsidian(self):
        from pathlib import Path
        from ..core import obsidian
        ids = self._text_entry_ids()
        if not ids:
            self.status_label.setText("请选择文稿或输出稿")
            return
        folder = getattr(self.cfg, "obsidian_dir", "")
        if not folder or not Path(folder).is_dir():
            folder = QFileDialog.getExistingDirectory(self, "选择 Obsidian 仓库里的目标文件夹")
            if not folder:
                return
            if self.cfg:
                self.cfg.obsidian_dir = folder
                self.cfg.save()
        titles = {eid: self._entry_title(eid) for eid in ids}
        def export(eid):
            document = library.load(eid)
            if document is None:
                raise FileNotFoundError("文稿无法读取，未导出")
            obsidian.save_note(document, folder, titles[eid], summary=library.get_summary(eid))
        self._start_batch(export, ids)

    def _batch_feishu(self):
        from datetime import datetime, timezone
        from ..core.feishu_storage import record_from_transcript
        from .feishu_dialog import send_records_to_feishu
        if self._batch_worker is not None or (getattr(self, "_feishu_worker", None) is not None
                and self._feishu_worker.isRunning()):
            self.status_label.setText("上一项批量操作尚未结束")
            return
        ids = self._text_entry_ids()
        if not ids:
            self.status_label.setText("请选择文稿或输出稿")
            return
        titles = {eid: self._entry_title(eid) for eid in ids}
        records = []
        captured = int(datetime.now(timezone.utc).timestamp() * 1000)
        def prepare(eid):
            document = library.load(eid)
            if document is None:
                raise FileNotFoundError("文稿无法读取，未发送")
            records.append(record_from_transcript(document, title=titles[eid],
                summary=library.get_summary(eid), captured_at_ms=captured))
        worker = LibraryBatchWorker(ids, prepare, self)
        self._batch_worker = worker
        def prepared(results):
            unreadable = {eid for eid, error in results if error}
            self.status_label.setToolTip("\n".join(f"{titles[eid]}：{error}" for eid, error in results if error))
            if not records:
                self.status_label.setText("所选文稿未能读取，原资料和选择已保留")
                return
            if unreadable:
                self.status_label.setText(f"{len(unreadable)} 项读取未完成，只准备发送其余 {len(records)} 项")
            self._feishu_worker = send_records_to_feishu(self, self.cfg, records,
                title=f"已选 {len(records)} 篇作品", context="selection", toast=self.status_label.setText)
            active = self._feishu_worker
            if active is not None:
                def sent(ok, receipt):
                    # Partial/unknown external outcomes retain the selection for
                    # receipt review; do not blindly resend uncertain rows.
                    if ok and getattr(receipt, "complete", False):
                        self._sel_entries.difference_update(set(ids) - unreadable)
                        self._selecting = bool(self._sel_entries)
                        self.refresh()
                active.done.connect(sent)
        worker.completed.connect(prepared)
        worker.finished.connect(lambda: self._batch_finished(worker))
        self.status_label.setText(f"正在准备 {len(ids)} 篇文稿…")
        worker.start()
