"""飞书知识库：OAuth 配置、作品库目标选择与多维表格统一写入入口。

鉴权走 OAuth 登录授权：用户在浏览器登录并同意 → 本地 127.0.0.1 回调拿 code →
换 user_access_token，以用户本人身份写其知识库（含飞书个人版）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from typing import Callable, Iterable
from uuid import uuid4

from PySide6.QtCore import QPoint, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import feishu, secrets
from . import dialogs, icons, theme as T
from .control_foundation import (
    PaperTextureFrame,
    adopt_consumer_theme_scope,
    adopt_scroll_area,
    adopt_surface,
    build_consumer_page_header,
)
from .macos import unify_titlebar

# 进程内"已告知会上传"标志，三处入口共享，只弹一次二次确认
_disclosed = False


def _unify_feishu_titlebar(window: QWidget) -> bool:
    """只在真实 macOS Cocoa 窗口上调用原生标题栏融合。

    离线界面测试使用 offscreen 平台，其 ``winId`` 不是 NSView 指针；
    必须在进入 Objective-C runtime 前先失败关闭。
    """

    app = QGuiApplication.instance()
    if app is None:
        return False
    try:
        if str(app.platformName()).strip().casefold() != "cocoa":
            return False
    except Exception:  # noqa: BLE001 - 无法确认平台时保留 Qt 默认标题栏
        return False
    return unify_titlebar(window)


@dataclass(frozen=True)
class FeishuActionOption:
    """飞书弹窗中的一个可点击动作。业务层只描述语义，不接触布局细节。"""

    title: str
    detail: str = ""
    icon: str = "chevron"
    badge: str = ""
    group: str = ""
    tone: str = "standard"
    actions: tuple[str, ...] = ()
    selectable: bool = True
    input_placeholder: str = ""
    input_value: str = ""
    input_action: str = ""


@dataclass(frozen=True)
class FeishuActionResult:
    index: int
    action: str = "select"


def _feishu_action_qss(consumer: T.ConsumerTheme) -> str:
    return f"""
    QDialog#feishuActionDialog {{
        background: {consumer.canvas}; color: {consumer.ink};
    }}
    QDialog#feishuActionDialog QLabel {{
        background: transparent; color: {consumer.ink};
        font-family: "{consumer.font_body}";
    }}
    QDialog#feishuActionDialog QLabel#feishuDialogEyebrow {{
        color: {consumer.accent}; font-size: 11px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QLabel#feishuDialogTitle {{
        color: {consumer.ink}; font-family: "{consumer.font_display}";
        font-size: 24px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QLabel#feishuDialogSubtitle {{
        color: {consumer.ink_muted}; font-size: 12px;
    }}
    QDialog#feishuActionDialog QFrame#feishuDialogRule {{
        background: {consumer.accent}; border: none; border-radius: 2px;
    }}
    QDialog#feishuActionDialog QScrollArea,
    QDialog#feishuActionDialog QScrollArea > QWidget > QWidget {{
        background: transparent; border: none;
    }}
    QDialog#feishuActionDialog QFrame#feishuActionGroup {{
        background: {consumer.surface_alt};
        border: 1px solid {consumer.line};
        border-radius: {consumer.radius_surface}px;
    }}
    QDialog#feishuActionDialog QLabel#feishuActionGroupTitle {{
        color: {consumer.ink_soft}; font-size: 12px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard {{
        background: {consumer.surface_focal}; color: {consumer.ink};
        border: 1px solid {consumer.line};
        border-radius: {consumer.radius_control + 2}px;
        padding: 0px; text-align: left;
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard:hover {{
        background: {consumer.paper_edge};
        border-color: {consumer.accent_hairline_active};
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard:pressed {{
        background: {consumer.accent_wash};
        border-color: {consumer.accent};
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard:focus {{
        border: 2px solid {consumer.focus_ring};
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="recommended"] {{
        background: {consumer.surface_focal};
        border-color: {consumer.accent_hairline_active};
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="primary"] {{
        background: {consumer.accent};
        border-color: {consumer.accent};
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="primary"] QLabel#feishuActionTitle,
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="primary"] QLabel#feishuActionDetail {{
        color: #FFFFFF;
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="primary"] QFrame#feishuActionIconWell {{
        background: rgba(255,255,255,0.14);
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="primary"] QLabel#feishuActionBadge {{
        color: #FFFFFF; background: rgba(255,255,255,0.12);
        border-color: rgba(255,255,255,0.46);
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="primary"]:hover {{
        background: {consumer.accent_hover};
        border-color: {consumer.accent_hover};
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[tone="danger"] {{
        border-color: {consumer.line};
    }}
    QDialog#feishuActionDialog QPushButton#feishuActionCard[passive="true"]:hover {{
        background: {consumer.surface_focal}; border-color: {consumer.line};
    }}
    QDialog#feishuActionDialog QFrame#feishuActionIconWell {{
        background: {consumer.accent_wash}; border: none;
        border-radius: {consumer.radius_control}px;
    }}
    QDialog#feishuActionDialog QFrame#feishuActionIconWell[tone="danger"] {{
        background: {consumer.secondary_hover};
    }}
    QDialog#feishuActionDialog QLabel#feishuActionTitle {{
        color: {consumer.ink}; font-size: 14px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QLabel#feishuActionDetail {{
        color: {consumer.ink_muted}; font-size: 11px;
    }}
    QDialog#feishuActionDialog QLabel#feishuActionBadge {{
        color: {consumer.accent}; background: {consumer.accent_wash};
        border: 1px solid {consumer.accent_hairline};
        border-radius: 8px; padding: 2px 7px;
        font-size: 10px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QLabel#feishuActionBadge[badgeTone="muted"] {{
        color: {consumer.ink_muted}; background: {consumer.secondary};
        border-color: {consumer.line};
    }}
    QDialog#feishuActionDialog QPushButton#feishuInlineAction {{
        min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px;
        background: {consumer.secondary}; color: {consumer.ink_muted};
        border: 1px solid transparent;
        border-radius: {consumer.radius_control}px; padding: 0px;
    }}
    QDialog#feishuActionDialog QPushButton#feishuInlineAction:hover {{
        background: {consumer.accent_wash};
        border-color: {consumer.accent_hairline};
    }}
    QDialog#feishuActionDialog QPushButton#feishuInlineAction[action="delete"]:hover {{
        color: {consumer.danger}; background: {consumer.secondary_hover};
        border-color: {consumer.danger};
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogCancel {{
        min-width: 82px; min-height: 34px;
        background: {consumer.secondary}; color: {consumer.ink_soft};
        border: 1px solid transparent;
        border-radius: {consumer.radius_control}px;
        padding: 0 16px; font-size: 12px; font-weight: 600;
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogCancel:hover {{
        background: {consumer.secondary_hover}; color: {consumer.ink};
        border-color: {consumer.line};
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogCancel:focus {{
        border: 2px solid {consumer.focus_ring};
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogBack {{
        min-width: 76px; min-height: 32px;
        background: transparent; color: {consumer.accent};
        border: 1px solid transparent;
        border-radius: {consumer.radius_control}px;
        padding: 0 10px; text-align: left;
        font-size: 12px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogBack:hover {{
        background: {consumer.accent_wash};
        border-color: {consumer.accent_hairline};
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogBack:focus {{
        border: 2px solid {consumer.focus_ring};
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogHeaderAction {{
        min-width: 112px; min-height: 36px;
        background: {consumer.accent}; color: #FFFFFF;
        border: 1px solid {consumer.accent};
        border-radius: {consumer.radius_control}px;
        padding: 0 14px; font-size: 12px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QPushButton#feishuDialogHeaderAction:hover {{
        background: {consumer.accent_hover}; border-color: {consumer.accent_hover};
    }}
    QDialog#feishuActionDialog QLineEdit#feishuInlineInput {{
        min-height: 38px; background: {consumer.surface_focal}; color: {consumer.ink};
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_control}px;
        padding: 0 12px; selection-background-color: {consumer.accent};
    }}
    QDialog#feishuActionDialog QLineEdit#feishuInlineInput:focus {{
        border: 2px solid {consumer.focus_ring};
    }}
    QDialog#feishuActionDialog QPushButton#feishuInlineSave {{
        min-width: 82px; min-height: 40px;
        background: {consumer.accent}; color: #FFFFFF;
        border: 1px solid {consumer.accent};
        border-radius: {consumer.radius_control}px;
        padding: 0 14px; font-size: 12px; font-weight: 650;
    }}
    QDialog#feishuActionDialog QPushButton#feishuInlineSave:hover {{
        background: {consumer.accent_hover}; border-color: {consumer.accent_hover};
    }}
    """


class _FeishuElidedLabel(QLabel):
    """名称过长时以省略号收束，完整内容仍保留在悬停提示中。"""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = str(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(self._full_text)
        QLabel.setText(self, self._full_text)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(0, self.fontMetrics().height() + 2)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        available = max(24, self.width())
        rendered = self.fontMetrics().elidedText(
            self._full_text,
            Qt.ElideRight,
            available,
        )
        QLabel.setText(self, rendered)
        super().resizeEvent(event)


class _FeishuActionCard(QPushButton):
    """整张可点击、可键盘聚焦的消费级动作卡。"""

    actionTriggered = Signal(int, str)
    valueTriggered = Signal(int, str)

    def __init__(
        self,
        option: FeishuActionOption,
        *,
        index: int,
        consumer: T.ConsumerTheme,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("feishuActionCard")
        self.setProperty("tone", option.tone)
        self.setProperty("optionIndex", index)
        self.setProperty("passive", "false" if option.selectable else "true")
        self.setCursor(Qt.PointingHandCursor if option.selectable else Qt.ArrowCursor)
        self.setFocusPolicy(Qt.StrongFocus if option.selectable else Qt.NoFocus)
        self.setAutoDefault(False)
        self.setDefault(False)
        base_height = 74 if option.detail else 62
        self.setMinimumHeight(base_height + (56 if option.input_action else 0))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setAccessibleName(option.title)
        self.setAccessibleDescription(option.detail)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(13, 10, 13, 10)
        outer.setSpacing(10)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        icon_well = QFrame(self)
        icon_well.setObjectName("feishuActionIconWell")
        icon_well.setProperty("tone", option.tone)
        icon_well.setFixedSize(40, 40)
        icon_layout = QHBoxLayout(icon_well)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        if option.tone == "primary":
            icon_color = "#FFFFFF"
        else:
            icon_color = consumer.danger if option.tone == "danger" else consumer.accent
        icon_label = icons.icon_label(option.icon, icon_color, 18, consumer.icon_stroke)
        icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        icon_layout.addWidget(icon_label, 0, Qt.AlignCenter)
        row.addWidget(icon_well, 0, Qt.AlignVCenter)

        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(4)
        title_label = _FeishuElidedLabel(option.title, self)
        title_label.setObjectName("feishuActionTitle")
        title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        copy.addWidget(title_label)
        if option.detail:
            detail_label = _FeishuElidedLabel(option.detail, self)
            detail_label.setObjectName("feishuActionDetail")
            detail_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            copy.addWidget(detail_label)
        row.addLayout(copy, 1)

        if option.badge:
            badge = QLabel(option.badge, self)
            badge.setObjectName("feishuActionBadge")
            badge.setProperty(
                "badgeTone",
                "muted" if option.tone in {"standard", "danger"} else "accent",
            )
            badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            row.addWidget(badge, 0, Qt.AlignVCenter)
        if option.actions:
            for action in option.actions:
                action_button = QPushButton(self)
                action_button.setObjectName("feishuInlineAction")
                action_button.setProperty("action", action)
                action_button.setCursor(Qt.PointingHandCursor)
                action_button.setFocusPolicy(Qt.StrongFocus)
                action_button.setToolTip(
                    "重命名预设" if action == "rename" else "忘记此预设"
                )
                action_button.setAccessibleName(action_button.toolTip())
                action_button.setIcon(
                    QIcon(
                        icons.pixmap(
                            "pen" if action == "rename" else "x",
                            consumer.ink_muted if action == "rename" else consumer.danger,
                            14,
                        )
                    )
                )
                action_button.clicked.connect(
                    lambda _checked=False, selected_action=action: self.actionTriggered.emit(
                        index, selected_action
                    )
                )
                row.addWidget(action_button, 0, Qt.AlignVCenter)
        elif option.selectable:
            chevron_color = "#FFFFFF" if option.tone == "primary" else consumer.ink_muted
            chevron = icons.icon_label("chevron", chevron_color, 15, 1.8)
            chevron.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            row.addWidget(chevron, 0, Qt.AlignVCenter)
        outer.addLayout(row)

        if option.input_action:
            input_row = QHBoxLayout()
            input_row.setContentsMargins(52, 0, 0, 0)
            input_row.setSpacing(10)
            self.inline_input = QLineEdit(option.input_value, self)
            self.inline_input.setObjectName("feishuInlineInput")
            self.inline_input.setPlaceholderText(option.input_placeholder)
            self.inline_input.setAccessibleName(option.input_placeholder or "预设内容")
            input_row.addWidget(self.inline_input, 1)
            save_button = QPushButton("保存", self)
            save_button.setObjectName("feishuInlineSave")
            save_button.setCursor(Qt.PointingHandCursor)
            save_button.setAccessibleName("保存此预设")
            save_button.clicked.connect(
                lambda _checked=False: self.valueTriggered.emit(
                    index,
                    self.inline_input.text().strip(),
                )
            )
            self.inline_input.returnPressed.connect(save_button.click)
            input_row.addWidget(save_button)
            outer.addLayout(input_row)


class FeishuActionDialog(QDialog):
    """Lulu 飞书场景专用的分组动作选择器。"""

    def __init__(
        self,
        parent,
        title: str,
        subtitle: str,
        options: list[FeishuActionOption],
        *,
        cancel: str = "取消",
    ) -> None:
        super().__init__(parent)
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.selected_inline_action = "select"
        self._page_handler: Callable[[int, str], None] | None = None
        self._page_stack: list[tuple[str, str, list[FeishuActionOption], str, object]] = []
        self.setObjectName("feishuActionDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(700)
        self.setMaximumWidth(900)
        self.setMinimumHeight(760)
        self.setMaximumHeight(760)
        self.setStyleSheet(_feishu_action_qss(self._consumer))
        _unify_feishu_titlebar(self)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        canvas = PaperTextureFrame(
            self,
            strength=min(0.22, self._consumer.paper_texture_opacity * 1.35),
            layer="floating",
            base_color=self._consumer.canvas,
        )
        canvas.setObjectName("feishuDialogCanvas")
        shell.addWidget(canvas)
        root = QVBoxLayout(canvas)
        root.setContentsMargins(28, 36, 28, 20)
        root.setSpacing(16)

        back_row = QHBoxLayout()
        back_row.setContentsMargins(0, 0, 0, -4)
        self._back_button = QPushButton("‹  返回", self)
        self._back_button.setObjectName("feishuDialogBack")
        self._back_button.setCursor(Qt.PointingHandCursor)
        self._back_button.setAccessibleName("返回上一级")
        self._back_button.clicked.connect(self._go_back)
        self._back_button.setVisible(cancel == "返回")
        back_row.addWidget(self._back_button, 0, Qt.AlignLeft)
        back_row.addStretch(1)
        root.addLayout(back_row)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(12)
        rule = QFrame(self)
        rule.setObjectName("feishuDialogRule")
        rule.setFixedSize(3, 58)
        heading.addWidget(rule, 0, Qt.AlignTop)
        heading_copy = QVBoxLayout()
        heading_copy.setContentsMargins(0, 0, 0, 0)
        heading_copy.setSpacing(5)
        eyebrow = QLabel("飞书知识库", self)
        eyebrow.setObjectName("feishuDialogEyebrow")
        heading_copy.addWidget(eyebrow)
        self._title_label = QLabel(title, self)
        self._title_label.setObjectName("feishuDialogTitle")
        self._title_label.setWordWrap(True)
        heading_copy.addWidget(self._title_label)
        heading.addLayout(heading_copy, 1)
        self._header_action = QPushButton(self)
        self._header_action.setObjectName("feishuDialogHeaderAction")
        self._header_action.setCursor(Qt.PointingHandCursor)
        self._header_action_callback = None
        self._header_action.hide()
        heading.addWidget(self._header_action, 0, Qt.AlignTop)
        root.addLayout(heading)

        self._subtitle_label = QLabel(subtitle, self)
        self._subtitle_label.setObjectName("feishuDialogSubtitle")
        self._subtitle_label.setWordWrap(True)
        self._subtitle_label.setVisible(bool(subtitle))
        root.addWidget(self._subtitle_label)

        scroll = QScrollArea(self)
        scroll.setObjectName("feishuActionScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget(scroll)
        inner.setObjectName("feishuActionInner")
        content = QVBoxLayout(inner)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(16)

        scroll.setWidget(inner)
        self._scroll = scroll
        self._content = content
        self.option_cards: list[_FeishuActionCard] = []
        self._render_options(options)
        root.addWidget(scroll)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        self._cancel_button = QPushButton(cancel, self)
        self._cancel_button.setObjectName("feishuDialogCancel")
        self._cancel_button.setCursor(Qt.PointingHandCursor)
        self._cancel_button.clicked.connect(self.reject)
        self._cancel_button.setVisible(cancel != "返回")
        footer.addWidget(self._cancel_button)
        root.addLayout(footer)

        self._current_page = (title, subtitle, list(options), cancel, None)

        self.resize(780, 760)
        self._focus_first_option(options)

    def _render_options(self, options: list[FeishuActionOption]) -> None:
        while self._content.count():
            item = self._content.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        grouped: list[tuple[str, list[tuple[int, FeishuActionOption]]]] = []
        for index, option in enumerate(options):
            if grouped and grouped[-1][0] == option.group:
                grouped[-1][1].append((index, option))
            else:
                grouped.append((option.group, [(index, option)]))

        self.option_cards = []
        estimated_content_height = 0
        for group_name, group_options in grouped:
            panel = QFrame(self)
            panel.setObjectName("feishuActionGroup")
            panel.setProperty("groupName", group_name)
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(14, 14, 14, 14)
            panel_layout.setSpacing(16)
            if group_name:
                group_label = QLabel(group_name, panel)
                group_label.setObjectName("feishuActionGroupTitle")
                panel_layout.addWidget(group_label)
            for index, option in group_options:
                card = _FeishuActionCard(
                    option,
                    index=index,
                    consumer=self._consumer,
                    parent=panel,
                )
                if option.selectable:
                    card.clicked.connect(
                        lambda _checked=False, selected=index: self._activate_option(selected)
                    )
                card.actionTriggered.connect(self._finish_inline_action)
                card.valueTriggered.connect(self._finish_inline_value)
                panel_layout.addWidget(card)
                self.option_cards.append(card)
            self._content.addWidget(panel)
            estimated_content_height += 28 + (25 if group_name else 0)
            estimated_content_height += sum(
                74 if option.detail else 62 for _index, option in group_options
            )
            estimated_content_height += max(0, len(group_options) - 1) * 16
        self._content.addStretch(1)
        estimated_content_height += max(0, len(grouped) - 1) * 16
        layout_safety = (len(grouped) * 4) + 4
        # 预设数量只改变内部滚动内容，不再推动整个窗口继续长高。
        self._scroll.setFixedHeight(544)

    def _focus_first_option(self, options: list[FeishuActionOption]) -> None:
        first_selectable = next(
            (card for card, option in zip(self.option_cards, options) if option.selectable),
            None,
        )
        if first_selectable is not None:
            first_selectable.setFocus(Qt.OtherFocusReason)

    def set_flow_handler(self, handler: Callable[[int, str], None]) -> None:
        """让后续选择留在当前原生窗口内，由调用方决定切页或结束。"""

        self._page_handler = handler
        title, subtitle, options, cancel, _old = self._current_page
        self._current_page = (title, subtitle, options, cancel, handler)

    def set_header_action(self, text: str = "", callback=None) -> None:
        """在标题右侧提供当前页的主操作；切页时由调用方显式更新。"""

        previous = self._header_action_callback
        if callable(previous):
            try:
                self._header_action.clicked.disconnect(previous)
            except (RuntimeError, TypeError):
                pass
        self._header_action_callback = None
        clean = str(text or "").strip()
        self._header_action.setText(clean)
        self._header_action.setVisible(bool(clean))
        if clean and callable(callback):
            self._header_action.clicked.connect(callback)
            self._header_action_callback = callback

    def set_header_action_enabled(self, enabled: bool, text: str | None = None) -> None:
        self._header_action.setEnabled(bool(enabled))
        if text is not None:
            self._header_action.setText(str(text))

    def push_flow_page(
        self,
        title: str,
        subtitle: str,
        options: list[FeishuActionOption],
        handler: Callable[[int, str], None],
    ) -> None:
        self._page_stack.append(self._current_page)
        self._show_flow_page(title, subtitle, options, "返回", handler)

    def replace_flow_page(
        self,
        title: str,
        subtitle: str,
        options: list[FeishuActionOption],
        handler: Callable[[int, str], None],
        *,
        cancel: str = "关闭",
    ) -> None:
        self._show_flow_page(title, subtitle, options, cancel, handler)

    def _show_flow_page(
        self,
        title: str,
        subtitle: str,
        options: list[FeishuActionOption],
        cancel: str,
        handler: Callable[[int, str], None],
    ) -> None:
        self.setWindowTitle(title)
        self._title_label.setText(title)
        self._subtitle_label.setText(subtitle)
        self._subtitle_label.setVisible(bool(subtitle))
        self._back_button.setVisible(bool(self._page_stack) or cancel == "返回")
        self._cancel_button.setText(cancel)
        self._cancel_button.setVisible(cancel != "返回")
        self._page_handler = handler
        self._current_page = (title, subtitle, list(options), cancel, handler)
        self._render_options(options)
        self._focus_first_option(options)
        self.resize(max(self.width(), 780), 760)

    def _go_back(self) -> None:
        if not self._page_stack:
            self.reject()
            return
        title, subtitle, options, cancel, handler = self._page_stack.pop()
        self._show_flow_page(title, subtitle, options, cancel, handler)

    def _activate_option(self, index: int) -> None:
        if self._page_handler is not None:
            self._page_handler(index, "select")
            return
        self.done(index + 1)

    def _finish_inline_action(self, index: int, action: str) -> None:
        self.selected_inline_action = str(action)
        if self._page_handler is not None:
            self._page_handler(index, str(action))
            return
        self.done(index + 1)

    def _finish_inline_value(self, index: int, value: str) -> None:
        self._inline_value = str(value or "")
        if self._page_handler is not None:
            self._page_handler(index, "save")
            return
        self.done(index + 1)

    @property
    def inline_value(self) -> str:
        return str(getattr(self, "_inline_value", ""))


def choose_feishu_action(
    parent,
    title: str,
    subtitle: str,
    options: list[FeishuActionOption],
    *,
    cancel: str = "取消",
) -> int | None:
    """显示飞书专属动作选择器；点击卡片即返回原始索引。"""

    result = choose_feishu_action_result(
        parent,
        title,
        subtitle,
        options,
        cancel=cancel,
    )
    return result.index if result is not None else None


def choose_feishu_action_result(
    parent,
    title: str,
    subtitle: str,
    options: list[FeishuActionOption],
    *,
    cancel: str = "取消",
) -> FeishuActionResult | None:
    """返回卡片索引以及卡片内联动作，供预设管理页避免二次跳转。"""

    dialog = FeishuActionDialog(
        parent,
        title,
        subtitle,
        options,
        cancel=cancel,
    )
    result = dialog.exec()
    if result <= 0:
        return None
    return FeishuActionResult(
        index=result - 1,
        action=dialog.selected_inline_action,
    )


def _feishu_dialog_qss(consumer: T.ConsumerTheme) -> str:
    scope = 'QDialog[consumerThemeScope="coreWorkspace"][workspacePage="feishuSettings"]'
    return f"""
    {scope} {{ background: {T.ACTIVE.BG}; color: {consumer.ink}; }}
    {scope} QFrame#feishuCanvas {{ background: {T.ACTIVE.BG}; border: none; }}
    {scope} QFrame#consumerPageHeader {{ background: transparent; border: none; }}
    {scope} QFrame#studioTitleRule {{
        background: {consumer.accent}; border: none; border-radius: 2px;
    }}
    {scope} QLabel {{ background: transparent; color: {consumer.ink}; }}
    {scope} QLabel#pageTitle {{
        color: {consumer.ink}; font-family: "{consumer.font_display}";
        font-size: {consumer.type_page}px; font-weight: 650;
    }}
    {scope} QLabel#pageMeta {{ color: {consumer.ink_muted}; }}
    {scope} QFrame#feishuWorkbench {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {consumer.surface_focal}, stop:1 {consumer.surface_work});
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_work}px;
    }}
    {scope} QFrame#feishuSection {{
        background: {consumer.surface_focal}; border: 1px solid {consumer.line};
        border-radius: {consumer.radius_surface}px;
    }}
    {scope} QLineEdit, {scope} QFrame#tplField {{
        background: {consumer.surface_focal}; color: {consumer.ink};
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_control}px;
    }}
    {scope} QLineEdit:focus, {scope} QFrame#tplField:focus {{
        border-color: {consumer.accent};
    }}
    {scope} QPushButton#act, {scope} QPushButton#linkBtn {{
        background: {consumer.secondary}; color: {consumer.ink_soft};
        border: 1px solid transparent; border-radius: {consumer.radius_control}px;
    }}
    {scope} QPushButton#act:hover, {scope} QPushButton#linkBtn:hover {{
        background: {consumer.accent_wash}; color: {consumer.accent};
        border-color: {consumer.accent_hairline};
    }}
    {scope} QPushButton#grabBtn {{
        background: {consumer.accent}; color: #FFFFFF;
        border: 1px solid {consumer.accent}; border-radius: {consumer.radius_control}px;
        font-weight: 600;
    }}
    {scope} QPushButton#grabBtn:hover {{
        background: {consumer.accent_hover}; border-color: {consumer.accent_hover};
    }}
    {scope} QScrollArea, {scope} QScrollArea > QWidget > QWidget {{
        background: transparent; border: none;
    }}
    """


class _OAuthWorker(QThread):
    """开本地回调服务 + 浏览器授权，拿到 code 后换 token。"""
    done = Signal(bool, str)

    def run(self) -> None:
        import http.server
        import secrets as pysecrets
        import time
        import urllib.parse
        import webbrowser

        state = pysecrets.token_urlsafe(16)
        holder: dict = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                holder["code"] = (params.get("code") or [""])[0]
                holder["state"] = (params.get("state") or [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body style='font-family:sans-serif;text-align:center;padding-top:80px'>"
                    "<h2>✓ 授权成功</h2><p>可以关闭此页面，回到 Lulu。</p></body></html>".encode("utf-8"))

            def log_message(self, *a):  # 静音
                pass

        srv = chosen = None
        for port in feishu.OAUTH_PORTS:
            try:
                srv = http.server.HTTPServer(("127.0.0.1", port), H)
                chosen = port
                break
            except OSError:
                continue
        if srv is None:
            ports = "/".join(str(p) for p in feishu.OAUTH_PORTS)
            self.done.emit(False, f"本地回调端口 {ports} 都被占用，关掉占用它们的程序后重试。")
            return
        ru = feishu.redirect_uri(chosen)
        srv.timeout = 0.5
        try:
            webbrowser.open(feishu.authorize_url(state, ru))
            deadline = time.monotonic() + 180
            while (
                not holder.get("code")
                and time.monotonic() < deadline
                and not self.isInterruptionRequested()
            ):
                srv.handle_request()
        finally:
            srv.server_close()

        if self.isInterruptionRequested():
            return

        if not holder.get("code"):
            self.done.emit(False, "没收到授权码（可能超时、被取消，或重定向 URL 没在应用里配对）。")
            return
        if holder.get("state") != state:
            self.done.emit(False, "授权校验失败（state 不匹配），请重试。")
            return
        try:
            feishu.exchange_code(holder["code"], ru, cancelled=self.isInterruptionRequested)
            self.done.emit(True, "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))


class _SpacesWorker(QThread):
    done = Signal(bool, object)

    def run(self) -> None:
        try:
            self.done.emit(True, feishu.list_spaces())
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, str(exc))


def creation_destinations(cfg) -> list[dict]:
    """返回可新建 Base 的本地目的地预设；云盘根目录始终可用。"""

    raw_destinations = getattr(cfg, "feishu_creation_presets", []) or []
    destinations = [
        dict(item)
        for item in raw_destinations
        if isinstance(item, dict)
        and str(item.get("preset_id") or "")
        and str(item.get("destination_kind") or "") in {"drive", "wiki"}
    ]
    legacy = dict(
        (getattr(cfg, "feishu_creation_target", {}) or {})
        or (getattr(cfg, "feishu_target", {}) or {})
    )
    legacy_space_id = str(legacy.get("space_id") or "")
    if not raw_destinations and legacy_space_id and not any(
        item.get("destination_kind") == "wiki"
        and str(item.get("space_id") or "") == legacy_space_id
        and str(item.get("node_token") or "")
        == str(legacy.get("node_token") or "")
        for item in destinations
    ):
        from ..config import _normalize_feishu_creation_preset

        migrated = _normalize_feishu_creation_preset(
            {"destination_kind": "wiki", **legacy}
        )
        if migrated is not None:
            destinations.append(migrated)
    if not any(item.get("destination_kind") == "drive" for item in destinations):
        from ..config import FEISHU_DRIVE_ROOT_PRESET_ID

        destinations.insert(
            0,
            {
                "preset_id": FEISHU_DRIVE_ROOT_PRESET_ID,
                "destination_kind": "drive",
                "name": "云盘（我的文稿库）",
            },
        )
    return destinations


def preferred_creation_destination(cfg, *, context: str) -> dict:
    destinations = creation_destinations(cfg)
    preferences = getattr(cfg, "feishu_storage_preferences", {}) or {}
    key = (
        "created_selection_destination_id"
        if context in {"selection", "batch_new"}
        else "created_single_destination_id"
    )
    wanted = str(preferences.get(key) or "")
    if wanted:
        match = next(
            (
                item
                for item in destinations
                if str(item.get("preset_id") or "") == wanted
            ),
            None,
        )
        if match is not None:
            return dict(match)
    legacy = dict(
        (getattr(cfg, "feishu_creation_target", {}) or {})
        or (getattr(cfg, "feishu_target", {}) or {})
    )
    if legacy.get("space_id"):
        match = next(
            (
                item
                for item in destinations
                if item.get("destination_kind") == "wiki"
                and str(item.get("space_id") or "")
                == str(legacy.get("space_id") or "")
                and str(item.get("node_token") or "")
                == str(legacy.get("node_token") or "")
            ),
            None,
        )
        if match is not None:
            return dict(match)
    drive = next(
        (item for item in destinations if item.get("destination_kind") == "drive"),
        None,
    )
    return dict(drive or destinations[0])


def _creation_target(cfg) -> dict:
    """旧版飞书侧栏对话框只识别知识库；从新预设中取一个兼容值。"""

    current = getattr(cfg, "feishu_creation_target", {}) or {}
    if current.get("space_id"):
        return dict(current)
    return next(
        (
            dict(item)
            for item in creation_destinations(cfg)
            if item.get("destination_kind") == "wiki" and item.get("space_id")
        ),
        {},
    )


def _ensure_feishu_ready(parent, cfg) -> bool:
    if feishu.is_configured() and feishu.is_authorized():
        return True
    if not dialogs.confirm(
        parent,
        "连接飞书",
        "还没完成飞书登录授权。现在去配置？",
        ok="去配置",
        cancel="以后再说",
    ):
        return False
    from .feishu_connection import open_connection_settings
    open_connection_settings(parent)
    return False


def choose_feishu_destination(
    parent,
    cfg,
    *,
    context: str,
    suggested_name: str,
) -> dict | None:
    """四入口选择器：两个设置默认 + 两个仅作用本次的自定义。"""

    if not _ensure_feishu_ready(parent, cfg):
        return None
    from ..core.feishu_storage import preferred_library_target

    targets = [
        dict(item)
        for item in (getattr(cfg, "feishu_table_targets", []) or [])
        if isinstance(item, dict)
        and str(item.get("url") or "").strip()
        and item.get("status") != "unavailable"
    ]
    preferred = preferred_library_target(
        targets,
        getattr(cfg, "feishu_storage_preferences", {}),
        context=context,
    )
    preferred_destination = preferred_creation_destination(cfg, context=context)
    default_creation_name = str(
        preferred_destination.get("name") or "云盘（我的文稿库）"
    )
    default_existing_name = (
        str(preferred.get("name") or "飞书作品库")
        if preferred is not None
        else "暂未设置默认表格"
    )
    options = [
        FeishuActionOption(
            "使用默认表格",
            default_existing_name,
            icon="link",
            badge="默认" if preferred is not None else "未设置",
            group="已有表格",
            tone="recommended" if preferred is not None else "standard",
        ),
        FeishuActionOption(
            "临时输入已有表格",
            "粘贴一张已有多维表格链接，仅对本次保存生效",
            icon="plus",
            badge="仅本次",
            group="已有表格",
        ),
        FeishuActionOption(
            "使用默认位置",
            default_creation_name,
            icon="layers",
            badge="默认",
            group="新建表格",
            tone="recommended",
        ),
        FeishuActionOption(
            "临时选择新建位置",
            "选择云盘或知识库层级，仅对本次保存生效",
            icon="folder",
            badge="仅本次",
            group="新建表格",
        ),
    ]
    dialog = FeishuActionDialog(
        parent,
        "存入飞书作品库",
        "选择这次数据的去向。默认设置可在“设置－飞书知识库”中调整。",
        options,
    )
    selected: dict = {}

    def finish(value: dict | None) -> None:
        if value is None:
            return
        selected.update(value)
        dialog.accept()

    def choose_custom_location() -> None:
        destinations = creation_destinations(cfg)
        custom_options = [
            FeishuActionOption(
                str(item.get("name") or "云盘（我的文稿库）"),
                (
                    "直接创建在飞书云盘常规位置"
                    if item.get("destination_kind") == "drive"
                    else "已保存的知识库层级"
                ),
                icon="cloud" if item.get("destination_kind") == "drive" else "folder",
                badge="云盘" if item.get("destination_kind") == "drive" else "知识库",
                group="已保存位置",
                tone=(
                    "recommended"
                    if item.get("destination_kind") == "drive"
                    else "standard"
                ),
            )
            for item in destinations
        ]
        custom_options.append(
            FeishuActionOption(
                "选择其他知识库层级",
                "粘贴飞书知识库页面链接，仅对本次保存生效",
                icon="link",
                badge="临时",
                group="其他位置",
                tone="primary",
            )
        )

        def custom_handler(index: int, _action: str) -> None:
            if index < len(destinations):
                location = dict(destinations[index])
            else:
                node_url = dialogs.prompt(
                    dialog,
                    "选择知识库层级",
                    "在飞书中打开目标层级页面，粘贴浏览器地址栏中的完整链接。",
                    ok="验证并使用",
                    placeholder="https://example.feishu.cn/wiki/...",
                )
                if not node_url:
                    return
                try:
                    node = feishu.resolve_node(node_url)
                except Exception as exc:  # noqa: BLE001 - 用户主动选择需明确反馈
                    dialogs.alert(dialog, "层级无法使用", str(exc))
                    return
                location = {
                    "preset_id": "",
                    "destination_kind": "wiki",
                    "space_id": str(node.get("space_id") or ""),
                    "node_token": str(node.get("node_token") or ""),
                    "name": str(node.get("title") or "指定知识库层级"),
                }
            finish(
                _prompt_new_table_selection(
                    dialog,
                    location,
                    context=context,
                    suggested_name=suggested_name,
                    temporary=True,
                )
            )

        dialog.push_flow_page(
            "新建表格导入｜自定义",
            "选择这一次的新建位置，不会改动设置中的默认项。",
            custom_options,
            custom_handler,
        )

    def root_handler(choice: int, _action: str) -> None:
        if choice == 0:
            if preferred is not None:
                finish(
                    {
                        "mode": "existing",
                        "source_kind": str(
                            preferred.get("source_kind") or "existing"
                        ),
                        "name": str(preferred.get("name") or "飞书作品库"),
                        "url": str(preferred.get("url") or ""),
                        "target_id": str(preferred.get("target_id") or ""),
                        "schema_version": preferred.get("schema_version"),
                        "context": context,
                        "temporary": False,
                    }
                )
                return
            dialogs.alert(
                dialog,
                "还没有默认表格",
                "这次请填写一张已有多维表格；需要长期默认时，可到“设置－飞书知识库”保存。",
            )
            finish(
                _prompt_existing_table_selection(
                    dialog,
                    context=context,
                    temporary=True,
                )
            )
        elif choice == 1:
            finish(
                _prompt_existing_table_selection(
                    dialog,
                    context=context,
                    temporary=True,
                )
            )
        elif choice == 2:
            finish(
                _prompt_new_table_selection(
                    dialog,
                    preferred_destination,
                    context=context,
                    suggested_name=suggested_name,
                    temporary=False,
                )
            )
        else:
            choose_custom_location()

    dialog.set_flow_handler(root_handler)
    result = dialog.exec()
    return selected if result > 0 and selected else None


def _prompt_new_table_selection(
    parent,
    location: dict,
    *,
    context: str,
    suggested_name: str,
    temporary: bool,
) -> dict | None:
    name = dialogs.prompt(
        parent,
        "创建标准作品库",
        (
            f"将在「{location.get('name') or '云盘（我的文稿库）'}」新建一张多维表格。"
            "Lulu 会自动创建标准作品字段并导入本次数据。"
        ),
        text=suggested_name or "Lulu 作品库",
        ok="创建并保存",
        placeholder="例如：AI 口播对标库",
    )
    name = str(name or "").strip()
    if not name:
        return None
    return {
        "mode": "new",
        "source_kind": "created",
        "name": name,
        "destination_kind": str(location.get("destination_kind") or "drive"),
        "destination_preset_id": str(location.get("preset_id") or ""),
        "space_id": str(location.get("space_id") or ""),
        "node_token": str(location.get("node_token") or ""),
        "location_name": str(location.get("name") or "云盘（我的文稿库）"),
        "schema_version": 1,
        "context": context,
        "temporary": temporary,
    }


def _choose_custom_creation_destination(parent, cfg) -> dict | None:
    destinations = creation_destinations(cfg)
    options = [
        FeishuActionOption(
            str(item.get("name") or "云盘（我的文稿库）"),
            (
                "直接创建在飞书云盘常规位置"
                if item.get("destination_kind") == "drive"
                else "已保存的知识库层级"
            ),
            icon="cloud" if item.get("destination_kind") == "drive" else "folder",
            badge="云盘" if item.get("destination_kind") == "drive" else "知识库",
            group="已保存位置",
            tone="recommended" if item.get("destination_kind") == "drive" else "standard",
        )
        for item in destinations
    ]
    options.append(
        FeishuActionOption(
            "选择其他知识库层级",
            "粘贴飞书知识库页面链接，仅对本次保存生效",
            icon="link",
            badge="临时",
            group="其他位置",
            tone="primary",
        )
    )
    picked = choose_feishu_action(
        parent,
        "新建表格导入｜自定义",
        "选择这一次的新建位置，不会改动设置中的默认项。",
        options,
    )
    if picked is None:
        return None
    if picked < len(destinations):
        return dict(destinations[picked])
    node_url = dialogs.prompt(
        parent,
        "选择知识库层级",
        "在飞书中打开目标层级页面，粘贴浏览器地址栏中的完整链接。",
        ok="验证并使用",
        placeholder="https://example.feishu.cn/wiki/...",
    )
    if not node_url:
        return None
    try:
        node = feishu.resolve_node(node_url)
    except Exception as exc:  # noqa: BLE001 - 用户主动选择的远端位置需明确反馈
        dialogs.alert(parent, "层级无法使用", str(exc))
        return None
    return {
        "preset_id": "",
        "destination_kind": "wiki",
        "space_id": str(node.get("space_id") or ""),
        "node_token": str(node.get("node_token") or ""),
        "name": str(node.get("title") or "指定知识库层级"),
    }


def _prompt_existing_table_selection(
    parent,
    *,
    context: str,
    temporary: bool,
) -> dict | None:
    url = dialogs.prompt(
        parent,
        "使用已有多维表格",
        "在飞书中打开要长期积累的具体数据表，复制浏览器地址栏里的完整链接。Lulu 会先验表，不会自动改动表结构。",
        ok="继续",
        placeholder="https://example.feishu.cn/base/...?...table=tbl...",
    )
    url = str(url or "").strip()
    if not url:
        return None
    alias = dialogs.prompt(
        parent,
        "作品库名称",
        "这个名称只用于 Lulu 中识别分类，不会改动飞书里的表名。",
        text="已有飞书作品库",
        ok="使用这张表",
        placeholder="例如：美妆对标库",
    )
    alias = str(alias or "").strip()
    if not alias:
        return None
    return {
        "mode": "existing",
        "source_kind": "existing",
        "name": alias,
        "url": url,
        "target_id": "",
        "schema_version": None,
        "context": context,
        "temporary": temporary,
    }


def remember_feishu_destination(cfg, selection: dict, worker, receipt) -> bool:
    """在云端结果已确认后原子登记作品库；失败则恢复内存旧值。"""

    if bool(selection.get("temporary")):
        worker.registered_target = None
        return True

    from ..core.feishu_storage import make_library_target, upsert_library_target

    app_token = str(getattr(receipt, "destination_app_token", "") or "")
    table_id = str(getattr(receipt, "destination_table_id", "") or "")
    url = str(getattr(receipt, "url", "") or selection.get("url") or "")
    if not url or not (table_id or selection.get("mode") == "existing"):
        return False
    name = str(
        selection.get("name")
        or getattr(receipt, "destination_table_name", "")
        or "飞书作品库"
    )
    target = make_library_target(
        name=name,
        url=url,
        app_token=app_token,
        table_id=table_id,
        status="ready",
        source_kind=(
            "created"
            if str(selection.get("source_kind") or "") == "created"
            else "existing"
        ),
    )
    schema_version = getattr(worker, "schema_version", 1)
    if isinstance(schema_version, int) and not isinstance(schema_version, bool):
        target["schema_version"] = max(0, schema_version)
    if target["schema_version"] == 0:
        target["template_id"] = "lulu.legacy-collection"
    target["last_used_at"] = datetime.now(timezone.utc).isoformat()
    previous_targets = deepcopy(getattr(cfg, "feishu_table_targets", []) or [])
    previous_preferences = deepcopy(
        getattr(cfg, "feishu_storage_preferences", {}) or {}
    )
    selected_target_id = str(selection.get("target_id") or "")
    update_targets = [
        item
        for item in previous_targets
        if str((item or {}).get("target_id") or "") != selected_target_id
    ]
    cfg.feishu_table_targets = upsert_library_target(update_targets, target)
    preferences = dict(previous_preferences)
    context = str(selection.get("context") or "single")
    context_key = "selection_target_id" if context == "selection" else "single_target_id"
    preferences[context_key] = target["target_id"]
    existing_key = f"existing_{context_key}"
    if not str(preferences.get(existing_key) or ""):
        preferences[existing_key] = target["target_id"]
    preferences["recent_target_id"] = target["target_id"]
    cfg.feishu_storage_preferences = preferences
    try:
        saved = bool(cfg.save())
    except Exception:  # noqa: BLE001
        saved = False
    if not saved:
        cfg.feishu_table_targets = previous_targets
        cfg.feishu_storage_preferences = previous_preferences
    worker.registered_target = target if saved else None
    return saved


def _archive_cover_preflight(records: tuple[object, ...]):
    """只读取 Lulu 受管封面；远端封面链接不会在保存动作里临时下载。"""

    from ..core.collect import export

    candidates = []
    skipped = []
    for index, record in enumerate(records):
        relative = str(getattr(record, "cover_local_path", "") or "")
        if not relative:
            continue
        path = export.resolve_managed_cover_file(relative)
        if path is None:
            skipped.append(
                export.BitableCoverIssue(
                    index,
                    str(getattr(record, "work_key", "") or f"row-{index + 1}"),
                    str(getattr(record, "title", ""))[:120],
                    "cover_file_rejected",
                    "回到作品详情重新保存封面后再试",
                )
            )
            continue
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        mime = export._cover_payload_mime(path, data)
        if not mime or len(data) > export._BITABLE_COVER_MAX_BYTES:
            skipped.append(
                export.BitableCoverIssue(
                    index,
                    str(getattr(record, "work_key", "") or f"row-{index + 1}"),
                    str(getattr(record, "title", ""))[:120],
                    "cover_type_mismatch",
                    "重新保存为 JPG、PNG 或 WebP 后再试",
                )
            )
            continue
        identity = str(getattr(record, "work_key", "") or f"row-{index + 1}")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        candidates.append(
            export.BitableCoverCandidate(
                index,
                identity,
                str(getattr(record, "title", ""))[:120],
                f"cover-{digest}{path.suffix.casefold()}",
                mime,
                data,
            )
        )
    return tuple(candidates), tuple(skipped)


class FeishuStorageWorker(QThread):
    """统一写入作品库；单条只是只有一行的批次。"""

    done = Signal(bool, object)

    def __init__(self, records: Iterable[object], selection: dict, result_title: str):
        super().__init__()
        self.records = tuple(records)
        self.selection = dict(selection)
        self.result_title = str(result_title or "Lulu 作品库")
        self.resolved_target = None
        self.registered_target = None
        self.schema_version = 1
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def run(self) -> None:
        from ..core.collect import export
        from ..core import feishu_storage

        try:
            deduped = feishu_storage.dedupe_archive_records(self.records)
            fields = list(feishu_storage.STANDARD_BITABLE_FIELDS)
            allowed_fields = feishu_storage.STANDARD_LIBRARY_TEMPLATE_V1.field_names
            if self.selection.get("mode") == "existing":
                try:
                    self.resolved_target = feishu.resolve_existing_bitable(
                        str(self.selection.get("url") or ""),
                        fields,
                    )
                except feishu.BitableTargetError:
                    # 老版本 Lulu 创建的 13 字段表继续可用；只写它确实拥有的字段。
                    if self.selection.get("schema_version") != 0:
                        raise
                    fields = list(export.BITABLE_FIELDS)
                    self.schema_version = 0
                    allowed_fields = export.BITABLE_HEADERS
                    self.resolved_target = feishu.resolve_existing_bitable(
                        str(self.selection.get("url") or ""),
                        fields,
                    )

            def perform_write(current_deduped):
                records = current_deduped.records
                if not records and self.resolved_target is not None:
                    return feishu.BitableExportReceipt(
                        url=self.resolved_target.url,
                        row_total=0,
                        row_success=0,
                        row_failed=0,
                        row_unconfirmed=0,
                        row_cancelled=0,
                        cover_candidate=0,
                        cover_success=0,
                        cover_skipped=0,
                        cover_failed=0,
                        cover_uncertain=0,
                        cancelled=False,
                        row_duplicate_skipped=current_deduped.duplicate_count,
                        destination_app_token=self.resolved_target.app_token,
                        destination_table_id=self.resolved_target.table_id,
                        destination_table_name=self.resolved_target.table_name,
                    )
                job_id = str(uuid4())
                payloads = [
                    feishu_storage.record_to_bitable_record(
                        record,
                        allowed_fields=allowed_fields,
                        write_id=f"{job_id}:{index}",
                    )
                    for index, record in enumerate(records)
                ]
                covers, cover_issues = _archive_cover_preflight(records)
                receipt = feishu.export_collection_to_new_bitable(
                    str(self.selection.get("space_id") or ""),
                    str(self.selection.get("node_token") or ""),
                    self.result_title,
                    "作品库",
                    fields,
                    payloads,
                    covers,
                    cover_issues,
                    destination_kind=str(
                        self.selection.get("destination_kind") or "wiki"
                    ),
                    existing_target=self.resolved_target,
                    is_cancelled=lambda: self._cancel or self.isInterruptionRequested(),
                )
                return replace(
                    receipt,
                    row_duplicate_skipped=current_deduped.duplicate_count,
                )

            if self.resolved_target is not None:
                with feishu._bitable_table_write_lock(
                    self.resolved_target.app_token,
                    self.resolved_target.table_id,
                ):
                    if self.schema_version == 1:
                        existing_urls = feishu.find_existing_bitable_text_values(
                            self.resolved_target.app_token,
                            self.resolved_target.table_id,
                            "原作品链接",
                            (record.work_url for record in deduped.records),
                        )
                        deduped = feishu_storage.dedupe_archive_records(
                            self.records,
                            existing_work_urls=existing_urls,
                        )
                    receipt = perform_write(deduped)
            else:
                receipt = perform_write(deduped)
            self.done.emit(True, receipt)
        except Exception as exc:  # noqa: BLE001
            safe_types = (
                feishu.FeishuApiError,
                feishu.FeishuTransportError,
                feishu.BitablePartialWriteError,
                feishu.BitableTargetError,
            )
            message = (
                str(exc)
                if isinstance(exc, safe_types)
                else "飞书作品库写入未完成，请稍后重试。"
            )
            self.done.emit(False, message)


# 旧名称保留给历史调用；产品入口使用统一保存面板。
FeishuUploadWorker = FeishuStorageWorker


def send_records_to_feishu(
    parent,
    cfg,
    records: Iterable[object],
    *,
    title: str,
    context: str = "single",
    toast=None,
):
    """All entry points share one bounded form and background save session."""
    from .feishu_save_dialog import FeishuSaveSession
    frozen = tuple(records)
    if not frozen:
        return None
    active = getattr(parent, "_lulu_feishu_storage_worker", None)
    if active is not None and active.isRunning():
        active.dialog.raise_() if hasattr(active, "dialog") else None
        if toast: toast("上一份飞书存储还在进行，请稍候…")
        return active
    session = FeishuSaveSession(parent, cfg, frozen, title, context, toast)
    if parent is not None:
        parent._lulu_feishu_storage_worker = session
        def release():
            if getattr(parent, "_lulu_feishu_storage_worker", None) is session:
                parent._lulu_feishu_storage_worker = None
        session.finished.connect(release)
    session.start()
    return session


def send_to_feishu(
    parent,
    cfg,
    transcript,
    title: str,
    *,
    summary: str | None = None,
    text_override: str | None = None,
    toast=None,
):
    """单篇入口：完整文稿与作品事实交给统一保存面板。"""

    from ..core.feishu_storage import record_from_transcript

    captured_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    record = record_from_transcript(
        transcript,
        title=title,
        text_override=text_override,
        summary=summary,
        captured_at_ms=captured_at_ms,
    )
    return send_records_to_feishu(
        parent,
        cfg,
        (record,),
        title=title,
        context="single",
        toast=toast,
    )


class FeishuConfigDialog(QDialog):
    """Legacy compatibility class; product entry points route to Settings."""
    def __init__(self, parent, cfg):
        super().__init__(parent)
        from .feishu_connection import FeishuConnectionPanel
        self.setWindowTitle('连接飞书')
        _unify_feishu_titlebar(self)
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(660, screen.width()-48), min(680, screen.height()-64))
        self.setMinimumSize(480, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.panel = FeishuConnectionPanel(cfg, self)
        layout.addWidget(self.panel)
        done = QPushButton('完成')
        done.clicked.connect(self.accept)
        layout.addWidget(done, 0, Qt.AlignRight)

    def reject(self):
        if self.panel.busy:
            self.panel.notice.setText('请先完成或取消当前系统授权。')
            return
        super().reject()

    def accept(self):
        if not self.panel.busy:
            super().accept()
