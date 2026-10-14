"""Lulu 宣纸的统一配色、字体、圆角、阴影与全局样式。

保留旧控件使用的配色接口；所有旧主题名称均回到默认宣纸。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget


def asset(name: str) -> str:
    """定位打包/开发两种环境下的 ui/assets 资源（如 logo.png）。"""
    here = Path(__file__).resolve().parent / "assets" / name
    if here.exists():
        return str(here)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for c in (Path(base) / "src" / "ui" / "assets" / name, Path(base) / "assets" / name):
            if c.exists():
                return str(c)
    return str(here)


# ——— 字体（系统无 Noto 时回退；不随主题变）———
SERIF = "Songti SC"      # 标题：温润衬线
SANS = "PingFang SC"     # 正文：系统无衬线


# ——— 调色板 ———
# 配色名称沿用现有控件接口（BG/INK/TERRA…），统一采用宣纸取值。
@dataclass(frozen=True)
class Palette:
    name: str
    BG: str          # 全窗口统一背景（侧栏/主区/右栏同色）
    BG_DEEP: str     # 极轻底：分段控件 / hover
    CARD: str        # 悬浮卡片（surface）
    INK: str         # 主文字
    INK_SOFT: str    # 次文字
    INK_FAINT: str   # 弱文字
    LINE: str        # 分隔线 + 卡片描边
    LINE_SOFT: str   # 更淡的次级分隔
    TERRA: str       # 默认配色的主强调色
    TERRA_SOFT: str  # 浅陶土（虚线框/禁用）
    TERRA_WASH: str  # 极浅底
    TERRA_DEEP: str  # 渐变深端
    OLIVE: str
    BLUE: str
    BLUE_WASH: str
    GREEN: str
    GREEN_WASH: str
    AMBER: str
    AMBER_WASH: str
    DANGER: str        # 危险/失败红（✗ 未转录、错误前景等）
    SHADOW_RGB: tuple  # 投影颜色 (r,g,b)，alpha 由 shadow() 传入
    # 保留原界面中渐变、悬停及状态提示使用的宣纸配色。
    GRAD_TOP: str         # 新建按钮渐变顶端
    GRAD_HOVER_TOP: str   # 新建按钮 hover 渐变顶端
    GRAD_HOVER_BOT: str   # 新建按钮 hover 渐变底端
    NAV_HOVER: str        # 侧栏未选中项 hover 底
    RAIL_HOVER: str       # 侧栏折叠按钮 hover 底
    SCROLL_HANDLE: str    # 滚动条滑块
    SCROLL_HANDLE_HOVER: str
    DROP_BG: str          # 拖拽区底色
    HOVER_RAISE: str      # 输入/字段 hover·focus 抬升底（暖陶土下=纯白）
    TOOLTIP_BG: str       # 气泡提示底
    TOOLTIP_FG: str       # 气泡提示字


@dataclass(frozen=True)
class ConsumerTheme:
    """逐页接入的消费级语义层；不替换现有 Palette 基线。"""

    name: str
    canvas: str
    surface: str
    surface_alt: str
    ink: str
    ink_soft: str
    ink_muted: str
    line: str
    accent: str
    accent_hover: str
    accent_wash: str
    accent_hairline: str
    accent_hairline_active: str
    danger: str
    secondary: str
    secondary_hover: str
    paper_edge: str
    radius_compact: int = 6
    radius_control: int = 8
    radius_surface: int = 12
    radius_work: int = 16
    radius_floating: int = 12
    border_width: int = 1
    motion_fast_ms: int = 160
    motion_standard_ms: int = 240
    spacing: tuple[int, ...] = (4, 8, 12, 16, 24, 32)
    font_display: str = SERIF
    font_body: str = SANS
    type_page: int = 24
    type_section: int = 16
    type_card: int = 14
    type_body: int = 12
    type_caption: int = 11
    icon_feature: int = 20
    icon_control: int = 16
    icon_compact: int = 13
    icon_stroke: float = 1.8
    control_shadow_alpha: int = 0
    popup_shadow_alpha: int = 10
    popup_max_height: int = 260
    paper_canvas_opacity: float = 0.0
    paper_texture_opacity: float = 0.0
    paper_rail_opacity: float = 0.0
    paper_queue_opacity: float = 0.0

    @property
    def surface_work(self) -> str:
        """承载相关任务的大工作面；只复用已确认的弱纸面色。"""
        return self.surface_alt

    @property
    def surface_focal(self) -> str:
        """当前可操作内容卡。"""
        return self.surface

    @property
    def surface_floating(self) -> str:
        """菜单等脱离文档流的浮层。"""
        return self.surface

    @property
    def focus_ring(self) -> str:
        """Keyboard-only focus ring; deliberately independent from accent."""
        return self.ink_muted

    @property
    def calendar_weekend(self) -> str:
        return self.danger

    @property
    def calendar_disabled(self) -> str:
        return self.ink_muted

    @property
    def popup_scroll_handle(self) -> str:
        return self.accent_hairline_active

    @property
    def task_group_border(self) -> str:
        return "#B7AC9A"

    @property
    def task_group_warning(self) -> str:
        return "#8A5A00"

    def task_group_status_color(self, kind: str) -> str:
        mapping = {
            "neutral": self.ink_muted,
            "info": self.accent,
            "success": "#6E8B5E",
            "warning": self.task_group_warning,
            "error": self.danger,
        }
        return mapping.get(kind, mapping["neutral"])

    @property
    def task_group_shadow(self) -> tuple[int, int, int, int, int, int]:
        return (43, 38, 34, 31, 14, 3)


# 暖陶土：现状基线，色值与重构前逐字一致（像素级不变）。
WARM = Palette(
    name="warm",
    BG="#F8F6F2", BG_DEEP="#EFEDE7", CARD="#FFFFFF",
    INK="#2B2622", INK_SOFT="#6B6258", INK_FAINT="#A89D8D",
    LINE="#E6E1D8", LINE_SOFT="#EFEBE3",
    TERRA="#D17B53", TERRA_SOFT="#E7B49C", TERRA_WASH="#F7EAE1", TERRA_DEEP="#C46A40",
    OLIVE="#8A8259", BLUE="#5B7A8C", BLUE_WASH="#E5EDF0",
    GREEN="#6E8B5E", GREEN_WASH="#E8EFE2", AMBER="#C28A2E", AMBER_WASH="#F6EDD9",
    DANGER="#C9503C",
    SHADOW_RGB=(43, 38, 34),
    GRAD_TOP="#DC8A63", GRAD_HOVER_TOP="#E29871", GRAD_HOVER_BOT="#D9805A",
    NAV_HOVER="rgba(255,255,255,0.55)", RAIL_HOVER="rgba(43,38,34,0.07)",
    SCROLL_HANDLE="rgba(43,38,34,0.15)", SCROLL_HANDLE_HOVER="rgba(43,38,34,0.30)",
    DROP_BG="rgba(246,231,221,0.45)", HOVER_RAISE="#FFFFFF",
    TOOLTIP_BG="#2B2622", TOOLTIP_FG="#FFFFFF",
)

THEMES = {"warm": WARM}
DEFAULT_THEME = "warm"


# 新暖纸消费级语义层。只有显式声明 consumerThemeScope 的页面会使用，
# 既有 Palette 数值与未迁移页面的像素表现保持不变。
CONSUMER_THEMES = {
    "warm": ConsumerTheme(
        name="warm",
        canvas="#F5EFE4", surface="#FBF7EE", surface_alt="#EFE8DB",
        ink="#2A2622", ink_soft="#4A433C", ink_muted="#6B6158", line="#D8CFBE",
        accent="#537D96", accent_hover="#3F6179", accent_wash="rgba(83,125,150,0.08)",
        accent_hairline="rgba(83,125,150,0.38)",
        accent_hairline_active="rgba(83,125,150,0.68)",
        danger="#8B2C1F",
        secondary="rgba(42,38,34,0.03)", secondary_hover="rgba(42,38,34,0.08)",
        paper_edge="#FFFCF5",
        paper_canvas_opacity=0.14,
        paper_texture_opacity=0.135,
        paper_rail_opacity=0.09,
        paper_queue_opacity=0.085,
    ),

}


def consumer_theme(name: str | None = None) -> ConsumerTheme:
    """返回消费级语义主题；未知名称与空值都安全回退到暖纸。"""
    return CONSUMER_THEMES.get(name or DEFAULT_THEME, CONSUMER_THEMES[DEFAULT_THEME])


def consumer_qcolor(value: str) -> QColor:
    """Parse consumer CSS colors for custom-paint widgets without losing alpha."""
    text = str(value).strip()
    if text.startswith("rgba(") and text.endswith(")"):
        parts = [part.strip() for part in text[5:-1].split(",")]
        if len(parts) == 4:
            try:
                color = QColor(*(max(0, min(255, int(part))) for part in parts[:3]))
                alpha = float(parts[3])
                color.setAlphaF(max(0.0, min(1.0, alpha)))
                return color
            except (TypeError, ValueError):
                pass
    color = QColor(text)
    return color if color.isValid() else QColor(0, 0, 0, 0)


def build_consumer_global_shell_qss(t: ConsumerTheme, p: Palette) -> str:
    """生成共享外壳的显式范围样式，不触及任何页面根节点。"""
    scope = '[consumerThemeScope="globalShell"]'
    return f"""

/* ===== Lulu consumer theme: explicitly opted-in global shell only ===== */
QFrame#sidebar{scope} {{
    background: {t.surface_work}; border: none; border-right: {t.border_width}px solid {t.line};
}}
QFrame#titlebarPaperLeft{scope} {{
    background: {t.surface_work}; border: none; border-right: {t.border_width}px solid {t.line};
}}
/* These bands continue the actual Batch-1 column canvas, not a floating card. */
QFrame#titlebarPaperCenter{scope} {{ background: {p.BG}; border: none; }}

QWidget#brandRow{scope}, QLabel#brandLogo{scope} {{
    background: transparent; border: none;
}}
QFrame#sidebar{scope} QLabel#navLabel {{
    color: {t.ink_muted}; background: transparent;
}}
QFrame#sidebar{scope} QFrame[luluControl="navRow"] {{
    background: transparent; border: none; border-left: 2px solid transparent;
    border-radius: {t.radius_control}px;
}}
QFrame#sidebar{scope} QFrame[luluControl="navRow"]:hover {{
    background: {t.secondary_hover};
}}
QFrame#sidebar{scope} QFrame[luluControl="navRow"][consumerAccent="true"] {{
    background: {t.accent_wash}; border-left-color: {t.accent};
}}
QFrame#sidebar{scope} QFrame[luluControl="navRow"]:focus {{
    border: {t.border_width}px solid {t.accent_hairline_active};
}}
QFrame#sidebar{scope} QLabel#navText {{ color: {t.ink_soft}; background: transparent; }}
QFrame#sidebar{scope} QLabel#navTextActive {{
    color: {t.accent}; background: transparent; font-weight: 600;
}}

QPushButton{scope}[luluControl="button"][controlVariant="pane"],
QPushButton{scope}[luluControl="button"][controlVariant="settings"] {{
    background: transparent; border: 2px solid transparent;
    border-radius: {t.radius_compact + 1}px; padding: 0;
}}
QPushButton{scope}[luluControl="button"][controlVariant="pane"]:hover,
QPushButton{scope}[luluControl="button"][controlVariant="settings"]:hover {{
    background: {t.secondary_hover}; border-color: transparent;
}}
QPushButton{scope}[luluControl="button"][controlVariant="pane"]:pressed,
QPushButton{scope}[luluControl="button"][controlVariant="settings"]:pressed,
QPushButton{scope}[luluControl="button"][controlVariant="settings"]:checked:pressed {{
    background: {t.accent_wash}; border-color: transparent;
}}
QPushButton{scope}[luluControl="button"][controlVariant="pane"]:focus,
QPushButton{scope}[luluControl="button"][controlVariant="settings"]:focus {{
    background: {t.secondary}; border: 2px solid {t.accent_hairline_active};
}}
QLabel#windowTitleLabel{scope} {{
    color: {t.ink_soft}; background: transparent; border: none;
    font-family: "{t.font_body}"; font-size: {t.type_body}px; font-weight: 600;
}}
"""


def build_consumer_core_workspace_qss(t: ConsumerTheme) -> str:
    """生成 Batch 2 核心工作页的显式范围样式，不触及共享外壳或其他页面。"""
    scope = '[consumerThemeScope="coreWorkspace"]'
    workspace_canvas = THEMES.get(t.name, WARM).BG
    return f"""

/* ===== Lulu consumer theme: explicitly opted-in Batch 2 workspaces only ===== */
QWidget#root{scope} {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 {t.surface_focal}, stop: 0.46 {t.canvas}, stop: 1 {t.surface_work}
    );
    color: {t.ink};
}}
QWidget#root{scope} QScrollArea,
QWidget#root{scope} QScrollArea > QWidget > QWidget {{
    background: transparent; color: {t.ink};
}}
QWidget#root{scope} QFrame#coreCanvas {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 {t.surface_focal}, stop: 0.46 {t.canvas}, stop: 1 {t.surface_work}
    );
    border: none;
}}
/* 原生工作台沿用文案提取标题栏的纸色。
   选择器必须带 workspacePage，避免反向命中文案提取母版。 */
QWidget#root{scope}[workspacePage="voice"],
QWidget#root{scope}[workspacePage="liveClips"],
QWidget#root{scope}[workspacePage="collection"],
QWidget#root{scope}[workspacePage="voice"] QFrame#coreCanvas,
QWidget#root{scope}[workspacePage="liveClips"] QFrame#coreCanvas,
QWidget#root{scope}[workspacePage="collection"] QFrame#coreCanvas {{
    background: {workspace_canvas}; border: none;
}}
QWidget#root{scope} QFrame#consumerPageHeader {{ background: transparent; border: none; }}
QWidget#root{scope} QFrame#studioTitleRule {{
    background: {t.accent}; border: none; border-radius: 2px;
}}
QWidget#root{scope} QLabel {{
    background: transparent; color: {t.ink}; font-family: "{t.font_body}";
}}
QWidget#root{scope} QLabel#pageTitle {{
    color: {t.ink}; font-family: "{t.font_display}";
    font-size: {t.type_page}px; font-weight: 650;
}}
QWidget#root{scope} QLabel#pageMeta,
QWidget#root{scope} QLabel#voicePageSubtitle,
QWidget#root{scope} QLabel#voiceSub,
QWidget#root{scope} QLabel#taskSrc,
QWidget#root{scope} QLabel#taskSub,
QWidget#root{scope} QLabel#pickedMeta {{
    color: {t.ink_muted}; font-size: {t.type_caption}px;
}}
QWidget#root{scope} QLabel#sectionTitle,
QWidget#root{scope} QLabel#voiceSectionTitle {{
    color: {t.ink}; font-family: "{t.font_display}";
    font-size: {t.type_section}px; font-weight: 650;
}}
QWidget#root{scope} QLabel#taskName,
QWidget#root{scope} QLabel#pickedTitle,
QWidget#root{scope} QLabel#voiceResultTitle {{
    color: {t.ink}; font-size: {t.type_card}px; font-weight: 600;
}}

QWidget#root{scope} QFrame[luluControl="surface"][surface="page"] {{
    background: {t.canvas}; border: none; border-radius: 0;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surface="card"],
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="resultCanvas"],
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="sourcePicker"] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_surface}px;
}}
/* The envelope paints its own rounded paper.  Remove the generic card below it,
   otherwise the square antialiased corners leak through as a pale wire frame. */
QWidget#root{scope}[workspacePage="liveClips"]
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"],
QWidget#root{scope}[workspacePage="voice"]
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"],
QWidget#root{scope}[workspacePage="liveClips"]
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"]:hover,
QWidget#root{scope}[workspacePage="voice"]
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"]:hover,
QWidget#root{scope}[workspacePage="liveClips"]
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"]:focus,
QWidget#root{scope}[workspacePage="voice"]
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"]:focus {{
    background: transparent; border: none; border-radius: 16px;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="coreWorkbench"],
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="tableWorkspace"] {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 {t.surface_focal}, stop: 1 {t.surface_work}
    );
    border: {t.border_width}px solid {t.line}; border-radius: {t.radius_work}px;
}}


QWidget#root{scope} QFrame[luluControl="surface"][surface="section"] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_surface}px;
}}

QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="resultBody"],
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="resultToolbar"] {{
    background: {t.surface_work}; border: none; border-radius: {t.radius_control}px;
}}

QWidget#root{scope}[workspacePage="liveClips"]
QFrame[luluControl="surface"][surfaceVariant="resultBody"] {{
    background: transparent; border: none; border-radius: {t.radius_control}px;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="sourceEnvelopeAction"] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.accent_hairline};
    border-radius: {t.radius_control}px;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="sourceWritingSheet"] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.accent_hairline};
    border-radius: 16px;
}}
QWidget#root{scope} QPlainTextEdit[luluControl="textArea"][surfaceVariant="sourceWritingSheetEditor"] {{
    background: transparent; color: {t.ink}; border: none;
    border-radius: 15px;
    padding: 14px 16px; selection-background-color: {t.accent_wash};
}}
QWidget#root{scope} QPlainTextEdit[luluControl="textArea"][surfaceVariant="sourceWritingSheetEditor"]:focus {{
    background: {t.accent_wash}; border: none;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surface="well"] {{
    background: {t.secondary}; border: none; border-radius: {t.radius_control}px;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="uploadHero"] {{
    background: {t.secondary}; border: {t.border_width}px dashed {t.accent_hairline};
    border-radius: {t.radius_surface}px;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="uploadHero"]:hover,
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="uploadHero"][dropActive="true"] {{
    background: {t.accent_wash}; border-color: {t.accent_hairline_active};
}}
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="selected"] {{
    background: {t.accent_wash}; border: {t.border_width}px solid {t.accent_hairline};
    border-radius: {t.radius_surface}px;
}}
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="sourcePickerGlyph"],
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="resultGlyph"],
QWidget#root{scope} QFrame[luluControl="surface"][surfaceVariant="uploadGlyph"] {{
    background: {t.accent_wash}; border: none; border-radius: {t.radius_control}px;
}}
QWidget#root{scope} QFrame[luluControl="listRow"] {{
    background: transparent; border: {t.border_width}px solid transparent;
    border-radius: {t.radius_control}px;
}}
QWidget#root{scope} QFrame[luluControl="listRow"]:hover {{
    background: {t.secondary_hover};
}}
QWidget#root{scope} QFrame[luluControl="listRow"]:focus {{
    border-color: {t.accent_hairline_active};
}}
QWidget#root{scope} QFrame[luluControl="listRow"][selected="true"] {{
    background: {t.accent_wash}; border-left: 2px solid {t.accent};
}}

QWidget#root{scope} QPushButton[luluControl="button"] {{
    border-radius: {t.radius_control}px; outline: none;
    font-size: {t.type_body}px; font-weight: 500;
}}
QWidget#root{scope} QPushButton[luluControl="button"][controlSize="sm"] {{
    border-radius: {t.radius_compact}px;
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="primary"],
QWidget#root{scope} QPushButton#grabBtn {{
    background: {t.accent}; color: #FFFFFF;
    border: {t.border_width}px solid {t.accent}; font-weight: 600;
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="primary"]:hover,
QWidget#root{scope} QPushButton#grabBtn:hover {{
    background: {t.accent_hover}; border-color: {t.accent_hover};
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="primary"]:pressed,
QWidget#root{scope} QPushButton#grabBtn:pressed {{
    background: {t.accent_hover}; border-color: {t.ink_soft}; padding-top: 1px;
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="secondary"],
QWidget#root{scope} QPushButton#act,
QWidget#root{scope} QPushButton#dirBtn {{
    background: {t.secondary}; color: {t.ink_soft};
    border: {t.border_width}px solid transparent;
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="secondary"]:hover,
QWidget#root{scope} QPushButton#act:hover,
QWidget#root{scope} QPushButton#dirBtn:hover {{
    background: {t.secondary_hover}; color: {t.ink};
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="secondary"]:pressed,
QWidget#root{scope} QPushButton#act:pressed,
QWidget#root{scope} QPushButton#dirBtn:pressed {{
    background: {t.accent_wash}; color: {t.accent}; border-color: {t.line};
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="tertiary"],
QWidget#root{scope} QPushButton[luluControl="button"][role="icon"] {{
    background: transparent; color: {t.ink_muted}; border-color: transparent;
}}
QWidget#root{scope} QPushButton[luluControl="button"][role="tertiary"]:hover,
QWidget#root{scope} QPushButton[luluControl="button"][role="icon"]:hover {{
    background: {t.secondary_hover}; color: {t.ink};
}}
QWidget#root{scope} QPushButton[luluControl="button"]:focus {{
    border: {t.border_width}px solid {t.accent_hairline_active};
}}
QWidget#root{scope} QPushButton[luluControl="button"]:disabled {{
    background: {t.surface_work}; color: {t.ink_muted}; border-color: {t.line};
}}

QWidget#root{scope} QLineEdit,
QWidget#root{scope} QPlainTextEdit,
QWidget#root{scope} QTextEdit,
QWidget#root{scope} QComboBox,
QWidget#root{scope} QSpinBox,
QWidget#root{scope} QFrame[luluControl="selectFieldFrame"] {{
    background: {t.surface_focal}; color: {t.ink};
    border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_control}px;
    selection-background-color: {t.accent_wash};
}}
QWidget#root{scope} QLineEdit:focus,
QWidget#root{scope} QPlainTextEdit:focus,
QWidget#root{scope} QTextEdit:focus,
QWidget#root{scope} QComboBox:focus,
QWidget#root{scope} QSpinBox:focus,
QWidget#root{scope} QFrame[luluControl="selectFieldFrame"]:focus {{
    border-color: {t.accent};
}}
QWidget#root{scope} QComboBox QAbstractItemView {{
    background: {t.surface_floating}; color: {t.ink};
    border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_floating}px; outline: none;
    selection-background-color: {t.accent_wash}; selection-color: {t.accent};
}}

QWidget#root{scope} QFrame[luluControl="segment"] {{
    background: {t.secondary}; border: none; border-radius: {t.radius_control}px;
}}
QWidget#root{scope} QPushButton[luluControl="segmentItem"] {{
    background: transparent; color: {t.ink_soft};
    border: {t.border_width}px solid transparent;
    border-radius: {t.radius_compact}px;
}}
QWidget#root{scope} QPushButton[luluControl="segmentItem"]:hover {{
    background: {t.accent_wash}; color: {t.accent};
}}
QWidget#root{scope} QPushButton[luluControl="segmentItem"]:checked {{
    background: {t.surface_focal}; color: {t.accent};
    border-color: {t.paper_edge}; font-weight: 600;
}}

QWidget#root{scope} QListWidget[luluControl="list"],
QWidget#root{scope} QTableWidget[luluControl="table"],
QWidget#root{scope} QTableWidget {{
    background: {t.surface_focal}; color: {t.ink};
    border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_surface}px; gridline-color: {t.line};
    selection-background-color: {t.accent_wash}; selection-color: {t.ink};
}}
QWidget#root{scope} QTableWidget QHeaderView::section {{
    background: {t.surface_work}; color: {t.ink_soft};
    border: none; border-bottom: {t.border_width}px solid {t.line};
}}
QWidget#root{scope} QTableWidget::item:hover {{ background: {t.secondary_hover}; }}
QWidget#root{scope} QTableWidget::item:selected {{
    background: {t.accent_wash}; color: {t.ink};
}}
QWidget#root{scope} QProgressBar {{
    background: {t.surface_work}; border: none; border-radius: 3px;
}}
QWidget#root{scope} QProgressBar::chunk {{ background: {t.accent}; border-radius: 3px; }}

/* 配音页历史局部 QSS 使用 Palette.TERRA；仅在本页把已接入的滑杆
   收回到母版蓝色强调，不改变任何数值、信号或其他页面。 */
QWidget#root{scope}[workspacePage="voice"] QSlider#luluSlider::groove:horizontal {{
    background: {t.line}; height: 4px; border-radius: 2px;
}}
QWidget#root{scope}[workspacePage="voice"] QSlider#luluSlider::sub-page:horizontal {{
    background: {t.accent}; height: 4px; border-radius: 2px;
}}
QWidget#root{scope}[workspacePage="voice"] QSlider#luluSlider::add-page:horizontal {{
    background: {t.line}; height: 4px; border-radius: 2px;
}}
QWidget#root{scope}[workspacePage="voice"] QSlider#luluSlider::handle:horizontal {{
    background: {t.surface_focal}; border: 2px solid {t.accent};
    width: 14px; height: 14px; margin: -6px 0; border-radius: 8px;
}}

QWidget#root{scope} QFrame#intake,
QWidget#root{scope} QFrame#browserField {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_control}px;
}}
QWidget#root{scope} QFrame#intake:hover,
QWidget#root{scope} QFrame#browserField:hover {{ border-color: {t.accent_hairline}; }}
QWidget#root{scope} QFrame#emptyCard {{ background: transparent; border: none; }}

QWidget#root{scope} QFrame#voiceWorkspaceCard {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_work}px;
}}
QWidget#root{scope} QFrame#voiceWorkspaceCard[voiceCardRole="section"],
QWidget#root{scope} QFrame#voiceFollowPanel,
QWidget#root{scope} QFrame#voiceFollowFooter,
QWidget#root{scope} QFrame#voiceGenerationDock,
QWidget#root{scope} QFrame#voiceResultEmpty {{
    background: {t.surface_work}; border: {t.border_width}px solid {t.line};
}}
QWidget#root{scope} QLabel#voiceStepBadge {{
    background: {t.accent_wash}; color: {t.accent}; border: {t.border_width}px solid {t.accent_hairline};
    border-radius: 11px; font-weight: 700;
}}
QWidget#root{scope} QLabel#voiceEnvelopeAction,
QWidget#root{scope} QLabel#voiceStrengthValue,
QWidget#root{scope} QLabel#voiceGateHint {{ color: {t.accent}; }}
QWidget#root{scope} QFrame#voiceGenerationDock,
QWidget#root{scope} QFrame#voiceFollowFooter,
QWidget#root{scope} QFrame#voiceStrengthBar {{
    background: {t.surface_work}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_surface}px;
}}
QWidget#root{scope} QPushButton[luluControl="button"][controlVariant="engine"],
QWidget#root{scope} QPushButton[luluControl="button"][templateChip="true"] {{
    background: {t.surface_focal}; color: {t.ink_soft};
    border: {t.border_width}px solid {t.line};
}}
QWidget#root{scope} QPushButton[luluControl="button"][controlVariant="engine"]:hover,
QWidget#root{scope} QPushButton[luluControl="button"][templateChip="true"]:hover {{
    background: {t.accent_wash}; color: {t.ink}; border-color: {t.accent_hairline};
}}
QWidget#root{scope} QPushButton[luluControl="button"][controlVariant="engine"]:checked,
QWidget#root{scope} QPushButton[luluControl="button"][templateChip="true"]:checked {{
    background: {t.accent_wash}; color: {t.accent};
    border-color: {t.accent}; font-weight: 650;
}}

QWidget#root{scope} QScrollBar:vertical {{
    width: 7px; background: transparent; margin: 2px;
}}
QWidget#root{scope} QScrollBar::handle:vertical {{
    min-height: 32px; background: {t.line}; border-radius: 3px;
}}

QFrame{scope}[luluControl="popover"], QDialog{scope}[luluControl="dialog"] {{
    background: {t.surface_floating}; color: {t.ink};
    border: {t.border_width}px solid {t.line}; border-radius: {t.radius_floating}px;
}}
QFrame{scope}[luluControl="popover"] QLabel,
QDialog{scope}[luluControl="dialog"] QLabel {{ background: transparent; color: {t.ink}; }}
QMenu{scope} {{
    background: {t.surface_floating}; color: {t.ink};
    border: {t.border_width}px solid {t.line}; border-radius: {t.radius_floating}px;
    padding: 6px;
}}
QMenu{scope}::item {{ min-height: 30px; padding: 0 12px; border-radius: {t.radius_compact}px; }}
QMenu{scope}::item:selected {{ background: {t.accent_wash}; color: {t.accent}; font-weight: 600; }}
"""


def build_consumer_sample_qss(t: ConsumerTheme) -> str:
    """仅生成文案提取样板页的范围化样式。"""
    scope = '[consumerThemeScope="transcriptionWorkbench"]'
    return f"""

/* ===== Lulu consumer theme sample: transcription workbench only ===== */
QWidget{scope}, QFrame{scope} {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 {t.surface_focal}, stop: 0.46 {t.canvas}, stop: 1 {t.surface_work}
    );
    color: {t.ink};
}}
QFrame{scope} QStackedWidget, QFrame{scope} QWidget#root {{ background: {t.canvas}; color: {t.ink}; }}
QWidget{scope} QLabel, QFrame{scope} QLabel {{
    background: transparent; color: {t.ink}; font-family: "{t.font_body}";
}}
QWidget{scope} QLabel#pageMeta,
QWidget{scope} QLabel#dropSub, QWidget{scope} QLabel#dropFmt,
QWidget{scope} QLabel#optionHint, QWidget{scope} QLabel#taskSrc,
QWidget{scope} QLabel#taskSub {{
    color: {t.ink_muted}; font-size: {t.type_caption}px;
}}
QWidget{scope} QLabel#pageMeta {{ color: {t.ink_soft}; font-size: {t.type_body}px; }}
QWidget{scope} QLabel#pageTitle {{
    color: {t.ink}; font-family: "{t.font_display}"; font-size: {t.type_page + 2}px; font-weight: 650;
}}
QWidget{scope} QLabel#sectionTitle {{
    color: {t.ink}; font-family: "{t.font_display}"; font-size: {t.type_section}px; font-weight: 650;
}}
QWidget{scope} QLabel#dropTitle, QWidget{scope} QLabel#recordTitle,
QWidget{scope} QLabel#taskName {{
    color: {t.ink}; font-size: {t.type_card}px; font-weight: 600;
}}
QWidget{scope} QLabel#linkLabel, QWidget{scope} QLabel#recordRowLabel {{
    color: {t.ink_soft}; font-size: {t.type_body}px; font-weight: 600;
}}
QWidget{scope} QLabel#optionLabel {{
    color: {t.ink_muted}; font-size: {t.type_caption}px; font-weight: 600;
}}
QWidget{scope} QLabel#linkLabel, QWidget{scope} QLabel#recordTitle {{
    font-family: "{t.font_display}"; font-size: {t.type_card}px; font-weight: 650;
}}

QWidget{scope} QFrame#studioTitleRule {{
    background: {t.accent}; border: none; border-radius: 2px;
}}
QWidget{scope} QFrame#studioSectionDot {{
    background: {t.accent}; border: none; border-radius: 3px;
}}

QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="workbenchIntake"] {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 {t.surface_focal}, stop: 1 {t.surface_work}
    );
    border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_work}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="linkCard"],
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="recordingCard"] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_surface}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="taskQueue"] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_surface}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="taskCard"],
QFrame{scope} QFrame[luluControl="surface"][surface="card"][surfaceVariant=""] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.paper_edge};
    border-radius: {t.radius_surface}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="taskGroup"] {{
    background: {t.surface_focal}; border: {t.border_width}px solid {t.task_group_border};
    border-radius: {t.radius_surface}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="taskGroupMembers"] {{
    background: {t.surface_work}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_control}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="uploadHero"] {{
    background: {t.secondary};
    border: {t.border_width}px dashed {t.accent_hairline};
    border-radius: {t.radius_surface}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="uploadHero"]:hover,
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="uploadHero"][dropActive="true"] {{
    background: {t.accent_wash}; border-color: {t.accent_hairline_active};
}}
QWidget{scope} QFrame[luluControl="surface"][surface="well"],
QWidget{scope} QFrame#mediaSwitch, QWidget{scope} QFrame#segBox,
QWidget{scope} QFrame[luluControl="segment"] {{
    background: {t.secondary}; border: none;
    border-radius: {t.radius_control}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="uploadGlyph"] {{
    background: {t.accent_wash}; border: none; border-radius: {t.radius_control}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="recordGlyph"] {{
    background: {t.accent_wash}; border: none; border-radius: {t.radius_control}px;
}}
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="optionBar"],
QWidget{scope} QFrame[luluControl="surface"][surfaceVariant="recordControls"] {{
    background: {t.secondary}; border: none; border-radius: {t.radius_compact}px;
}}

QWidget{scope} QPushButton[luluControl="button"] {{
    border-radius: {t.radius_control}px; outline: none; font-size: {t.type_body}px; font-weight: 500;
}}
QWidget{scope} QPushButton[luluControl="button"][controlSize="sm"] {{ border-radius: {t.radius_compact}px; }}
QWidget{scope} QPushButton[luluControl="button"][role="primary"],
QWidget{scope} QPushButton#grabBtn {{
    background: {t.accent}; color: #FFFFFF; border: {t.border_width}px solid {t.accent};
    border-radius: {t.radius_control}px; font-weight: 600;
}}
QWidget{scope} QPushButton[luluControl="button"][role="primary"]:hover,
QWidget{scope} QPushButton#grabBtn:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QWidget{scope} QPushButton[luluControl="button"][role="primary"]:pressed,
QWidget{scope} QPushButton#grabBtn:pressed {{
    background: {t.accent_hover}; border-color: {t.ink_soft}; padding-top: 1px;
}}
QWidget{scope} QPushButton[luluControl="button"][role="secondary"],
QWidget{scope} QPushButton#act, QWidget{scope} QPushButton#dirBtn {{
    background: {t.secondary}; color: {t.ink_soft};
    border: {t.border_width}px solid transparent; border-radius: {t.radius_control}px;
}}
QWidget{scope} QPushButton[luluControl="button"][role="secondary"]:hover,
QWidget{scope} QPushButton#act:hover, QWidget{scope} QPushButton#dirBtn:hover {{
    background: {t.secondary_hover}; color: {t.ink}; border-color: transparent;
}}
QWidget{scope} QPushButton[luluControl="button"][role="secondary"]:pressed,
QWidget{scope} QPushButton#act:pressed, QWidget{scope} QPushButton#dirBtn:pressed {{
    background: {t.accent_wash}; color: {t.accent}; border-color: {t.line};
}}
QWidget{scope} QPushButton[luluControl="button"][panelAction="paperSecondary"] {{
    background: {t.surface_focal}; color: {t.ink_soft};
    border: {t.border_width}px solid {t.line}; border-radius: {t.radius_compact}px;
}}
QWidget{scope} QPushButton[luluControl="button"][panelAction="paperSecondary"]:hover {{
    background: {t.accent_wash}; color: {t.ink}; border-color: {t.accent_hairline};
}}
QWidget{scope} QPushButton[luluControl="button"][panelAction="paperSecondary"]:pressed {{
    background: {t.secondary_hover}; color: {t.accent}; border-color: {t.accent};
    padding-top: 1px;
}}
QWidget{scope} QPushButton[luluControl="button"][panelAction="paperSecondary"]:focus {{
    background: {t.surface_focal}; border: 2px solid {t.accent_hairline_active};
}}
QWidget{scope} QPushButton[luluControl="button"][panelAction="paperSecondary"]:disabled {{
    background: {t.surface_work}; color: {t.ink_muted}; border-color: {t.line};
}}
QWidget{scope} QPushButton[luluControl="button"][role="tertiary"],
QWidget{scope} QPushButton[luluControl="button"][role="icon"] {{
    background: transparent; color: {t.ink_muted}; border-color: transparent;
    border-radius: {t.radius_control}px;
}}
QWidget{scope} QPushButton[luluControl="button"][role="tertiary"]:hover,
QWidget{scope} QPushButton[luluControl="button"][role="icon"]:hover {{
    background: {t.secondary_hover}; color: {t.ink}; border-color: transparent;
}}
QWidget{scope} QPushButton[luluControl="button"][role="tertiary"]:pressed,
QWidget{scope} QPushButton[luluControl="button"][role="icon"]:pressed {{
    background: {t.accent_wash}; color: {t.accent}; border-color: transparent;
}}
QWidget{scope} QPushButton[luluControl="button"][role="destructive"] {{
    background: transparent; color: {t.danger}; border: {t.border_width}px solid {t.danger};
    border-radius: {t.radius_control}px;
}}
QWidget{scope} QPushButton[luluControl="button"]:focus {{ border: {t.border_width}px solid {t.accent}; }}
QWidget{scope} QPushButton[luluControl="button"]:disabled {{
    background: {t.surface_alt}; color: {t.ink_muted}; border-color: {t.line};
}}

QWidget{scope} QLineEdit, QFrame{scope} QLineEdit,
QWidget{scope} QPlainTextEdit, QFrame{scope} QPlainTextEdit,
QWidget{scope} QTextEdit, QFrame{scope} QTextEdit,
QWidget{scope} QComboBox, QFrame{scope} QComboBox,
QWidget{scope} QFrame[luluControl="selectFieldFrame"] {{
    background: {t.surface_focal}; color: {t.ink}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_control}px; selection-background-color: {t.accent_wash};
}}
QWidget{scope} QLineEdit:focus, QFrame{scope} QLineEdit:focus,
QWidget{scope} QPlainTextEdit:focus, QFrame{scope} QPlainTextEdit:focus,
QWidget{scope} QTextEdit:focus, QFrame{scope} QTextEdit:focus,
QWidget{scope} QComboBox:focus, QFrame{scope} QComboBox:focus,
QWidget{scope} QFrame[luluControl="selectFieldFrame"]:focus {{ border-color: {t.accent}; }}
QWidget{scope} QComboBox QAbstractItemView, QFrame{scope} QComboBox QAbstractItemView {{
    background: {t.surface_floating}; color: {t.ink}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_floating}px; outline: none; selection-background-color: {t.accent_wash};
    selection-color: {t.accent};
}}

QWidget{scope} QPushButton[luluControl="segmentItem"], QWidget{scope} QPushButton#seg,
QWidget{scope} QPushButton#mediaSegLeft, QWidget{scope} QPushButton#mediaSegRight {{
    background: transparent; color: {t.ink_soft}; border: {t.border_width}px solid transparent;
    border-radius: {t.radius_compact}px; font-weight: 500;
}}
QWidget{scope} QPushButton[luluControl="segmentItem"]:hover,
QWidget{scope} QPushButton#seg:hover, QWidget{scope} QPushButton#mediaSegLeft:hover,
QWidget{scope} QPushButton#mediaSegRight:hover {{ background: {t.accent_wash}; color: {t.accent}; }}
QWidget{scope} QPushButton[luluControl="segmentItem"]:checked,
QWidget{scope} QPushButton#seg:checked, QWidget{scope} QPushButton#mediaSegLeft:checked,
QWidget{scope} QPushButton#mediaSegRight:checked {{
    background: {t.surface_focal}; color: {t.accent}; border-color: {t.paper_edge}; font-weight: 600;
}}

QMenu{scope} {{
    background: {t.surface_floating}; color: {t.ink}; border: {t.border_width}px solid {t.line};
    border-radius: {t.radius_floating}px; padding: 6px;
}}
QMenu{scope}::item {{ min-height: 30px; padding: 0 12px; border-radius: {t.radius_compact}px; }}
QMenu{scope}::item:selected {{ background: {t.accent_wash}; color: {t.accent}; font-weight: 600; }}
QMenu{scope}::separator {{ height: 1px; background: {t.line}; margin: 5px 8px; }}

QWidget{scope} QFrame[luluControl="badge"] {{ border: none; border-radius: 11px; }}

QWidget{scope} QScrollBar:vertical, QFrame{scope} QScrollBar:vertical {{
    width: 7px; background: transparent; margin: 2px;
}}
QWidget{scope} QScrollBar::handle:vertical, QFrame{scope} QScrollBar::handle:vertical {{
    min-height: 32px; background: {t.line}; border-radius: 3px;
}}
"""


def apply_fonts(app) -> None:
    f = QFont(SANS)
    f.setPixelSize(13)
    app.setFont(f)


# 当前调色板固定为宣纸，兼容现有控件读取 ACTIVE 的接口。
ACTIVE: Palette = WARM


def shadow(widget: QWidget, *, blur: int = 34, dy: int = 10, alpha: int = 26) -> QWidget:
    """给卡片加柔和投影（QSS 不支持 box-shadow，用图形特效）。返回该 widget 方便链式。

    投影颜色取宣纸配色的 SHADOW_RGB；alpha 由调用方控制强弱。
    """
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setXOffset(0)
    eff.setYOffset(dy)
    r, g, b = ACTIVE.SHADOW_RGB
    eff.setColor(QColor(r, g, b, alpha))
    widget.setGraphicsEffect(eff)
    return widget


def apply_task_group_shadow(widget: QWidget, theme: ConsumerTheme) -> QWidget:
    """每张任务组卡只创建一次的局部轻阴影。"""
    red, green, blue, alpha, blur, offset = theme.task_group_shadow
    widget.setProperty(
        "taskGroupShadowSpec",
        f"{red},{green},{blue},{alpha},{blur},{offset}",
    )
    # Qt offscreen 插件对直接 QWidget.render + graphicsEffect 会返回空源图。
    # 离屏验收保留真实实体像素；正常 Cocoa 前台仍使用单一阴影对象。
    if QApplication.platformName() == "offscreen":
        if widget.graphicsEffect() is not None:
            widget.setGraphicsEffect(None)
        return widget
    effect = getattr(widget, "_lulu_task_group_shadow", None)
    if effect is None:
        effect = QGraphicsDropShadowEffect(widget)
        widget._lulu_task_group_shadow = effect
        widget.setGraphicsEffect(effect)
    effect.setBlurRadius(blur)
    effect.setXOffset(0)
    effect.setYOffset(offset)
    effect.setColor(QColor(red, green, blue, alpha))
    return widget


# ——— 全局 QSS（按调色板生成）———
# 切片3 起：渐变/hover 底/滚动条/拖拽区/气泡等原写死的字面色均已收口为主题 token;
# 「强调底上的白字」（newBtn/grabBtn 的 color:#FFFFFF）保留现有取值。
def build_qss(p: Palette) -> str:
    base = f"""
* {{ font-family: "{SANS}"; color: {p.INK}; }}
QWidget#root {{ background: {p.BG}; }}
QFrame#titlebarPaperLeft {{
    background: {p.BG}; border: none; border-right: 1px solid {p.LINE};
}}
QFrame#titlebarPaperCenter {{
    background: {p.BG}; border: none;
}}
QLabel#windowTitleLabel[immersiveTitlebar="true"] {{
    color: {p.INK_SOFT}; background: transparent; border: none;
    font-size: 12px; font-weight: 600;
}}

/* ====== 侧边栏 ====== */
/* 侧栏：与主区同色、无渐变（上下统一），靠右侧一根清晰竖线划分三区之一 */
QFrame#sidebar {{ background: {p.BG}; border: none; border-right: 1px solid {p.LINE}; }}
QLabel#brandName {{ font-family: "{SERIF}"; font-size: 19px; font-weight: 700; color: {p.INK}; }}
QLabel#brandSub  {{ font-size: 10px; color: {p.INK_FAINT}; letter-spacing: 2px; }}

QPushButton#newBtn {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {p.GRAD_TOP}, stop:1 {p.TERRA});
    color: #FFFFFF; border: none; border-radius: 12px;
    padding: 11px 14px; font-size: 14px; font-weight: 600;
}}
QPushButton#newBtn:hover {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {p.GRAD_HOVER_TOP}, stop:1 {p.GRAD_HOVER_BOT}); }}

QLabel#navLabel {{ font-size: 10px; color: {p.INK_FAINT}; letter-spacing: 2px; font-weight: 600; padding: 14px 12px 4px; }}

/* 未选中也留 3px 透明左边框，跟选中态（陶土左边框）等宽，避免选中项图标/文字被推右 3px */
QFrame#navItem {{ background: transparent; border-radius: 11px; border-left: 3px solid transparent; }}
QFrame#navItem:hover {{ background: {p.NAV_HOVER}; }}
QFrame#navItemActive {{ background: {p.CARD}; border-radius: 11px; border-left: 3px solid {p.TERRA}; }}
QLabel#navText {{ font-size: 13px; color: {p.INK_SOFT}; background: transparent; }}
QLabel#navTextActive {{ font-size: 13px; color: {p.INK}; font-weight: 600; background: transparent; }}
QFrame#navCountLive {{ background: {p.BLUE_WASH}; border-radius: 8px; }}
QLabel#navCountLiveText {{ color: {p.BLUE}; font-size: 11px; background: transparent; }}

QFrame#footTag {{ background: {p.TERRA_WASH}; border-radius: 9px; }}
QLabel#footTagText {{ color: {p.TERRA}; font-size: 11px; font-weight: 600; }}

/* ====== 主区 ====== */
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel#pageTitle {{ font-family: "{SERIF}"; font-size: 23px; font-weight: 600; color: {p.INK}; }}
QLabel#pageMeta  {{ font-size: 13px; color: {p.INK_SOFT}; }}

QFrame#segBox {{ background: {p.BG_DEEP}; border-radius: 11px; }}
QPushButton#seg {{ border: none; background: transparent; color: {p.INK_SOFT}; font-size: 12px; padding: 6px 14px; border-radius: 8px; }}
QPushButton#seg:checked {{ background: {p.CARD}; color: {p.INK}; font-weight: 600; }}
QFrame#segBox[choiceEmphasis="true"] {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 10px; }}
QPushButton#seg[choiceEmphasis="true"] {{ border: 1px solid transparent; background: transparent; color: {p.INK_SOFT}; font-weight: 600; border-radius: 8px; }}
QPushButton#seg[choiceEmphasis="true"]:hover {{ background: {p.TERRA_WASH}; color: {p.INK}; border-color: {p.TERRA_SOFT}; }}
QPushButton#seg[choiceEmphasis="true"]:checked {{ background: {p.TERRA_WASH}; color: {p.TERRA}; border: 1px solid {p.TERRA}; font-weight: 700; }}
QPushButton#seg[choiceEmphasis="true"]:focus {{ border: 2px solid {p.TERRA}; }}

/* 统一输入区（干净近白卡片，靠轻投影浮起，不用渐变） */
QFrame#intake {{
    background: {p.CARD};
    border: 1px solid {p.LINE}; border-radius: 20px;
}}
QFrame#intake[focusMode="true"] {{
    background: transparent;
    border: none;
}}
QFrame#dropZone {{
    background: {p.DROP_BG}; border: 2px dashed {p.TERRA_SOFT}; border-radius: 15px;
}}
QFrame#dropZone:hover {{ background: {p.TERRA_WASH}; border: 2px dashed {p.TERRA}; }}
QLabel#dropTitle {{ font-size: 14px; font-weight: 600; color: {p.INK}; }}
QLabel#dropSub {{ font-size: 12px; color: {p.INK_SOFT}; }}
QLabel#dropFmt {{ font-size: 10px; color: {p.INK_FAINT}; }}

QFrame#linkBox, QFrame#micBox {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 15px; }}
QLabel#linkLabel {{ font-size: 12px; font-weight: 600; color: {p.INK}; }}
QLineEdit#linkInput {{
    background: {p.BG}; border: 1px solid {p.LINE}; border-radius: 10px;
    padding: 8px 12px; font-size: 12px; color: {p.INK};
}}
QLineEdit#linkInput:focus {{ background: {p.HOVER_RAISE}; border: 1px solid {p.TERRA_SOFT}; }}
QLabel#optionLabel {{ color: {p.INK_SOFT}; font-size: 11px; font-weight: 600; background: transparent; }}
QLabel#optionHint {{ color: {p.INK_FAINT}; font-size: 11px; background: transparent; }}
QFrame#mediaSwitch {{
    background: {p.BG_DEEP};
    border: 1px solid {p.LINE};
    border-radius: 12px;
}}
QPushButton#mediaSegLeft, QPushButton#mediaSegRight {{
    background: transparent;
    border: none;
    color: {p.INK_SOFT};
    font-size: 12px;
    padding: 5px 12px;
    border-radius: 9px;
}}
QPushButton#mediaSegLeft:hover, QPushButton#mediaSegRight:hover {{ color: {p.INK}; }}
QPushButton#mediaSegLeft:checked, QPushButton#mediaSegRight:checked {{
    background: {p.CARD};
    color: {p.INK};
    font-weight: 700;
    border: 1px solid {p.TERRA_SOFT};
}}
QFrame#mediaSwitch[choiceEmphasis="true"] {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 10px; }}
QPushButton#mediaSegLeft[choiceEmphasis="true"], QPushButton#mediaSegRight[choiceEmphasis="true"] {{
    background: transparent; color: {p.INK_SOFT}; border: 1px solid transparent; font-weight: 600; border-radius: 8px;
}}
QPushButton#mediaSegLeft[choiceEmphasis="true"]:hover, QPushButton#mediaSegRight[choiceEmphasis="true"]:hover {{
    background: {p.TERRA_WASH}; color: {p.INK}; border-color: {p.TERRA_SOFT};
}}
QPushButton#mediaSegLeft[choiceEmphasis="true"]:checked, QPushButton#mediaSegRight[choiceEmphasis="true"]:checked {{
    background: {p.TERRA_WASH}; color: {p.TERRA}; border: 1px solid {p.TERRA}; font-weight: 700;
}}
QPushButton#mediaSegLeft[choiceEmphasis="true"]:focus, QPushButton#mediaSegRight[choiceEmphasis="true"]:focus {{ border: 2px solid {p.TERRA}; }}
/* 提示词模板：自定义下拉字段 + 弹层（不用原生 QComboBox） */
QFrame#tplField {{ background: {p.BG}; border: 1px solid {p.LINE}; border-radius: 10px; }}
QFrame#tplField:hover {{ background: {p.HOVER_RAISE}; border: 1px solid {p.TERRA_SOFT}; }}
QFrame#tplField[consumerDropdown="true"]:focus {{
    background: {p.HOVER_RAISE}; border: 2px solid {p.TERRA};
}}
QFrame#tplField[consumerDropdown="true"][pressed="true"],
QFrame#tplField[consumerDropdown="true"][menuOpen="true"] {{
    background: {p.TERRA_WASH}; border: 1px solid {p.TERRA};
}}
QLabel#tplFieldText {{ font-size: 13px; color: {p.INK}; font-weight: 600; background: transparent; }}
QFrame#tplPopup {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 12px; }}
QMenu#consumerSelectMenu {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE};
    border-radius: 10px; padding: 6px;
}}
QMenu#consumerSelectMenu::item {{ padding: 8px 28px 8px 12px; border-radius: 7px; }}
QMenu#consumerSelectMenu::item:selected {{ background: {p.TERRA_WASH}; color: {p.INK}; }}
QFrame#tplRow {{ background: transparent; border: none; border-radius: 9px; }}
QFrame#tplRow:hover {{ background: {p.TERRA_WASH}; }}
QFrame#tplRow[picked="true"] {{ background: {p.TERRA_WASH}; }}
QLabel#tplRowName {{ font-size: 13px; color: {p.INK}; font-weight: 600; background: transparent; }}
QLabel#tplRowDesc {{ font-size: 11px; color: {p.INK_FAINT}; background: transparent; }}
QLabel#tplGroupLabel {{ font-size: 11px; color: {p.INK_FAINT}; font-weight: 600; background: transparent; padding: 6px 10px 2px; }}
QLabel#tplManageText {{ font-size: 12px; color: {p.TERRA}; font-weight: 600; background: transparent; }}
QListWidget#tplList {{
    background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 10px;
    padding: 4px; outline: none; font-size: 13px; color: {p.INK};
}}
QListWidget#tplList::item {{ padding: 9px 10px; border-radius: 7px; }}
QListWidget#tplList::item:selected {{ background: {p.TERRA_WASH}; color: {p.INK}; }}

/* 来源：文稿库「选择」按钮 + 选中卡 */
QPushButton#pickBtn {{
    background: {p.BG}; border: 1px solid {p.LINE}; border-radius: 10px;
    color: {p.INK_SOFT}; font-size: 13px; font-weight: 600; padding: 9px 18px;
}}
QPushButton#pickBtn:hover {{ background: {p.HOVER_RAISE}; border: 1px solid {p.TERRA_SOFT}; color: {p.TERRA}; }}
QFrame#pickedCard {{ background: {p.TERRA_WASH}; border: 1px solid {p.TERRA_SOFT}; border-radius: 12px; }}
QLabel#pickedTitle {{ font-size: 13px; color: {p.INK}; font-weight: 700; background: transparent; }}
QLabel#pickedMeta {{ font-size: 11px; color: {p.INK_SOFT}; background: transparent; }}
QLabel#kindBadge {{
    background: {p.CARD}; color: {p.TERRA}; font-size: 10px; font-weight: 600;
    border-radius: 6px; padding: 1px 7px;
}}
QFrame#sectionPanel {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 14px; }}
QLabel#sectionTitle {{ font-size: 13px; font-weight: 700; color: {p.INK}; background: transparent; }}
QLabel#stepTitle {{ font-size: 13px; font-weight: 700; color: {p.INK}; background: transparent; }}
QFrame#dirCard {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 14px; }}
QFrame#dirCard:hover {{ background: {p.TERRA_WASH}; border: 1px solid {p.TERRA_SOFT}; }}
QLabel#dirCardName {{ font-size: 14px; font-weight: 600; color: {p.INK}; background: transparent; }}
QLabel#dirCardSub {{ font-size: 12px; color: {p.INK_SOFT}; background: transparent; }}
QPushButton#grabBtn {{ background: {p.TERRA}; color: #FFFFFF; border: none; border-radius: 10px; padding: 8px 16px; font-size: 12px; font-weight: 600; }}
QPushButton#grabBtn:hover {{ background: {p.TERRA_DEEP}; }}
QPushButton#grabBtn:disabled {{ background: {p.TERRA_SOFT}; }}
QLabel#micText {{ font-size: 13px; font-weight: 600; color: {p.INK}; }}
QLabel#micSub {{ font-size: 11px; color: {p.INK_FAINT}; }}
QFrame#soon {{ background: transparent; border: 1px solid {p.LINE}; border-radius: 6px; }}
QLabel#soonText {{ color: {p.INK_FAINT}; font-size: 9px; }}

/* 区段标题 */
QLabel#sectionTitle {{ font-family: "{SERIF}"; font-size: 16px; font-weight: 600; color: {p.INK}; }}
QFrame#badge {{ background: {p.BLUE_WASH}; border-radius: 8px; }}
QLabel#badgeText {{ color: {p.BLUE}; font-size: 11px; font-weight: 600; }}

/* 任务卡 */
QFrame#taskCard {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 14px; }}
QFrame#taskCard[selected="true"] {{ background: {p.TERRA_WASH}; border: 1px solid {p.TERRA}; }}
QLabel#taskName {{ font-size: 14px; font-weight: 600; color: {p.INK}; }}
QLabel#taskSrc {{ font-size: 11px; color: {p.INK_FAINT}; }}
QLabel#taskSub {{ font-size: 11px; color: {p.INK_FAINT}; }}
QProgressBar#taskBar {{ background: {p.BG_DEEP}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar#taskBar::chunk {{ background: {p.TERRA}; border-radius: 3px; }}
QLineEdit#inlineTitleEdit {{ background: {p.CARD}; border: 1px solid {p.TERRA_SOFT}; border-radius: 8px; padding: 4px 8px; color: {p.INK}; font-size: 14px; font-weight: 600; selection-background-color: {p.TERRA_SOFT}; }}

QPushButton#act {{ background: {p.BG_DEEP}; border: 1px solid transparent; border-radius: 8px; color: {p.INK_SOFT}; font-size: 11px; padding: 5px 11px; }}
QPushButton#act:hover {{ background: {p.TERRA_WASH}; color: {p.TERRA}; }}
QPushButton#act:disabled {{ background: transparent; border: 1px dashed {p.LINE}; color: {p.INK_FAINT}; }}
/* 列表操作按钮：比 #act 更像实体按钮（白底实描边 + 深字 + 图标），用于采集结果工具行 */
QPushButton#listAct {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 9px; color: {p.INK}; font-size: 12px; font-weight: 500; padding: 6px 14px; }}
QPushButton#listAct:hover {{ background: {p.TERRA_WASH}; border-color: {p.TERRA_SOFT}; color: {p.TERRA}; }}
QPushButton#listAct:disabled {{ background: transparent; border: 1px dashed {p.LINE}; color: {p.INK_FAINT}; }}
QFrame#voicePlayerBar {{ background: {p.BG_DEEP}; border: 1px solid {p.LINE_SOFT}; border-radius: 10px; }}
QPushButton#voicePlayBtn {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 8px; color: {p.INK_SOFT}; font-size: 11px; padding: 5px 11px; }}
QPushButton#voicePlayBtn:hover {{ background: {p.TERRA_WASH}; color: {p.TERRA}; }}
QPushButton#voicePlayBtn:disabled {{ background: transparent; border: 1px dashed {p.LINE}; color: {p.INK_FAINT}; }}
QSlider#voiceProgress::groove:horizontal {{ background: {p.LINE_SOFT}; height: 6px; border-radius: 3px; }}
QSlider#voiceProgress::sub-page:horizontal {{ background: {p.TERRA}; height: 6px; border-radius: 3px; }}
QSlider#voiceProgress::add-page:horizontal {{ background: {p.LINE_SOFT}; height: 6px; border-radius: 3px; }}
QSlider#voiceProgress::handle:horizontal {{ background: {p.CARD}; border: 1px solid {p.TERRA_SOFT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider#voiceProgress:disabled::groove:horizontal,
QSlider#voiceProgress:disabled::add-page:horizontal {{ background: {p.LINE_SOFT}; }}
QSlider#voiceProgress:disabled::sub-page:horizontal {{ background: {p.LINE_SOFT}; }}
QPushButton#iconBtn {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 9px; }}
QPushButton#iconBtn:hover {{ background: {p.TERRA_WASH}; border: 1px solid {p.TERRA_SOFT}; }}

QPushButton#dirBtn {{ background: {p.TERRA_WASH}; color: {p.TERRA}; border: none; border-radius: 7px; padding: 3px 9px; font-size: 11px; font-weight: 500; }}
QPushButton#dirBtn:hover {{ background: {p.TERRA_SOFT}; color: #FFFFFF; }}
QPushButton#dirBtn:disabled {{ background: transparent; color: {p.INK_FAINT}; }}

QMenu {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 18px 7px 14px; border-radius: 7px; font-size: 13px; color: {p.INK}; }}
QMenu::item:selected {{ background: {p.TERRA_WASH}; color: {p.TERRA}; }}
QMenu::item:disabled {{ color: {p.INK_FAINT}; }}
QMenu::separator {{ height: 1px; background: {p.LINE}; margin: 5px 8px; }}

QToolTip {{ background: {p.TOOLTIP_BG}; color: {p.TOOLTIP_FG}; border: none; padding: 4px 8px; border-radius: 6px; }}

/* ====== 三栏：顶栏折叠按钮 ====== */
/* 顶栏与下方内容同色、无分隔线 —— 不形成独立色块，整片连通 */
QFrame#topBar {{ background: transparent; border: none; }}
QPushButton#paneToggle {{ background: transparent; border: none; border-radius: 8px; }}
QPushButton#paneToggle:hover {{ background: {p.BG_DEEP}; }}
/* 侧栏折叠按钮：对齐 Codex —— 扁平图标，无卡片底/无描边，仅 hover 浮出极淡底 */
QPushButton#paneToggleRail {{
    background: transparent; border: 2px solid transparent; border-radius: 7px; padding: 0px;
}}
QPushButton#paneToggleRail:hover {{ background: {p.RAIL_HOVER}; }}
QPushButton#paneToggleRail:pressed {{ background: {p.BG_DEEP}; }}
QPushButton#paneToggleRail:focus {{ border: 2px solid {p.TERRA_SOFT}; }}
QPushButton#paneToggleRail:disabled {{ background: transparent; border-color: transparent; }}

/* ====== 共用文稿选择空态、术语与音色参考文本编辑 ====== */
QLabel#panelEmpty {{ font-size: 13px; color: {p.INK_FAINT}; }}
QPlainTextEdit#mdEditor {{
    background: {p.BG}; border: none; border-radius: 0px;
    padding: 8px; font-size: 14px; line-height: 1.7; color: {p.INK};
    selection-background-color: {p.TERRA_SOFT};
}}
QPlainTextEdit#mdEditor:focus {{ border: none; }}

/* ====== 滚动条：细、圆角、无箭头、半透明，融入界面 ====== */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p.SCROLL_HANDLE}; border-radius: 4px; min-height: 36px; margin: 2px 3px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.SCROLL_HANDLE_HOVER}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {p.SCROLL_HANDLE}; border-radius: 4px; min-width: 36px; margin: 3px 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.SCROLL_HANDLE_HOVER}; }}
/* 去掉两端箭头按钮与凹槽分页 */
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: none; border: none; }}
QScrollBar::up-arrow, QScrollBar::down-arrow,
QScrollBar::left-arrow, QScrollBar::right-arrow {{ width: 0; height: 0; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QPushButton#linkBtn {{ background: transparent; border: none; color: {p.INK_SOFT}; font-size: 12px; padding: 4px 6px; }}
QPushButton#linkBtn:hover {{ color: {p.TERRA}; }}

/* ====== 文稿选择浮层 ====== */
QFrame#pickerCard {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 16px; }}
QWidget#pickList {{ background: transparent; }}
QFrame#pickSearch {{ background: {p.BG}; border: 1px solid {p.LINE}; border-radius: 10px; min-height: 34px; }}
QFrame#pickSearch:focus-within {{ border: 1px solid {p.TERRA_SOFT}; }}
QLineEdit#pickSearchInput {{ background: transparent; border: none; font-size: 13px; color: {p.INK}; }}
QFrame#pickRow {{ background: transparent; border-left: 3px solid transparent; border-radius: 9px; }}
QFrame#pickRow:hover {{ background: {p.BG_DEEP}; }}
QFrame#pickRowSel {{ background: {p.TERRA_WASH}; border-left: 3px solid {p.TERRA}; border-radius: 9px; }}
QLabel#pickTitle {{ font-size: 13px; font-weight: 600; color: {p.INK}; background: transparent; }}
QLabel#pickMeta {{ font-size: 11px; color: {p.INK_FAINT}; background: transparent; }}
QFrame#pickBadgeTr {{ background: {p.BLUE_WASH}; border-radius: 9px; }}
QFrame#pickBadgeRw {{ background: {p.TERRA_WASH}; border-radius: 9px; }}
QLabel#pickTagTr {{ background: {p.BLUE_WASH}; color: {p.BLUE}; font-size: 11px; font-weight: 600; border-radius: 8px; padding: 2px 8px; }}
QLabel#pickTagRw {{ background: {p.TERRA_WASH}; color: {p.TERRA}; font-size: 11px; font-weight: 600; border-radius: 8px; padding: 2px 8px; }}
QFrame#pickDivider {{ background: {p.LINE_SOFT}; margin-left: 54px; }}
"""
    # Stage-0 foundation selectors are scoped to new lulu* object names, so
    # existing business pages keep their current appearance until migration.
    from .control_foundation import foundation_qss
    consumer = consumer_theme(p.name)
    return (
        base
        + foundation_qss(p)
        + build_consumer_global_shell_qss(consumer, p)
        + build_consumer_core_workspace_qss(consumer)
        + build_consumer_sample_qss(consumer)
    )


# 模块级配色接口的初始化
# 其它文件用 `T.BG / T.INK / …` 与 `T.QSS`;apply_theme 把激活调色板的值
# 写到这些模块全局，控件在 apply_theme 之后创建即读到当前主题。
def _install(p: Palette) -> None:
    global ACTIVE, QSS
    global BG, BG_DEEP, CARD, INK, INK_SOFT, INK_FAINT, LINE, LINE_SOFT
    global TERRA, TERRA_SOFT, TERRA_WASH, TERRA_DEEP, OLIVE
    global BLUE, BLUE_WASH, GREEN, GREEN_WASH, AMBER, AMBER_WASH, DANGER
    global GRAD_TOP, GRAD_HOVER_TOP, GRAD_HOVER_BOT, NAV_HOVER, RAIL_HOVER
    global SCROLL_HANDLE, SCROLL_HANDLE_HOVER, DROP_BG, HOVER_RAISE, TOOLTIP_BG, TOOLTIP_FG
    ACTIVE = p
    BG, BG_DEEP, CARD = p.BG, p.BG_DEEP, p.CARD
    INK, INK_SOFT, INK_FAINT = p.INK, p.INK_SOFT, p.INK_FAINT
    LINE, LINE_SOFT = p.LINE, p.LINE_SOFT
    TERRA, TERRA_SOFT, TERRA_WASH, TERRA_DEEP = p.TERRA, p.TERRA_SOFT, p.TERRA_WASH, p.TERRA_DEEP
    OLIVE = p.OLIVE
    BLUE, BLUE_WASH = p.BLUE, p.BLUE_WASH
    GREEN, GREEN_WASH = p.GREEN, p.GREEN_WASH
    AMBER, AMBER_WASH = p.AMBER, p.AMBER_WASH
    DANGER = p.DANGER
    GRAD_TOP, GRAD_HOVER_TOP, GRAD_HOVER_BOT = p.GRAD_TOP, p.GRAD_HOVER_TOP, p.GRAD_HOVER_BOT
    NAV_HOVER, RAIL_HOVER = p.NAV_HOVER, p.RAIL_HOVER
    SCROLL_HANDLE, SCROLL_HANDLE_HOVER = p.SCROLL_HANDLE, p.SCROLL_HANDLE_HOVER
    DROP_BG, HOVER_RAISE = p.DROP_BG, p.HOVER_RAISE
    TOOLTIP_BG, TOOLTIP_FG = p.TOOLTIP_BG, p.TOOLTIP_FG
    QSS = build_qss(p)


def apply_theme(name: str) -> Palette:
    """装载唯一的宣纸配色；旧名称保留为兼容输入。"""
    p = THEMES.get(name, WARM)
    _install(p)
    return p


# import 时先装暖陶土，保证 BG/INK/…/QSS 等模块常量始终存在。
_install(WARM)
