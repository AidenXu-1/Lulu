"""Lulu 设置中心：只管理已有 AppConfig 偏好，不复制业务页开关。"""

from __future__ import annotations

import os
from pathlib import Path
import uuid

from PySide6.QtCore import QEvent, QThread, QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QBoxLayout,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..version import __version__
from ..core import models_manager
from ..core.synthesis import voices
from . import dialogs, icons, theme as T
from .consumer_select import ConsumerSelect
from .control_foundation import (
    PaperTextureFrame,
    adopt_button,
    adopt_consumer_theme_scope,
    adopt_scroll_area,
    adopt_surface,
    build_consumer_page_header,
)

_CATEGORY_OPTIONS = (
    ("general", "通用", "提醒、任务与系统行为"),
    ("storage", "文件与存储", "导出下载、资源位置与本地资料"),
    ("skills", "技能", "安装、启停和管理 Lulu 技能"),
    ("cli", "CLI", "让其他 Agent 调用 Lulu"),
    ("feishu", "飞书知识库", "连接、新建位置与表格预设"),
    ("about", "关于", "版本、更新与产品信息"),
)
_PATH_OPTIONS = (
    ("last_output_dir", "文稿导出", "从文稿库或提取结果导出文稿时，默认打开此文件夹。", "storage_document"),
    (
        "media_dir", "视频与封面下载",
        "内容提取中选择的视频和封面保存到这里；主页采集的素材由 Lulu 单独管理。",
        "storage_media",
    ),
    ("recording_dir", "录音文件", "麦克风与系统声音的原始录音保存在这里。", "storage_recording"),
    ("tts_output_dir", "音频下载", "配音生成后先保存在音频库，点击下载或导出时默认使用此位置。", "storage_voice_output"),
    ("voice_library_dir", "音色库", "参考音频和音色资料。更改时复制已有音色，保留旧文件。", "storage_voice_library"),
    ("models_dir", "本地模型与运行环境", "转录与四款配音模型。更改时安全迁移已安装资源，需等待当前任务结束。", "storage_ai_models"),
)

REPOSITORY_URL = "https://github.com/AidenXu-1/Lulu"
AUTHOR_URL = "https://github.com/AidenXu-1"
PRODUCT_BOUNDARY = (
    "Lulu坚持本地优先，本地模式下音频与逐字稿只留在本机，不会自动上传内容。\n"
    "只有主动使用云端模型或软件更新时，相关功能才会联网。"
)


def _settings_workspace_qss(consumer: T.ConsumerTheme) -> str:
    """Settings-only paper hierarchy; no selector escapes the explicit scope."""
    scope = 'QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="settings"]'
    return f"""
    {scope}, {scope} QFrame#coreCanvas {{
        background: {T.ACTIVE.BG}; border: none;
    }}
    {scope} QFrame#settingsWorkbench {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {consumer.surface_focal}, stop:1 {consumer.surface_work});
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_work}px;
    }}
    {scope} QFrame#settingsCategoryNav {{
        background: {consumer.surface_work}; border: none;
        border-radius: {consumer.radius_surface}px;
    }}
    {scope} QFrame#settingsCategoryNav QPushButton {{
        color: {consumer.ink_soft}; background: transparent;
        border: 1px solid transparent; border-radius: {consumer.radius_control}px;
        text-align: left; padding-left: 12px;
    }}
    {scope} QFrame#settingsCategoryNav QPushButton:hover {{
        background: {consumer.secondary_hover}; color: {consumer.ink};
    }}
    {scope} QFrame#settingsCategoryNav QPushButton:checked {{
        background: {consumer.accent_wash}; color: {consumer.accent};
        border-color: {consumer.accent_hairline}; font-weight: 600;
    }}
    {scope} QFrame#settingsPanel {{
        background: {consumer.surface_focal}; border: 1px solid {consumer.line};
        border-radius: {consumer.radius_surface}px;
    }}
    {scope} QFrame#settingsInfoRow,
    {scope} QFrame#settingsControlRow,
    {scope} QFrame#settingsPathRow {{
        background: transparent; border: none;
    }}
    {scope} QFrame#settingsFeishuSectionRule {{
        background: {consumer.accent}; border: none; border-radius: 2px;
    }}
    {scope} QLabel#settingsFeishuSectionTitle {{
        color: {consumer.ink}; font-family: "{consumer.font_display}";
        font-size: 18px; font-weight: 650;
    }}
    {scope} QLabel#settingsFeishuBadge {{
        background: {consumer.accent_wash}; color: {consumer.accent};
        border: 1px solid {consumer.accent_hairline};
        border-radius: 7px; padding: 3px 7px; font-size: 10px; font-weight: 600;
    }}
    {scope} QLineEdit[feishuField="true"] {{
        min-height: 38px; padding: 0 12px; font-size: 13px;
        color: {consumer.ink}; background: {consumer.surface_focal};
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_control}px;
    }}
    {scope} QLineEdit[feishuField="true"]:focus {{
        border: 2px solid {consumer.accent}; padding: 0 11px;
    }}
    {scope} QScrollArea, {scope} QScrollArea > QWidget > QWidget {{
        background: transparent; border: none;
    }}
    """


class StorageRelocateWorker(QThread):
    """在后台切换真实存储消费者；配置保存失败时尽力恢复旧位置。"""

    succeeded = Signal(str, str)
    failed = Signal(str, str, bool, str, str)

    def __init__(self, field: str, old_root: Path, new_root: Path, cfg, parent=None):
        super().__init__(parent)
        if field not in ("voice_library_dir", "models_dir"):
            raise ValueError(f"unsupported storage relocation: {field}")
        self.field = field
        self.old_root = Path(old_root).expanduser()
        self.new_root = Path(new_root).expanduser()
        self.cfg = cfg

    def run(self) -> None:
        old_value = str(getattr(self.cfg, self.field, ""))
        expected_model_dirs = tuple(
            name for name in ("models", "tts") if (self.old_root / name).exists()
        ) if self.field == "models_dir" else ()
        model_relocation_completed = False
        try:
            if self.field == "models_dir":
                models_manager.relocate_models(
                    self.new_root,
                    source_root=self.old_root,
                    progress=lambda _message, _fraction: None,
                )
                model_relocation_completed = True
                resolved = self.new_root
            else:
                resolved = Path(voices.relocate_voice_library(self.new_root))
            setattr(self.cfg, self.field, str(resolved))
            if not bool(self.cfg.save()):
                raise RuntimeError("新位置没有写入本机配置")
        except Exception as exc:  # noqa: BLE001 - 后台边界必须恢复消费者真相
            setattr(self.cfg, self.field, old_value)
            rollback_error = ""
            try:
                if self.field == "models_dir":
                    missing_before = tuple(
                        name for name in expected_model_dirs
                        if not (self.old_root / name).exists()
                    )
                    if model_relocation_completed or missing_before:
                        models_manager.relocate_models(
                            self.old_root,
                            source_root=self.new_root,
                            progress=lambda _message, _fraction: None,
                        )
                    missing_after = tuple(
                        name for name in expected_model_dirs
                        if not (self.old_root / name).exists()
                    )
                    if missing_after:
                        rollback_error = (
                            "；恢复旧位置后仍缺少：" + "、".join(missing_after)
                        )
                elif self.field == "voice_library_dir":
                    voices.configure_voice_library_dir(self.old_root)
            except Exception as rollback_exc:  # noqa: BLE001
                missing_after = tuple(
                    name for name in expected_model_dirs
                    if not (self.old_root / name).exists()
                )
                if self.field != "models_dir" or missing_after:
                    rollback_error = f"；恢复旧位置时遇到问题：{rollback_exc}"
            self.failed.emit(
                self.field,
                f"{exc}{rollback_error}",
                not bool(rollback_error),
                str(self.old_root),
                str(self.new_root),
            )
            return
        self.succeeded.emit(self.field, str(resolved))


class AuthorContactDialog(QDialog):
    """二维码只在用户主动点击后显示，不在关于页预览。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("作者联系方式")
        self.setWindowModality(Qt.ApplicationModal)
        self.setModal(True)
        self.resize(500, 650)
        self.setMinimumSize(420, 560)
        self.setStyleSheet(
            f"QDialog{{background:{T.BG};color:{T.INK};}}"
            f"QLabel{{background:transparent;color:{T.INK};}}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(10)
        title = QLabel("兆基｜AI运营")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"font-size:18px;font-weight:600;color:{T.INK};")
        root.addWidget(title)
        meta = QLabel("使用微信扫码联系作者")
        meta.setObjectName("pageMeta")
        meta.setAlignment(Qt.AlignCenter)
        root.addWidget(meta)
        root.addSpacing(6)

        self.qr_pixmap = QPixmap(T.asset("author_contact_wechat.jpg"))
        self.qr_label = QLabel()
        self.qr_label.setObjectName("authorContactQr")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        if self.qr_pixmap.isNull():
            self.qr_label.setText("二维码资源暂不可用")
        else:
            self.qr_label.setPixmap(
                self.qr_pixmap.scaled(410, 520, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        root.addWidget(self.qr_label, 1)
        close_button = QPushButton("关闭")
        adopt_button(close_button, role="secondary", size="md", accessible_name="关闭作者联系方式")
        close_button.clicked.connect(self.accept)
        root.addWidget(close_button, 0, Qt.AlignHCenter)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None:
            target = parent.window().frameGeometry().center()
            frame = self.frameGeometry()
            frame.moveCenter(target)
            self.move(frame.topLeft())


_SETTINGS_READ_WORKERS: set[QThread] = set()


class _SettingsReadWorker(QThread):
    done = Signal(bool, object)

    def __init__(self, read):
        super().__init__()
        self._read = read

    def run(self) -> None:
        try:
            value = self._read()
        except Exception:
            self.done.emit(False, None)
        else:
            self.done.emit(True, value)


def _start_settings_read(worker: _SettingsReadWorker) -> None:
    # A read may be waiting on macOS consent when its page is closed.
    # Keep the thread alive until the system call returns; never terminate it.
    _SETTINGS_READ_WORKERS.add(worker)
    worker.finished.connect(lambda: _SETTINGS_READ_WORKERS.discard(worker))
    worker.start()


class SettingsPage(QWidget):
    category_changed = Signal(str)
    """独立设置中心；业务页单次选择仍由业务页优先。"""

    preferences_changed = Signal(str)
    notification_permission_finished = Signal(object)
    storage_activity_changed = Signal(bool)
    install_requested = Signal()
    manual_download = Signal(str)
    legacy_tasks_migrated = Signal(bool)

    def __init__(
        self,
        cfg,
        parent: QWidget | None = None,
        *,
        update_controller=None,
        notification_service=None,
        external_storage_busy=None,
        inheritance_controller=None,
        agent_entry_installer_factory=None,
        agent_entry_dmg_selector=None,
        agent_skill_status_provider=None,
        lazy_categories=False,
    ):
        super().__init__(parent)
        self.cfg = cfg
        self._lazy_categories = lazy_categories
        self._loaded_categories = set()
        self.update_controller = update_controller
        self.notification_service = notification_service
        if agent_entry_installer_factory is None:
            from ..core.agent_entry.formal_install import build_default_formal_installer

            agent_entry_installer_factory = build_default_formal_installer
        self._agent_entry_installer_factory = agent_entry_installer_factory
        self._agent_entry_dmg_selector = (
            agent_entry_dmg_selector or self._select_agent_entry_dmg
        )
        if agent_skill_status_provider is None:
            if QGuiApplication.platformName() == "offscreen":
                agent_skill_status_provider = lambda: "not_installed"
            else:
                from ..core.agent_entry.formal_install import (
                    inspect_default_agent_skill_status,
                )

                agent_skill_status_provider = inspect_default_agent_skill_status
        self._agent_skill_status_provider = agent_skill_status_provider
        self.agent_skill_state = "checking"
        self._settings_closing = False
        self._skill_status_worker = None
        self._feishu_config_worker = None
        self._feishu_read_failed = False
        self._feishu_config_loading = False
        self._external_storage_busy = external_storage_busy or (lambda: False)
        self._inheritance_controller = inheritance_controller
        self._storage_activity = False
        self.category_order = tuple(key for key, _label, _meta in _CATEGORY_OPTIONS)
        self._all_category_order = self.category_order
        self.current_category = "general"
        self.category_buttons: dict[str, QPushButton] = {}
        self.detail_scrolls: dict[str, QScrollArea] = {}
        self.setting_panels: list[QFrame] = []
        self.path_labels: dict[str, QLabel] = {}
        self.path_name_labels: dict[str, QLabel] = {}
        self.path_buttons: dict[str, QPushButton] = {}
        self.path_open_buttons: dict[str, QPushButton] = {}
        self.path_icon_labels: dict[str, QLabel] = {}
        self.path_icon_names: dict[str, str] = {}
        self.path_titles: dict[str, str] = {}
        self.path_description_labels: dict[str, QLabel] = {}
        self.path_action_layouts: list[QBoxLayout] = []
        self.cli_feature_layouts: list[tuple[QBoxLayout, QLabel]] = []
        self.cli_action_layouts: list[QBoxLayout] = []
        self.feishu_auth_badge: QLabel | None = None
        self.feishu_app_id: QLineEdit | None = None
        self.feishu_app_secret: QLineEdit | None = None
        self.feishu_login_button: QPushButton | None = None
        self.feishu_logout_button: QPushButton | None = None
        self.feishu_single_select: ConsumerSelect | None = None
        self.feishu_selection_select: ConsumerSelect | None = None
        self.feishu_created_single_select: ConsumerSelect | None = None
        self.feishu_created_selection_select: ConsumerSelect | None = None
        self.feishu_existing_manage_button: QPushButton | None = None
        self.feishu_created_manage_button: QPushButton | None = None
        self._feishu_credentials_loaded = False
        self._feishu_spaces: list[dict] = []
        self._pending_creation_preset = False
        self._feishu_oauth_worker: QThread | None = None
        self._feishu_spaces_worker: QThread | None = None
        self._storage_workers: dict[str, StorageRelocateWorker] = {}
        self._pending_notification_policy: tuple[str, str] | None = None
        self.notification_permission_finished.connect(self._finish_notification_permission)
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.setObjectName("root")
        self.setProperty("workspacePage", "settings")
        adopt_consumer_theme_scope(self, "coreWorkspace")
        # SettingsPage 也会被隔离预览和测试直接创建；显式带上成熟 Lulu
        # 控件基础，避免离开 MainWindow 父级后退化为系统原生矩形。
        self.setStyleSheet(T.QSS + _settings_workspace_qss(self._consumer))
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        canvas = PaperTextureFrame(
            strength=self._consumer.paper_canvas_opacity,
            layer="canvas",
        )
        canvas.setObjectName("coreCanvas")
        canvas_lay = QVBoxLayout(canvas)
        canvas_lay.setContentsMargins(14, 0, 14, 28)
        canvas_lay.setSpacing(0)
        root.addWidget(canvas)

        header, self.title_label, self.meta_label = build_consumer_page_header(
            "设置",
            "管理 Lulu 的全局偏好。业务页里的单次选择仍以当次为准。",
        )
        canvas_lay.addWidget(header)
        canvas_lay.addSpacing(24)

        body = PaperTextureFrame(
            strength=self._consumer.paper_texture_opacity,
            corner_radius=self._consumer.radius_work,
            layer="work",
        )
        body.setObjectName("settingsWorkbench")
        self.settings_workbench = body
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(12, 12, 12, 12)
        body_lay.setSpacing(24)
        self.category_nav = self._build_navigation()
        body_lay.addWidget(self.category_nav, 0, Qt.AlignTop)

        self.category_stack = QStackedWidget()
        self.category_stack.setObjectName("settingsCategoryStack")
        self.category_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        body_lay.addWidget(self.category_stack, 1)
        canvas_lay.addWidget(body, 1)

        self._category_builders = {
            "general": self._general_page,
            "storage": self._storage_page,
            "skills": self._skills_page,
            "cli": self._cli_page,
            "feishu": self._feishu_page,
            "about": self._about_page,
        }
        for key in self._all_category_order:
            if self._lazy_categories:
                self.category_stack.addWidget(QWidget())
            else:
                self.category_stack.addWidget(self._category_builders[key]())
                self._loaded_categories.add(key)
        self.set_category("general")

    def _build_navigation(self) -> QFrame:
        nav = QFrame()
        nav.setObjectName("settingsCategoryNav")
        nav.setFixedWidth(180)
        lay = QVBoxLayout(nav)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(5)
        self._category_group = QButtonGroup(self)
        self._category_group.setExclusive(True)
        for key, label, description in _CATEGORY_OPTIONS:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setFixedHeight(40)
            button.setAccessibleName(f"设置分类：{label}")
            button.setAccessibleDescription(description)
            button.setToolTip(description)
            button.setProperty("settingsCategory", key)
            button.setFocusPolicy(Qt.StrongFocus)
            button.setCursor(Qt.PointingHandCursor)
            button.installEventFilter(self)
            button.clicked.connect(lambda _checked=False, category=key: self.set_category(category))
            self._category_group.addButton(button)
            self.category_buttons[key] = button
            lay.addWidget(button)
        lay.addStretch(1)
        return nav

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        category = watched.property("settingsCategory") if isinstance(watched, QPushButton) else None
        if (
            category
            and event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
        ):
            self.set_category(str(category))
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        # 窄窗口仍保留左侧分类，但收紧导航宽度，把纸面空间让给正文；
        # 620px 级别下四个分类也不会挤出横向滚动。
        narrow = self.width() < 720
        self.category_nav.setFixedWidth(124 if narrow else 180)
        direction = (
            QBoxLayout.Direction.TopToBottom
            if narrow
            else QBoxLayout.Direction.LeftToRight
        )
        for actions in self.path_action_layouts:
            actions.setDirection(direction)
        for actions in self.cli_action_layouts:
            actions.setDirection(direction)
        for row, title in self.cli_feature_layouts:
            row.setDirection(direction)
            row.setSpacing(5 if narrow else 18)
            title.setMinimumWidth(0 if narrow else 128)
            title.setMaximumWidth(16777215 if narrow else 128)

    def set_category(self, key: str) -> None:
        if key not in self._all_category_order:
            return
        if key not in self._loaded_categories:
            index = self._all_category_order.index(key)
            page = self._category_builders[key]()
            old = self.category_stack.widget(index)
            self.category_stack.removeWidget(old)
            old.deleteLater()
            self.category_stack.insertWidget(index, page)
            self._loaded_categories.add(key)
        self.current_category = key
        self.settings_workbench.set_paper_texture_strength(
            self._consumer.paper_texture_opacity * (0.55 if key == "about" else 1.0)
        )
        self.category_stack.setCurrentIndex(self._all_category_order.index(key))
        for category, button in self.category_buttons.items():
            button.blockSignals(True)
            button.setChecked(category == key)
            button.blockSignals(False)
        if key == "feishu":
            self._refresh_feishu_settings()
        if key == "skills":
            self.skills_page.refresh()
        self.category_changed.emit(key)

    def _skills_page(self):
        from .skills_page import SkillsPage
        self.skills_page = SkillsPage(self)
        return self.skills_page

    def _cli_page(self):
        from .cli_info import CLI_FEATURES, CLI_UNAVAILABLE, AGENT_SKILL_REPOSITORY_URL
        page, col = self._detail_page(
            "cli", "CLI", "让你常用的 Agent 连接 Lulu，使用本机已有的内容和工具。",
        )
        card, lay = self._card("可以调用什么", "共用 Lulu 的文稿、任务与结果。连接时会检查当前版本实际开放的能力。")
        self._cli_feature_rows(lay, CLI_FEATURES)
        col.addWidget(card)

        card, lay = self._card("尚未向 CLI 开放", "以下功能仍可在 Lulu 应用内使用。")
        self._cli_feature_rows(lay, CLI_UNAVAILABLE)
        col.addWidget(card)

        card, lay = self._card("连接你的 Agent", "复制下面的安装提示词，发给你常用的 Agent，由它安装 Lulu Agent Skill。")
        self.cli_repository_url = AGENT_SKILL_REPOSITORY_URL
        self.cli_repository_button = QPushButton("打开 GitHub 仓库")
        adopt_button(self.cli_repository_button, role="secondary", accessible_name="打开 Lulu Agent Skill 的 GitHub 仓库")
        self.cli_repository_button.clicked.connect(self._open_cli_repository)
        self.cli_copy_button = QPushButton("复制安装提示词")
        adopt_button(self.cli_copy_button, role="primary", accessible_name="复制 Lulu Agent Skill 安装提示词")
        self.cli_copy_button.clicked.connect(self._copy_cli_install_prompt)
        actions = QBoxLayout(QBoxLayout.TopToBottom if self.width() < 720 else QBoxLayout.LeftToRight)
        actions.setSpacing(8)
        actions.addWidget(self.cli_copy_button, 0, Qt.AlignLeft)
        actions.addWidget(self.cli_repository_button, 0, Qt.AlignLeft)
        actions.addStretch(1)
        self.cli_action_layouts.append(actions)
        lay.addLayout(actions)
        self.cli_install_note = QLabel("复制后发给 Agent 即可；安装前会检查与本机 Lulu 的兼容性。" if self.cli_repository_url else "Skill 的公开仓库尚未配置，配置后即可复制安装提示词。")
        self.cli_install_note.setObjectName("settingsInlineNote")
        self.cli_install_note.setWordWrap(True)
        lay.addWidget(self.cli_install_note)
        self.cli_repository_button.setEnabled(bool(self.cli_repository_url))
        self.cli_copy_button.setEnabled(bool(self.cli_repository_url))
        col.addWidget(card)

        self.cli_details_button = QPushButton("本机连接与命令参考")
        self.cli_details_button.setCheckable(True)
        adopt_button(self.cli_details_button, role="tertiary", accessible_name="展开本机连接与命令参考")
        col.addWidget(self.cli_details_button, 0, Qt.AlignLeft)
        details = self._agent_skill_card()
        details.setVisible(False)
        self.cli_details_button.toggled.connect(details.setVisible)
        col.addWidget(details)
        col.addStretch(1)
        return page

    def _cli_feature_rows(self, layout, features):
        narrow = self.width() < 720
        for index, (title, description) in enumerate(features):
            if index:
                self._setting_divider(layout)
            row = QFrame()
            row.setObjectName("settingsCliFeatureRow")
            line = QBoxLayout(QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight, row)
            line.setContentsMargins(0, 9, 0, 9)
            line.setSpacing(5 if narrow else 18)
            name = QLabel(title)
            name.setObjectName("fieldLabel")
            name.setWordWrap(True)
            name.setMinimumWidth(0 if narrow else 128)
            name.setMaximumWidth(16777215 if narrow else 128)
            line.addWidget(name, 0, Qt.AlignTop)
            detail = QLabel(description)
            detail.setObjectName("pageMeta")
            detail.setWordWrap(True)
            line.addWidget(detail, 1)
            self.cli_feature_layouts.append((line, name))
            layout.addWidget(row)

    def _open_cli_repository(self):
        if self.cli_repository_url and not QDesktopServices.openUrl(QUrl(self.cli_repository_url)):
            self.cli_install_note.setText("暂时无法打开浏览器，请稍后重试。")

    def _copy_cli_install_prompt(self):
        from .cli_info import agent_skill_install_prompt
        if self.cli_repository_url:
            QGuiApplication.clipboard().setText(agent_skill_install_prompt(self.cli_repository_url))
            self.cli_install_note.setText("已复制。现在可以粘贴给你常用的 Agent。")

    def _agent_skill_card(self) -> QFrame:
        card, lay = self._card(
            "Lulu Agent Skill",
            "让支持 Skill 的 Agent 通过 Lulu 处理任务。",
        )
        self.agent_skill_status_label = QLabel("尚未安装")
        self.agent_skill_status_label.setObjectName("settingsInlineNote")
        self.agent_skill_status_label.setWordWrap(True)
        lay.addWidget(self.agent_skill_status_label)
        self.agent_skill_install_button = QPushButton("安装 Lulu Agent Skill")
        adopt_button(
            self.agent_skill_install_button,
            role="primary",
            size="md",
            accessible_name="安装 Lulu Agent Skill",
        )
        self.agent_skill_install_button.clicked.connect(self._install_agent_skill)
        lay.addWidget(self.agent_skill_install_button, 0, Qt.AlignLeft)
        self.tools_cli_button = QPushButton("工具与 CLI 入口")
        adopt_button(self.tools_cli_button, role="secondary", accessible_name="工具与 CLI 入口")
        self.tools_cli_button.clicked.connect(self._show_tools_cli)
        lay.addWidget(self.tools_cli_button, 0, Qt.AlignLeft)
        self.agent_skill_status_label.setText("正在检查安装状态…")
        self.agent_skill_install_button.setEnabled(False)
        worker = _SettingsReadWorker(self._agent_skill_status_provider)
        self._skill_status_worker = worker
        worker.done.connect(self._finish_skill_status)
        _start_settings_read(worker)
        return card

    @Slot(bool, object)
    def _finish_skill_status(self, ok: bool, value) -> None:
        if self._settings_closing or self.agent_skill_state != "checking":
            return
        self._show_agent_skill_state(str(value) if ok else "incompatible")

    def _show_agent_skill_state(self, state: str) -> None:
        presentations = {
            "not_installed": ("尚未安装", "安装 Lulu Agent Skill", True, True),
            "ready": ("已安装，适配当前 Lulu", "安装 Lulu Agent Skill", False, False),
            "update_available": (
                "可更新为当前 Lulu 的配套版本",
                "更新 Lulu Agent Skill",
                True,
                True,
            ),
            "ownership_conflict": (
                "检测到现有其他内容，未覆盖",
                "安装 Lulu Agent Skill",
                False,
                False,
            ),
            "incompatible": (
                "版本不兼容，已停止",
                "更新 Lulu Agent Skill",
                True,
                True,
            ),
            "incomplete": (
                "安装/更新未完成，现场已保留",
                "更新 Lulu Agent Skill",
                False,
                False,
            ),
            "rollback_complete": (
                "安装/更新未完成，已安全恢复到操作前",
                "更新 Lulu Agent Skill",
                True,
                True,
            ),
        }
        if state not in presentations:
            state = "incompatible"
        status, action, visible, enabled = presentations[state]
        self.agent_skill_state = state
        self.agent_skill_status_label.setText(status)
        self.agent_skill_install_button.setText(action)
        self.agent_skill_install_button.setVisible(visible)
        self.agent_skill_install_button.setEnabled(enabled)

    def _show_tools_cli(self) -> None:
        from .tools_cli_dialog import ToolsCliDialog
        dialog = ToolsCliDialog(self)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.open()

    def _install_agent_skill(self) -> None:
        from ..core.agent_entry.formal_install import DmgSelectionRequired

        previous_state = self.agent_skill_state
        self.agent_skill_state = "installing"
        self.agent_skill_status_label.setText(
            "正在更新" if previous_state == "update_available" else "正在安装"
        )
        self.agent_skill_install_button.setEnabled(False)
        try:
            try:
                installer = self._agent_entry_installer_factory()
            except DmgSelectionRequired:
                selected_dmg = self._agent_entry_dmg_selector()
                if not selected_dmg:
                    self._show_agent_skill_state(previous_state)
                    return
                self.agent_skill_status_label.setText("正在核对当前 Lulu 的配套版本")
                installer = self._agent_entry_installer_factory(Path(selected_dmg))
            receipt = installer.install(transaction_id=f"agent-install-{uuid.uuid4().hex}")
            if (
                type(receipt) is not dict
                or receipt.get("phase") != "COMMITTED"
                or receipt.get("state") != "ready"
                or receipt.get("effect_count") not in {0, 1, 2, 3}
            ):
                raise RuntimeError("AGENT_ENTRY_FINAL_VERIFICATION_FAILED")
        except Exception as exc:  # noqa: BLE001 - product boundary maps closed states to visible truth
            state = getattr(exc, "state", "update_required")
            visible_state = {
                "ownership_conflict": "ownership_conflict",
                "rollback_incomplete": "incomplete",
                "rollback_complete": "rollback_complete",
            }.get(state, "incompatible")
            self._show_agent_skill_state(visible_state)
            return
        self._show_agent_skill_state("ready")

    def _select_agent_entry_dmg(self) -> Path | None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择当前 Lulu 配套的 DMG",
            str(Path.home() / "Downloads"),
            "Lulu 安装包 (*.dmg)",
        )
        return Path(path) if path else None

    def _detail_page(self, key: str, title: str, description: str) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setObjectName(f"settingsDetail_{key}")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        adopt_scroll_area(scroll)
        scroll.viewport().setProperty("settingsDetailViewport", key)
        scroll.viewport().installEventFilter(self)

        viewport = QWidget()
        viewport.setObjectName("root")
        outer = QHBoxLayout(viewport)
        outer.setContentsMargins(0, 0, 12, 24)
        outer.setAlignment(Qt.AlignTop | (Qt.AlignLeft if key == "about" else Qt.AlignHCenter))
        content = QWidget()
        content.setObjectName("settingsDetailContent")
        content.setMaximumWidth(740 if key == "about" else 900)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12 if key == "about" else 14)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        if key == "about":
            heading.setStyleSheet(f'font-family:"{T.SERIF}";font-size:22px;font-weight:600;color:{self._consumer.ink};')
        col.addWidget(heading)
        if description:
            subtitle = QLabel(description)
            subtitle.setObjectName("pageMeta")
            subtitle.setWordWrap(True)
            col.addWidget(subtitle)
            col.addSpacing(8)
        outer.addWidget(content, 1)
        scroll.setWidget(viewport)
        self.detail_scrolls[key] = scroll
        return scroll, col

    def _card(self, title: str, description: str = "", *, variant: str = "settings") -> tuple[QFrame, QVBoxLayout]:
        card = PaperTextureFrame(
            strength=self._consumer.paper_texture_opacity,
            corner_radius=self._consumer.radius_surface,
            layer="section",
        )
        adopt_surface(card, "section", variant=variant)
        card.setObjectName("settingsPanel")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(7)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        lay.addWidget(heading)
        if description:
            desc = QLabel(description)
            desc.setObjectName("pageMeta")
            desc.setWordWrap(True)
            lay.addWidget(desc)
        self.setting_panels.append(card)
        return card, lay

    def _feishu_section(
        self,
        title: str,
        *,
        header_action: QWidget | None = None,
    ) -> tuple[QWidget, QVBoxLayout]:
        """区域标题独立于功能卡片外，卡片内只保留可操作内容。"""

        section = QWidget()
        section_lay = QVBoxLayout(section)
        section_lay.setContentsMargins(0, 0, 0, 0)
        section_lay.setSpacing(8)
        header = QWidget()
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(2, 0, 0, 0)
        header_lay.setSpacing(9)
        rule = QFrame()
        rule.setObjectName("settingsFeishuSectionRule")
        rule.setFixedSize(4, 19)
        header_lay.addWidget(rule, 0, Qt.AlignVCenter)
        heading = QLabel(title)
        heading.setObjectName("settingsFeishuSectionTitle")
        header_lay.addWidget(heading)
        header_lay.addStretch(1)
        if header_action is not None:
            header_lay.addWidget(header_action, 0, Qt.AlignVCenter)
        section_lay.addWidget(header)

        card = PaperTextureFrame(
            strength=self._consumer.paper_texture_opacity,
            corner_radius=self._consumer.radius_surface,
            layer="section",
        )
        adopt_surface(card, "section", variant="settings")
        card.setObjectName("settingsPanel")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(18, 14, 18, 14)
        card_lay.setSpacing(7)
        self.setting_panels.append(card)
        section_lay.addWidget(card)
        return section, card_lay

    def _feishu_control_row(
        self,
        lay: QVBoxLayout,
        title: str,
        description: str,
        control: QWidget,
    ) -> None:
        row = QFrame()
        row.setObjectName("settingsControlRow")
        row_lay = QVBoxLayout(row)
        row_lay.setContentsMargins(0, 8, 0, 8)
        row_lay.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(3)
        name = QLabel(title)
        name.setObjectName("fieldLabel")
        text.addWidget(name)
        if description:
            detail = QLabel(description)
            detail.setObjectName("pageMeta")
            detail.setWordWrap(True)
            text.addWidget(detail)
        row_lay.addLayout(text)
        control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row_lay.addWidget(control)
        lay.addWidget(row)

    def _info_row(self, lay: QVBoxLayout, title: str, description: str, status: str = "") -> None:
        row = QFrame()
        row.setObjectName("settingsInfoRow")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 7, 0, 7)
        row_lay.setSpacing(14)
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("fieldLabel")
        text.addWidget(name)
        detail = QLabel(description)
        detail.setObjectName("pageMeta")
        detail.setWordWrap(True)
        text.addWidget(detail)
        row_lay.addLayout(text, 1)
        if status:
            badge = QLabel(status)
            badge.setStyleSheet(
                f"background:{self._consumer.accent_wash};color:{self._consumer.accent};"
                f"border:1px solid {self._consumer.accent_hairline};"
                "border-radius:7px;padding:4px 8px;font-size:11px;font-weight:600;"
            )
            row_lay.addWidget(badge, 0, Qt.AlignVCenter)
        lay.addWidget(row)

    def _general_page(self) -> QWidget:
        page, col = self._detail_page(
            "general", "通用", "决定任务何时提醒你、完成后去哪里，以及处理期间如何保护任务。"
        )
        task_card, task_lay = self._card("任务与系统")
        current_policy = str(getattr(self.cfg, "notification_policy", "never"))
        self.notification_select = ConsumerSelect(
            (
                ("always", "始终提醒"),
                ("inactive", "仅 Lulu 不在前台时提醒"),
                ("never", "从不提醒"),
            ),
            value=current_policy,
            accessible_name="任务完成提醒",
            consumer_theme_scope="coreWorkspace",
            consumer_theme_name=T.ACTIVE.name,
        )
        self.notification_select.setMinimumWidth(220)
        self.notification_select.value_changed.connect(self._pick_notification_policy)
        self._setting_row(
            task_lay,
            "任务完成提醒",
            "使用 macOS 系统通知；只显示固定隐私文案。",
            self.notification_select,
        )
        self.notification_status_label = QLabel(
            "通知只会写“任务已完成”，不会包含文件名、文案或其他内容。"
        )
        self.notification_status_label.setObjectName("settingsInlineNote")
        self.notification_status_label.setWordWrap(True)
        self.notification_status_label.setStyleSheet(
            f"color:{T.INK_FAINT};font-size:11px;padding:0 2px;background:transparent;"
        )
        task_lay.addWidget(self.notification_status_label)
        self._setting_divider(task_lay)

        from .update_ui import UpdateSwitch

        self.failure_notification_switch = UpdateSwitch()
        self.failure_notification_switch.setObjectName("failureNotificationSwitch")
        self.failure_notification_switch.setAccessibleName("失败任务也提醒")
        self.failure_notification_switch.setChecked(
            bool(getattr(self.cfg, "notify_on_failure", True))
        )
        self.failure_notification_switch.toggled.connect(
            lambda checked: self._save_value("notify_on_failure", bool(checked))
        )
        self._setting_row(
            task_lay,
            "失败任务也提醒",
            "沿用上方通知时机，只显示“任务处理失败”，不包含错误详情。",
            self.failure_notification_switch,
        )
        self._setting_divider(task_lay)

        self.notification_sound_switch = UpdateSwitch()
        self.notification_sound_switch.setObjectName("notificationSoundSwitch")
        self.notification_sound_switch.setAccessibleName("任务完成提示音")
        self.notification_sound_switch.setChecked(
            bool(getattr(self.cfg, "notification_sound_enabled", True))
        )
        self.notification_sound_switch.toggled.connect(
            lambda checked: self._save_value("notification_sound_enabled", bool(checked))
        )
        self._setting_row(
            task_lay,
            "任务完成提示音",
            "关闭后仍保留通知横幅，只是不播放系统提示音。",
            self.notification_sound_switch,
        )
        self._setting_divider(task_lay)

        self.prevent_sleep_switch = UpdateSwitch()
        self.prevent_sleep_switch.setObjectName("preventSleepSwitch")
        self.prevent_sleep_switch.setAccessibleName("处理任务时防止 Mac 自动休眠")
        self.prevent_sleep_switch.setAccessibleDescription(
            "开启后，只在 Lulu 正在处理任务时阻止 Mac 自动休眠，任务结束立即释放。"
        )
        self.prevent_sleep_switch.setChecked(
            bool(getattr(self.cfg, "prevent_sleep_during_tasks", False))
        )
        self.prevent_sleep_switch.toggled.connect(self._pick_prevent_sleep)
        self._setting_row(
            task_lay,
            "处理任务时防止 Mac 自动休眠",
            "仅在任务进行期间生效；最后一个任务结束后立即恢复系统原本行为。",
            self.prevent_sleep_switch,
        )
        self._setting_divider(task_lay)

        self.exit_protection_switch = UpdateSwitch()
        self.exit_protection_switch.setObjectName("exitProtectionSwitch")
        self.exit_protection_switch.setAccessibleName("运行中退出保护")
        self.exit_protection_switch.setChecked(
            bool(getattr(self.cfg, "confirm_exit_during_tasks", True))
        )
        self.exit_protection_switch.toggled.connect(
            lambda checked: self._save_value("confirm_exit_during_tasks", bool(checked))
        )
        self._setting_row(
            task_lay,
            "运行中退出保护",
            "仍有任务处理时，退出 Lulu 前先询问，避免误操作中断任务。",
            self.exit_protection_switch,
        )
        self._setting_divider(task_lay)

        self.completion_action_select = ConsumerSelect(
            (("stay", "停留当前页面"), ("open_result", "打开对应结果")),
            value=str(getattr(self.cfg, "completion_action", "stay")),
            accessible_name="任务完成后的行为",
            consumer_theme_scope="coreWorkspace",
            consumer_theme_name=T.ACTIVE.name,
        )
        self.completion_action_select.setMinimumWidth(220)
        self.completion_action_select.value_changed.connect(
            lambda value: self._save_value("completion_action", value)
        )
        self._setting_row(
            task_lay,
            "任务完成后的行为",
            "打开结果只切换 Lulu 内部页面，不会把 App 强行抢到前台。",
            self.completion_action_select,
        )
        col.addWidget(task_card)
        col.addStretch(1)
        return page

    @staticmethod
    def _setting_divider(lay: QVBoxLayout) -> None:
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background:{T.LINE_SOFT};border:none;")
        lay.addWidget(divider)

    def _setting_row(
        self,
        lay: QVBoxLayout,
        title: str,
        description: str,
        control: QWidget,
    ) -> None:
        row = QFrame()
        row.setObjectName("settingsControlRow")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 10, 0, 10)
        row_lay.setSpacing(18)
        text = QVBoxLayout()
        text.setSpacing(3)
        from .update_ui import AccessibleBuddyLabel

        name = AccessibleBuddyLabel(title)
        name.setObjectName("fieldLabel")
        name.setBuddy(control)
        name.setCursor(Qt.PointingHandCursor)
        text.addWidget(name)
        detail = QLabel(description)
        detail.setObjectName("pageMeta")
        detail.setWordWrap(True)
        text.addWidget(detail)
        row_lay.addLayout(text, 1)
        row_lay.addWidget(control, 0, Qt.AlignVCenter)
        lay.addWidget(row)

    def _storage_page(self) -> QWidget:
        page, col = self._detail_page(
            "storage", "文件与存储",
            "安排导出文件的位置，查看 Lulu 在本机保存的资料。",
        )
        self.storage_groups = {}
        groups = (
            ("导出与下载", "更改后用于新的导出与下载；已有文件留在原处。每次操作仍可另选位置。", _PATH_OPTIONS[:4]),
            ("音色与模型", "这些位置保存可重复使用的资源。更改位置前会说明如何处理已有文件。", _PATH_OPTIONS[4:]),
        )
        for title, description, options in groups:
            card, lay = self._card(title, description)
            self.storage_groups[title] = card
            for index, option in enumerate(options):
                self._storage_path_row(lay, option)
                if index < len(options) - 1:
                    self._setting_divider(lay)
            col.addWidget(card)

        card, lay = self._card("Lulu 资料", "文稿、音频和对话自动保存在本机，由 Lulu 管理。导出与下载会另存一份。")
        self.storage_groups["Lulu 资料"] = card
        note = QLabel("需要恢复或清理文稿、音频时，请到资料库的「最近删除」。转录过程中产生的临时文件会在任务结束后清理。")
        note.setWordWrap(True)
        note.setObjectName("pageMeta")
        lay.addWidget(note)
        from .legacy_task_migration import LegacyTaskMigrationPanel
        self.legacy_task_migration = (LegacyTaskMigrationPanel(controller=self._inheritance_controller, parent=self)
            if self._inheritance_controller is not None else None)
        if self.legacy_task_migration is not None:
            lay.addWidget(self.legacy_task_migration)
        self.managed_locations_button = QPushButton("查看资料位置")
        self.managed_locations_button.setCheckable(True)
        adopt_button(self.managed_locations_button, role="tertiary", accessible_name="展开本机资料位置")
        lay.addWidget(self.managed_locations_button, 0, Qt.AlignLeft)
        self.managed_locations = QWidget()
        managed_lay = QVBoxLayout(self.managed_locations)
        managed_lay.setContentsMargins(0, 8, 0, 0)
        managed_lay.setSpacing(16)
        from ..core import library
        from ..core.agent_workspace.skills import skill_root
        root = Path(library.LIBRARY_DIR)
        locations = (
            ("文稿与版本记录", root, "文稿正文、分类、历史版本与最近删除记录。"),
            ("库内音频", root / "audio", "Lulu 生成的音频；引用的外部音频仍在原位置。"),
            ("AI 对话", root / ".workspace", "文稿旁的会话、消息与对话草稿。"),
            ("主页采集资料", models_manager.app_data_dir() / "collection", "采集记录、封面和下载的音视频。"),
            ("已安装技能", skill_root(), "在「技能」设置中安装或卸载。"),
        )
        self.managed_path_labels = {}
        self.managed_open_buttons = {}
        for title, path, description in locations:
            row = QHBoxLayout()
            text = QVBoxLayout()
            name = QLabel(title)
            name.setObjectName("fieldLabel")
            text.addWidget(name)
            hint = QLabel(description)
            hint.setWordWrap(True)
            hint.setObjectName("pageMeta")
            text.addWidget(hint)
            label = QLabel(str(path))
            label.setWordWrap(True)
            label.setTextFormat(Qt.PlainText)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            label.setObjectName("pageMeta")
            self.managed_path_labels[title] = label
            text.addWidget(label)
            row.addLayout(text, 1)
            button = QPushButton("打开")
            adopt_button(button, role="tertiary", size="sm", accessible_name=f"在访达打开{title}")
            button.clicked.connect(lambda _checked=False, target=path: self._open_storage_path(target, managed=True))
            self.managed_open_buttons[title] = button
            row.addWidget(button)
            managed_lay.addLayout(row)
        self.managed_locations.hide()
        self.managed_locations_button.toggled.connect(self._toggle_managed_locations)
        lay.addWidget(self.managed_locations)
        col.addWidget(card)
        col.addStretch(1)
        return page

    def _toggle_managed_locations(self, checked: bool) -> None:
        self.managed_locations.setVisible(checked)
        self.managed_locations_button.setText("收起资料位置" if checked else "查看资料位置")
        self.managed_locations_button.setAccessibleName("收起本机资料位置" if checked else "展开本机资料位置")

    def _storage_path_row(self, lay, option) -> None:
        field, title, description, icon_name = option
        self.path_icon_names[field] = icon_name
        self.path_titles[field] = title
        row = QFrame()
        row.setObjectName("settingsPathRow")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 11, 0, 11)
        row_lay.setSpacing(12)

        icon_wrap = QFrame()
        icon_wrap.setFixedSize(36, 36)
        icon_wrap.setStyleSheet(
            f"background:{self._consumer.accent_wash};"
            f"border:1px solid {self._consumer.accent_hairline};border-radius:10px;"
        )
        icon_label = QLabel(icon_wrap)
        icon_label.setPixmap(icons.pixmap(icon_name, self._consumer.accent, 18, 2.0))
        icon_label.setFixedSize(18, 18)
        icon_label.setAccessibleName(f"{title}图标")
        icon_label.move(9, 9)
        self.path_icon_labels[field] = icon_label
        row_lay.addWidget(icon_wrap, 0, Qt.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("fieldLabel")
        text_box.addWidget(name)
        hint = QLabel(description)
        hint.setObjectName("pageMeta")
        hint.setWordWrap(True)
        self.path_description_labels[field] = hint
        text_box.addWidget(hint)

        folder_name = QLabel()
        folder_name.setObjectName("settingsFolderName")
        folder_name.setStyleSheet(f"color:{T.INK};font-size:12px;font-weight:600;")
        folder_name.setWordWrap(True)
        self.path_name_labels[field] = folder_name
        text_box.addWidget(folder_name)

        path_label = QLabel()
        path_label.setObjectName("settingsPathValue")
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        path_label.setAccessibleName(f"{title}保存位置")
        path_label.setStyleSheet(f"color:{self._consumer.ink_muted};font-size:11px;")
        self.path_labels[field] = path_label
        text_box.addWidget(path_label)
        row_lay.addLayout(text_box, 1)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        self.path_action_layouts.append(actions)
        open_button = QPushButton("打开")
        adopt_button(open_button, role="tertiary", size="sm", accessible_name=f"打开{title}保存位置")
        open_button.clicked.connect(lambda _checked=False, name=field: self._open_directory(name))
        self.path_open_buttons[field] = open_button
        actions.addWidget(open_button)
        change_button = QPushButton("更改…")
        adopt_button(change_button, role="secondary", size="sm", accessible_name=f"更改{title}保存位置")
        change_button.clicked.connect(lambda _checked=False, name=field: self._pick_directory(name))
        self.path_buttons[field] = change_button
        actions.addWidget(change_button)
        row_lay.addLayout(actions)
        lay.addWidget(row)
        self._sync_path(field)

    def _feishu_page(self) -> QWidget:
        from .feishu_connection import FeishuConnectionPanel
        self.feishu_connection = FeishuConnectionPanel(self.cfg)
        return self.feishu_connection

    def _refresh_feishu_settings(self, *, force: bool = False) -> None:
        self.feishu_connection.load_if_needed(force=force)

    def _about_page(self) -> QWidget:
        page, col = self._detail_page("about", "关于", "")
        about_viewport_layout = page.widget().layout()
        if about_viewport_layout is not None:
            about_viewport_layout.setContentsMargins(0, 0, 12, 16)
        hero = QFrame()
        adopt_surface(hero, "section", variant="aboutHero")
        hero.setObjectName("aboutHero")
        hero.setStyleSheet(
            f"QFrame#aboutHero{{background:transparent;border:none;}}"
        )
        hero_lay = QHBoxLayout(hero)
        hero_lay.setContentsMargins(0, 4, 0, 4)
        hero_lay.setSpacing(16)
        logo = QLabel()
        logo.setFixedSize(56, 56)
        logo.setProperty("brandAsset", "logo_mascot_transparent.png")
        pixmap = icons.image_pixmap(T.asset("logo_mascot_transparent.png"), 56)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
            logo.setAccessibleName("Lulu 图标")
            hero_lay.addWidget(logo, 0, Qt.AlignVCenter)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(3)
        product_name = QLabel("Lulu")
        product_name.setStyleSheet(
            f'font-family:"{T.SERIF}";font-size:24px;font-weight:600;color:{self._consumer.ink};background:transparent;'
        )
        brand_heading = QHBoxLayout()
        brand_heading.setSpacing(10)
        brand_heading.addWidget(product_name, 0, Qt.AlignBaseline)
        self.about_tagline = QLabel("一个专为你省钱的内容助手")
        self.about_tagline.setObjectName("pageMeta")
        self.about_tagline.setWordWrap(True)
        self.about_tagline.setStyleSheet(f'font-size:12px;color:{self._consumer.ink_soft};background:transparent;')
        self.about_version = QLabel(f"v{__version__}")
        self.about_version.setObjectName("pageMeta")
        self.about_version.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.about_version.setStyleSheet(f'font-size:12px;color:{self._consumer.ink_muted};background:transparent;')
        brand_heading.addWidget(self.about_version, 0, Qt.AlignBaseline)
        brand_heading.addStretch(1)
        brand_text.addLayout(brand_heading)
        brand_text.addWidget(self.about_tagline)
        hero_lay.addLayout(brand_text, 1)
        col.addWidget(hero)

        if self.update_controller is not None:
            from .update_ui import AboutUpdatePanel

            self.update_panel = AboutUpdatePanel(
                self.cfg,
                self.update_controller,
                repository_url=REPOSITORY_URL,
                author_url=AUTHOR_URL,
                product_boundary=PRODUCT_BOUNDARY,
                open_url=self._open_external_url,
                show_contact=self._show_author_contact,
            )
            self.update_panel.install_requested.connect(self.install_requested.emit)
            self.update_panel.manual_download.connect(self.manual_download.emit)
            col.addWidget(self.update_panel)
            self.about_text = self.update_panel.product_boundary
        else:
            fallback, _fallback_lay = self._card("软件更新", "更新服务尚未准备好。")
            self.update_panel = fallback
            col.addWidget(fallback)
        col.addStretch(1)
        return page

    def _open_external_url(self, url: str) -> None:
        if url not in (REPOSITORY_URL, AUTHOR_URL):
            return
        try:
            opened = bool(QDesktopServices.openUrl(QUrl(url)))
        except Exception:  # noqa: BLE001
            opened = False
        if not opened:
            dialogs.alert(self, "链接暂时无法打开", "系统暂时无法打开这个 GitHub 页面，请稍后重试。")

    def _show_author_contact(self) -> None:
        dialog = AuthorContactDialog(self)
        self._author_contact_dialog = dialog
        dialog.exec()

    def _save_value(self, field: str, value) -> bool:
        old_value = getattr(self.cfg, field)
        if old_value == value:
            self._sync_control(field)
            return True
        setattr(self.cfg, field, value)
        try:
            saved = bool(self.cfg.save())
        except Exception:  # noqa: BLE001 - 统一转为可见失败并回滚
            saved = False
        if not saved:
            setattr(self.cfg, field, old_value)
            self._rollback_control(field)
            dialogs.alert(self, "设置暂未保存", "没有写入本机配置，已恢复原来的选择。请稍后重试。")
            return False
        self._sync_control(field)
        self.preferences_changed.emit(field)
        return True

    def _sync_control(self, field: str) -> None:
        if field == "notification_policy":
            current = str(getattr(self.cfg, "notification_policy", "never"))
            self.notification_select.set_value(current if current in ("always", "inactive", "never") else "never")
        elif field == "notify_on_failure":
            self._sync_switch(self.failure_notification_switch, bool(getattr(self.cfg, field, True)))
        elif field == "notification_sound_enabled":
            self._sync_switch(self.notification_sound_switch, bool(getattr(self.cfg, field, True)))
        elif field == "prevent_sleep_during_tasks":
            self._sync_switch(self.prevent_sleep_switch, bool(getattr(self.cfg, field, False)))
        elif field == "confirm_exit_during_tasks":
            self._sync_switch(self.exit_protection_switch, bool(getattr(self.cfg, field, True)))
        elif field == "completion_action":
            current = str(getattr(self.cfg, field, "stay"))
            self.completion_action_select.set_value(
                current if current in ("stay", "open_result") else "stay"
            )
        elif field in self.path_labels:
            self._sync_path(field)

    @staticmethod
    def _sync_switch(control, checked: bool) -> None:
        previous_blocked = control.blockSignals(True)
        if hasattr(control, "set_checked"):
            control.set_checked(bool(checked), animate=control.isVisible())
        else:
            control.setChecked(bool(checked))
        control.blockSignals(previous_blocked)

    def _rollback_control(self, field: str) -> None:
        switches = {
            "notify_on_failure": (self.failure_notification_switch, True),
            "notification_sound_enabled": (self.notification_sound_switch, True),
            "prevent_sleep_during_tasks": (self.prevent_sleep_switch, False),
            "confirm_exit_during_tasks": (self.exit_protection_switch, True),
        }
        entry = switches.get(field)
        if entry is None:
            self._sync_control(field)
            return
        control, fallback = entry
        checked = bool(getattr(self.cfg, field, fallback))
        if hasattr(control, "animate_rollback"):
            control.animate_rollback(checked)
        else:
            self._sync_switch(control, checked)

    def _pick_prevent_sleep(self, checked: bool) -> None:
        self._save_value("prevent_sleep_during_tasks", bool(checked))

    def _show_notification_unchanged(self) -> None:
        current = str(getattr(self.cfg, "notification_policy", "never"))
        label = {
            "always": "始终提醒",
            "inactive": "仅 Lulu 不在前台时",
            "never": "从不提醒",
        }.get(current, "从不提醒")
        self.notification_status_label.setText(
            f"设置未改变，仍为“{label}”。提醒不会包含文件名、文案或其他内容。"
        )

    def _pick_notification_policy(self, value: str) -> None:
        requested = str(value or "never")
        old = str(getattr(self.cfg, "notification_policy", "never"))
        if requested == old:
            return
        if requested == "never":
            if self._save_value("notification_policy", "never"):
                self.notification_status_label.setText(
                    "通知只会写“任务已完成”，不会包含文件名、文案或其他内容。"
                )
            return
        if requested not in ("always", "inactive") or self.notification_service is None:
            self._sync_control("notification_policy")
            self._show_notification_unchanged()
            dialogs.alert(
                self,
                "系统通知暂不可用",
                "当前无法连接 macOS 通知服务，设置没有保存。请稍后重试。",
            )
            return

        self._pending_notification_policy = (requested, old)
        self.notification_select.setEnabled(False)
        self.notification_status_label.setText(
            "正在检查 macOS 通知权限。若尚未决定，系统会询问是否允许 Lulu 发送任务完成提醒。"
        )
        try:
            self.notification_service.request_enable(
                self.notification_permission_finished.emit
            )
        except Exception:  # noqa: BLE001 - 原生桥异常必须回滚控件
            self._pending_notification_policy = None
            self.notification_select.setEnabled(True)
            self._sync_control("notification_policy")
            self._show_notification_unchanged()
            dialogs.alert(
                self,
                "系统通知暂不可用",
                "当前无法连接 macOS 通知服务，设置没有保存。请稍后重试。",
            )

    def _finish_notification_permission(self, result) -> None:
        pending = self._pending_notification_policy
        if pending is None:
            return
        self._pending_notification_policy = None
        requested, _old = pending
        self.notification_select.setEnabled(True)
        if bool(getattr(result, "granted", False)):
            if self._save_value("notification_policy", requested):
                self.notification_status_label.setText(
                    "已使用 macOS 系统通知。提醒不会包含文件名、文案或其他内容。"
                )
            else:
                self._show_notification_unchanged()
            return

        self._sync_control("notification_policy")
        raw_status = getattr(result, "status", "")
        status = str(getattr(raw_status, "value", raw_status)).lower()
        if "denied" in status:
            message = "macOS 没有允许 Lulu 发送通知，设置没有保存。你可以以后在系统设置中重新允许。"
        elif "timeout" in status:
            message = "等待 macOS 通知权限超时，设置没有保存。请稍后重试。"
        else:
            message = "当前无法使用 macOS 通知服务，设置没有保存。请稍后重试。"
        self.notification_status_label.setText(message)
        dialogs.alert(self, "任务完成提醒未开启", message)

    def _pick_directory(self, field: str) -> None:
        current = str(self._effective_directory(field))
        selected = QFileDialog.getExistingDirectory(self, "选择保存文件夹", current)
        if not selected or Path(selected).expanduser() == Path(current).expanduser():
            return
        if field in ("voice_library_dir", "models_dir"):
            self._start_storage_relocation(field, Path(current), Path(selected))
            return
        self._save_value(field, selected)

    def _open_directory(self, field: str) -> None:
        self._open_storage_path(self._effective_directory(field))

    def _open_storage_path(self, path: Path, *, managed: bool = False) -> None:
        if managed and not path.exists():
            dialogs.alert(self, "暂无资料", "使用对应功能后，Lulu 会自动创建这个文件夹。")
            return
        readable = path.is_dir() and os.access(path, os.R_OK | os.X_OK)
        if not readable:
            dialogs.alert(self, "文件夹无法打开", "当前位置不存在或无权访问，请更换一个可用文件夹。")
            return
        try:
            opened = bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        except Exception:  # noqa: BLE001 - 系统打开失败转为可见提示
            opened = False
        if not opened:
            dialogs.alert(self, "文件夹无法打开", "系统暂时无法打开这个位置，请稍后重试。")

    def _sync_path(self, field: str) -> None:
        path = str(self._effective_directory(field))
        label = self.path_labels[field]
        label.setText(path)
        label.setToolTip(path)
        label.setAccessibleDescription(path)
        folder_name = Path(path).name or path
        self.path_name_labels[field].setText(folder_name)
        self.path_name_labels[field].setToolTip(path)

    def _effective_directory(self, field: str) -> Path:
        raw = str(getattr(self.cfg, field, "") or "").strip()
        if raw:
            return Path(raw).expanduser()
        if field == "voice_library_dir":
            return Path(voices.current_voice_library_dir()).expanduser()
        if field == "models_dir":
            return Path(models_manager.models_root()).expanduser()
        return Path.home()

    def _start_storage_relocation(self, field: str, old_root: Path, new_root: Path) -> None:
        running = self._storage_workers.get(field)
        if running is not None and running.isRunning():
            return
        try:
            other_work_active = bool(self._external_storage_busy())
        except Exception:  # noqa: BLE001 - gate failure must fail closed
            other_work_active = True
        if other_work_active:
            dialogs.alert(
                self,
                "暂时不能更改保存位置",
                "有任务、模型下载或安装正在进行。请等当前操作结束后再更改位置。",
            )
            return
        title = self.path_titles[field]
        if field == "models_dir":
            body = (
                f"已安装的转录与配音模型会移动到：\n{new_root}\n\n"
                "移动期间请勿退出 Lulu，素材库与文稿不会受影响。"
            )
        else:
            body = (
                f"已有音色会复制到：\n{new_root}\n\n"
                "旧音色库会保留，切换完成后新建音色使用新位置。"
            )
        if not dialogs.confirm(self, f"更改{title}位置", body, ok="继续"):
            return
        worker = StorageRelocateWorker(field, old_root, new_root, self.cfg, self)
        self._storage_workers[field] = worker
        self._set_storage_activity(True)
        self.path_name_labels[field].setText("正在切换位置…")
        worker.succeeded.connect(self._storage_relocation_succeeded)
        worker.failed.connect(self._storage_relocation_failed)
        worker.finished.connect(lambda name=field: self._storage_relocation_finished(name))
        try:
            worker.start()
        except Exception:  # noqa: BLE001 - start failure must release every gate
            self._storage_workers.pop(field, None)
            self._set_storage_activity(False)
            self._sync_path(field)
            dialogs.alert(self, "保存位置未更改", "后台迁移任务没有启动，请稍后重试。")

    def _set_managed_storage_buttons_enabled(self, enabled: bool) -> None:
        for name in ("voice_library_dir", "models_dir"):
            button = self.path_buttons.get(name)
            if button is not None:
                button.setEnabled(bool(enabled))

    def _storage_relocation_finished(self, field: str) -> None:
        self._storage_workers.pop(field, None)
        if not self.storage_operation_active():
            self._set_storage_activity(False)

    def _storage_relocation_succeeded(self, field: str, _path: str) -> None:
        self._sync_path(field)
        self.preferences_changed.emit(field)
        dialogs.alert(self, "保存位置已更新", f"{self.path_titles[field]}已切换到新位置。")

    def _storage_relocation_failed(
        self,
        field: str,
        message: str,
        rollback_succeeded: bool,
        old_path: str,
        new_path: str,
    ) -> None:
        if rollback_succeeded:
            self._sync_path(field)
            dialogs.alert(
                self,
                "保存位置未更改",
                f"{self.path_titles[field]}仍使用原来的位置。\n{message}",
            )
            return

        truth = f"旧位置：{old_path}\n新位置：{new_path}"
        self.path_name_labels[field].setText("位置需要检查")
        self.path_name_labels[field].setToolTip(truth)
        self.path_labels[field].setText(truth)
        self.path_labels[field].setToolTip(truth)
        self.path_labels[field].setAccessibleDescription(
            f"{self.path_titles[field]}自动恢复未完全成功；{truth}"
        )
        dialogs.alert(
            self,
            "保存位置需要检查",
            f"{self.path_titles[field]}迁移没有完成，且自动恢复未完全成功。"
            "请先不要继续使用相关模型或音色，并检查下面两个位置：\n"
            f"{truth}\n{message}",
        )

    def _set_storage_activity(self, active: bool) -> None:
        active = bool(active)
        if self._storage_activity == active:
            return
        self._storage_activity = active
        self._set_managed_storage_buttons_enabled(not active)
        self.category_nav.setEnabled(not active)
        self.category_stack.setEnabled(not active)
        self.storage_activity_changed.emit(active)

    def storage_operation_active(self) -> bool:
        return (
            self._storage_activity
            or any(worker.isRunning() for worker in self._storage_workers.values())
        )

    def shutdown_workers(self) -> None:
        self._settings_closing = True
        if hasattr(self, "feishu_connection"): self.feishu_connection.shutdown()
        for worker in (self._skill_status_worker, self._feishu_config_worker):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(100)
        if self._feishu_oauth_worker is not None and self._feishu_oauth_worker.isRunning():
            self._feishu_oauth_worker.requestInterruption()
            self._feishu_oauth_worker.wait()
        if self._feishu_spaces_worker is not None and self._feishu_spaces_worker.isRunning():
            self._feishu_spaces_worker.requestInterruption()
            self._feishu_spaces_worker.wait()
        for worker in tuple(self._storage_workers.values()):
            if worker.isRunning():
                worker.wait()
