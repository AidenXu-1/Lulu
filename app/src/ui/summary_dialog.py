"""智能摘要弹窗：把逐字稿交给云端 LLM（默认 DeepSeek）整理成摘要并展示。

- 传入 entry_id 时：已有摘要直接显示（不重复花钱），生成后写回文稿库持久化。
- 摘要会把逐字稿上传到所选厂家——是用户显式触发的云端能力。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..core import library, summarize
from . import icons, theme as T


class _SummaryWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def run(self) -> None:
        try:
            self.done.emit(True, summarize.summarize(self._text))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))


class SummaryDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None, entry_id: str | None = None):
        super().__init__(parent)
        self._md = ""
        self._text = text
        self._entry_id = entry_id
        self._worker: _SummaryWorker | None = None
        self.setWindowTitle(f"{title} · 摘要")
        self.resize(560, 560)
        self.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        head = QLabel("智能摘要")
        head.setObjectName("pageTitle")
        lay.addWidget(head)
        self.hint = QLabel()
        self.hint.setObjectName("pageMeta")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setStyleSheet(
            f"QTextBrowser{{background:{T.CARD};border:1px solid {T.LINE};border-radius:12px;"
            f"padding:14px;font-size:14px;color:{T.INK};}}"
        )
        lay.addWidget(self.body, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.regen_btn = QPushButton("  重新生成")
        self.regen_btn.setObjectName("act")
        self.regen_btn.setCursor(Qt.PointingHandCursor)
        self.regen_btn.clicked.connect(self._generate)
        self.regen_btn.setVisible(False)
        self.copy_btn = QPushButton("  复制摘要")
        self.copy_btn.setObjectName("grabBtn")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setIcon(QIcon(icons.pixmap("copy", "#FFFFFF", 14)))
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy)
        row.addWidget(self.regen_btn)
        row.addWidget(self.copy_btn)
        lay.addLayout(row)

        cached = library.get_summary(entry_id) if entry_id else None
        if cached:
            self._show(cached, cached_note=True)
        else:
            self._generate()

    # ------------------------------------------------------------------ #
    def _generate(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self.hint.setStyleSheet("")
        self.hint.setText("正在生成摘要…（逐字稿会上传到你配置的摘要服务）")
        self.body.setMarkdown("")
        self.copy_btn.setEnabled(False)
        self.regen_btn.setVisible(False)
        self._worker = _SummaryWorker(self._text)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, payload: str) -> None:
        if ok:
            if self._entry_id:
                try:
                    library.save_summary(self._entry_id, payload)
                except Exception:  # noqa: BLE001
                    pass
            self._show(payload, cached_note=False)
        else:
            self.hint.setStyleSheet(f"color:{T.TERRA};font-size:11px;")
            self.hint.setText("摘要没生成成功，稍后再试试")
            self.body.setMarkdown(f"**出错了**\n\n{payload}")
            self.regen_btn.setVisible(True)

    def _show(self, md: str, cached_note: bool) -> None:
        self._md = md
        self.hint.setStyleSheet("")
        self.hint.setText(
            "这是已保存的摘要（随文稿一起存，可重复查看）。"
            if cached_note else
            "已生成并保存 · 下次打开直接看，无需重新生成。"
        )
        self.body.setMarkdown(md)
        self.copy_btn.setEnabled(True)
        self.regen_btn.setVisible(bool(self._entry_id))

    def _copy(self) -> None:
        if self._md:
            QGuiApplication.clipboard().setText(self._md)
            self.copy_btn.setText("  已复制 ✓")
