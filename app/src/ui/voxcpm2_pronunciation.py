"""Transactional Vox pronunciation editor using the official phoneme notation."""
from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QScrollArea, QPlainTextEdit
from ..core.synthesis.voxcpm2_pronunciation import validate_terms, apply_pronunciations


def edit_pronunciations(workbench):
    w = workbench
    d, layout = w._dialog('术语读音')
    d.resize(660, 540)
    hint = w._label('仅用于 VoxCPM2。原文保持不变，生成时应用读音。')
    hint.setWordWrap(True); layout.addWidget(hint)
    hint = w._label('中文：重庆 → chong2 qing4　英文：hello → HH AH0 L OW1')
    hint.setWordWrap(True); layout.addWidget(hint)
    scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    body = QWidget(); rows = QVBoxLayout(body); rows.setContentsMargins(0, 0, 8, 0); rows.addStretch()
    scroll.setWidget(body); layout.addWidget(scroll, 1)
    items = []
    error = w._label(''); error.setWordWrap(True)
    preview = QPlainTextEdit(); preview.setReadOnly(True); preview.setAccessibleName('生成时的读音预览'); preview.setMaximumHeight(110)
    def values():
        return validate_terms([{'source': a.text(), 'reading': b.text()} for _, a, b in items if a.text().strip() or b.text().strip()])
    def refresh():
        try:
            preview.setPlainText(apply_pronunciations(w.page.text_edit.toPlainText(), values())); error.clear()
        except ValueError as exc:
            preview.clear(); error.setText(str(exc))
    def add(term=None):
        if len(items) >= 100:
            error.setText('最多保存 100 条术语读音。'); return
        term = term or {}; cell = QWidget(); row = QHBoxLayout(cell); row.setContentsMargins(0, 0, 0, 0)
        source = QLineEdit(term.get('source', '')); source.setPlaceholderText('原词，例如 重庆'); source.setAccessibleName('术语原词')
        reading = QLineEdit(term.get('reading', '')); reading.setPlaceholderText('读音，例如 chong2 qing4'); reading.setAccessibleName('术语读音')
        item = (cell, source, reading); items.append(item)
        def remove():
            items.remove(item); cell.deleteLater(); refresh()
        row.addWidget(source, 1); row.addWidget(reading, 2); row.addWidget(w._button('移除', remove))
        rows.insertWidget(rows.count()-1, cell)
        source.textChanged.connect(refresh); reading.textChanged.connect(refresh)
    for term in w.pronunciations: add(term)
    if not items: add()
    layout.addWidget(w._button('添加术语', lambda: add()))
    layout.addWidget(w._label('生成文本预览 · 命中读音标注时自动跳过数字整理'))
    layout.addWidget(preview); layout.addWidget(error)
    def save():
        try: terms = values()
        except ValueError as exc: error.setText(str(exc)); return
        previous = deepcopy(getattr(w.page.cfg, 'voxcpm2_pronunciations', []))
        w.page.cfg.voxcpm2_pronunciations = terms
        try:
            if w.page.cfg.save() is False: raise OSError()
        except Exception:
            w.page.cfg.voxcpm2_pronunciations = previous; error.setText('保存失败，请重试。'); return
        w.pronunciations = deepcopy(terms); d.accept()
    footer = QHBoxLayout(); footer.addStretch(); footer.addWidget(w._button('取消', d.reject)); footer.addWidget(w._button('保存读音', save, True))
    layout.addLayout(footer); refresh(); d.exec(); d.deleteLater()
