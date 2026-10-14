"""选择文稿对话框：点「从文稿库选择…」弹出。

用普通 QDialog（不是 Qt.Popup）——输入法正常、空间大、文稿显示得全。
搜索 + 筛选 + 精致行（徽章/粗标题/日期字数/类型标签/选中态/行间分隔）+ 确定。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import library
from . import icons, theme as T
from .widgets import ElideLabel


class DocRow(QFrame):
    def __init__(self, entry: dict, selected: bool, on_click, on_dblclick):
        super().__init__()
        self._entry = entry
        self._on_click = on_click
        self._on_dblclick = on_dblclick
        rewrite = entry.get("kind") == "rewrite"
        self.setObjectName("pickRowSel" if selected else "pickRow")
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 9, 10, 9)
        h.setSpacing(10)

        badge = QFrame()
        badge.setObjectName("pickBadgeRw" if rewrite else "pickBadgeTr")
        badge.setFixedSize(34, 34)
        bl = QHBoxLayout(badge)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setAlignment(Qt.AlignCenter)
        bl.addWidget(icons.icon_label("layers" if rewrite else "file",
                                      T.TERRA if rewrite else T.BLUE, 16))
        h.addWidget(badge, 0, Qt.AlignVCenter)

        body = QVBoxLayout()
        body.setSpacing(2)
        trow = QHBoxLayout()
        trow.setSpacing(8)
        full = library.display_title((entry.get("title") or "未命名").replace("\n", " ").strip())
        name = ElideLabel(full)
        name.setObjectName("pickTitle")
        name.setToolTip(full)
        trow.addWidget(name, 1)
        tag = QLabel("输出" if rewrite else "文稿")
        tag.setObjectName("pickTagRw" if rewrite else "pickTagTr")
        trow.addWidget(tag, 0, Qt.AlignVCenter)
        if selected:
            trow.addWidget(icons.icon_label("check", T.TERRA, 14), 0, Qt.AlignVCenter)
        body.addLayout(trow)
        date = (entry.get("created_at") or "").replace("T", " ")[:16]
        cnt = entry.get("char_count")
        meta = ElideLabel(f"{date}  ·  {cnt} 字" if isinstance(cnt, int) else date)
        meta.setObjectName("pickMeta")
        body.addWidget(meta)
        h.addLayout(body, 1)

    def mousePressEvent(self, e):  # noqa: N802
        self._on_click(self._entry)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        self._on_dblclick(self._entry)


class DocPickerDialog(QDialog):
    def __init__(self, entries: list[dict], selected_id=None, parent=None, on_go_workbench=None):
        super().__init__(parent)
        self.payload = None
        self._entries = entries
        self._selected_id = selected_id
        self._query = ""
        self._kind = "all"
        self._go = on_go_workbench
        self._request = 0
        self._worker = None
        self._closing = False
        self._pending_query = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(160)
        self._search_timer.timeout.connect(self._query_entries)
        self.setWindowTitle("选择文稿")
        self.resize(580, 600)
        self.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        head = QLabel("选择文稿")
        head.setObjectName("pageTitle")
        lay.addWidget(head)

        # 搜索
        swrap = QFrame()
        swrap.setObjectName("pickSearch")
        sl = QHBoxLayout(swrap)
        sl.setContentsMargins(10, 0, 10, 0)
        sl.setSpacing(7)
        sl.addWidget(icons.icon_label("search", T.INK_FAINT, 14))
        self.search = QLineEdit()
        self.search.setObjectName("pickSearchInput")
        self.search.setPlaceholderText("搜索标题或全文…")
        self.search.setClearButtonEnabled(True)   # 输入时出现「×」一键清空
        self.search.textChanged.connect(self._on_search)
        sl.addWidget(self.search, 1)
        lay.addWidget(swrap)

        # 筛选
        seg = QFrame()
        seg.setObjectName("segBox")
        gl = QHBoxLayout(seg)
        gl.setContentsMargins(3, 3, 3, 3)
        gl.setSpacing(2)
        group = QButtonGroup(self)
        for key, label in (("all", "全部"), ("transcript", "文稿库"), ("rewrite", "输出库")):
            b = QPushButton(label)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(key == "all")
            b.clicked.connect(lambda _=False, k=key: self._set_kind(k))
            group.addButton(b)
            gl.addWidget(b)
        frow = QHBoxLayout()
        frow.addWidget(seg, 0, Qt.AlignLeft)
        frow.addStretch(1)
        lay.addLayout(frow)

        from .library_workspace import LibraryRows
        self.scroll = QListView()
        self.scroll.setUniformItemSizes(True)
        self.model = LibraryRows(self)
        self.scroll.setModel(self.model)
        self.scroll.clicked.connect(lambda index: self._select(index.data(Qt.UserRole)))
        self.scroll.doubleClicked.connect(lambda index: self._confirm_with(index.data(Qt.UserRole)))
        lay.addWidget(self.scroll, 1)
        self.status = QLabel("正在读取文稿…")
        self.status.setObjectName("pickMeta")
        lay.addWidget(self.status)

        # 底部按钮
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("act")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("  确定")
        self.ok.setObjectName("grabBtn")
        self.ok.setCursor(Qt.PointingHandCursor)
        self.ok.setIcon(QIcon(icons.pixmap("check", "#FFFFFF", 13)))
        self.ok.clicked.connect(self._confirm)
        self.ok.setEnabled(selected_id is not None)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)

        self._rebuild()
        self._query_entries()
        self.search.setFocus()

    # ------------------------------------------------------------------ #
    def _on_search(self, text: str) -> None:
        self._query = text.strip().lower()
        self._request += 1
        self._search_timer.start()

    def _set_kind(self, kind: str) -> None:
        self._kind = kind
        self._request += 1
        self._query_entries()

    def _query_entries(self):
        if self._closing:
            return
        if self._worker is not None and self._worker.isRunning():
            self._pending_query = True
            return
        from .library_workspace import LibraryQueryWorker
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = LibraryQueryWorker(self._request,
            dict(kind=self._kind, search=self._query, folder_id=None, sort="updated"), self)
        self._worker.completed.connect(self._loaded)
        self._worker.failed.connect(lambda request, message:
            self.status.setText(message) if request == self._request else None)
        self._worker.finished.connect(self._query_finished)
        self._worker.start()

    def _query_finished(self):
        if self._pending_query and not self._closing:
            self._pending_query = False
            self._query_entries()

    def _filtered(self) -> list[dict]:
        out = [e for e in self._entries if e.get("kind", "transcript") in {"transcript", "rewrite"}]
        if self._kind != "all":
            out = [e for e in out if e.get("kind", "transcript") == self._kind]
        return out

    def _loaded(self, _request, result):
        if _request != self._request or self._closing:
            return
        self._entries = result["entries"]
        self._rebuild()
        count = len(self._filtered())
        self.status.setText(f"{count} 项" if count else "没有找到文稿，可更换关键词或先添加文稿。")
        if result.get("errors"):
            self.status.setText(self.status.text() + " · 部分资料未能读取，原文件已保留")

    def _rebuild(self):
        from PySide6.QtCore import QItemSelectionModel
        rows = self._filtered()
        self.model.replace(rows)
        for n, row in enumerate(rows):
            if row["id"] == self._selected_id:
                self.scroll.selectionModel().setCurrentIndex(self.model.index(n), QItemSelectionModel.ClearAndSelect)
                break

    def done(self, result):
        self._closing = True
        self._search_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.finished.connect(lambda: super(DocPickerDialog, self).done(result))
            return
        super().done(result)

    def _empty(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(20, 40, 20, 40)
        v.setSpacing(8)
        v.addWidget(icons.icon_label("file", T.INK_FAINT, 30), 0, Qt.AlignHCenter)
        t = QLabel("没找到相关文稿" if self._query else "还没有可选的文稿")
        t.setObjectName("panelEmpty")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        if not self._query:
            s = QLabel("先去工作台转写一篇，或换「上传文件 / 粘贴文案」")
            s.setObjectName("pickMeta")
            s.setAlignment(Qt.AlignCenter)
            s.setWordWrap(True)
            v.addWidget(s)
            if self._go:
                btn = QPushButton("  去工作台")
                btn.setObjectName("act")
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(lambda: (self.reject(), self._go()))
                v.addWidget(btn, 0, Qt.AlignHCenter)
        return w

    def _select(self, entry: dict) -> None:
        self._selected_id = entry["id"]
        self.ok.setEnabled(True)
        self._rebuild()

    def _confirm_with(self, entry: dict) -> None:   # 双击直接确认
        self._selected_id = entry["id"]
        self._confirm()

    def _confirm(self) -> None:
        sel = next((e for e in self._entries if e["id"] == self._selected_id), None)
        if sel is None:
            return
        self.payload = sel
        self.accept()
