"""Small local glossary editor for TTS pronunciation preprocessing."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.synthesis import glossary
from . import dialogs, icons, theme as T
from .control_foundation import LuluCheckBox, adopt_select_field


class GlossaryPanel(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, pronunciation_enabled=False):
        super().__init__(parent)
        self.pronunciation_enabled = pronunciation_enabled
        self.model_version = "2.5"
        self._editing_id: str | None = None
        self._last_result = glossary.GlossaryApplyResult("", False, 0, 0)
        self._build()
        self.reload()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.header = QPushButton("  术语读音")
        self.header.setObjectName("act")
        self.header.setCheckable(True)
        self.header.setChecked(False)
        self.header.setIcon(QIcon(icons.pixmap("chevron", T.INK_SOFT, 13)))
        self.header.clicked.connect(self._toggle_body)
        root.addWidget(self.header)

        self.body = QFrame()
        self.body.setObjectName("sectionPanel")
        body = QVBoxLayout(self.body)
        body.setContentsMargins(12, 10, 12, 12)
        body.setSpacing(8)

        self.apply_check = LuluCheckBox("生成时应用术语读音")
        self.apply_check.toggled.connect(self._on_apply_toggled)
        body.addWidget(self.apply_check)

        hint = QLabel("本机词典 · 原始文本不会被改写；生成时使用预览后的文本。")
        hint.setObjectName("pageMeta")
        hint.setWordWrap(True)
        body.addWidget(hint)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("文字替换，例如 Lulu → 露露", "replacement")
        self.kind_combo.addItem("2.5 发音标注：拼音 / 英文音素 / 日语假名", "pronunciation")
        adopt_select_field(self.kind_combo, accessible_name="术语读音处理方式")
        self.kind_combo.setVisible(self.pronunciation_enabled)
        body.addWidget(self.kind_combo)
        self.example = QLabel("发音标注示例：行 → XING2；hello → HH AH0 L OW1；東京 → トウキョウ。\n只影响朗读。选段试听可检验效果；也可仅在本次文案写 <行|XING2>，不保存词条。")
        self.example.setObjectName("pageMeta")
        self.example.setWordWrap(True)
        self.example.setVisible(self.pronunciation_enabled)
        body.addWidget(self.example)

        self.rows = QVBoxLayout()
        self.rows.setSpacing(6)
        body.addLayout(self.rows)

        form = QHBoxLayout()
        self.source_input = QLineEdit()
        self.source_input.setObjectName("linkInput")
        self.source_input.setPlaceholderText("原词")
        form.addWidget(self.source_input, 1)
        self.replacement_input = QLineEdit()
        self.replacement_input.setObjectName("linkInput")
        self.replacement_input.setPlaceholderText("读作")
        form.addWidget(self.replacement_input, 1)
        self.save_btn = QPushButton("  添加术语")
        self.save_btn.setObjectName("act")
        self.save_btn.setIcon(QIcon(icons.pixmap("plus", T.INK_SOFT, 13)))
        self.save_btn.clicked.connect(self._save_term)
        form.addWidget(self.save_btn)
        self.cancel_edit_btn = QPushButton("取消")
        self.cancel_edit_btn.setObjectName("act")
        self.cancel_edit_btn.clicked.connect(self._cancel_edit)
        self.cancel_edit_btn.setVisible(False)
        form.addWidget(self.cancel_edit_btn)
        body.addLayout(form)

        preview_row = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("pageMeta")
        preview_row.addWidget(self.summary, 1)
        self.preview_btn = QPushButton("  预览替换结果")
        self.preview_btn.setObjectName("act")
        self.preview_btn.setIcon(QIcon(icons.pixmap("search", T.INK_SOFT, 13)))
        preview_row.addWidget(self.preview_btn)
        body.addLayout(preview_row)

        self.preview = QPlainTextEdit()
        self.preview.setObjectName("mdEditor")
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(140)
        self.preview.setVisible(False)
        body.addWidget(self.preview)

        self.body.setVisible(False)
        root.addWidget(self.body)

    def bind_preview_button(self, callback) -> None:
        self.preview_btn.clicked.connect(callback)

    def set_model_version(self, version: str) -> None:
        self.model_version = version
        legacy = version == "2.0"
        self.kind_combo.setItemText(1, "官方中文拼音，例如 落空 → LUO4 KONG1" if legacy
                                   else "2.5 发音标注：拼音 / 英文音素 / 日语假名")
        self.example.setText(
            "官方中文拼音：落空 → LUO4 KONG1，每个音节带 1–5 声调，多个音节用空格分开。\n"
            "Lulu 会把指定词转换成拼音供模型朗读，不改原文；英文音素和日语假名词条保留给 2.5，当前不应用。读音效果请先选段试听。"
            if legacy else
            "发音标注示例：行 → XING2；hello → HH AH0 L OW1；東京 → トウキョウ。\n"
            "只影响朗读。选段试听可检验效果；也可仅在本次文案写 <行|XING2>，不保存词条。"
        )
        self.preview.clear()
        self.preview.hide()
        self.reload()

    def _toggle_body(self) -> None:
        self.body.setVisible(self.header.isChecked())

    def reload(self) -> None:
        store = glossary.load_store()
        self.apply_check.blockSignals(True)
        self.apply_check.setChecked(bool(store.get("apply_enabled", True)))
        self.apply_check.blockSignals(False)
        self._render_terms(store)
        if store.get('load_error'):
            self.header.setText('  术语读音 · 文件读取异常')
            self.summary.setText('原词典文件已保留，请先检查文件；本次不会应用词典。')

    def apply_enabled(self) -> bool:
        return self.apply_check.isChecked()

    def set_apply_enabled(self, enabled: bool, *, emit: bool = True) -> None:
        self.apply_check.blockSignals(True)
        self.apply_check.setChecked(bool(enabled))
        self.apply_check.blockSignals(False)
        try:
            glossary.set_apply_enabled(bool(enabled))
        except (ValueError, OSError) as exc:
            self.reload()
            dialogs.alert(self, "术语读音", str(exc) if isinstance(exc, ValueError) else "词典保存失败，旧词典已保留。")
            return
        self.reload()
        if emit:
            self.changed.emit()

    def _render_terms(self, store: dict | None = None) -> None:
        store = store or glossary.load_store()
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        terms = list(store.get("terms") or [])
        enabled_count = sum(1 for term in terms if term.get("enabled", True))
        if not terms:
            empty = QLabel("还没有术语读音。添加品牌名、人名、英文缩写或技术词。")
            empty.setObjectName("pageMeta")
            empty.setWordWrap(True)
            self.rows.addWidget(empty)
        for term in terms:
            self.rows.addWidget(self._term_row(term))
        state = "已关闭" if not store.get("apply_enabled", True) else ("未添加" if not terms else f"已启用 {enabled_count} 条")
        self.header.setText(f"  术语读音 · {state}")
        self.summary.setText(f"本机词典 · 已启用 {enabled_count} 条 / 共 {len(terms)} 条")

    def _term_row(self, term: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("sectionPanel")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        kind = "发音标注" if term.get("kind") == "pronunciation" else "文字替换"
        unavailable = (self.model_version == "2.0" and term.get("kind") == "pronunciation"
                       and glossary.index2_pinyin(str(term.get("replacement", ""))) is None)
        if unavailable:
            kind += " · 本模型不应用，词条保留"
        label = QLabel(f"{term.get('source', '')} → {term.get('replacement', '')} · {kind}")
        label.setObjectName("pageMeta")
        label.setWordWrap(True)
        lay.addWidget(label, 1)
        toggle = QPushButton("停用" if term.get("enabled", True) else "启用")
        toggle.setObjectName("act")
        toggle.clicked.connect(lambda _=False, tid=term["id"], en=not term.get("enabled", True): self._set_enabled(tid, en))
        lay.addWidget(toggle)
        edit = QPushButton("编辑")
        edit.setObjectName("act")
        edit.setEnabled(not unavailable)
        if unavailable:
            edit.setToolTip("请切换到 IndexTTS 2.5 编辑这条发音标注")
        edit.clicked.connect(lambda _=False, item=dict(term): self._edit_term(item))
        lay.addWidget(edit)
        delete = QPushButton()
        delete.setObjectName("act")
        delete.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 13)))
        delete.clicked.connect(lambda _=False, tid=term["id"]: self._delete_term(tid))
        lay.addWidget(delete)
        return row

    def _save_term(self) -> None:
        if (self.model_version == "2.0" and self.kind_combo.currentData() == "pronunciation"
                and glossary.index2_pinyin(self.replacement_input.text()) is None):
            dialogs.alert(self, "术语读音", "IndexTTS 2 请填写带声调的中文拼音，例如 LUO4 KONG1；英文音素或日语假名请在 2.5 中设置。")
            return
        try:
            glossary.upsert_term(
                self.source_input.text(),
                self.replacement_input.text(),
                term_id=self._editing_id,
                enabled=True,
                kind=str(self.kind_combo.currentData()),
            )
        except ValueError as exc:
            dialogs.alert(self, "术语读音", str(exc))
            return
        except Exception:
            dialogs.alert(self, "术语读音", "保存失败，请重试。")
            return
        self._cancel_edit(clear_only=True)
        self.reload()
        self.changed.emit()

    def _edit_term(self, term: dict) -> None:
        self._editing_id = str(term.get("id"))
        self.source_input.setText(str(term.get("source") or ""))
        self.replacement_input.setText(str(term.get("replacement") or ""))
        self.kind_combo.setCurrentIndex(1 if term.get("kind") == "pronunciation" else 0)
        self.save_btn.setText("保存")
        self.cancel_edit_btn.setVisible(True)

    def _cancel_edit(self, *, clear_only: bool = False) -> None:
        self._editing_id = None
        self.source_input.clear()
        self.replacement_input.clear()
        self.kind_combo.setCurrentIndex(0)
        self.save_btn.setText("  添加术语")
        self.cancel_edit_btn.setVisible(False)
        if not clear_only:
            self.changed.emit()

    def _delete_term(self, term_id: str) -> None:
        if not dialogs.confirm(self, "删除这个术语读音？", "删除后，生成时不会再按这条读音处理。", ok="删除"):
            return
        try:
            glossary.delete_term(term_id)
        except (ValueError, OSError):
            dialogs.alert(self, "术语读音", "删除未完成，旧词典已保留。请检查词典文件和保存权限。")
            return
        self.reload()
        self.changed.emit()

    def _set_enabled(self, term_id: str, enabled: bool) -> None:
        try:
            glossary.set_term_enabled(term_id, enabled)
        except (ValueError, OSError):
            dialogs.alert(self, "术语读音", "修改未完成，旧词典已保留。请检查词典文件和保存权限。")
            return
        self.reload()
        self.changed.emit()

    def _on_apply_toggled(self, enabled: bool) -> None:
        self.set_apply_enabled(enabled)

    def preview_text(self, text: str) -> glossary.GlossaryApplyResult:
        store = glossary.load_store()
        result = glossary.apply_glossary(text, store, pronunciation=self.pronunciation_enabled, model_version=self.model_version)
        self._last_result = result
        if result.applied:
            self.preview.setPlainText(result.text)
            self.preview.setVisible(True)
            self.summary.setText(f"将应用 {result.matched_terms} 条词条，预计替换 {result.replacement_count} 处")
        else:
            self.preview.setPlainText(result.text)
            self.preview.setVisible(bool(text.strip()))
            reason = "术语读音已开启，当前文本未命中" if store.get("apply_enabled", True) else "术语读音未应用"
            self.summary.setText(reason)
        if "pronunciation_unavailable" in result.warnings:
            self.summary.setText(self.summary.text() + "；部分读音不适用于当前模型，已跳过并保留原词")
        return result

    def apply_to_text(self, text: str) -> glossary.GlossaryApplyResult:
        try:
            return glossary.apply_glossary(text, pronunciation=self.pronunciation_enabled, model_version=self.model_version)
        except Exception:
            return glossary.GlossaryApplyResult(text, False, 0, 0, ("glossary_failed",))
