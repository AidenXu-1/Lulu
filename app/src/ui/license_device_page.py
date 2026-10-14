"""设置中心的本机授权摘要与在线主动停用页面。"""

from __future__ import annotations

from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import dialogs, theme as T
from .control_foundation import (
    LuluButton,
    PaperTextureFrame,
    adopt_consumer_theme_scope,
    adopt_scroll_area,
    adopt_surface,
    set_status_kind,
)


_PENDING_CODES = {
    "outcome_unknown",
    "deactivation_in_progress",
    "pending_deactivation_cache_missing",
    "pending_deactivation_mismatch",
}


def _license_device_action_qss(consumer: T.ConsumerTheme) -> str:
    """只补 core workspace 尚未覆盖的克制 danger 角色，颜色全部来自现有 token。"""

    scope = 'QWidget#licenseDevicePage[consumerThemeScope="coreWorkspace"]'
    return f"""
    {scope} QPushButton[luluControl="button"][role="destructive"] {{
        background: transparent;
        color: {consumer.danger};
        border: {consumer.border_width}px solid {consumer.danger};
        border-radius: {consumer.radius_control}px;
    }}
    {scope} QPushButton[luluControl="button"][role="destructive"]:hover {{
        background: {consumer.secondary_hover};
        color: {consumer.danger};
        border-color: {consumer.danger};
    }}
    {scope} QPushButton[luluControl="button"][role="destructive"]:pressed {{
        background: {consumer.accent_wash};
        color: {consumer.danger};
        border-color: {consumer.danger};
        padding-top: 1px;
    }}
    {scope} QPushButton[luluControl="button"]:focus {{
        border: {consumer.border_width}px solid {consumer.accent_hairline_active};
    }}
    {scope} QPushButton[luluControl="button"]:disabled {{
        background: {consumer.surface_work};
        color: {consumer.ink_muted};
        border-color: {consumer.line};
    }}
    {scope} QPushButton[luluControl="button"][state="loading"] {{
        background: {consumer.surface_work};
        color: {consumer.ink_muted};
        border-color: {consumer.line};
        font-weight: 500;
    }}
    """


class LicenseDeviceWorker(QThread):
    """在后台执行一次授权服务动作，避免网络或安全存储阻塞 Qt 主线程。"""

    completed = Signal(str, object)
    failed = Signal(str)

    def __init__(self, service, action: str, parent: QWidget | None = None):
        super().__init__(parent)
        if action not in {"refresh", "deactivate", "resume"}:
            raise ValueError(f"unsupported license device action: {action}")
        self.service = service
        self.action = action

    def run(self) -> None:
        try:
            method_name = {
                "refresh": "refresh_device_summary",
                "deactivate": "deactivate_current_device",
                "resume": "resume_pending_deactivation",
            }[self.action]
            status = getattr(self.service, method_name)()
        except Exception:  # noqa: BLE001 - 页面边界不显示或记录底层敏感异常
            self.failed.emit(self.action)
            return
        self.completed.emit(self.action, status)


class LicenseDevicePage(QWidget):
    """只显示当前设备，且只在 core 明确完成本地清理后返回激活页。"""

    deactivated = Signal(object)
    activity_changed = Signal(bool)

    def __init__(self, service=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.state = "idle"
        self._worker: LicenseDeviceWorker | None = None
        self._worker_result: tuple[str, object] | None = None
        self._worker_failed_action = ""
        self._last_ready_summary: tuple[int, int] | None = None
        self._deactivation_emitted = False
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.setObjectName("licenseDevicePage")
        adopt_consumer_theme_scope(self, "coreWorkspace")
        self.setStyleSheet(_license_device_action_qss(self._consumer))
        self._build()
        self._show_idle()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("licenseDeviceScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        adopt_scroll_area(self.scroll)
        root.addWidget(self.scroll)

        viewport = QWidget()
        viewport.setObjectName("licenseDeviceViewport")
        outer = QHBoxLayout(viewport)
        outer.setContentsMargins(0, 0, 12, 24)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        content = QWidget()
        content.setObjectName("licenseDeviceContent")
        content.setMaximumWidth(900)
        content.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        title = QLabel("授权与设备")
        title.setObjectName("pageTitle")
        title.setAccessibleName("授权与设备")
        col.addWidget(title)
        subtitle = QLabel("联网查看当前这台 Mac 的授权状态，并在需要时释放本机名额。")
        subtitle.setObjectName("pageMeta")
        subtitle.setWordWrap(True)
        col.addWidget(subtitle)
        col.addSpacing(8)

        self.summary_card = PaperTextureFrame(
            strength=self._consumer.paper_texture_opacity,
            corner_radius=self._consumer.radius_surface,
            layer="section",
        )
        self.summary_card.setObjectName("licenseDeviceSummary")
        adopt_surface(self.summary_card, "section", variant="settings")
        self.summary_card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.summary_card.setAccessibleName("本机授权摘要")
        summary_lay = QVBoxLayout(self.summary_card)
        summary_lay.setContentsMargins(18, 16, 18, 16)
        summary_lay.setSpacing(8)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        section_title = QLabel("当前设备")
        section_title.setObjectName("sectionTitle")
        status_row.addWidget(section_title)
        status_row.addStretch(1)
        self.status_label = QLabel()
        set_status_kind(self.status_label, "neutral")
        status_row.addWidget(self.status_label)
        summary_lay.addLayout(status_row)

        self.usage_label = QLabel()
        self.usage_label.setObjectName("fieldLabel")
        self.usage_label.setWordWrap(True)
        self.usage_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        summary_lay.addWidget(self.usage_label)

        self.message_label = QLabel()
        self.message_label.setObjectName("pageMeta")
        self.message_label.setWordWrap(True)
        self.message_label.setMinimumHeight(38)
        summary_lay.addWidget(self.message_label)
        col.addWidget(self.summary_card)

        action_card = PaperTextureFrame(
            strength=self._consumer.paper_texture_opacity,
            corner_radius=self._consumer.radius_surface,
            layer="section",
        )
        action_card.setObjectName("licenseDeviceAction")
        adopt_surface(action_card, "section", variant="settings")
        action_lay = QVBoxLayout(action_card)
        action_lay.setContentsMargins(18, 16, 18, 16)
        action_lay.setSpacing(10)

        action_title = QLabel("停用当前设备")
        action_title.setObjectName("sectionTitle")
        action_lay.addWidget(action_title)
        explanation = QLabel(
            "停用需要联网。服务端明确成功后，本机才会清除授权缓存并返回激活页；"
            "文稿、素材、模型、设置和其他本机文件都不会删除。"
        )
        explanation.setObjectName("pageMeta")
        explanation.setWordWrap(True)
        action_lay.addWidget(explanation)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.recovery_button = LuluButton(
            "重新连接并检查",
            role="secondary",
            size="md",
        )
        self.recovery_button.setAccessibleName("重新连接并检查本机授权状态")
        self.recovery_button.clicked.connect(self.retry_current_state)
        buttons.addWidget(self.recovery_button)

        self.deactivate_button = LuluButton(
            "停用这台设备",
            role="destructive",
            size="md",
        )
        self.deactivate_button.setAccessibleName("停用这台设备，需要二次确认")
        self.deactivate_button.clicked.connect(self.request_deactivation)
        buttons.addWidget(self.deactivate_button)
        action_lay.addLayout(buttons)

        support = QLabel(
            "这里只能操作当前这台设备。若旧设备已经损坏、丢失或无法进入 Lulu，请联系授权方重置设备。"
        )
        support.setObjectName("pageMeta")
        support.setWordWrap(True)
        action_lay.addWidget(support)
        col.addWidget(action_card)
        col.addStretch(1)

        outer.addWidget(content, 1)
        self.scroll.setWidget(viewport)
        self.setTabOrder(self.summary_card, self.deactivate_button)
        self.setTabOrder(self.deactivate_button, self.recovery_button)

    def on_activated(self) -> None:
        """每次用户真正进入页面都在线刷新，不能让本地旧计数冒充当前值。"""

        if self.service is None:
            self._show_unavailable()
            return
        if self.operation_active():
            return
        action = "resume" if self.state in {"outcome_unknown", "cleanup_required"} else "refresh"
        self._start_action(action)

    def retry_current_state(self) -> None:
        if (
            self.service is None
            or self.operation_active()
            or self.recovery_button.isHidden()
            or not self.recovery_button.isEnabled()
        ):
            return
        action = "resume" if self.state in {"outcome_unknown", "cleanup_required"} else "refresh"
        self._start_action(action)

    def request_deactivation(self) -> None:
        if (
            self.service is None
            or self.operation_active()
            or self.state not in {"ready", "failed"}
            or not self.deactivate_button.isEnabled()
        ):
            return
        previous_state = self.state
        self.state = "confirming"
        self._render(
            status="等待确认",
            kind="warning",
            usage=self._summary_text(),
            message="确认层关闭前不会提交停用请求。",
            action="deactivate",
            action_text="等待确认",
            action_enabled=False,
        )
        confirmed = dialogs.confirm(
            self,
            "停用当前设备？",
            "停用成功后，本机将立即退出授权状态并返回激活页。"
            "当前授权会释放 1 个设备名额。\n\n"
            "本机文件不会被删除。此操作需要联网。",
            ok="停用本机",
            cancel="取消",
        )
        if not confirmed:
            if previous_state == "ready" and self._last_ready_summary is not None:
                self._show_ready(*self._last_ready_summary)
            else:
                self._show_failed("本机仍保持授权。你可以稍后重新尝试。")
            return
        self._start_action("deactivate")

    def operation_active(self) -> bool:
        return self._worker is not None

    def _start_action(self, action: str) -> None:
        if self.service is None or self.operation_active():
            return
        if action == "refresh":
            self.state = "loading"
            self._render(
                status="正在联网确认",
                kind="info",
                usage="使用名额：联网刷新后显示",
                message="正在向授权服务核对当前这台设备。",
                action="recovery",
                action_text="正在连接并检查…",
                action_enabled=False,
                action_loading=True,
            )
        elif action == "deactivate":
            self.state = "submitting"
            self._render(
                status="正在停用本机",
                kind="info",
                usage=self._summary_text(),
                message="请求正在处理，请不要重复提交或退出 Lulu。",
                action="deactivate",
                action_text="正在停用这台设备…",
                action_enabled=False,
                action_loading=True,
            )
        else:
            self.state = "resolving"
            self._render(
                status="正在确认停用结果",
                kind="warning",
                usage="使用名额：结果核清后更新",
                message="正在使用同一笔请求核清结果，不会创建新的停用操作。",
                action="recovery",
                action_text="正在确认停用结果…",
                action_enabled=False,
                action_loading=True,
            )

        self._worker_result = None
        self._worker_failed_action = ""
        worker = LicenseDeviceWorker(self.service, action, self)
        self._worker = worker
        worker.completed.connect(self._capture_result)
        worker.failed.connect(self._capture_failure)
        worker.finished.connect(lambda current=worker: self._finish_worker(current))
        self.activity_changed.emit(True)
        try:
            worker.start()
        except Exception:  # noqa: BLE001 - 启动失败时不得假装请求已完成
            self._worker = None
            self.activity_changed.emit(False)
            if action in {"deactivate", "resume"}:
                self._show_outcome_unknown("停用结果暂时无法确认。请保持联网后继续核清。")
            else:
                self._show_failed("本机授权状态暂时无法刷新，请稍后重试。", retry_deactivation=False)

    def _capture_result(self, action: str, status) -> None:
        self._worker_result = (action, status)

    def _capture_failure(self, action: str) -> None:
        self._worker_failed_action = action

    def _finish_worker(self, worker: LicenseDeviceWorker) -> None:
        if self._worker is not worker:
            worker.deleteLater()
            return
        self._worker = None
        self.activity_changed.emit(False)
        result = self._worker_result
        failed_action = self._worker_failed_action
        self._worker_result = None
        self._worker_failed_action = ""
        worker.deleteLater()
        if result is not None:
            self._apply_status(*result)
        elif failed_action in {"deactivate", "resume"}:
            self._show_outcome_unknown("停用结果暂时无法确认。请保持联网后继续核清。")
        else:
            self._show_failed("本机授权状态暂时无法刷新，请稍后重试。", retry_deactivation=False)

    def _apply_status(self, action: str, status) -> None:
        code = str(getattr(status, "code", "") or "")
        deactivation_status = str(getattr(status, "deactivation_status", "") or "")
        message = str(getattr(status, "message", "") or "授权状态暂时无法确认。")
        active = getattr(status, "active_device_count", None)
        maximum = getattr(status, "max_devices", None)

        if (
            code == "stopped"
            and deactivation_status == "stopped"
            and self._valid_summary(active, maximum)
        ):
            self.state = "stopped"
            self._last_ready_summary = None
            self._render(
                status="本机已停用",
                kind="success",
                usage=f"当前授权已使用 {active}/{maximum} 台设备",
                message="本机授权缓存已清理，正在返回激活页。本机文件没有删除。",
                action=None,
            )
            if not self._deactivation_emitted:
                self._deactivation_emitted = True
                QTimer.singleShot(0, lambda current=status: self.deactivated.emit(current))
            return

        if code == "ready" and bool(getattr(status, "allowed", False)) and self._valid_summary(active, maximum):
            self._show_ready(active, maximum)
            return

        if code == "no_pending_deactivation" and action == "resume":
            self._start_action("refresh")
            return

        if code == "network_unavailable":
            self._show_offline(message)
            return

        if code == "local_cleanup_incomplete" or deactivation_status == "cleanup_required":
            self._show_cleanup_required(message)
            return

        if code in _PENDING_CODES or deactivation_status in {"resolving", "submitting"}:
            self._show_outcome_unknown(message)
            return

        if action == "deactivate" and deactivation_status == "failed":
            self._show_failed(message, retry_deactivation=True)
            return

        self._show_failed(message, retry_deactivation=False)

    def _show_idle(self) -> None:
        if self.service is None:
            self._show_unavailable()
            return
        self.state = "idle"
        self._render(
            status="需要联网确认",
            kind="neutral",
            usage="使用名额：进入页面后联网显示",
            message="打开“授权与设备”后会刷新当前这台设备的授权摘要。",
            action=None,
        )

    def _show_unavailable(self) -> None:
        self.state = "unavailable"
        self._render(
            status="授权服务暂不可用",
            kind="warning",
            usage="使用名额：暂不可用",
            message="当前运行环境没有接入授权服务，未进行任何联网或缓存操作。",
            action=None,
        )

    def _show_ready(self, active: int, maximum: int) -> None:
        self.state = "ready"
        self._last_ready_summary = (active, maximum)
        self._render(
            status="本机已授权",
            kind="success",
            usage=f"当前授权已使用 {active}/{maximum} 台设备",
            message="该摘要来自授权服务，只包含当前设备状态和动态使用数量。",
            action="deactivate",
            action_text="停用这台设备",
            action_enabled=True,
        )

    def _show_offline(self, message: str) -> None:
        self.state = "offline"
        self._last_ready_summary = None
        self._render(
            status="需要联网确认",
            kind="warning",
            usage="使用名额：联网后显示",
            message=message or "连接授权服务后才能停用本机。",
            action="recovery",
            action_text="重新连接并检查",
            action_enabled=True,
        )

    def _show_outcome_unknown(self, message: str) -> None:
        self.state = "outcome_unknown"
        self._last_ready_summary = None
        self._render(
            status="正在确认停用结果",
            kind="warning",
            usage="使用名额：结果核清后更新",
            message=message,
            action="recovery",
            action_text="继续确认停用结果",
            action_enabled=True,
        )

    def _show_cleanup_required(self, message: str) -> None:
        self.state = "cleanup_required"
        self._last_ready_summary = None
        self._render(
            status="本地授权状态需要清理",
            kind="error",
            usage="使用名额：完成本地清理后更新",
            message=message,
            action="recovery",
            action_text="重试清理并返回激活页",
            action_enabled=True,
        )

    def _show_failed(self, message: str, *, retry_deactivation: bool = True) -> None:
        self.state = "failed"
        action = "deactivate" if retry_deactivation else "recovery"
        action_text = (
            "重新尝试停用本机"
            if retry_deactivation
            else "重新连接并检查"
        )
        self._render(
            status="本机仍保持授权",
            kind="error",
            usage=self._summary_text(),
            message=message,
            action=action,
            action_text=action_text,
            action_enabled=True,
        )

    def _render(
        self,
        *,
        status: str,
        kind: str,
        usage: str,
        message: str,
        action: str | None,
        action_text: str = "",
        action_enabled: bool = False,
        action_loading: bool = False,
    ) -> None:
        if action not in {None, "deactivate", "recovery"}:
            raise ValueError(f"unsupported page action: {action}")
        self.status_label.setText(status)
        set_status_kind(self.status_label, kind)
        self.usage_label.setText(usage)
        self.message_label.setText(message)
        self._configure_action_button(
            self.deactivate_button,
            visible=action == "deactivate",
            text=action_text or "停用这台设备",
            enabled=action_enabled,
            loading=action_loading and action == "deactivate",
            accessible_name=(
                f"{action_text or '停用这台设备'}，需要二次确认"
            ),
        )
        self._configure_action_button(
            self.recovery_button,
            visible=action == "recovery",
            text=action_text or "重新连接并检查",
            enabled=action_enabled,
            loading=action_loading and action == "recovery",
            accessible_name=action_text or "重新连接并检查",
        )
        description = f"{status}；{usage}；{message}"
        self.summary_card.setAccessibleDescription(description)
        self.usage_label.setAccessibleName(usage)
        self.message_label.setAccessibleName(message)

    @staticmethod
    def _configure_action_button(
        button: LuluButton,
        *,
        visible: bool,
        text: str,
        enabled: bool,
        loading: bool,
        accessible_name: str,
    ) -> None:
        button.set_loading(False)
        button.setText(text)
        button.setAccessibleName(accessible_name)
        button.setEnabled(bool(enabled and visible))
        button.setVisible(bool(visible))
        if visible:
            button.setMinimumWidth(max(button.minimumWidth(), button.sizeHint().width()))
        if loading:
            button.set_loading(True, text)

    def _summary_text(self) -> str:
        if self._last_ready_summary is None:
            return "使用名额：联网刷新后显示"
        active, maximum = self._last_ready_summary
        return f"当前授权已使用 {active}/{maximum} 台设备"

    @staticmethod
    def _valid_summary(active, maximum) -> bool:
        return (
            isinstance(active, int)
            and not isinstance(active, bool)
            and active >= 0
            and isinstance(maximum, int)
            and not isinstance(maximum, bool)
            and maximum >= 1
        )

    def shutdown_workers(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait()
        self._worker = None
