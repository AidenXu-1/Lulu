"""小型通用控件。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class StatRow(QWidget):
    """互动数据行：作者段（可省略）+ 各指标「图标 + 数字」。

    给完成的转录任务卡 / 素材详情用，替代旧的 emoji 文本行。
    """

    def __init__(self, color: str | None = None, size: int = 13):
        super().__init__()
        if color is None:
            from . import theme as T
            color = T.INK_SOFT  # 默认随当前主题次文字色（不在签名里冻结暖色字面）
        self._color = color
        self._size = size
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(10)

    def set_meta(self, meta: dict | None) -> bool:
        """按 meta 重建；有内容返回 True，否则 False（调用方据此显隐）。"""
        from . import icons
        from ..core import obsidian

        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        meta = meta or {}
        author = obsidian._author_bit(meta)
        items = obsidian.stat_items(meta)
        if not author and not items:
            return False

        # 作者 + 各指标统一左对齐紧挨排布，末尾留弹性空间。
        # 作者用普通 QLabel（按内容自适应宽度）;ElideLabel 不加弹性会缩成「…」把作者吃掉。
        if author:
            al = QLabel(author)
            al.setStyleSheet(f"color:{self._color};font-size:11px;")
            self._lay.addWidget(al, 0)

        for key, emoji, cnt in items:
            grp = QWidget()
            gl = QHBoxLayout(grp)
            gl.setContentsMargins(0, 0, 0, 0)
            gl.setSpacing(3)
            pm = icons.stat_pixmap(key, self._size)
            ic = QLabel()
            if pm is not None:
                ic.setPixmap(pm)
                ic.setFixedSize(self._size, self._size)
            else:                       # 没图标的指标（播放等）用 emoji 兜底
                ic.setText(emoji)
                ic.setStyleSheet("font-size:11px;")
            gl.addWidget(ic)
            num = QLabel(cnt)
            num.setStyleSheet(f"color:{self._color};font-size:11px;")
            gl.addWidget(num)
            self._lay.addWidget(grp, 0)

        self._lay.addStretch(1)
        return True


class ElideLabel(QLabel):
    """单行省略标签：窗口变窄时文本截断为「…」，不把完整宽度撑给布局。

    普通 QLabel 会把整段文本宽度报告为 minimumSizeHint —— 一条长视频标题
    就能把整页最小宽度撑到几千像素，迫使页面出横向滚动条/被裁切。
    本控件最小宽度恒小，文本按当前实际宽度即时省略（悬停可由调用方配 tooltip）。
    """

    def __init__(self, text: str = "", parent=None, *, elide_mode=Qt.ElideRight):
        super().__init__(parent)
        self._full = ""
        self._elide_mode = elide_mode
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if text:
            self.setText(text)

    def setText(self, text) -> None:  # noqa: N802
        self._full = str(text or "")
        self._apply()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(24, super().minimumSizeHint().height())

    def _apply(self) -> None:
        width = max(24, self.width() - 2)
        # 折叠换行/制表/多空格为单空格：单行标签遇到换行会被挤成多行、在定高里被裁切。
        flat = " ".join(self._full.split())
        super().setText(self.fontMetrics().elidedText(flat, self._elide_mode, width))


class FileDropArea(QFrame):
    """工作台同款拖放区：可拖入文件，也可点击选择。on_click/on_drop 由调用方提供。"""

    def __init__(self, on_click, on_drop, sub: str = "", *, title: str = "拖入文案文件"):
        super().__init__()
        from . import icons, theme as T
        from .control_foundation import adopt_surface

        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setMinimumHeight(130)
        self._on_click = on_click
        self._on_drop = on_drop
        v = QVBoxLayout(self)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(4)
        v.setContentsMargins(16, 10, 16, 10)
        self.glyph = QFrame()
        self.glyph.setFixedSize(38, 38)
        adopt_surface(self.glyph, "well", variant="uploadGlyph")
        glyph_layout = QHBoxLayout(self.glyph)
        glyph_layout.setContentsMargins(0, 0, 0, 0)
        glyph_layout.setAlignment(Qt.AlignCenter)
        glyph_layout.addWidget(icons.icon_label("download", T.TERRA, 20))
        v.addWidget(self.glyph, 0, Qt.AlignCenter)
        self.title = QLabel(title)
        self.title.setObjectName("dropTitle")
        self.title.setAlignment(Qt.AlignCenter)
        v.addWidget(self.title)
        self.action = QLabel("或点击选择")
        self.action.setObjectName("dropSub")
        self.action.setAlignment(Qt.AlignCenter)
        v.addWidget(self.action)
        self.sub = ElideLabel(sub)
        self.sub.setObjectName("dropFmt")
        self.sub.setAlignment(Qt.AlignCenter)
        self.sub.setToolTip(sub)
        v.addWidget(self.sub)

    def set_status(self, text: str) -> None:
        self.sub.setText(text)
        self.sub.setToolTip(text)

    def mousePressEvent(self, e):  # noqa: N802
        self._on_click()

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._on_click()
            e.accept()
            return
        super().keyPressEvent(e)

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p:
                self._on_drop(p)
                break
