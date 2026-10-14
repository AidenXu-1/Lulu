"""主题化弹窗：替换系统灰色 QMessageBox，跟暖陶土界面统一风格。

- alert（parent, 标题， 正文）：单按钮提示。
- confirm（parent, 标题， 正文） -> bool：确认/取消，返回是否确认。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme as T
from . import icons


def _build(
    parent,
    title: str,
    text: str,
    ok_text: str,
    cancel_text: str | None,
    *,
    default_ok: bool = True,
) -> QDialog:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(400)
    dlg.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(24, 22, 24, 18)
    lay.setSpacing(12)

    head = QLabel(title)
    head.setObjectName("sectionTitle")
    lay.addWidget(head)

    body = QLabel(text)
    body.setWordWrap(True)
    body.setStyleSheet(f"color:{T.INK};font-size:13px;line-height:1.6;")
    lay.addWidget(body)
    lay.addSpacing(6)

    row = QHBoxLayout()
    row.addStretch(1)
    if cancel_text:
        c = QPushButton(cancel_text)
        c.setObjectName("act")
        c.setCursor(Qt.PointingHandCursor)
        c.clicked.connect(dlg.reject)
        c.setDefault(not default_ok)
        row.addWidget(c)
    ok = QPushButton("  " + ok_text)
    ok.setObjectName("grabBtn")
    ok.setCursor(Qt.PointingHandCursor)
    ok.clicked.connect(dlg.accept)
    ok.setDefault(default_ok)
    row.addWidget(ok)
    lay.addLayout(row)
    return dlg


def alert(parent, title: str, text: str, ok: str = "知道了") -> None:
    _build(parent, title, text, ok, None).exec()


def confirm(parent, title: str, text: str, ok: str = "确定", cancel: str = "取消") -> bool:
    return _build(parent, title, text, ok, cancel).exec() == QDialog.Accepted


def confirm_open(parent, title: str, text: str) -> bool:
    """显示持久结果框；返回 True 表示用户选择“打开”。"""
    return (
        _build(
            parent,
            title,
            text,
            "打开",
            "确定",
            default_ok=False,
        ).exec()
        == QDialog.Accepted
    )


def prompt(parent, title: str, label: str = "", text: str = "",
           ok: str = "确定", cancel: str = "取消", placeholder: str = "",
    secret: bool = False) -> str | None:
    """主题化文字输入弹窗。返回去空白后的输入；取消/留空返回 None。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(400)
    dlg.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
    if secret:
        # Opt this new credential surface into Lulu's current controls without
        # restyling the existing prompts used by the three workbenches.
        dlg.setObjectName("credentialPrompt")
        dlg.setMinimumWidth(480)
        paper = T.consumer_theme(T.ACTIVE.name)
        dlg.setStyleSheet(T.QSS + f"""
QDialog#credentialPrompt {{ background: {paper.surface}; color: {paper.ink}; }}
QDialog#credentialPrompt QPushButton {{ min-height: 32px; padding: 0 14px; border-radius: 8px; }}
QDialog#credentialPrompt QPushButton#act {{ background: {paper.secondary}; color: {paper.ink_soft}; border: 1px solid transparent; }}
QDialog#credentialPrompt QPushButton#act:hover {{ background: {paper.secondary_hover}; color: {paper.ink}; }}
QDialog#credentialPrompt QPushButton#grabBtn {{ background: {paper.accent}; color: white; border: 1px solid {paper.accent}; }}
QDialog#credentialPrompt QPushButton#grabBtn:hover {{ background: {paper.accent_hover}; }}
QDialog#credentialPrompt QPushButton:pressed {{ background: {paper.accent_wash}; color: {paper.accent}; }}
QDialog#credentialPrompt QPushButton:focus {{ border: 1px solid {paper.accent}; }}
QDialog#credentialPrompt QPushButton#grabBtn:disabled {{ background: {paper.secondary}; color: {paper.ink_muted}; border-color: {paper.line}; }}
QDialog#credentialPrompt QLineEdit#linkInput {{ background: {paper.surface}; color: {paper.ink}; border: 1px solid {paper.line}; border-radius: 8px; }}
QDialog#credentialPrompt QLineEdit#linkInput:focus {{ border-color: {paper.accent}; }}
""")
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(24, 22, 24, 18)
    lay.setSpacing(10)

    head = QLabel(title)
    head.setObjectName("sectionTitle")
    lay.addWidget(head)
    if label:
        lab = QLabel(label)
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{T.INK_SOFT};font-size:13px;")
        lay.addWidget(lab)

    edit = QLineEdit(text)
    edit.setObjectName("linkInput")
    edit.setMinimumHeight(36)
    if placeholder:
        edit.setPlaceholderText(placeholder)
    if secret:
        edit.setEchoMode(QLineEdit.Password)
        edit.setMaxLength(8192)
        edit.setAccessibleName("API 密钥")
        field = QHBoxLayout()
        field.addWidget(edit, 1)
        reveal = QPushButton("显示")
        reveal.setObjectName("act")
        reveal.setCheckable(True)
        reveal.setAutoDefault(False)
        reveal.setAccessibleName("显示或隐藏密钥")
        reveal.setCursor(Qt.PointingHandCursor)
        def toggle(checked):
            edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
            reveal.setText("隐藏" if checked else "显示")
        reveal.toggled.connect(toggle)
        field.addWidget(reveal)
        lay.addLayout(field)
    else:
        edit.returnPressed.connect(dlg.accept)
        lay.addWidget(edit)
    lay.addSpacing(4)

    row = QHBoxLayout()
    row.addStretch(1)
    c = QPushButton(cancel)
    c.setObjectName("act")
    c.setCursor(Qt.PointingHandCursor)
    c.clicked.connect(dlg.reject)
    row.addWidget(c)
    okb = QPushButton("  " + ok)
    okb.setObjectName("grabBtn")
    okb.setCursor(Qt.PointingHandCursor)
    okb.clicked.connect(dlg.accept)
    okb.setDefault(True)
    if secret:
        def valid(value):
            return bool(value.strip()) and not any(c.isspace() for c in value.strip())
        okb.setEnabled(valid(edit.text()))
        edit.textChanged.connect(lambda value: okb.setEnabled(valid(value)))
        edit.returnPressed.connect(lambda: dlg.accept() if okb.isEnabled() else None)
    row.addWidget(okb)
    lay.addLayout(row)

    edit.setFocus()
    edit.selectAll()
    accepted = dlg.exec() == QDialog.Accepted
    result = edit.text().strip() if accepted else None
    edit.clear()
    dlg.deleteLater()
    return result or None


def choose(parent, title: str, label: str, options: list[str],
           current: int = 0, cancel: str = "取消") -> int | None:
    """主题化单选列表弹窗（点行即选）。返回选中项索引；取消返回 None。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(400)
    dlg.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(24, 22, 24, 18)
    lay.setSpacing(10)

    head = QLabel(title)
    head.setObjectName("sectionTitle")
    lay.addWidget(head)
    if label:
        lab = QLabel(label)
        lab.setStyleSheet(f"color:{T.INK_SOFT};font-size:13px;")
        lay.addWidget(lab)

    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(
        "QScrollArea{background:transparent;border:none;}"
        "QScrollArea>QWidget>QWidget{background:transparent;}")
    inner = QWidget()
    iv = QVBoxLayout(inner)
    iv.setContentsMargins(0, 0, 0, 0)
    iv.setSpacing(2)
    for i, opt in enumerate(options):
        row = QFrame()
        row.setObjectName("tplRow")
        row.setProperty("picked", "true" if i == current else "false")
        row.setCursor(Qt.PointingHandCursor)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(12, 9, 12, 9)
        rl.setSpacing(10)
        nm = QLabel(opt)
        nm.setObjectName("tplRowName")
        rl.addWidget(nm, 1)
        if i == current:
            rl.addWidget(icons.icon_label("check", T.TERRA, 15), 0, Qt.AlignVCenter)
        row.mousePressEvent = lambda e, idx=i: dlg.done(idx + 1)   # 返回码 = 索引+1
        iv.addWidget(row)
    iv.addStretch(1)
    scroll.setWidget(inner)
    inner.adjustSize()
    scroll.setMaximumHeight(min(inner.sizeHint().height() + 4, 360))
    lay.addWidget(scroll)

    row = QHBoxLayout()
    row.addStretch(1)
    c = QPushButton(cancel)
    c.setObjectName("act")
    c.setCursor(Qt.PointingHandCursor)
    c.clicked.connect(dlg.reject)
    row.addWidget(c)
    lay.addLayout(row)

    res = dlg.exec()
    return (res - 1) if res > 0 else None
