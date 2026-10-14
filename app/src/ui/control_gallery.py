"""Developer-only gallery for the stage-0 consumer control foundation."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .control_foundation import (
    LuluButton,
    LuluCheckBox,
    LuluIconButton,
    LuluSegmentedControl,
    LuluSelectField,
    LuluSurface,
    LuluSwitch,
    LuluTextField,
)


def _demo_state(widget, state: str) -> None:
    widget.setProperty("demoState", state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class ControlGallery(QWidget):
    """A real-widget state matrix; never added to product navigation or build entrypoints."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("controlGallery")
        self.setWindowTitle("Lulu 控件基础 Gallery")
        self.setMinimumSize(960, 700)
        self._build()

    def _build(self) -> None:
        scroll = QScrollArea()
        scroll.setObjectName("controlGalleryScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page = LuluSurface("page")
        scroll.setWidget(page)
        content = QVBoxLayout(page)
        content.setContentsMargins(24, 24, 24, 32)
        content.setSpacing(24)

        title = QLabel("Lulu 控件基础 Gallery")
        title.setObjectName("pageTitle")
        subtitle = QLabel("阶段 0 · 新组件专属样式 · 旧业务页面不受影响")
        subtitle.setObjectName("pageMeta")
        content.addWidget(title)
        content.addWidget(subtitle)

        content.addWidget(self._actions_section())
        content.addWidget(self._input_section())
        content.addWidget(self._selection_section())
        content.addWidget(self._surface_section())
        content.addStretch(1)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

    def _section(self, title: str) -> tuple[LuluSurface, QVBoxLayout]:
        card = LuluSurface("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
        return card, layout

    def _actions_section(self) -> LuluSurface:
        card, layout = self._section("动作与状态")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        labels = ("Default", "Hover", "Pressed", "Focus", "Disabled", "Loading", "Danger")
        for column, text in enumerate(labels):
            label = QLabel(text)
            label.setObjectName("pageMeta")
            grid.addWidget(label, 0, column)

        self.primary_button = LuluButton("开始处理", role="primary", size="md", icon="play")
        self.hover_button = LuluButton("导出内容", role="secondary", size="md", icon="export")
        _demo_state(self.hover_button, "hover")
        self.pressed_button = LuluButton("保存设置", role="secondary", size="md")
        _demo_state(self.pressed_button, "pressed")
        self.focus_button = LuluButton("再次检查", role="secondary", size="md")
        _demo_state(self.focus_button, "focus")
        self.disabled_button = LuluButton("不可使用", role="secondary", size="md")
        self.disabled_button.setEnabled(False)
        self.loading_button = LuluButton("生成配音", role="primary", size="md", icon="play")
        self.loading_button.set_loading(True, "正在生成…")
        self.danger_button = LuluButton("删除记录", role="destructive", size="md")
        for column, widget in enumerate((
            self.primary_button,
            self.hover_button,
            self.pressed_button,
            self.focus_button,
            self.disabled_button,
            self.loading_button,
            self.danger_button,
        )):
            grid.addWidget(widget, 1, column)

        size_row = QHBoxLayout()
        size_row.setSpacing(12)
        size_row.addWidget(LuluButton("小按钮", role="secondary", size="sm"))
        size_row.addWidget(LuluButton("默认按钮", role="secondary", size="md"))
        size_row.addWidget(LuluButton("大型主动作", role="primary", size="lg"))
        self.icon_button = LuluIconButton("copy", accessible_name="复制", tooltip="复制当前内容")
        size_row.addWidget(self.icon_button)
        size_row.addStretch(1)
        layout.addLayout(grid)
        layout.addLayout(size_row)
        return card

    def _input_section(self) -> LuluSurface:
        card, layout = self._section("输入与下拉")
        row = QGridLayout()
        row.setHorizontalSpacing(12)
        row.setVerticalSpacing(8)
        self.text_field = LuluTextField(accessible_name="作品链接", placeholder="粘贴作品链接…")
        self.text_field.setText("https://example.com/work")
        self.focus_field = LuluTextField(accessible_name="保存名称", placeholder="输入保存名称")
        _demo_state(self.focus_field, "focus")
        self.error_field = LuluTextField(accessible_name="授权码", placeholder="输入授权码")
        self.error_field.set_error("授权码格式不正确")
        self.disabled_field = LuluTextField(accessible_name="只读来源", placeholder="不可编辑")
        self.disabled_field.setText("自动识别来源")
        self.disabled_field.setEnabled(False)
        self.select_field = LuluSelectField(accessible_name="初筛模型")
        self.select_field.addItems(("Agnes", "DeepSeek", "本地规则"))
        for column, (label_text, widget) in enumerate((
            ("Default", self.text_field),
            ("Focus", self.focus_field),
            ("Error", self.error_field),
            ("Disabled", self.disabled_field),
            ("Select", self.select_field),
        )):
            label = QLabel(label_text)
            label.setObjectName("pageMeta")
            row.addWidget(label, 0, column)
            row.addWidget(widget, 1, column)
        error = QLabel("授权码格式不正确，请检查后重试")
        error.setObjectName("luluErrorText")
        row.addWidget(error, 2, 2)
        layout.addLayout(row)
        return card

    def _selection_section(self) -> LuluSurface:
        card, layout = self._section("选择控件")
        row = QHBoxLayout()
        row.setSpacing(16)
        self.segment = LuluSegmentedControl(
            [("audio", "仅音频"), ("video", "音视频")],
            accessible_name="下载内容",
        )
        self.checkbox = LuluCheckBox("优先抓取字幕", accessible_name="优先抓取字幕")
        self.checkbox.setChecked(True)
        self.checkbox_off = LuluCheckBox("保存原始文件")
        self.checkbox_disabled = LuluCheckBox("暂不可用")
        self.checkbox_disabled.setEnabled(False)
        self.switch_off = LuluSwitch(accessible_name="自动检查更新")
        self.switch_on = LuluSwitch(accessible_name="自动保存")
        self.switch_on.setChecked(True)
        self.switch_disabled = LuluSwitch(accessible_name="禁用开关")
        self.switch_disabled.setEnabled(False)
        for widget in (
            self.segment,
            self.checkbox,
            self.checkbox_off,
            self.checkbox_disabled,
            self.switch_off,
            self.switch_on,
            self.switch_disabled,
        ):
            row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        layout.addLayout(row)
        return card

    def _surface_section(self) -> LuluSurface:
        card, layout = self._section("基础 Surface")
        row = QHBoxLayout()
        row.setSpacing(12)
        self.well = LuluSurface("well")
        well_layout = QVBoxLayout(self.well)
        well_layout.setContentsMargins(12, 12, 12, 12)
        well_layout.addWidget(QLabel("Well · 局部输入与播放器"))
        self.popover = LuluSurface("popover")
        pop_layout = QVBoxLayout(self.popover)
        pop_layout.setContentsMargins(12, 12, 12, 12)
        pop_layout.addWidget(QLabel("Popover · 独立浮层"))
        row.addWidget(self.well, 1)
        row.addWidget(self.popover, 1)
        layout.addLayout(row)
        return card

    def state_summary(self) -> dict:
        return {
            "button_heights": sorted({
                self.primary_button.height(),
                self.hover_button.height(),
                self.loading_button.height(),
            }),
            "loading_stable": self.loading_button.property("state") == "loading",
            "error_state": self.error_field.property("state") == "error",
            "select_count": self.select_field.count(),
            "segment_value": self.segment.value(),
            "checkbox_checked": self.checkbox.isChecked(),
            "switch_on": self.switch_on.isChecked(),
            "icon_accessible": bool(self.icon_button.accessibleName() and self.icon_button.toolTip()),
        }
