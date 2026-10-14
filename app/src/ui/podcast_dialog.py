"""播客选集弹窗：解析 RSS 后，列出每集供用户勾选转写（可多选）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ..core import podcast
from . import icons, theme as T


class _FeedWorker(QThread):
    done = Signal(object)   # podcast.Feed | None

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        feed = podcast.fetch_feed(self._url)
        if not self.isInterruptionRequested():
            self.done.emit(feed)


class PodcastPicker(QDialog):
    """返回用户勾选的 Episode 列表（self.chosen）。"""

    def __init__(self, feed: podcast.Feed, parent=None):
        super().__init__(parent)
        self.feed = feed
        self.chosen: list[podcast.Episode] = []
        self.setWindowTitle(f"{feed.title} · 选集转写")
        self.resize(560, 600)
        self.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        title = QLabel(feed.title)
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        lay.addWidget(title)
        meta = QLabel(f"共 {len(feed.episodes)} 集 · 勾选要转写的（可多选），点下方按钮加入队列")
        meta.setObjectName("pageMeta")
        lay.addWidget(meta)

        search = QLineEdit()
        search.setObjectName("linkInput")
        search.setPlaceholderText("🔍 按标题筛选…")
        search.textChanged.connect(self._filter)
        lay.addWidget(search)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setStyleSheet(
            f"QListWidget{{background:{T.CARD};border:1px solid {T.LINE};border-radius:12px;"
            f"padding:6px;font-size:13px;color:{T.INK};}}"
            f"QListWidget::item{{padding:7px 8px;border-radius:8px;}}"
            f"QListWidget::item:hover{{background:{T.TERRA_WASH};}}"
        )
        for ep in feed.episodes:
            tail = " · ".join(x for x in (ep.date, ep.duration) if x)
            it = QListWidgetItem(f"{ep.title}\n    {tail}" if tail else ep.title)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            it.setData(Qt.UserRole, ep)
            self.list.addItem(it)
        self.list.itemChanged.connect(lambda *_: self._update_btn())
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("act")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("  加入队列")
        self.ok.setObjectName("grabBtn")
        self.ok.setCursor(Qt.PointingHandCursor)
        self.ok.setIcon(QIcon(icons.pixmap("plus", "#FFFFFF", 14)))
        self.ok.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)
        self._update_btn()

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            ep = it.data(Qt.UserRole)
            it.setHidden(bool(q) and q not in ep.title.lower())

    def _checked(self) -> list[podcast.Episode]:
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _update_btn(self) -> None:
        n = len(self._checked())
        self.ok.setText(f"  加入队列（{n}）" if n else "  加入队列")
        self.ok.setEnabled(n > 0)

    def _accept(self) -> None:
        self.chosen = self._checked()
        self.accept()
