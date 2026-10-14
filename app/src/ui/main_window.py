"""Lulu · 工作台主窗口（对齐 design/ui 设计稿）。

左侧边栏 + 右工作台：统一输入区（拖拽/链接/录音占位）+ 任务队列。
现有功能（本地文件 / 链接 / 转录 / 复制 / 导出）真接通；未建功能标「待上线」。
"""

from __future__ import annotations

from ..core.extraction import has_unsaved_result

import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QSize,
    QThread,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QAbstractButton, QAbstractItemView, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QApplication, QScrollBar, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTextEdit, QVBoxLayout, QWidget

from ..config import AppConfig
from ..core.task import Task, TaskExecutionPolicy, TaskGroup, TaskGroupCandidate, TaskGroupMember, TaskState, derive_collection_item_runtime, task_display_title
from . import dialogs, icons, theme as T
from .control_foundation import PaperTextureFrame, adopt_badge, adopt_button, adopt_consumer_theme_scope, adopt_nav_row, adopt_toast

_DEFAULT_TASK_QUEUE_STORE = object()

APP_NAME = "Lulu"
LIVE_RECORDING_PAGE = 9
SIDEBAR_W = 232    # 左活动栏固定宽
CENTER_MIN_W = 620
ANIM_MS = 280      # 折叠/展开动画时长
TITLEBAR_TOTAL_H = 43
SIDEBAR_TOTAL_TOP = 43
PANE_TOGGLE_FALLBACK_Y = 5
MAX_TRUSTED_SAFE_TOP = 64
WINDOW_TITLE_W = 360
BRAND_LOGO_SIZE = 60

# 折叠动画缓动：CSS easeInOutCubic 的三次贝塞尔（0.65,0,0.35,1），对称缓入缓出、丝滑
_EASE_C1 = QPointF(0.65, 0.0)
_EASE_C2 = QPointF(0.35, 1.0)


class _PaneToggleButton(QPushButton):
    """Window-level pane switch with explicit Enter/Return keyboard support."""

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)






class _TranscriptCoverRetryWorker(QThread):
    """只结算既有文稿的封面旁路，不重新下载媒体或转录。"""

    done = Signal(str, object, str)
    failed = Signal(str)

    def __init__(self, entry_id: str, meta: dict, workflow):
        super().__init__()
        self._entry_id = entry_id
        self._meta = dict(meta)
        self._workflow = workflow
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            from ..core.collect.preferences import CollectCoverPreferenceSnapshot
            from ..core.pipeline import _settle_single_link_cover
            from ..core.models import TranscriptCover

            meta = self._meta
            original_cover = TranscriptCover.from_meta(meta)
            if original_cover.platform == "douyin":
                from ..core.collect.douyin_fetch import fetch_item_with_page_fallback
                def check_cancel(*_):
                    if self._cancel:
                        raise InterruptedError("封面重试已取消")
                item = fetch_item_with_page_fallback(original_cover.work_id, check_cancel)
                if item.work_id != original_cover.work_id or not item.cover_url:
                    raise RuntimeError("封面来源尚未确认")
                meta = TranscriptCover(platform="douyin", work_id=item.work_id,
                    url=item.cover_url, status="url_only").merge_meta(meta)

            receipt_status = ""

            class _ReceiptCapture:
                def localize(_capture, **kwargs):
                    nonlocal receipt_status
                    receipt = self._workflow.localize(**kwargs)
                    receipt_status = str(getattr(receipt, "status", "") or "")
                    return receipt

            updated = _settle_single_link_cover(
                meta,
                snapshot=CollectCoverPreferenceSnapshot(True),
                workflow=_ReceiptCapture(),
                is_cancelled=lambda: self._cancel,
                progress=lambda _message, _fraction: None,
            )
            if receipt_status == "recovered_previous":
                updated = self._meta
            elif original_cover.status == "downloaded" and TranscriptCover.from_meta(updated).status != "downloaded":
                self.failed.emit(self._entry_id)
                return
            self.done.emit(self._entry_id, updated, receipt_status)
        except Exception:
            self.failed.emit(self._entry_id)


class MainWindow(QWidget):
    def __init__(
        self,
        *,
        cover_preference_service=None,
        cover_localizer=None,
        collection_cover_workflow=None,
        collection_service=None,
        task_queue_store=_DEFAULT_TASK_QUEUE_STORE,
        defer_core_startup=False,
        local_file_transcription_authorization_guard=None,
    ):
        super().__init__()
        self.cfg = AppConfig.load()
        self._local_file_transcription_authorization_guard = (
            local_file_transcription_authorization_guard
        )
        from ..core.collect.cover import CoverLocalizationService
        from ..core.collect.cover_workflow import CoverWorkflowService
        from ..core.collect.preferences import MaterialPreferenceService
        from ..core.models_manager import app_data_dir

        self._material_preferences = (
            cover_preference_service
            or MaterialPreferenceService(self.cfg)
        )
        # 属性名保留给 0.2.5 测试注入面；生产中只有一个完整偏好服务。
        self._cover_preferences = self._material_preferences
        self._cover_localizer = (
            cover_localizer
            or CoverLocalizationService(app_data_dir() / "collection" / "covers")
        )
        self._collection_cover_workflow = (
            collection_cover_workflow
            or CoverWorkflowService(self._cover_localizer)
        )
        from ..core.collect.application import CollectionApplicationService

        self._collection_service = (
            collection_service
            or CollectionApplicationService(
                cover_workflow=self._collection_cover_workflow
            )
        )
        self._transcript_cover_retry_worker: _TranscriptCoverRetryWorker | None = None
        self._feed_worker: QThread | None = None
        self._close_settling = False
        self._close_retry_scheduled = False
        self._close_xhs_action = "keep"
        self._task_queue_startup_pending = False
        self._install_preparation_pending = bool(defer_core_startup)
        self._install_preparation_error = ""
        self._task_queue_initial_connection = False
        self._task_queue_startup_worker = None
        self._task_queue_auto_sync_allowed = True
        self._task_queue_sync_retry_pending = False
        self._task_queue_resume_ids = set()
        self._core_result_worker = None
        self._task_queue_recovery = None
        self._task_queue_recovery_checked = False
        self._core_recovery_pending = False
        if task_queue_store is _DEFAULT_TASK_QUEUE_STORE:
            if getattr(sys, "frozen", False):
                from ..core.gui_task_ledger import CoreTaskQueueStore
                from ..core.gui_task_recovery import GuiTaskRecoveryStore

                task_queue_store = CoreTaskQueueStore.unavailable()
                self._task_queue_recovery = GuiTaskRecoveryStore.production()
                self._task_queue_startup_pending = True
                self._task_queue_initial_connection = True
            else:
                task_queue_store = None
        self._task_queue_store = task_queue_store
        self._task_queue_store_failed = False
        self._task_queue_snapshot_locked = False
        from ..core.power_management import PowerManager
        from ..core.user_notifications import UserNotificationService
        from ..core.completion_feedback import CompletionFeedback

        self.notification_service = UserNotificationService()
        self.completion_feedback = CompletionFeedback(
            self.cfg, self.notification_service, self._open_completion_target
        )
        self.power_manager = PowerManager(
            enabled=bool(getattr(self.cfg, "prevent_sleep_during_tasks", False))
        )
        # 统一使用 Lulu 宣纸；旧主题偏好由配置读取兼容。
        T.apply_theme(self.cfg.theme)
        self.workbench_consumer_theme = T.consumer_theme(T.ACTIVE.name)
        self.tasks: list[Task] = []
        self.task_groups: list[TaskGroup] = []
        self._paused_task_group_ids: set[int] = set()
        self._deleting_task_group_ids: set[int] = set()
        self._cancelling_task_ids: set[int | str] = set()
        self.current: tuple[Task, object] | None = None
        self._next_id = 1
        self._next_group_id = 1

        self._left_open = True
        self._pane_anims: dict[str, QPropertyAnimation] = {}
        self._native_titlebar_active = False
        self._titlebar_safe_top = 0
        self._window_is_active = True
        self._titlebar_inset = 0
        self._drag_band_height = 38
        self._window_dragging = False
        self._window_drag_global = QPoint()
        self._window_drag_pos = QPoint()
        self._storage_relocation_in_progress = False
        from .legacy_task_migration import installed_inheritance_controller
        self._legacy_inheritance = installed_inheritance_controller(
            busy_probe=self.storage_relocation_busy, parent=self)
        if self._legacy_inheritance is not None:
            self._legacy_inheritance.finished.connect(self._legacy_tasks_migration_finished)
        from .live_controller import LiveController
        self.live_controller = LiveController(self)
        self.live_controller.error.connect(self._toast)
        self.live_controller.activity_changed.connect(self._sync_runtime_preferences)
        self._rec_source = "microphone"
        self._rec_active_source = "microphone"
        self._rec_cancel_requested = False
        self._system_rec_session = None
        self._mixed_rec_active = False
        self._mixed_pending_stop = False
        self._mixed_system_result = None
        self._mixed_mic_path = None
        self._mixed_system_path = None
        self._mixed_output_path = None
        self._rec_elapsed = 0
        self._rec_elapsed_timer = QTimer(self)
        self._rec_elapsed_timer.timeout.connect(self._on_rec_elapsed_tick)
        from .update_ui import UpdateController
        self.update_controller = UpdateController(
            active_work=lambda: self._install_preparation_pending or self.has_active_work(),
            unsaved_work=self.has_unsaved_work, parent=self)
        self.update_controller.install_accepted.connect(lambda: QApplication.instance().quit())

        self.setObjectName("root")
        self.setWindowTitle(APP_NAME)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.setMinimumSize(min(1200, available.width()) if available else 1200,
                            min(760, available.height()) if available else 760)
        self.resize(min(1680, available.width()) if available else 1680,
                    min(960, available.height()) if available else 960)
        saved = getattr(self.cfg, 'window_geometry', {})
        if available and isinstance(saved, dict) and all(type(saved.get(k)) is int for k in ('x','y','width','height')):
            self.resize(max(self.minimumWidth(), min(saved['width'],available.width())),
                        max(self.minimumHeight(), min(saved['height'],available.height())))
            self.move(max(available.left(), min(saved['x'],available.right()-self.width()+1)),
                      max(available.top(), min(saved['y'],available.bottom()-self.height()+1)))
        self.setAcceptDrops(True)
        self.setStyleSheet(T.QSS)
        self._build()
        self._task_queue_sync_timer = QTimer(self)
        self._task_queue_sync_timer.setInterval(15000)
        self._task_queue_sync_timer.timeout.connect(self._sync_shared_tasks)
        if getattr(self._task_queue_store, "authoritative", False):
            self._task_queue_sync_timer.start()
        if self._task_queue_startup_pending:
            QTimer.singleShot(0, self, self._start_task_queue_connection)
        else:
            self._restore_task_queue()
        self._sync_workbench_paper_scope()
        self._runtime_preferences_timer = QTimer(self)
        self._runtime_preferences_timer.setInterval(400)
        self._runtime_preferences_timer.timeout.connect(self._sync_runtime_preferences)
        self._runtime_preferences_timer.start()
        self._sync_runtime_preferences()
        self.installEventFilter(self)
        # The source/test runner must never create real user update state or make
        # background network requests. PyInstaller marks the shipped app frozen.
        if getattr(sys, "frozen", False):
            self._update_check_timer = QTimer(self)
            self._update_check_timer.setSingleShot(True)
            self._update_check_timer.timeout.connect(
                lambda: self.update_controller.maybe_auto_check(bool(getattr(self.cfg, "auto_update_enabled", True)))
            )
            self._update_check_timer.start(5000)

    # ================================================================== #
    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                child.installEventFilter(self)
        elif event.type() == QEvent.Type.MouseButtonPress and self._can_start_window_drag(watched, event):
            self._window_dragging = True
            self._window_drag_global = event.globalPosition().toPoint()
            self._window_drag_pos = self.pos()
            return True
        elif event.type() == QEvent.Type.MouseMove and self._window_dragging:
            if event.buttons() & Qt.MouseButton.LeftButton:
                delta = event.globalPosition().toPoint() - self._window_drag_global
                self.move(self._window_drag_pos + delta)
                return True
            self._window_dragging = False
        elif event.type() == QEvent.Type.MouseButtonRelease and self._window_dragging:
            self._window_dragging = False
            return True
        return super().eventFilter(watched, event)

    def _can_start_window_drag(self, watched, event) -> bool:
        """macOS 透明标题栏下，给顶部非交互区域补一条可靠拖拽带。"""
        if event.button() != Qt.MouseButton.LeftButton or not isinstance(watched, QWidget):
            return False
        if self._is_interactive_drag_target(watched):
            return False
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        local = watched.mapTo(self, pos)
        return 0 <= local.y() <= max(self._drag_band_height, self._titlebar_inset)


    def _is_interactive_drag_target(self, widget: QWidget) -> bool:
        interactive = (
            QAbstractButton,
            QAbstractItemView,
            QComboBox,
            QLineEdit,
            QPlainTextEdit,
            QScrollBar,
            QSlider,
            QSpinBox,
            QTextEdit,
        )
        cur: QWidget | None = widget
        while cur is not None and cur is not self:
            if isinstance(cur, interactive):
                return True
            if "mousePressEvent" in getattr(cur, "__dict__", {}):
                return True
            if cur.cursor().shape() == Qt.CursorShape.PointingHandCursor:
                return True
            cur = cur.parentWidget()
        return False

    # ================================================================== #
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 导航与主区共享窗口，文稿编辑统一由素材库负责。
        self.root_layout = root
        self.left_wrap = self._wrap(self._build_sidebar(), SIDEBAR_W)
        root.addWidget(self.left_wrap)
        self.center_wrap = self._build_center()
        self.center_wrap.setMinimumWidth(CENTER_MIN_W)
        self.center_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.center_wrap, 1)
        self._build_pane_toggle_rails()
        self._sync_toggle_icons()
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)
        QTimer.singleShot(0, self, self._position_pane_toggles)

    # ---------------- 三栏：折叠容器 + 动画 ---------------- #
    def _wrap(self, inner: QWidget, width: int) -> QWidget:
        """把侧栏包一层固定宽容器，折叠时对容器 maximumWidth 做动画（内部不重排）。"""
        inner.setFixedWidth(width)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(inner)
        w.setMinimumWidth(0)
        w.setMaximumWidth(width)
        return w


    def _build_center(self) -> QWidget:
        box = PaperTextureFrame(strength=0.0, layer="canvas")
        box.setObjectName("root")
        box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        v = QVBoxLayout(box)
        self.center_layout = v
        # Window-level pane controls occupy y=5..33. Keep every page header below them.
        v.setContentsMargins(0, TITLEBAR_TOTAL_H, 0, 0)
        v.setSpacing(0)
        topbar = self._build_topbar()
        topbar.setVisible(False)
        topbar.setFixedHeight(0)
        v.addWidget(topbar)
        v.addWidget(self._build_main(), 1)
        return box

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setFixedHeight(24)
        self._topbar = bar
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(0)
        self._topbar_lay = h
        # 全局训练进度芯片：训练专属音色在后台跑时，任何页都能看到进度；点它可取消
        h.addStretch(1)
        h.addStretch(1)
        return bar

    def _build_pane_toggle_rails(self) -> None:
        # Three mouse-transparent material strips cover the real root-column
        # geometry behind macOS chrome.  They sit above the column widgets but
        # below every titlebar control, so the traffic lights, pane toggles and
        # native drag region keep their original behaviour.
        self.titlebar_paper_left = PaperTextureFrame(self, strength=0.0, layer="rail")
        self.titlebar_paper_left.setObjectName("titlebarPaperLeft")
        self.titlebar_paper_center = PaperTextureFrame(self, strength=0.0, layer="canvas")
        self.titlebar_paper_center.setObjectName("titlebarPaperCenter")
        for material in self._titlebar_paper_frames():
            adopt_consumer_theme_scope(material, "globalShell")
            material.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.window_title_label = QLabel(self.windowTitle(), self)
        self.window_title_label.setObjectName("windowTitleLabel")
        adopt_consumer_theme_scope(self.window_title_label, "globalShell")
        self.window_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.window_title_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.window_title_label.setFixedHeight(28)

        self.rail_left = _PaneToggleButton(self)
        self.rail_left.setObjectName("paneToggleRail")
        adopt_button(
            self.rail_left,
            role="icon",
            size="sm",
            variant="pane",
            accessible_name="导航侧栏",
        )
        adopt_consumer_theme_scope(self.rail_left, "globalShell")
        self.rail_left.setFixedSize(30, 28)
        self.rail_left.setIconSize(QSize(18, 18))
        self.rail_left.setCheckable(True)
        self.rail_left.clicked.connect(lambda _checked=False: self._toggle_pane("left"))

        self.settings_button = _PaneToggleButton(self)
        self.settings_button.setObjectName("settingsButton")
        adopt_button(
            self.settings_button,
            role="icon",
            size="sm",
            variant="settings",
            accessible_name="打开设置",
            tooltip="设置",
        )
        adopt_consumer_theme_scope(self.settings_button, "globalShell")
        self.settings_button.setFixedSize(30, 28)
        self.settings_button.setIconSize(QSize(16, 16))
        self.settings_button.setCheckable(True)
        self.settings_button.clicked.connect(lambda _checked=False: self._open_update_settings())

        self.setTabOrder(self.rail_left, self.settings_button)

    def _titlebar_paper_frames(self) -> tuple[PaperTextureFrame, ...]:
        return tuple(
            frame
            for frame in (
                getattr(self, "titlebar_paper_left", None),
                getattr(self, "titlebar_paper_center", None),
            )
            if isinstance(frame, PaperTextureFrame)
        )

    def _position_titlebar_materials(self) -> None:
        if not hasattr(self, "titlebar_paper_left"):
            return
        height = max(TITLEBAR_TOTAL_H, self._titlebar_safe_top)
        columns = (
            (self.titlebar_paper_left, self.left_wrap),
            (self.titlebar_paper_center, self.center_wrap),
        )
        for material, column in columns:
            geometry = column.geometry()
            material.setGeometry(geometry.x(), 0, geometry.width(), height)

    def _toggle_pane(self, side: str) -> None:
        if side != "left":
            return
        self._left_open = not self._left_open
        self._animate_wrap(self.left_wrap, (176 if self.width() <= 1220 else SIDEBAR_W) if self._left_open else 0)
        self._sync_toggle_icons()

    def _animate_wrap(self, wrap: QWidget, target: int) -> None:
        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(_EASE_C1, _EASE_C2, QPointF(1.0, 1.0))
        side = "left" if wrap is self.left_wrap else "right"
        previous = self._pane_anims.pop(side, None)
        if previous is not None:
            previous.stop()
            previous.deleteLater()
        anim = QPropertyAnimation(wrap, b"maximumWidth", self)
        anim.setDuration(ANIM_MS)
        anim.setStartValue(wrap.maximumWidth())
        anim.setEndValue(target)
        anim.setEasingCurve(curve)
        anim.valueChanged.connect(lambda _value: self._activate_root_layout())
        self._pane_anims[side] = anim
        def finish() -> None:
            if self._pane_anims.get(side) is anim:
                self._pane_anims.pop(side, None)
            anim.deleteLater()

        anim.finished.connect(finish)
        anim.start()


    def _activate_root_layout(self) -> None:
        self.root_layout.invalidate()
        self.root_layout.activate()
        self._position_pane_toggles()


    def _sync_toggle_icons(self) -> None:
        if hasattr(self, "rail_left"):
            settings_active = hasattr(self, "stack") and self.stack.currentIndex() == 6
            self.rail_left.setChecked(self._left_open)
            shell_theme = T.consumer_theme(T.ACTIVE.name)
            pane_accent = shell_theme.accent
            self.rail_left.setProperty("consumerAccentIcon", self._left_open)
            left_color = pane_accent if self._left_open else shell_theme.ink_soft
            self.rail_left.setIcon(QIcon(icons.pixmap(
                "panel_left", left_color, 18)))
            left_action = "隐藏导航侧栏" if self._left_open else "显示导航侧栏"
            self.rail_left.setToolTip(left_action)
            self.rail_left.setAccessibleDescription(left_action)
            self.settings_button.setChecked(settings_active)
            self.settings_button.setProperty("consumerAccentIcon", settings_active)
            settings_color = pane_accent if settings_active else shell_theme.ink_soft
            self.settings_button.setIcon(QIcon(icons.pixmap("gear", settings_color, 16)))
            self.settings_button.setAccessibleDescription(
                "当前正在显示设置" if settings_active else "打开中央设置页"
            )

    def _position_pane_toggles(self) -> None:
        if not hasattr(self, "rail_left"):
            return
        self._position_titlebar_materials()
        if self._native_titlebar_active and 1 <= self._titlebar_safe_top <= MAX_TRUSTED_SAFE_TOP:
            rail_y = max(0, round((max(TITLEBAR_TOTAL_H, self._titlebar_safe_top) - self.rail_left.height()) / 2))
        else:
            rail_y = PANE_TOGGLE_FALLBACK_Y
        self.rail_left.move(84, rail_y)
        self.settings_button.move(120, rail_y)
        title_w = min(WINDOW_TITLE_W, max(0, self.width() - 360))
        self.window_title_label.setGeometry(
            max(0, (self.width() - title_w) // 2),
            rail_y,
            title_w,
            self.rail_left.height(),
        )
        self.rail_left.raise_()
        self.settings_button.raise_()
        self.window_title_label.raise_()

    def enable_titlebar_inset(self) -> None:
        """Reserve one shared band for native controls and the custom titlebar."""
        self._native_titlebar_active = True
        self._apply_titlebar_safe_area()
        self._sync_workbench_paper_scope()
        QTimer.singleShot(0, self, self._apply_titlebar_safe_area)

    def _apply_titlebar_safe_area(self, safe_top_override: int | None = None) -> None:
        if safe_top_override is None:
            from .macos import native_safe_area_top
            native_value = native_safe_area_top(self)
            safe_top = int(native_value) if native_value is not None else 0
        else:
            safe_top = int(safe_top_override)
        trusted = self._native_titlebar_active and 1 <= safe_top <= MAX_TRUSTED_SAFE_TOP
        self._titlebar_safe_top = safe_top if trusted else 0
        self._titlebar_inset = self._titlebar_safe_top
        # Full-size content starts at y=0. Its safe area is an overlap warning,
        # not an inset Qt has already applied to our QWidget layouts.
        center_top = max(TITLEBAR_TOTAL_H, self._titlebar_safe_top)
        sidebar_top = max(SIDEBAR_TOTAL_TOP, self._titlebar_safe_top)
        if hasattr(self, "center_layout"):
            self.center_layout.setContentsMargins(0, center_top, 0, 0)
        if hasattr(self, "sidebar_layout"):
            side_padding = 10 if self.width() <= 1220 else 16
            self.sidebar_layout.setContentsMargins(side_padding, sidebar_top, side_padding, 18)
        if self.layout() is not None:
            self.layout().invalidate()
            self.layout().activate()
        self._position_pane_toggles()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if hasattr(self, "sidebar_paper"):
            sidebar_width = 176 if self.width() <= 1220 else SIDEBAR_W
            self.sidebar_paper.setFixedWidth(sidebar_width)
            if self._left_open and "left" not in self._pane_anims:
                self.left_wrap.setMaximumWidth(sidebar_width)
            self._sync_sidebar_density()
        # Width limits invalidate the layout; titlebar materials need its new geometry.
        if hasattr(self, "root_layout"):
            self.root_layout.activate()
        if self._native_titlebar_active:
            self._apply_titlebar_safe_area()
        else:
            self._position_pane_toggles()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowTitleChange and hasattr(self, "window_title_label"):
            self.window_title_label.setText(self.windowTitle())
        elif event.type() == QEvent.Type.ActivationChange and hasattr(self, "rail_left"):
            self._window_is_active = self.isActiveWindow()
            self._sync_toggle_icons()
        elif event.type() == QEvent.Type.WindowStateChange and self._native_titlebar_active:
            QTimer.singleShot(0, self, self._apply_titlebar_safe_area)

    def open_transcript_in_library(self, transcript, title: str, entry_id: str | None = None,
                                   summary: str | None = None, kind: str = "transcript") -> str:
        """Open an existing result, or preserve an unsaved result before editing."""
        if not entry_id:
            from ..core import library
            entry_id = library.save(transcript, title)
        self.open_material_entry(entry_id, kind=kind)
        return entry_id


    def _view_entry(self, entry_id: str) -> None:
        from ..core import library
        record = library._load_raw(entry_id)
        if record is None:
            self._toast("这条资料已被移动或删除")
            return
        kind = "voice" if record.get("kind") == "voice" else "transcript"
        self.open_library(kind)
        self.library_page.open_entry(entry_id)

    def retry_transcript_cover(self, entry_id: str) -> bool:
        """由用户显式重试一篇既有文稿的封面，不重跑转录。"""
        if (
            self._transcript_cover_retry_worker is not None
            and self._transcript_cover_retry_worker.isRunning()
        ):
            self._toast("另一条封面正在处理，请稍候")
            return False
        from ..core import library
        from ..core.models import TranscriptCover

        raw = library._load_raw(entry_id)
        if raw is None:
            self._toast("这篇文稿已经不存在")
            return False
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        cover = TranscriptCover.from_meta(meta)
        if not cover.eligible or cover.status not in {
            "url_only", "failed", "downloaded",
        }:
            self._toast("这篇文稿没有可安全重试的封面来源")
            return False
        worker = _TranscriptCoverRetryWorker(
            entry_id,
            meta,
            self._cover_localizer,
        )
        self._transcript_cover_retry_worker = worker
        worker.done.connect(self._on_transcript_cover_retry_done)
        worker.failed.connect(self._on_transcript_cover_retry_failed)
        worker.finished.connect(
            lambda w=worker: self._on_transcript_cover_retry_finished(w)
        )
        self._toast("正在只重试封面，不会重新转录")
        worker.start()
        return True

    def _on_transcript_cover_retry_done(
        self,
        entry_id: str,
        meta: dict,
        receipt_status: str,
    ) -> None:
        from ..core import library
        from ..core.models import TranscriptCover

        raw = library._load_raw(entry_id)
        if raw is None:
            self._toast("文稿已删除，封面记录未写入")
            return
        raw["meta"] = dict(meta)
        try:
            library._write_raw(raw)
        except Exception:
            self._toast("封面文件已处理，但文稿记录更新失败")
            return
        cover = TranscriptCover.from_meta(meta)
        messages = {
            "downloaded": "封面已安全保存",
            "url_only": "封面保存已取消，原链接仍保留",
            "failed": (
                "上一份封面仍待安全恢复"
                if cover.error_code == "cover_commit_uncertain"
                else "封面仍未保存，原链接已保留"
            ),
        }
        message = (
            "已恢复上一次封面，本次新封面未保存"
            if receipt_status == "recovered_previous"
            else messages.get(cover.status, "封面状态已更新")
        )
        self._toast(message)
        if self.library_page is not None:
            self.library_page.refresh()

    def _on_transcript_cover_retry_failed(self, _entry_id: str) -> None:
        self._toast("封面重试未完成，原文稿和封面链接均已保留")

    def _on_transcript_cover_retry_finished(self, worker) -> None:
        if self._transcript_cover_retry_worker is worker:
            self._transcript_cover_retry_worker = None
        worker.deleteLater()


    def _on_library_entry_renamed(self, entry_id: str, _title: str) -> None:
        if self.library_page is not None:
            self.library_page.refresh()

    def rename_material_entry(self, entry_id: str, title: str) -> str | None:
        """素材标题保存入口。"""
        from ..core import library
        try:
            saved = library.rename(entry_id, title)
        except ValueError:
            dialogs.alert(self, "修改素材标题", "标题不能为空。")
            return None
        except (OSError, FileNotFoundError) as exc:
            dialogs.alert(self, "修改素材标题失败", str(exc))
            return None
        if self.library_page is not None:
            self.library_page.refresh()
        self._on_library_entry_renamed(entry_id, saved)
        return saved

    # ---------------- 侧边栏 ---------------- #
    def _sync_sidebar_density(self) -> None:
        compact = self.width() <= 1220
        if not hasattr(self, "brand_logo_label") or getattr(self, "_sidebar_compact", None) == compact:
            return
        self._sidebar_compact = compact
        margins = self.sidebar_layout.contentsMargins()
        padding = 10 if compact else 16
        self.sidebar_layout.setContentsMargins(padding, margins.top(), padding, margins.bottom())
        brand = self.brand_row.layout()
        brand.setContentsMargins(8, 6, 8, 6)
        brand.setSpacing(8 if compact else 11)
        logo_size = 52 if compact else BRAND_LOGO_SIZE
        self.brand_logo_label.setFixedSize(logo_size, logo_size)
        self.brand_logo_label.setPixmap(icons.image_pixmap(T.asset("logo_mascot_transparent.png"), logo_size))

    def _build_sidebar(self) -> QWidget:
        bar = PaperTextureFrame(strength=0.0, layer="rail")
        bar.setObjectName("sidebar")
        adopt_consumer_theme_scope(bar, "globalShell")
        self.sidebar_paper = bar
        bar.setFixedWidth(SIDEBAR_W)
        lay = QVBoxLayout(bar)
        self.sidebar_layout = lay
        lay.setContentsMargins(16, SIDEBAR_TOTAL_TOP, 16, 18)
        lay.setSpacing(4)
        self._sidebar_lay = lay

        # 品牌
        brand_row = QWidget()
        brand_row.setObjectName("brandRow")
        adopt_consumer_theme_scope(brand_row, "globalShell")
        brand_row.setFixedHeight(72)
        self.brand_row = brand_row
        brand = QHBoxLayout(brand_row)
        brand.setContentsMargins(8, 6, 8, 6)
        brand.setSpacing(11)
        logo = QLabel()
        logo.setObjectName("brandLogo")
        adopt_consumer_theme_scope(logo, "globalShell")
        logo.setProperty("brandAsset", "logo_mascot_transparent.png")
        logo.setFixedSize(BRAND_LOGO_SIZE, BRAND_LOGO_SIZE)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setAccessibleName("Lulu 品牌形象")
        logo.setPixmap(
            icons.image_pixmap(
                T.asset("logo_mascot_transparent.png"),
                BRAND_LOGO_SIZE,
            )
        )
        self.brand_logo_label = logo
        brand.addStretch(1)
        brand.addWidget(logo, 0, Qt.AlignmentFlag.AlignCenter)
        brand.addStretch(1)
        lay.addWidget(brand_row)

        self._queue_count_label = None
        self._library_count_labels = {}
        self._page_navs: list = []
        lay.addWidget(self._nav_label("工作台"))
        lay.addWidget(self._page_nav("headphones", "文案提取", 0, live_count=0))
        lay.addWidget(self._page_nav("workspace_grid", "主页采集", 5))
        lay.addWidget(self._page_nav("mic", "文案配音", 4))
        lay.addWidget(self._page_nav("video", "直播录制", LIVE_RECORDING_PAGE))
        lay.addWidget(self._nav_label("素材库"))
        lay.addWidget(self._library_nav("workspace_doc", "文稿库", "transcript"))
        lay.addWidget(self._library_nav("waveform", "音频库", "voice"))
        lay.addWidget(self._library_nav("trash", "最近删除", "trash"))
        lay.addStretch(1)
        lay.addWidget(self._nav_label("配置与连接"))
        lay.addWidget(self._page_nav("ai_cube", "AI 大模型", 1))
        lay.addWidget(self._nav_item("layers", "Obsidian 知识库",
                                     on_click=self._config_obsidian,
                                     img=T.asset("obsidian.png")))
        self.feishu_nav = self._nav_item("file", "飞书知识库", on_click=self._config_feishu, img=T.asset("feishu.png"))
        self._page_navs.append((6, "feishu", self.feishu_nav, None, self.feishu_nav.layout().itemAt(1).widget(), None))
        lay.addWidget(self.feishu_nav)

        self.local_storage_note = QLabel("●  资料留在本地")
        self.local_storage_note.setStyleSheet("font-size: 11px; color: #82796e; padding: 14px 12px 0;")
        lay.addWidget(self.local_storage_note)

        self._sync_toggle_icons()
        return bar

    def _nav_label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("navLabel")
        return lab

    def _nav_item(self, ico: str, text: str, active: bool = False,
                  soon: bool = False, live_count: int | None = None,
                  on_click=None, img: str | None = None) -> QFrame:
        row = QFrame()
        row.setFixedHeight(40)
        row.setObjectName("navItemActive" if active else "navItem")
        if on_click is not None:
            activate = on_click
        elif active:
            activate = lambda: None
        else:
            activate = lambda n=text: self._toast(f"「{n}」待上线")
        adopt_nav_row(
            row,
            selected=active,
            accessible_name=f"导航：{text}",
            on_activate=activate,
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 9, 10, 9)
        h.setSpacing(11)
        # img 给品牌 logo（原色、不随选中态变色）；否则用线性图标
        if img:
            h.addWidget(icons.image_label(img, 17))
        else:
            shell_theme = T.consumer_theme(T.ACTIVE.name)
            h.addWidget(icons.icon_label(
                ico,
                shell_theme.accent if active else shell_theme.ink_soft,
                17,
            ))
        lab = QLabel(text)
        lab.setObjectName("navTextActive" if active else "navText")
        h.addWidget(lab)
        h.addStretch(1)

        if live_count is not None:
            chip = QFrame()
            chip.setObjectName("navCountLive")
            adopt_badge(chip, kind="info")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(7, 1, 7, 1)
            ct = QLabel(str(live_count))
            ct.setObjectName("navCountLiveText")
            cl.addWidget(ct)
            h.addWidget(chip)
            self._queue_count_label = ct
            self._queue_count_chip = chip
            chip.setVisible(live_count > 0)
        elif soon:
            chip = QFrame()
            chip.setObjectName("soon")
            adopt_badge(chip, kind="neutral")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(7, 2, 7, 2)
            ct = QLabel("待上线")
            ct.setObjectName("soonText")
            cl.addWidget(ct)
            h.addWidget(chip)

        return row

    # ---------------- 引擎 / 设置 ---------------- #
    def _open_update_settings(self) -> None:
        self._switch_page(6)

    def _startup_target_page(self) -> int:
        from ..core.runtime_preferences import startup_page_index

        return startup_page_index(self.cfg)

    def _remember_top_level_page(self, index: int) -> bool:
        from ..core.runtime_preferences import remember_top_level_page

        return remember_top_level_page(self.cfg, index)

    def _sync_runtime_preferences(self) -> None:
        """把活动任务聚合成一个防休眠 token，不改任何业务页可见状态。"""

        if getattr(self, "power_manager", None) is None:
            return
        enabled = bool(getattr(self.cfg, "prevent_sleep_during_tasks", False))
        if getattr(self.power_manager, "enabled", None) != enabled:
            self.power_manager.set_enabled(enabled)
        self.power_manager.sync_activity(
            bool(self.has_active_work()), owner="main-window"
        )

    def _notify_task_completed(
        self,
        *,
        page_index: int | None = None,
        library_kind: str | None = None,
        entry_id: str | None = None,
    ) -> None:
        """统一消费通知、提示音和可选结果定位。"""

        from ..core.completion_feedback import CompletionTarget

        target = (
            CompletionTarget(page_index, library_kind=library_kind, entry_id=entry_id)
            if page_index is not None
            else None
        )
        self.completion_feedback.success(
            target,
            app_is_active=bool(getattr(self, "_window_is_active", True)),
        )

    def _notify_task_failed(self) -> None:
        """失败提醒只使用固定隐私文案，不接收业务错误内容。"""

        self.completion_feedback.failure(
            app_is_active=bool(getattr(self, "_window_is_active", True))
        )

    def _open_completion_target(self, target) -> None:
        """只在用户选择“打开对应结果”后定位，不抢占系统前台。"""

        if getattr(target, "entry_id", None):
            self.open_material_entry(target.entry_id, kind=getattr(target, "library_kind", None))
            return
        index = getattr(target, "page_index", None)
        if type(index) is int and index in range(LIVE_RECORDING_PAGE + 1):
            self._switch_page(index)

    def _request_update_install(self) -> None:
        if self.live_controller.active:
            self._toast("请先暂停直播监控并等待录像保存完成，再安装更新。")
            return
        gate = self.update_controller.install_gate()
        if gate == "active":
            self._toast("还有任务正在进行，完成后再更新。")
        elif gate == "unsaved":
            dialogs.alert(self, "保存后更新", "还有未保存的内容，请保存后再安装更新。")
        else:
            result = self.update_controller.launch_install()
            if result != "launching":
                self._open_update_settings()

    # ---------------- 切页导航 ---------------- #
    def _page_nav(self, ico: str, text: str, index: int, live_count: int | None = None) -> QFrame:
        row = QFrame()
        # Selected bold text and focus borders must not move subsequent rows.
        row.setFixedHeight(40)
        row.setObjectName("navItem")
        adopt_nav_row(
            row,
            accessible_name=f"导航：{text}",
            on_activate=lambda i=index: self._switch_page(i),
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 9, 10, 9)
        h.setSpacing(11)
        iconlab = icons.icon_label(ico, T.consumer_theme(T.ACTIVE.name).ink_soft, 17)
        lab = QLabel(text)
        lab.setObjectName("navText")
        h.addWidget(iconlab)
        h.addWidget(lab)
        h.addStretch(1)
        if live_count is not None:        # 「N 个进行中」小红点（挂在工作台上）
            chip = QFrame()
            chip.setObjectName("navCountLive")
            adopt_badge(chip, kind="info")
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(7, 1, 7, 1)
            ct = QLabel(str(live_count))
            ct.setObjectName("navCountLiveText")
            cl.addWidget(ct)
            h.addWidget(chip)
            self._queue_count_label = ct
            self._queue_count_chip = chip
            chip.setVisible(live_count > 0)
        self._page_navs.append((index, None, row, iconlab, lab, ico))
        return row

    def _library_nav(self, ico: str, text: str, kind: str) -> QFrame:
        row = QFrame()
        row.setFixedHeight(40)
        row.setObjectName("navItem")
        adopt_nav_row(
            row,
            accessible_name=f"导航：{text}",
            on_activate=lambda k=kind: self.open_library(k),
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 9, 10, 9)
        h.setSpacing(11)
        iconlab = icons.icon_label(ico, T.consumer_theme(T.ACTIVE.name).ink_soft, 17)
        lab = QLabel(text)
        lab.setObjectName("navText")
        h.addWidget(iconlab)
        h.addWidget(lab)
        h.addStretch(1)
        count = QLabel()
        count.setStyleSheet(f"font-size: 11px; color: {T.consumer_theme(T.ACTIVE.name).ink_soft};")
        count.setAccessibleName(f"{text}数量")
        h.addWidget(count)
        self._library_count_labels[kind] = count
        self._page_navs.append((2, kind, row, iconlab, lab, ico))
        return row

    def _update_library_counts(self, documents: int, voice: int) -> None:
        for kind, value in (("transcript", documents), ("voice", voice)):
            self._library_count_labels[kind].setText(str(value))

    def open_library(self, kind: str = "all") -> None:
        self._switch_page(2, library_kind=kind)

    def _open_recently_deleted(self) -> None:
        self.open_library("trash")

    def _open_workspace_models(self):
        self._switch_page(1)
        self.models_page.show_purpose(2)

    def _on_models_changed(self) -> None:
        workspace = self.extraction_workspace
        workspace._models_at = 0
        if self.voice_page is not None:
            self.voice_page.invalidate_model_status()
        # Do not refresh a hidden dubbing workbench inside the settings click.
        QTimer.singleShot(0, self, lambda: workspace.publish(force=True))

    def _switch_page(self, index: int, *, library_kind: str | None = None) -> None:
        if index in {3, 7}:  # Retired text workbenches now lead to the manuscript library.
            index = 2
            library_kind = "transcript"
        if index == 8:  # Retired workspace; old saved navigation stays safe.
            index = 0
        if self._storage_relocation_in_progress and index != 6:
            return
        self._ensure_page_loaded(index)
        self.stack.setCurrentIndex(index)
        self._sync_workbench_paper_scope(index)
        self._remember_top_level_page(index)
        # 切页后强制当前页按栈尺寸重排，避免上一页（如配音页）改过尺寸后，新页停在错误布局，
        # 需手动 toggle 侧栏才恢复。延后一拍等栈几何稳定。
        QTimer.singleShot(0, self, self._relayout_active_page)
        if index == 1:
            self.models_page.refresh()
        elif index == 2:
            if library_kind and hasattr(self.library_page, "_set_kind"):
                self.library_page._set_kind(library_kind)
            else:
                self.library_page.refresh()
        elif index == 4:
            QTimer.singleShot(0, self, lambda: self.voice_page.refresh() if self.stack.currentIndex() == 4 else None)
        elif index == 5:
            QTimer.singleShot(0, self, lambda: self.collection_page.refresh() if self.stack.currentIndex() == 5 else None)
        self._sync_page_navigation()
        self._sync_toggle_icons()

    def _sync_page_navigation(self, *_args) -> None:
        index = self.stack.currentIndex()
        current_kind = getattr(self.library_page, "_kind", "all") if self.library_page is not None else "all"
        if index == 6:
            current_kind = getattr(self.settings_page, "current_category", "general")
        for idx, kind, row, iconlab, lab, ico in self._page_navs:
            active = (idx == index and (kind is None or kind == current_kind)) or (idx == 1 and index == 2 and current_kind == "models")
            row.setObjectName("navItemActive" if active else "navItem")
            row.setProperty("selected", active)
            row.setProperty("consumerAccent", active)
            lab.setObjectName("navTextActive" if active else "navText")
            icon_color = (
                self.workbench_consumer_theme.accent
                if active
                else self.workbench_consumer_theme.ink_soft
            )
            if iconlab is not None:
                iconlab.setPixmap(icons.pixmap(ico, icon_color, 17))
            for w in (row, lab):
                w.style().unpolish(w)
                w.style().polish(w)
        self._sync_toggle_icons()

    def _sync_workbench_paper_scope(self, index: int | None = None) -> None:
        """Share the native paper canvas between extraction and model settings."""
        if index is None:
            index = self.stack.currentIndex() if hasattr(self, "stack") else 0
        active = index == 0
        shared_paper = index in (0, 1, LIVE_RECORDING_PAGE)
        shell_rail_strength = self.workbench_consumer_theme.paper_rail_opacity
        shell_canvas_strength = self.workbench_consumer_theme.paper_canvas_opacity
        from .agent_workspace.shell_style import apply as apply_workspace_shell_style
        apply_workspace_shell_style(self, index)
        canvas_strength = shell_canvas_strength if shared_paper else 0.0
        for frame in (
            getattr(self, "sidebar_paper", None),
        ):
            if isinstance(frame, PaperTextureFrame):
                frame.set_paper_texture_strength(shell_rail_strength)
                frame.setProperty("paperTextureActive", True)
        center = getattr(self, "center_wrap", None)
        if isinstance(center, PaperTextureFrame):
            center.set_paper_texture_strength(canvas_strength)
            center.setProperty("paperTextureActive", shared_paper)
        titlebar_strengths = (
            (getattr(self, "titlebar_paper_left", None), shell_rail_strength),
            (getattr(self, "titlebar_paper_center", None), shell_canvas_strength),
        )
        for material, strength in titlebar_strengths:
            if isinstance(material, PaperTextureFrame):
                material.set_paper_texture_strength(strength)
                material.setProperty("paperTextureActive", True)
                material.setVisible(True)
        title = getattr(self, "window_title_label", None)
        immersive = active or index == 2
        if title is not None:
            if title.property("immersiveTitlebar") != immersive:
                title.setProperty("immersiveTitlebar", immersive)
                title.style().unpolish(title)
                title.style().polish(title)
            title.setVisible(True)
        self._position_pane_toggles()
        if self._native_titlebar_active:
            from .macos import set_page_titlebar_immersive
            self._immersive_titlebar_ready = set_page_titlebar_immersive(self, immersive)

    def _relayout_active_page(self) -> None:
        # 页面在 QStackedWidget 里被切走时，内部（尤其 Ignored/嵌套 QStackedWidget）可能停在最小宽度，
        # 切回来不会自动重排。resize 到相同尺寸是 no-op 不触发 resizeEvent，这里先变 1px 再复原，
        # 强制一次真实 resizeEvent，让整棵子树重新布满。
        w = self.stack.currentWidget()
        if w is None:
            return
        sz = self.stack.size()
        if sz.width() > 2 and sz.height() > 2:
            # Chromium retains the previous page frame. A synthetic 1 px resize
            # invalidates it twice and visibly delays returning to the library.
            if w is not self.library_page:
                w.resize(sz.width() - 1, sz.height())
            w.resize(sz)
        lay = w.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()

    # ---------------- 主区（多页） ---------------- #
    def _build_main(self) -> QWidget:
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_workbench())          # 0 工作台
        self.models_page = None
        self.library_page = None
        self.voice_page = None
        self.collection_page = None
        self.settings_page = None
        self.live_recording_page = None
        for _ in range(1, LIVE_RECORDING_PAGE + 1):
            self.stack.addWidget(self._build_lazy_page_placeholder())
        self._switch_page(self._startup_target_page())
        QTimer.singleShot(350, self, self._prepare_library)
        return self.stack

    def _prepare_voice_page(self) -> None:
        # Build controls after the shell has painted; never load an inference
        # model or navigate. The same page is retained for the first user visit.
        if self.isVisible() and self.voice_page is None and not self._storage_relocation_in_progress:
            self._ensure_page_loaded(4)

    def _prepare_library(self) -> None:
        # Prepare the same persistent workspace after the shell's first frame.
        # Never navigate away from the user's current workbench.
        if self.isVisible() and self.library_page is None and not self._storage_relocation_in_progress:
            self._ensure_page_loaded(2)
            self.library_page._set_kind("transcript")
            if self.stack.currentIndex() != 2:
                self.library_page.call('visible', False)
        if self.isVisible() and not self._storage_relocation_in_progress:
            QTimer.singleShot(850, self, self._prepare_voice_page)

    def _build_lazy_page_placeholder(self) -> QWidget:
        page = QWidget()
        page.setObjectName("card")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(34, 34, 34, 34)
        lay.addStretch(1)
        label = QLabel("加载中…")
        label.setObjectName("muted")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(label)
        lay.addStretch(1)
        return page

    def _ensure_page_loaded(self, index: int) -> QWidget:
        if index in {3, 7}:
            index = 2
        if index == 0:
            return self.stack.widget(0)
        existing = {
            1: self.models_page,
            2: self.library_page,
            4: self.voice_page,
            5: self.collection_page,
            6: self.settings_page,
            LIVE_RECORDING_PAGE: self.live_recording_page,
        }.get(index)
        if existing is not None:
            return existing

        if index == 1:
            from .models_page import ModelsPage
            widget = ModelsPage(
                self.cfg,
                on_changed=self._on_models_changed,
                storage_busy=self.storage_relocation_active,
                model_in_use=self._model_usage_reason,
                relocation_busy=self.storage_relocation_busy,
                storage_activity_changed=self._on_settings_storage_activity_changed,
                workspace_models=self._open_workspace_models,
            )
            self.models_page = widget
        elif index == 2:
            from .agent_workspace.widget import AgentWorkspace
            widget = AgentWorkspace(self.cfg, self)
            widget.library_counts_changed.connect(self._update_library_counts)
            self.library_page = widget
        elif index == 4:
            from .voice_page import VoicePage
            widget = VoicePage(self.cfg, self)
            self.voice_page = widget
        elif index == 5:
            from .collection_page import CollectionPage
            widget = CollectionPage(
                self.cfg,
                on_transcribe=self._collect_transcribe,
                collection_service=self._collection_service,
                cover_preference_service=self._cover_preferences,
                cover_workflow=self._collection_cover_workflow,
                collection_task_states=self._collection_task_states,
            )
            self.collection_page = widget
        elif index == 6:
            from .settings_page import SettingsPage
            widget = SettingsPage(
                self.cfg,
                update_controller=self.update_controller,
                notification_service=self.notification_service,
                external_storage_busy=self.storage_relocation_busy,
                inheritance_controller=self._legacy_inheritance,
                lazy_categories=True,
            )
            widget.category_changed.connect(self._sync_page_navigation)
            widget.preferences_changed.connect(self._sync_preferences_to_workbench)
            widget.storage_activity_changed.connect(
                self._on_settings_storage_activity_changed
            )
            widget.install_requested.connect(self._request_update_install)
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            from ..core.updater.manifest import is_safe_manual_url
            widget.manual_download.connect(lambda url: QDesktopServices.openUrl(QUrl(url)) if is_safe_manual_url(url) else None)
            self.settings_page = widget
        elif index == LIVE_RECORDING_PAGE:
            from .live_recording_page import LiveRecordingPage
            widget = LiveRecordingPage(self.live_controller, transcribe=self._live_transcribe, parent=self)
            self.live_recording_page = widget
        elif index == 8:
            return self.stack.widget(0)
        else:
            return self.stack.widget(index)

        old = self.stack.widget(index)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(index, widget)
        return widget

    def _sync_preferences_to_workbench(self, field: str) -> None:
        """设置页保存成功后，把已加载工作台控件同步到同一份 AppConfig。"""
        if field in {"keep_media", "prefer_subtitles", "link_media_mode", "media_dir", "recording_dir"}:
            self._publish_extraction_state()
        elif field == "tts_output_dir" and self.voice_page is not None:
            for method_name in (
                "_update_output_dir_btn",
            ):
                method = getattr(self.voice_page, method_name, None)
                if method is not None:
                    method()
        elif field == "voice_library_dir":
            from ..core.synthesis import voices

            voices.configure_voice_library_dir(getattr(self.cfg, "voice_library_dir", ""))
            if self.voice_page is not None:
                self.voice_page.refresh()
        elif field == "models_dir":
            if self.models_page is not None:
                self.models_page.refresh()
        elif field == "prevent_sleep_during_tasks":
            self._sync_runtime_preferences()

    def storage_relocation_active(self) -> bool:
        return bool(self._storage_relocation_in_progress)

    def storage_relocation_busy(self) -> bool:
        from ..core.engines.local_models import LOCAL_MODELS
        from ..core.synthesis.runtime_packages import TTS_RUNTIMES
        page = getattr(self, "models_page", None)
        return bool(page is not None and page.storage_is_busy()) or self.has_active_work() or any(self._model_usage_reason(mid) for mid in (*LOCAL_MODELS, *TTS_RUNTIMES))

    def _on_settings_storage_activity_changed(self, active: bool) -> None:
        """迁移模型或音色期间，冻结所有可能产生并发读写的业务入口。"""
        active = bool(active)
        self._storage_relocation_in_progress = active
        self.left_wrap.setEnabled(not active)
        for page in (self.models_page, self.voice_page):
            if page is not None:
                page.setEnabled(not active)

    def open_voice_studio(self, text: str = "", source: dict | None = None) -> None:
        self._switch_page(4)
        if self.voice_page.set_text(text, source=source):
            self._toast("已带入配音文本")
        elif str(text or "").strip():
            dialogs.alert(self, "带入配音失败", "这篇成稿没有唯一可用的“逐字稿”正文，未带入配音。")

    def open_voice_library(self) -> None:
        self.open_library("voice")

    def open_material_entry(self, entry_id: str, kind: str | None = None) -> None:
        """跳转到一条素材（如配音详情里点「跳转来源」回到上游文稿/输出稿）。
        kind 给定来源类型（transcript/rewrite）时，直接落到对应的分库，
        而不是泛泛的「全部」，跳转更精确、和侧栏分库一致。"""
        from ..core import library
        if not entry_id or library._load_raw(entry_id) is None:
            dialogs.alert(self, "跳转来源", "来源素材已删除或不在原位置。")
            return
        # 来源类型不明时，以素材实际 kind 兜底；再不行才用「全部」
        target = kind if kind in ("transcript", "rewrite", "voice") else None
        if target is None:
            entry = library.document_snapshot(entry_id)
            ek = str((entry or {}).get("kind") or "")
            target = ek if ek in ("transcript", "rewrite", "voice") else "all"
        self.open_library(target)
        self.library_page.open_entry(entry_id)


    def _build_workbench(self) -> QWidget:
        from .extraction.widget import ExtractionWorkspace
        self.extraction_workspace = ExtractionWorkspace(self)
        return self.extraction_workspace


    # ================================================================== #
    # 交互

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):  # noqa: N802
        super().dragLeaveEvent(e)

    def dropEvent(self, e):  # noqa: N802
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p:
                self._add_task(p, "file", Path(p).name)

    def _pick_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择音频/视频文件", self.cfg.last_output_dir,
            "媒体文件 (*.mp3 *.wav *.m4a *.aac *.flac *.mp4 *.mov *.mkv *.webm);;所有文件 (*)",
        )
        workspace = getattr(self, "extraction_workspace", None)
        if workspace is not None:
            workspace.add_files(paths)
        else:
            for p in paths:
                self._add_task(p, "file", Path(p).name)

    def _grab_link(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._toast("先粘贴一个链接吧")
            return
        from ..core import podcast
        if podcast.looks_like_feed(text):       # 像播客 RSS 订阅 → 先解析、再选集
            if self._feed_worker is not None:
                self._toast("正在解析播客订阅，请稍候")
                return
            self._toast("正在解析播客订阅…")
            from .podcast_dialog import _FeedWorker
            worker = _FeedWorker(text, self)
            self._feed_worker = worker
            worker.done.connect(
                lambda feed, raw=text, current=worker:
                self._on_feed_result(current, feed, raw)
            )
            worker.finished.connect(
                lambda current=worker: self._finish_feed_worker(current)
            )
            worker.start()
            return
        self._add_task(text, "link", "链接转写")

    def _collect_transcribe(self, sources: list[str], folder_name: str | None = None) -> None:
        """采集页「批量转录选中」→ 加入工作台转录队列。folder_name 非空时，这批自动归入同名文件夹。"""
        from ..core import library
        fid = library.ensure_folder("transcript", folder_name) if folder_name else None
        for s in sources:
            self._add_task(s, "link", "采集转写", folder_id=fid)
        self._switch_page(0)
        self._toast(f"已加入 {len(sources)} 条到转录队列")

    def _live_transcribe(self, source: str) -> bool:
        """Create a local text task directly from a completed recording segment."""
        from ..core.engines.local_models import LOCAL_MODELS
        from ..core.engines.providers import DEFAULT_ENGINE
        from ..core.collect.preferences import MaterialPreferenceSnapshot
        path = Path(source)
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.suffix.lower() not in {'.ts', '.mp4', '.mp3'}
                or not 0 < path.stat().st_size <= 2 * 1024**3):
            self._toast("请选择已完成且不超过 2 GB 的录像分段，原录像保留")
            return False
        if self._task_start_blocked():
            return False
        engine = self.cfg.engine_name if self.cfg.engine_name in LOCAL_MODELS else DEFAULT_ENGINE
        policy = TaskExecutionPolicy(origin='single_link', mode='materialize', stable_key='',
            material_snapshot=MaterialPreferenceSnapshot('audio', False, False),
            media_destination='none', prefer_subtitles=False, engine_name=engine,
            requested_outputs=('text',))
        task = self._add_task(str(path), "file", "直播录像转录", execution_policy=policy)
        self._switch_page(0)
        if task.state == TaskState.FAILED:
            return False
        self._toast("已创建本地文案任务，点击任务上的“开始”即可提取")
        return True

    def _collect_transcribe_group(
        self,
        candidates: list[TaskGroupCandidate],
        folder_name: str | None = None,
    ) -> None:
        """把两条以上可执行作品编成运行期组，子任务仍走既有串行队列。"""
        selected_engine = self.cfg.engine_name
        unfinished = {
            (task.stable_key, task.execution_policy.engine_name or selected_engine) for task in self.tasks
            if task.stable_key and task.state in {TaskState.PENDING, TaskState.QUEUED, TaskState.RUNNING}
            and task.execution_policy is not None
            and task.execution_policy.origin == "homepage_batch_transcribe"
        }
        unique = []
        for candidate in candidates:
            identity = (candidate.stable_key, selected_engine)
            if candidate.executable and identity in unfinished:
                continue
            unique.append(candidate)
            if candidate.executable:
                unfinished.add(identity)
        if not unique:
            self._switch_page(0)
            self._toast("所选作品已在转录队列中")
            return
        candidates = unique
        executable = [candidate for candidate in candidates if candidate.executable]
        if len(executable) < 2:
            if executable:
                candidate = executable[0]
                from ..core import library

                folder_id = (
                    library.ensure_folder("transcript", folder_name)
                    if folder_name
                    else None
                )
                self._add_task(
                    candidate.source,
                    candidate.kind,
                    task_display_title(candidate.title or "采集转写"),
                    folder_id=folder_id,
                    stable_key=candidate.stable_key,
                    execution_policy=TaskExecutionPolicy.transcription_only(
                        origin="homepage_batch_transcribe",
                        stable_key=candidate.stable_key,
                        snapshot=self._material_snapshot(),
                    ),
                )
                self._switch_page(0)
                self._toast("已加入 1 条到转录队列")
            return

        from ..core import library

        folder_id = (
            library.ensure_folder("transcript", folder_name)
            if folder_name
            else None
        )
        group_id = self._next_group_id
        self._next_group_id += 1
        group = TaskGroup(
            id=group_id,
            name=folder_name or f"批量转录 {group_id}",
            members=[],
            folder_id=folder_id,
        )
        self.task_groups.append(group)
        for candidate in candidates:
            member = TaskGroupMember(
                stable_key=candidate.stable_key,
                title=candidate.title,
                source=candidate.source,
                kind=candidate.kind,
                skipped_reason=candidate.skipped_reason,
            )
            group.members.append(member)
            if candidate.executable:
                task = self._add_task(
                    candidate.source,
                    candidate.kind,
                    task_display_title(candidate.title or "采集转写"),
                    folder_id=folder_id,
                    stable_key=candidate.stable_key,
                    execution_policy=TaskExecutionPolicy.transcription_only(
                        origin="homepage_batch_transcribe",
                        stable_key=candidate.stable_key,
                        snapshot=self._material_snapshot(),
                    ),
                    defer_queue_refresh=True,
                )
                member.task_id = task.id
                member.attempt_task_ids.append(task.id)

        self._refresh_badge()
        self._persist_task_queue()
        self._switch_page(0)
        skipped = len(candidates) - len(executable)
        suffix = f"，{skipped} 条已诚实跳过" if skipped else ""
        self._toast(f"已建立 {len(executable)} 条批量转录任务{suffix}")

    def _on_feed(self, feed, raw: str) -> None:
        if feed is None:                        # 不是有效播客 RSS → 当普通链接处理
            self._add_task(raw, "link", "链接转写")
            return
        from .podcast_dialog import PodcastPicker
        dlg = PodcastPicker(feed, self)
        if dlg.exec() and dlg.chosen:
            for ep in dlg.chosen:
                self._add_task(ep.audio_url, "link", ep.title[:60] or "播客单集")
            self._toast(f"已加入 {len(dlg.chosen)} 集，点卡片「开始」转写")

    def _on_feed_result(self, worker, feed, raw: str) -> None:
        if self._feed_worker is not worker or self._close_settling:
            return
        self._on_feed(feed, raw)

    def _finish_feed_worker(self, worker) -> None:
        if self._feed_worker is worker:
            self._feed_worker = None
        worker.deleteLater()

    # ---------------- 提取偏好与录音来源 ---------------- #


    def _on_rec_source_selected(self, source: str) -> None:
        if source not in {"microphone", "system_audio", "mixed"}:
            raise ValueError("录音来源无效")
        self._rec_source = source
        self._publish_extraction_state()


    def _commit_quick_preference(self, field: str, value) -> bool:
        """偏好落盘成功后才提交控件与下一条任务会读取的内存值。"""
        previous = getattr(self.cfg, field)
        setattr(self.cfg, field, value)
        try:
            saved = self.cfg.save() is True
        except Exception:  # noqa: BLE001
            saved = False
        if not saved:
            setattr(self.cfg, field, previous)
            MainWindow._sync_preferences_to_workbench(self, field)
            self._toast("设置没有保存，本次仍按原设置执行")
            return False
        MainWindow._sync_preferences_to_workbench(self, field)
        return True

    def _material_snapshot(self):
        """生产服务返回完整快照；旧 fixture 注入时安全补齐三项。"""
        from ..core.collect.preferences import MaterialPreferenceSnapshot

        try:
            snapshot = self._material_preferences.snapshot()
        except Exception:
            snapshot = None
        if isinstance(snapshot, MaterialPreferenceSnapshot):
            return snapshot
        cover = bool(getattr(snapshot, "include_cover", False))
        return MaterialPreferenceSnapshot(
            download_mode=(
                "video" if self.cfg.link_media_mode == "video" else "audio"
            ),
            save_media=bool(self.cfg.keep_media),
            save_cover=cover,
        )


    # ---------------- 队列 ---------------- #
    def _persist_task_queue(self, *, force: bool = False) -> bool:
        if getattr(self, "_install_preparation_pending", False):
            # Never touch the Core ledger while installation is reconciling it.
            # Closing can retain new local intents without overwriting old drafts.
            if force and self._close_settling:
                if self.tasks or self.task_groups:
                    try:
                        self._task_queue_recovery.save(self.tasks, self.task_groups, preserve_existing=True,
                            hidden_ids=getattr(self, "_core_hidden_intents", set()))
                    except (OSError, ValueError, RuntimeError, TypeError):
                        self._result_recovery_failed = True
                        self._toast("恢复记录未能保存，请检查磁盘空间后重试")
                        return False
                return True
            self._core_save_pending = bool(self.tasks or self.task_groups)
            return False
        recovery = getattr(self, "_task_queue_recovery", None)
        if recovery is not None and getattr(self, "_task_queue_recovery_checked", False):
            try:
                recovery.save(self.tasks, self.task_groups, preserve_existing=False,
                    hidden_ids=getattr(self, "_core_hidden_intents", set()))
                self._result_recovery_failed = False
            except (OSError, ValueError, RuntimeError, TypeError):
                self._result_recovery_failed = True
                self._core_save_pending = True
                # No Core write has been issued by this call. Release only an
                # unsubmitted start; an in-flight acknowledgement settles first.
                starting = self._task(getattr(self, "_core_starting_task_id", None))
                if (starting is not None and self.current is None
                        and getattr(self, "_core_save_worker", None) is None
                        and getattr(starting, "_core_state", None) != "running"):
                    starting.state = TaskState.QUEUED
                    self._core_starting_task_id = None
                self._suspend_unstarted_tasks()
                if getattr(self, "_task_queue_sync_retry_pending", False):
                    self._task_queue_sync_retry_pending = False
                    self._task_queue_store_failed = True
                    self._task_queue_auto_sync_allowed = False
                    self._refresh_badge()
                self._toast("恢复记录未能保存，请检查磁盘空间后重试")
                return False
        store = getattr(self, "_task_queue_store", None)
        if (
            store is None
            or getattr(self, "_task_queue_store_failed", False)
            or (getattr(self, "_task_queue_snapshot_locked", False) and not force)
        ):
            return False
        if getattr(store, "async_write", False):
            if self._task_queue_startup_pending and not (force and self._close_settling):
                self._core_save_pending = True
                return False
            # History-only saves still settle recovery and in-flight writes.
            # The ledger itself skips transitions for read-only historical jobs.
            signature = (
                tuple((t.id, t.state, t.library_id, t.transcript is not None, t.error, t.title, t.execution_policy) for t in self.tasks),
                tuple((g.id, g.name, g.expanded, tuple((m.task_id, m.skipped_reason) for m in g.members)) for g in self.task_groups),
            )
            worker = getattr(self, "_core_save_worker", None)
            if worker is not None:
                self._core_save_pending = True
                return False
            if (signature == getattr(self, "_core_saved_signature", None)
                    and not getattr(self, "_core_recovery_pending", False)):
                return True
            from .worker import CoreTaskSaveWorker
            worker = CoreTaskSaveWorker(store, self.tasks, self.task_groups, self)
            worker.signature = signature
            worker.hidden_intents = set(getattr(self, "_core_hidden_intents", set()))
            self._core_save_worker = worker
            self._core_save_pending = False
            worker.finished.connect(lambda: self._finish_core_task_save(worker))
            worker.start()
            return False
        try:
            return bool(store.save(self.tasks, self.task_groups))
        except Exception:
            self._task_queue_store_failed = True
            self._task_queue_auto_sync_allowed = False
            if hasattr(self, "_toast"):
                self._toast("任务尚未保存，已暂停启动；恢复后可继续")
            return False

    def _finish_core_task_save(self, worker) -> None:
        from ..core.gui_task_ledger import CoreTaskQueueStore
        for snapshot in worker.tasks:
            task = self._task(snapshot.id)
            if task is not None and hasattr(snapshot, "_core_job_revision"):
                CoreTaskQueueStore._adopt(task, snapshot)
        self._core_save_worker = None
        if worker.ok:
            getattr(self, "_core_hidden_intents", set()).difference_update(worker.hidden_intents)
            self._core_saved_signature = worker.signature
            if not getattr(self, "_core_save_pending", False):
                self._task_queue_sync_retry_pending = False
            if (getattr(self, "_core_recovery_pending", False)
                    and not getattr(self, "_core_save_pending", False)
                    and not any(has_unsaved_result(t) for t in self.tasks)):
                try:
                    self._task_queue_recovery.archive()
                    self._core_recovery_pending = False
                except OSError:
                    self._toast("任务已同步，恢复草稿仍保留在本机")
            if getattr(self, "_core_save_pending", False):
                self._core_save_pending = False
                if self._persist_task_queue():
                    self._pump()
            else:
                self._pump()
        else:
            self._task_queue_store_failed = True
            self._task_queue_sync_retry_pending = False
            # A successful list read cannot prove a failed write will work.
            # Retry only after explicit Continue or Sync, including partial batches.
            self._task_queue_auto_sync_allowed = False
            self._suspend_unstarted_tasks("任务尚未保存，输入已保留；恢复后点击继续")
            code = getattr(worker, "error_code", "CORE_OPERATION_FAILED")
            message = {
                "GUI_TASK_PAYLOAD_INVALID": "任务信息未能保存，输入已保留；请重新加入提示异常的作品",
                "GUI_VIEW_INVALID": "批次信息未能保存，输入已保留；请检查批次后重新同步",
                "GUI_VIEW_REVISION_CONFLICT": "任务列表刚有变化，输入已保留；点击重新同步可核对记录",
                "CORE_TRANSPORT_UNAVAILABLE": "任务服务连接中断，输入已保留；点击重新同步可重新连接",
                "CORE_TRANSPORT_TIMEOUT": "任务保存确认超时，输入已保留；点击重新同步可核对保存状态",
            }.get(code, "任务尚未保存，输入已保留；点击重新同步可重试，仍失败时请检查本机存储")
            failed = self._task(getattr(worker, "failed_task_id", None))
            if failed is not None and code == "GUI_TASK_PAYLOAD_INVALID":
                failed.message = "任务信息未能保存，请重新加入这条作品"
                message = f"“{task_display_title(failed.title, limit=40)}”的任务信息未能保存，输入已保留；请重新加入这条作品"
            self._toast(message)
        self._refresh_badge()
        worker.deleteLater()

    def _retry_task_queue_sync(self) -> None:
        """Explicitly settle saved results/removals without starting any work."""
        if (not self._task_queue_store_failed or self._task_queue_startup_pending
                or getattr(self, "_core_save_worker", None) is not None
                or getattr(self, "_install_preparation_pending", False)
                or self._task_start_blocked()):
            return
        self._task_queue_sync_retry_pending = True
        self._task_queue_startup_pending = True
        self._refresh_badge()
        self._start_task_queue_connection()

    def _suspend_unstarted_tasks(self, message="任务服务暂时不可用，输入已保留；恢复后点击继续") -> None:
        """连接故障取消自动启动意图，正在实际执行的线程仍由正常路径收尾。"""
        for task in self.tasks:
            if self.current is not None and self.current[0].id == task.id:
                continue
            if task.state in (TaskState.QUEUED, TaskState.RUNNING):
                task._core_recovered_queue = True
                task.message = message

    def finish_install_preparation(self, success: bool, *, reason=None) -> None:
        if not self._install_preparation_pending:
            return
        self._install_preparation_pending = False
        from ..core.agent_entry.standalone_bootstrap import preparation_failure_message
        self._install_preparation_error = "" if success else preparation_failure_message(reason)
        workspace = getattr(self, 'library_page', None)
        if workspace is not None and hasattr(workspace, 'finish_install_preparation'):
            workspace.finish_install_preparation(success, self._install_preparation_error)
        if success:
            self._start_task_queue_connection()
        else:
            self._task_queue_connection_failed("INSTALLATION_PREPARATION_FAILED")

    def _task_start_blocked(self) -> bool:
        if getattr(self, "_close_settling", False):
            return True
        if getattr(self, '_storage_relocation_in_progress', False):
            self._toast('正在处理本地资料，请稍后开始任务')
            return True
        reason = getattr(self, "_install_preparation_error", "")
        if reason:
            self._toast(reason)
            return True
        return False

    def _legacy_tasks_migration_finished(self, success):
        if self.live_controller.connected or self.live_controller.worker is not None:
            self.live_controller.reconnect()
        controller = getattr(self, '_legacy_inheritance', None)
        if controller is not None and controller.state in {'failed', 'partial'}:
            self._toast('部分旧版记录尚未读取，原资料保留。可在「文件与存储」查看详情')
        self._task_queue_auto_sync_allowed = False
        self._task_queue_initial_connection = True
        self._task_queue_startup_pending = True
        self._start_task_queue_connection()

    def _reconnect_before_task_start(self, tasks) -> bool:
        """Keep explicit starts paused until one connection read has succeeded."""
        if not getattr(self, "_task_queue_store_failed", False):
            return False
        intents = getattr(self, "_task_queue_resume_ids", set())
        self._task_queue_resume_ids = intents
        for task in tasks:
            pending_start = (self.current is None and task.id == getattr(self, "_core_starting_task_id", None))
            if ((task.state in (TaskState.PENDING, TaskState.QUEUED) or pending_start)
                    and not getattr(task, "_core_view_only", False)):
                intents.add(task.id)
                if not pending_start:
                    task.state = TaskState.QUEUED
                task._core_recovered_queue = True
                task.message = "正在重新连接，输入已保留…"
        self._refresh_badge()
        # Repeated clicks share the same read. Permission failures still never
        # enable automatic retry; another failure consumes this user intent.
        if intents and not self._task_queue_startup_pending:
            self._task_queue_startup_pending = True
            self._start_task_queue_connection()
        return True

    def _start_task_queue_connection(self) -> None:
        if getattr(self, "_install_preparation_pending", False) or not self._task_queue_startup_pending or self._close_settling:
            return
        from .worker import CoreTaskStartupWorker
        previous = self._task_queue_startup_worker
        if previous is not None and previous.isRunning():
            # ready/failed can reach the GUI before QThread has fully exited.
            # Preserve a new explicit retry until that previous read is done.
            previous.finished.connect(self._start_task_queue_connection, Qt.SingleShotConnection)
            return
        if previous is not None:
            previous.deleteLater()
        store = None if getattr(self, "_task_queue_initial_connection", True) else self._task_queue_store
        worker = CoreTaskStartupWorker(self, store=store)
        self._task_queue_startup_worker = worker
        worker.ready.connect(self._task_queue_connection_ready)
        worker.failed.connect(self._task_queue_connection_failed)
        worker.start()

    def _sync_shared_tasks(self) -> None:
        """后台读取共享任务；不重放创建、开始或保存请求。"""
        if getattr(self, "_install_preparation_pending", False) or getattr(self, '_storage_relocation_in_progress', False) or not self._task_queue_auto_sync_allowed:
            return
        if (getattr(self, "_core_save_worker", None) is not None
                or (getattr(self, "_core_starting_task_id", None) is not None
                    and not getattr(self, "_task_queue_store_failed", False)
                    and not getattr(self, "_result_recovery_failed", False))):
            return
        worker = self._task_queue_startup_worker
        if self._close_settling or (worker is not None and worker.isRunning()):
            return
        self._task_queue_startup_pending = True
        # An injected/previously ready store is already a valid connection.
        if not self._task_queue_store_failed:
            self._task_queue_initial_connection = False
        self._start_task_queue_connection()

    def _task_queue_connection_ready(self, store, snapshot) -> None:
        self._task_queue_startup_pending = False
        resume_ids = getattr(self, "_task_queue_resume_ids", set())
        self._task_queue_resume_ids = set()
        if self._close_settling:
            return
        self._task_queue_store = store
        self._task_queue_initial_connection = False
        self._task_queue_startup_pending = False
        self._task_queue_store_failed = False
        self._task_queue_auto_sync_allowed = True
        inheritance = getattr(self, '_legacy_inheritance', None)
        if inheritance is not None:
            QTimer.singleShot(0, self, inheritance.maybe_start)
        if getattr(sys, "frozen", False):
            self.live_controller.start()
        recovery = getattr(self, "_task_queue_recovery", None)
        if recovery is not None and not self._task_queue_recovery_checked:
            try:
                snapshot, self._core_recovery_pending = recovery.merge(snapshot,
                    authoritative_hidden_ids=getattr(store, 'hidden_ids', ()))
                if not hasattr(self, "_core_hidden_intents"):
                    self._core_hidden_intents = set()
                self._core_hidden_intents.update(recovery.hidden_ids)
                self._task_queue_recovery_checked = True
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                self._toast("上次的恢复草稿无法读取，原文件已保留")
        self._restore_task_queue(snapshot)
        for task in self.tasks:
            if (task.id in resume_ids and task.state in (TaskState.PENDING, TaskState.QUEUED)
                    and MainWindow._task_group_id_for_task(self, task.id)
                    not in (getattr(self, "_paused_task_group_ids", set())
                            | getattr(self, "_deleting_task_group_ids", set()))):
                self._start_task(task.id)
        if getattr(store, "async_write", False) and (getattr(self, "_task_queue_sync_retry_pending", False) or getattr(self, "_core_save_pending", False) or self._core_recovery_pending or getattr(self, "_core_hidden_intents", set()) or any(
            not getattr(t, "_core_view_only", False)
            and store._desired_state(t) != getattr(t, "_core_state", None)
            for t in self.tasks
        )):
            self._core_saved_signature = None
            self._persist_task_queue()
        elif getattr(store, "async_write", False):
            # The read may have deferred the next current-session queue item.
            # _pump still excludes recovered work until the user resumes it.
            self._pump()
        else:
            self._task_queue_sync_retry_pending = False

    def _task_queue_connection_failed(self, _reason: str) -> None:
        explicit_retry = bool(getattr(self, "_task_queue_resume_ids", set())) or getattr(self, "_task_queue_sync_retry_pending", False)
        self._task_queue_sync_retry_pending = False
        self._task_queue_startup_pending = False
        self._task_queue_resume_ids = set()
        if self._close_settling:
            return
        self._task_queue_startup_pending = False
        self._task_queue_store_failed = True
        self._suspend_unstarted_tasks()
        # 仅连接中断和超时允许后台重试。权限/数据校验失败停止读取，
        # 避免每个定时周期重新弹出系统授权或反复读取异常存储。
        self._task_queue_auto_sync_allowed = not explicit_retry and _reason in {
            f"CORE_TASK_SERVICE_UNAVAILABLE:{stage}:{code}"
            for stage in ("connect", "load") for code in (20, 21)
        }
        self._refresh_badge()
        self._toast(getattr(self, "_install_preparation_error", "") or "任务服务暂时不可用，原记录已保留")

    def _restore_task_queue(self, snapshot=None) -> None:
        store = getattr(self, "_task_queue_store", None)
        if store is None:
            return
        try:
            if snapshot is None:
                snapshot = store.load()
        except Exception:
            self._task_queue_store_failed = True
            message = (
                "任务服务暂时不可用，任务列表尚未同步"
                if getattr(store, "authoritative", False)
                else "上次任务列表无法恢复，原记录已保留"
            )
            QTimer.singleShot(
                0,
                lambda: self._toast(message),
            )
            return

        authoritative = bool(getattr(store, "authoritative", False))
        existing_tasks = {task.id: task for task in self.tasks}
        # Completion belongs to the current GUI session. The Core ledger and
        # saved transcripts remain intact; polling must not resurrect old cards.
        if not hasattr(self, "_session_hidden_completed"):
            self._session_hidden_completed = {
                task.id for task in snapshot.tasks
                if task.state == TaskState.DONE and task.id not in existing_tasks
            }
        hidden_completed = self._session_hidden_completed
        for task in snapshot.tasks:
            if task.id in hidden_completed:
                continue
            if task.id in getattr(self, "_core_hidden_intents", set()):
                continue
            existing = existing_tasks.get(task.id)
            if authoritative and existing is not None:
                from ..core.gui_task_ledger import CoreTaskQueueStore
                old_ref = getattr(existing, "_core_result_ref", None)
                had_local_intent = CoreTaskQueueStore._desired_state(existing) != getattr(existing, "_core_state", None)
                CoreTaskQueueStore._adopt(existing, task)
                if existing.id == getattr(self, "_core_starting_task_id", None) and self.current is None:
                    # Core 已认领但 GUI 没有实际启动：结算为空启动失败，不擅自执行。
                    existing.state = TaskState.FAILED if task._core_state == "running" else TaskState.QUEUED
                    existing._core_recovered_queue = True
                    existing.message = "上次启动未完成，点击重试" if existing.state == TaskState.FAILED else "连接已恢复，点击继续"
                    if existing.state == TaskState.FAILED:
                        existing.error = "任务启动确认中断，尚未开始下载或转录。请点击重试。"
                    had_local_intent = True
                    self._core_starting_task_id = None
                if bool(getattr(existing, "_core_view_only", False)) or (not had_local_intent and (self.current is None or self.current[0].id != existing.id)):
                    existing.state, existing.message = task.state, task.message
                    existing.error = task.error
                    existing._core_job_revision = task._core_job_revision
                    existing._core_result_ref, existing._core_state = task._core_result_ref, task._core_state
                    if old_ref != task._core_result_ref:
                        existing.transcript = task.transcript
                        existing.library_id = task.library_id
                if task._core_state == "cancel_requested" and self.current is not None and self.current[0].id == task.id:
                    self._request_task_cancel(task.id)
                continue
            if existing is not None:
                continue
            if not authoritative and task.state != TaskState.FAILED:
                was_interrupted = task.state in (TaskState.QUEUED, TaskState.RUNNING)
                task.state = TaskState.PENDING
                task.progress = 0.0
                task.message = "上次未完成，待继续" if was_interrupted else "待转录"
                task.error = None
            self.tasks.append(task)
            existing_tasks[task.id] = task

        restored_groups = []
        for group in snapshot.groups:
            from dataclasses import replace
            group = replace(group, members=[m for m in group.members
                            if m.task_id not in hidden_completed
                            and m.task_id not in getattr(self, "_core_hidden_intents", set())])
            if group.members:
                restored_groups.append(group)
        from ..core.task import merge_task_groups
        self.task_groups = merge_task_groups(self.task_groups, restored_groups)

        numeric_ids = [task.id for task in (*self.tasks, *snapshot.tasks) if type(task.id) is int]
        self._next_id = max(numeric_ids, default=0) + 1
        self._next_group_id = max((group.id for group in (*self.task_groups, *snapshot.groups)), default=0) + 1
        self._refresh_badge()
        # 恢复历史只恢复任务和分组，不替用户隐藏导入、链接与录音区域。
        # 旧恢复文件要把假运行态收敛为 pending；Core 权威态保持原样。
        if not authoritative:
            self._persist_task_queue()


    def _add_task(
        self,
        source: str,
        kind: str,
        title: str,
        folder_id: str | None = None,
        *,
        stable_key: str = "",
        execution_policy: TaskExecutionPolicy | None = None,
        defer_queue_refresh: bool = False,
    ) -> Task:
        workspace = getattr(self, "extraction_workspace", None)
        if execution_policy is None and workspace is not None:
            execution_policy = workspace.initial_policy(source, kind)
        task = Task(
            id=self._next_id,
            title=title,
            source=source,
            kind=kind,
            folder_id=folder_id,
            stable_key=str(stable_key or ""),
            execution_policy=execution_policy,
        )
        task._display_new = True
        register = getattr(self._task_queue_store, "prepare" if getattr(self._task_queue_store, "async_write", False) else "register", None)
        if callable(register):
            try:
                task = register(task)
            except Exception:
                self._task_queue_store_failed = True
                task.state = TaskState.FAILED
                task.message = "任务服务暂时不可用"
                task.error = "后台任务服务暂时不可用，新任务尚未保存"
                self._toast("任务服务暂时不可用，新任务尚未保存")
        self._next_id += 1
        self.tasks.append(task)
        if not defer_queue_refresh:
            self._refresh_badge()
            self._persist_task_queue()
        # 新任务停在「待转录」，等用户点卡片上的「开始」再排队转录。
        return task


    def _start_task(self, tid: int | str) -> None:
        if MainWindow._task_start_blocked(self):
            return
        t = self._task(tid)
        if (
            t is None
            or (t.state not in (TaskState.PENDING, TaskState.QUEUED)
                and not (getattr(self, "_task_queue_store_failed", False)
                         and self.current is None and t.id == getattr(self, "_core_starting_task_id", None)))
            or bool(getattr(t, "_core_view_only", False))
        ):
            return
        if t.execution_policy is not None and t.execution_policy.requested_outputs == ():
            return
        if MainWindow._reconnect_before_task_start(self, [t]):
            return
        self._freeze_task_policy(t)
        t._core_recovered_queue = False
        t.state = TaskState.QUEUED
        t.message = "排队中…"
        self._refresh_badge()
        self._persist_task_queue()
        self._pump()

    def _start_all(self) -> None:
        if MainWindow._task_start_blocked(self):
            return
        selected = [t for t in self.tasks
                    if (t.state == TaskState.PENDING or (t.state == TaskState.QUEUED
                        and getattr(t, "_core_recovered_queue", False))
                        or (getattr(self, "_task_queue_store_failed", False)
                            and self.current is None and t.id == getattr(self, "_core_starting_task_id", None)))
                    and not getattr(t, "_core_view_only", False)
                    and (t.execution_policy is None or t.execution_policy.requested_outputs != ())]
        if MainWindow._reconnect_before_task_start(self, selected):
            return
        # 把所有「待转录」一次性排进队列（仍按顺序逐条转）。
        for t in selected:
            t._core_recovered_queue = False
            if (t.execution_policy is not None and t.execution_policy.requested_outputs == ()):
                continue
            if t.state == TaskState.PENDING and not bool(
                getattr(t, "_core_view_only", False)
            ):
                self._freeze_task_policy(t)
                t.state = TaskState.QUEUED
                t.message = "排队中…"
        self._refresh_badge()
        self._persist_task_queue()
        self._pump()

    def _freeze_task_policy(self, task: Task) -> None:
        """仅在用户启动一条新任务时读取当前偏好。"""
        if getattr(task, "execution_policy", None) is not None:
            from dataclasses import replace
            policy = task.execution_policy
            if policy.engine_name is None and (policy.requested_outputs is None or task.state is TaskState.RUNNING):
                task.execution_policy = replace(policy, engine_name=self.cfg.engine_name,
                    prefer_subtitles=(policy.prefer_subtitles if policy.prefer_subtitles is not None
                                      else bool(self.cfg.prefer_subtitles)))
            return
        snapshot_reader = getattr(self, "_material_snapshot", None)
        if callable(snapshot_reader):
            snapshot = snapshot_reader()
        else:
            from ..core.collect.preferences import MaterialPreferenceSnapshot

            snapshot = MaterialPreferenceSnapshot(
                getattr(self.cfg, "link_media_mode", "audio"),
                bool(getattr(self.cfg, "keep_media", False)),
                bool(getattr(self.cfg, "collect_cover", False)),
            )
        task.execution_policy = TaskExecutionPolicy(
            origin="single_link",
            mode="materialize",
            stable_key="",
            material_snapshot=snapshot,
            media_destination=(
                "user_directory"
                if getattr(task, "kind", "link") == "link"
                else "none"
            ),
            media_directory=str(
                Path(getattr(self.cfg, "media_dir", "")).expanduser()
            ),
            prefer_subtitles=bool(
                getattr(self.cfg, "prefer_subtitles", False)
            ),
            engine_name=self.cfg.engine_name,
        )

    # ---------------- 录音 ---------------- #

    def _pick_recording_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择录音保存的文件夹", self.cfg.recording_dir)
        if d:
            self.cfg.recording_dir = d
            self.cfg.save()
            self._publish_extraction_state()

    def _ensure_recorder(self):
        if getattr(self, "_recorder_obj", None) is None:
            from .recorder import MicRecorder
            self._recorder_obj = MicRecorder()
            self._recorder_obj.tick.connect(self._on_rec_tick)
            self._recorder_obj.finished.connect(self._on_rec_finished)
            self._recorder_obj.failed.connect(self._on_rec_failed)
        return self._recorder_obj

    def _ensure_system_audio_backend(self):
        if getattr(self, "_system_audio_backend", None) is None:
            from ..core.recording import MacOSSystemAudioBackend

            self._system_audio_backend = MacOSSystemAudioBackend()
        return self._system_audio_backend

    def _toggle_record(self) -> None:
        if self._mixed_rec_active or self._mixed_pending_stop:
            self._stop_mixed_recording()
            return
        if self._system_rec_session is not None and self._system_rec_session.is_active():
            self._stop_system_recording()
            return
        rec = self._ensure_recorder()
        if rec.is_recording():
            rec.stop()
            return
        if self._rec_source == "system_audio":
            self._start_system_recording()
            return
        if self._rec_source == "mixed":
            self._start_mixed_recording()
            return
        self._start_microphone_recording()

    def _recording_path(self, prefix: str) -> str | None:
        from datetime import datetime

        d = Path(self.cfg.recording_dir)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._toast("录音保存目录不可写，请重新选择")
            return None
        now = datetime.now()
        self._rec_started_label = now.strftime("%m-%d %H:%M")
        return str(d / f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}.m4a")

    def _start_microphone_recording(self) -> None:
        rec = self._ensure_recorder()
        if not rec.has_input():
            self._toast("没检测到麦克风设备")
            return
        path = self._recording_path("录音")
        if not path:
            return
        self._rec_active_source = "microphone"
        self._rec_cancel_requested = False
        self._rec_title = f"麦克风录音 {self._rec_started_label}"
        rec.start(path)
        self._set_recording_ui("microphone")

    def _start_system_recording(self) -> None:
        path = self._recording_path("系统声音录音")
        if not path:
            return
        try:
            from ..core.recording import RecordingRequest, RecordingSource, RecordingUse

            request = RecordingRequest(
                source=RecordingSource.SYSTEM_AUDIO,
                output_path=Path(path),
                use=RecordingUse.TRANSCRIPTION,
            )
            backend = self._ensure_system_audio_backend()
            self._system_rec_session = backend.start(request)
        except Exception as exc:
            self._system_rec_session = None
            self._toast(self._friendly_system_recording_error(exc))
            return
        self._rec_active_source = "system_audio"
        self._rec_cancel_requested = False
        self._rec_title = f"系统声音录音 {self._rec_started_label}"
        self._set_recording_ui("system_audio")

    def _start_mixed_recording(self) -> None:
        rec = self._ensure_recorder()
        if not rec.has_input():
            self._toast("没检测到麦克风设备")
            return
        output = self._recording_path("混合录音")
        if not output:
            return
        output_path = Path(output)
        stem = output_path.stem
        mic_path = output_path.with_name(f"{stem}_麦克风轨.m4a")
        system_path = output_path.with_name(f"{stem}_系统声音轨.m4a")
        try:
            from ..core.recording import RecordingRequest, RecordingSource, RecordingUse

            request = RecordingRequest(
                source=RecordingSource.SYSTEM_AUDIO,
                output_path=system_path,
                use=RecordingUse.TRANSCRIPTION,
            )
            backend = self._ensure_system_audio_backend()
            self._system_rec_session = backend.start(request)
        except Exception as exc:
            self._system_rec_session = None
            self._toast(self._friendly_system_recording_error(exc))
            return
        self._rec_active_source = "mixed"
        self._rec_cancel_requested = False
        self._rec_title = f"混合录音 {self._rec_started_label}"
        self._mixed_rec_active = True
        self._mixed_pending_stop = False
        self._mixed_system_result = None
        self._mixed_mic_path = mic_path
        self._mixed_system_path = system_path
        self._mixed_output_path = output_path
        rec.start(str(mic_path))
        self._set_recording_ui("mixed")

    def _set_recording_ui(self, source: str) -> None:
        self._rec_elapsed = 0
        if source in {"system_audio", "mixed"}:
            self._rec_elapsed_timer.start(1000)
        self._publish_extraction_state()

    def _friendly_system_recording_error(self, exc: Exception) -> str:
        text = str(exc) or "系统声音录制失败"
        if "权限" in text or "permission" in text.lower() or "not authorized" in text.lower():
            return "系统声音权限未打开。你可以先改为麦克风录音，或到系统设置允许 Lulu 录制电脑声音后重试。"
        if "超时" in text:
            return "系统声音录制启动超时。你可以先改为麦克风录音，或检查系统声音权限后重试。"
        if "清晰" in text or "静音" in text or "没有录到" in text:
            return "没有录到清晰的系统声音，请先播放电脑声音后重试。"
        return "系统声音录制失败。你可以先改为麦克风录音，或检查系统声音权限后重试。"

    def _stop_system_recording(self) -> None:
        session = self._system_rec_session
        if session is None:
            return
        try:
            result = session.stop()
        except Exception as exc:
            self._system_rec_session = None
            self._reset_rec_ui()
            self._toast(self._friendly_system_recording_error(exc))
            return
        self._system_rec_session = None
        self._on_rec_finished(str(result.output_path))

    def _stop_mixed_recording(self) -> None:
        if self._mixed_pending_stop:
            return
        session = self._system_rec_session
        if session is None:
            self._fail_mixed_recording("系统声音录制状态异常，请重新录制")
            return
        self._mixed_pending_stop = True
        self._publish_extraction_state()
        try:
            self._mixed_system_result = session.stop()
        except Exception as exc:
            self._system_rec_session = None
            self._fail_mixed_recording(self._friendly_system_recording_error(exc))
            return
        self._system_rec_session = None
        rec = self._ensure_recorder()
        if rec.is_recording():
            rec.stop()
        else:
            self._finish_mixed_recording(self._mixed_mic_path)

    def _fail_mixed_recording(self, message: str) -> None:
        rec = getattr(self, "_recorder_obj", None)
        if rec is not None and rec.is_recording():
            self._rec_cancel_requested = True
            rec.stop()
        self._cleanup_mixed_paths()
        self._clear_mixed_state()
        self._reset_rec_ui()
        self._toast(message)

    def _cancel_recording(self) -> None:
        self._rec_cancel_requested = True
        if self._mixed_rec_active or self._mixed_pending_stop:
            self._cancel_mixed_recording()
            return
        if self._system_rec_session is not None and self._system_rec_session.is_active():
            self._system_rec_session.cancel()
            self._system_rec_session = None
            self._reset_rec_ui()
            self._toast("已取消录音，不会加入转录队列")
            return
        rec = self._ensure_recorder()
        if rec.is_recording():
            rec.stop()
            return
        self._reset_rec_ui()

    def _cancel_mixed_recording(self) -> None:
        if self._system_rec_session is not None and self._system_rec_session.is_active():
            self._system_rec_session.cancel()
        self._system_rec_session = None
        rec = self._ensure_recorder()
        if rec.is_recording():
            rec.stop()
        self._cleanup_mixed_paths()
        self._clear_mixed_state()
        self._reset_rec_ui()
        self._toast("已取消录音，不会加入转录队列")

    def _on_rec_tick(self, sec: int) -> None:
        self._rec_elapsed = int(sec)
        self._publish_extraction_state()

    def _on_rec_elapsed_tick(self) -> None:
        if self._rec_active_source not in {"system_audio", "mixed"}:
            self._rec_elapsed_timer.stop()
            return
        self._rec_elapsed += 1
        self._on_rec_tick(self._rec_elapsed)

    def _reset_rec_ui(self) -> None:
        self._rec_elapsed_timer.stop()
        self._publish_extraction_state()

    def _on_rec_finished(self, path: str) -> None:
        if self._mixed_rec_active or self._mixed_pending_stop:
            self._finish_mixed_recording(Path(path) if path else None)
            return
        self._reset_rec_ui()
        p = Path(path) if path else None
        if self._rec_cancel_requested:
            self._rec_cancel_requested = False
            try:
                if p and p.exists():
                    p.unlink()
            except OSError:
                pass
            self._toast("已取消录音，不会加入转录队列")
            return
        if not p or not p.exists() or p.stat().st_size == 0:
            self._toast("这段录音是空的，没有保存。")
            return
        title = getattr(self, "_rec_title", None) or p.name
        self._add_task(str(p), "file", title)
        self._toast("录音已加入转录队列，点卡片「开始」转写")

    def _finish_mixed_recording(self, mic_path: Path | None) -> None:
        if self._rec_cancel_requested:
            self._rec_cancel_requested = False
            self._cleanup_mixed_paths(extra=mic_path)
            self._clear_mixed_state()
            self._reset_rec_ui()
            self._toast("已取消录音，不会加入转录队列")
            return
        system_result = self._mixed_system_result
        system_path = self._mixed_system_path
        output_path = self._mixed_output_path
        if not mic_path or not mic_path.exists() or mic_path.stat().st_size == 0:
            self._fail_mixed_recording("没有录到麦克风声音，请检查麦克风权限后重试")
            return
        if system_result is None or not system_path or not system_path.exists() or system_path.stat().st_size == 0:
            self._fail_mixed_recording("没有录到系统声音，请先播放电脑声音后重试")
            return
        try:
            mic_stats = self._analyze_recording_file(mic_path)
        except Exception:
            self._fail_mixed_recording("麦克风录音无法读取，请检查麦克风权限后重试")
            return
        if mic_stats.get("is_silent") is True:
            self._fail_mixed_recording("没有录到清晰的麦克风声音，请检查麦克风权限后重试")
            return
        try:
            self._mix_recording_tracks(mic_path, system_path, output_path)
        except Exception:
            self._fail_mixed_recording("混合录音保存失败，请重新录制")
            return
        self._cleanup_mixed_track_paths()
        self._clear_mixed_state()
        self._reset_rec_ui()
        if not output_path or not output_path.exists() or output_path.stat().st_size == 0:
            self._toast("混合录音保存失败，请重新录制")
            return
        title = getattr(self, "_rec_title", None) or output_path.name
        self._add_task(str(output_path), "file", title)
        self._toast("混合录音已加入转录队列，点卡片「开始」转写")

    def _analyze_recording_file(self, path: Path) -> dict[str, object]:
        from ..core.recording import analyze_recording_file

        return analyze_recording_file(path)

    def _mix_recording_tracks(self, mic_path: Path, system_path: Path, output_path: Path) -> dict[str, object]:
        from ..core.recording import mix_recording_tracks

        return mix_recording_tracks(mic_path, system_path, output_path)

    def _cleanup_mixed_track_paths(self) -> None:
        for p in (self._mixed_mic_path, self._mixed_system_path):
            try:
                if p and Path(p).exists():
                    Path(p).unlink()
            except OSError:
                pass

    def _cleanup_mixed_paths(self, extra: Path | None = None) -> None:
        for p in (self._mixed_mic_path, self._mixed_system_path, self._mixed_output_path, extra):
            try:
                if p and Path(p).exists():
                    Path(p).unlink()
            except OSError:
                pass

    def _clear_mixed_state(self) -> None:
        self._mixed_rec_active = False
        self._mixed_pending_stop = False
        self._mixed_system_result = None
        self._mixed_mic_path = None
        self._mixed_system_path = None
        self._mixed_output_path = None

    def _on_rec_failed(self, msg: str) -> None:
        if self._mixed_rec_active or self._mixed_pending_stop:
            self._fail_mixed_recording("麦克风录制失败，请检查麦克风权限后重试")
            return
        self._reset_rec_ui()
        self._toast(f"录音失败：{msg}")

    def _pump(self) -> None:
        if getattr(self, "_close_settling", False):
            return
        if getattr(self, "_result_recovery_failed", False):
            return
        if self.current is not None:
            return
        store = getattr(self, "_task_queue_store", None)
        asynchronous = bool(getattr(store, "async_write", False))
        # A read must settle before creating a start intent. Otherwise its
        # callback mistakes our deferred start for an interrupted prior run.
        if asynchronous and getattr(self, "_task_queue_startup_pending", False):
            return
        if store is not None and (getattr(self, "_task_queue_store_failed", False)
                                  or getattr(self, "_core_save_worker", None) is not None):
            return
        blocked_group_ids = (
            getattr(self, "_paused_task_group_ids", set())
            | getattr(self, "_deleting_task_group_ids", set())
        )
        nxt = next(
            (
                task
                for task in self.tasks
                if (task.state == TaskState.QUEUED or
                    (asynchronous and task.id == getattr(self, "_core_starting_task_id", None)
                     and task.state == TaskState.RUNNING))
                and not bool(getattr(task, "_core_view_only", False))
                and not bool(getattr(task, "_core_recovered_queue", False))
                and MainWindow._task_group_id_for_task(self, task.id)
                not in blocked_group_ids
            ),
            None,
        )
        if nxt is None:
            return
        if asynchronous and nxt.state is TaskState.QUEUED and getattr(nxt, "_core_state", None) != "queued":
            self._persist_task_queue()
            return
        nxt.state = TaskState.RUNNING
        MainWindow._freeze_task_policy(self, nxt)
        nxt.message = "启动中…"
        nxt.progress = -1.0
        if asynchronous:
            self._core_starting_task_id = nxt.id
            if getattr(nxt, "_core_state", None) != "running":
                self._persist_task_queue()
                return
            self._core_starting_task_id = None
        elif store is not None and not MainWindow._persist_task_queue(self):
            nxt.state = TaskState.QUEUED
            nxt.message = "任务尚未保存，恢复后可继续"
            return
        from .worker import TranscribeWorker

        policy = getattr(nxt, "execution_policy", None)
        frozen_media_dir = (
            getattr(policy, "media_directory", None)
            if policy is not None
            else None
        )
        frozen_prefer_subtitles = (
            getattr(policy, "prefer_subtitles", None)
            if policy is not None
            else None
        )
        worker = TranscribeWorker(
            nxt.source, (policy.engine_name if policy and policy.engine_name else self.cfg.engine_name), self.cfg.enable_link_download,
            keep_media=self.cfg.keep_media,
            media_dir=(
                frozen_media_dir
                if frozen_media_dir is not None
                else self.cfg.media_dir
            ),
            link_media_mode=self.cfg.link_media_mode,
            prefer_subtitles=(
                frozen_prefer_subtitles
                if frozen_prefer_subtitles is not None
                else self.cfg.prefer_subtitles
            ),
            cover_preference_service=self._cover_preferences,
            cover_workflow=self._cover_localizer,
            execution_policy=nxt.execution_policy,
            local_file_transcription_authorization_guard=(
                getattr(
                    self,
                    "_local_file_transcription_authorization_guard",
                    None,
                )
            ),
        )
        worker.audit_job_id = nxt.id

        def owns_attempt():
            return self.current is not None and self.current[0] is nxt and self.current[1] is worker

        def update_stage(stage):
            if owns_attempt():
                nxt._execution_stage = stage

        if hasattr(worker, "stage_changed"):
            worker.stage_changed.connect(update_stage)
        worker.progress.connect(lambda m, f, tid=nxt.id: self._on_progress(tid, m, f) if owns_attempt() else None)
        worker.finished_ok.connect(lambda tr, tid=nxt.id: self._on_done(tid, tr) if owns_attempt() else None)
        worker.failed.connect(lambda msg, tid=nxt.id: self._on_failed(tid, msg) if owns_attempt() else None)
        worker.cancelled.connect(lambda tid=nxt.id: self._on_cancelled(tid) if owns_attempt() else None)
        worker.finished.connect(
            lambda tid=nxt.id, owned_worker=worker:
            self._on_transcribe_worker_finished(tid, owned_worker)
        )
        self.current = (nxt, worker)
        worker.start()
        MainWindow._sync_runtime_preferences(self)
        self._refresh_badge()

    def _task(self, tid: int | str) -> Task | None:
        return next((t for t in self.tasks if t.id == tid), None)

    def _on_progress(self, tid: int | str, msg: str, frac: float) -> None:
        t = self._task(tid)
        if not t:
            return
        if tid in getattr(self, "_cancelling_task_ids", set()):
            return
        # 链接下载后，把标题换成更有意义的（作品文案）
        title_changed = False
        if t.kind == "link" and "作品：" in msg:
            next_title = msg.split("作品：", 1)[1].rstrip("…").strip() or t.title
            title_changed = next_title != t.title
            t.title = next_title
        t.message = msg
        t.progress = frac
        # 组摘要不展示单条进度或普通状态文案；只有作品标题变化才需重绘。
        if title_changed:
            self._publish_extraction_state()
            self._persist_task_queue()

    def _on_done(self, tid: int, transcript) -> None:
        if tid in getattr(self, "_cancelling_task_ids", set()):
            return
        task = self._task(tid)
        if task is not None and task.execution_policy is not None and task.execution_policy.requested_outputs is not None:
            self._commit_extraction_result(task, transcript)
            return
        if not (transcript.text or "").strip():
            self._on_failed(tid, "未识别到有效语音，请检查音频是否有声音，或换一个本地模型重试。")
            return
        t = self._task(tid)
        if t and t.library_id:
            return
        if t:
            t.transcript = transcript
            t.state = TaskState.FAILED
            t.message = "结果待保存"
            t.error = "转录结果尚未保存到素材库"
            # 链接来源：用解析到的真实标题替换占位的「链接转写」
            if transcript.title and t.kind == "link":
                t.title = transcript.title.strip()[:80]
            self._commit_transcription_result(t)
            self._persist_task_queue()
        if t is not None and t.library_id:
            MainWindow._notify_task_completed(
                self,
                page_index=0,
                library_kind="transcript",
                entry_id=t.library_id,
            )

    def _commit_extraction_result(self, task, transcript):
        from ..core.extraction import save_result, outputs
        task.transcript = transcript
        if transcript.title and task.kind == "link":
            task.title = transcript.title.strip()[:80]
        meta = transcript.meta = dict(transcript.meta or {})
        failures = list(meta.get("extraction_errors", []))
        if "text" in outputs(task.execution_policy):
            if (transcript.text or "").strip():
                if not self._commit_transcription_result(task, persist=False):
                    return
            else:
                failures.append("未识别到有效语音，请检查音频或更换模型")
        meta["extraction_errors"] = failures
        meta["extraction_library_id"] = task.library_id
        has_result = bool(task.library_id or meta.get("extraction_assets"))
        if not has_result:
            # An empty failed attempt has no result to recover. Keeping an empty
            # Transcript here would make exit wait forever for a nonexistent save.
            task.transcript = None
            task._extraction_result_ref = None
            task.state = TaskState.FAILED
            task.message = "未完成"
            task.error = "；".join(failures) or "没有获取到所选内容，请重试"
            self._persist_task_queue()
            return
        try:
            task._extraction_result_ref = save_result(str(task.id), transcript)
        except (OSError, ValueError, RuntimeError):
            task.state = TaskState.FAILED
            task.message = "结果待保存"
            task.error = "结果记录未保存，已生成的文件仍保留"
            self._persist_task_queue()
            return
        task.state = TaskState.DONE
        task.message = "部分完成" if failures else "已完成"
        task.error = "；".join(failures) or None
        self._persist_task_queue()
        if task.library_id:
            MainWindow._notify_task_completed(self, page_index=0, library_kind="transcript", entry_id=task.library_id)

    def _commit_transcription_result(self, task: Task, *, persist=True) -> bool:
        """保存并复读成功才把任务迁移到 DONE。"""
        from ..core import library, obsidian

        trace = getattr(self.current[1], "_attempt_trace", None) if self.current and self.current[0] is task else None
        if trace:
            trace.enter("saving")
        try:
            self._merge_collection_transcript_facts(task)
            entry_id = str(task.library_id or getattr(task, "_pending_library_id", "") or "")
            durable = isinstance(task.id, str) and task.id.startswith("gui-job-")
            if durable:
                from uuid import uuid4
                from ..core.gui_task_recovery import GuiTaskRecoveryStore
                if not entry_id:
                    entry_id = task._pending_library_id = f"transcript-{uuid4().hex}"
                if self._task_queue_recovery is None:
                    self._task_queue_recovery = GuiTaskRecoveryStore.production()
                self._task_queue_recovery.save(self.tasks, self.task_groups,
                    preserve_existing=not self._task_queue_recovery_checked,
                    hidden_ids=getattr(self, "_core_hidden_intents", set()))
                entry_id = library.save(task.transcript, task.title,
                    folder_id=task.folder_id, entry_id=entry_id)
            if not entry_id:
                entry_id = str(
                    library.save(
                        task.transcript,
                        task.title,
                        folder_id=task.folder_id,
                    )
                    or ""
                )
                if not entry_id:
                    raise OSError("素材库没有返回有效 id")
                task._pending_library_id = entry_id
            current_body = library.load_markdown(entry_id)
            # 新记录最初只含原始正文。先一次性落成素材库与 Obsidian
            # 共用的标准成稿，再允许任务进入 DONE；重试时不覆盖用户编辑。
            if current_body == task.transcript.text:
                canonical_body = obsidian.note_body(task.transcript, task.title)
                library.save_markdown(entry_id, canonical_body, preserve_timestamps=True,
                                      expected_hash=library._content_hash(current_body))
                if library.load_markdown(entry_id) != canonical_body:
                    raise OSError("素材库成稿复读失败")
            if library.load(entry_id) is None:
                raise OSError("素材库复读失败")
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.returned("save_failed", exc)
            task.library_id = None
            task.state = TaskState.FAILED
            task.message = "结果待保存"
            task.error = f"转录已完成，素材库保存尚未提交：{exc}"
            self._persist_task_queue()
            return False

        task.library_id = entry_id
        if trace:
            trace.returned("saved")
        task._pending_library_id = ""
        task.state = TaskState.DONE
        task.error = None
        task.message = (
            "已完成 · 已存音视频"
            if task.transcript and task.transcript.saved_media
            else "已完成"
        )
        self._persist_collection_transcript_status(task, "completed")
        if persist: self._persist_task_queue()
        return True

    def _merge_collection_transcript_facts(self, task: Task) -> None:
        """批量转录保存前补回采集主档事实，避免作者和互动数据在队列中丢失。"""

        policy = getattr(task, "execution_policy", None)
        transcript = getattr(task, "transcript", None)
        stable_key = str(getattr(task, "stable_key", "") or "")
        if (
            transcript is None
            or not stable_key
            or policy is None
            or policy.origin != "homepage_batch_transcribe"
        ):
            return
        page = getattr(self, "collection_page", None)
        item = next(
            (
                candidate
                for candidate in (getattr(page, "_items", ()) or ())
                if str(getattr(candidate, "stable_key", "") or "") == stable_key
            ),
            None,
        )
        if item is None:
            try:
                from ..core.collect.repository import CollectionRepository

                repository = getattr(page, "_records_repo", None) or CollectionRepository()
                item = next(
                    (
                        candidate
                        for candidate in repository.load().items
                        if candidate.stable_key == stable_key
                    ),
                    None,
                )
            except Exception:
                item = None
        if item is None:
            return
        from ..core import feishu_storage

        collected_meta = feishu_storage.transcript_meta_from_collected_item(item)
        merged = dict(getattr(transcript, "meta", {}) or {})
        for nested_key in ("author", "stats"):
            combined = dict(merged.get(nested_key) or {})
            combined.update(dict(collected_meta.get(nested_key) or {}))
            if combined:
                merged[nested_key] = combined
        for key, value in collected_meta.items():
            if key not in {"author", "stats"}:
                merged[key] = value
        transcript.meta = merged
        original_url = str(collected_meta.get("original_url") or "").strip()
        if original_url.startswith(("http://", "https://")):
            # 保存时就固定原作品地址，让素材库与飞书共用同一事实。
            transcript.source = original_url

    def _persist_collection_transcript_status(
        self, task: Task, status: str
    ) -> None:
        policy = task.execution_policy
        if (
            not task.stable_key
            or policy is None
            or policy.origin != "homepage_batch_transcribe"
            or status not in {"queued", "running", "completed", "failed"}
        ):
            return
        try:
            from ..core.collect.repository import CollectionRepository
            repository = getattr(
                getattr(self, "collection_page", None), "_records_repo", None
            ) or CollectionRepository()
            current = next(
                (
                    item
                    for item in repository.load().items
                    if item.stable_key == task.stable_key
                ),
                None,
            )
            if current is None or current.transcript_status == status:
                return
            repository.compare_and_swap_fields(
                task.stable_key,
                expected={"transcript_status": current.transcript_status},
                updates={"transcript_status": status},
            )
        except Exception as exc:
            from ..core.diagnostics import record_failure
            record_failure("collection_transcript_status", exc)
            # 文稿真实物仍由 library 决定；主档竞态不反向破坏任务。
            return

    def _retry_result_save(self, tid: int) -> bool:
        task = self._task(tid)
        if task is None or task.transcript is None or task.library_id:
            return False
        if not self._commit_transcription_result(task):
            self._toast("素材库仍未保存，转录结果继续保留在当前任务中")
            self._refresh_badge()
            return False
        MainWindow._notify_task_completed(
            self,
            page_index=0,
            library_kind="transcript",
            entry_id=task.library_id,
        )
        self._refresh_badge()
        self._toast("转录结果已保存到素材库")
        return True

    def _on_failed(self, tid: int, msg: str) -> None:
        if tid in getattr(self, "_cancelling_task_ids", set()):
            return
        t = self._task(tid)
        if t:
            t.state = TaskState.FAILED
            t.error = msg
            t.message = "转写失败"
            self._persist_collection_transcript_status(t, "failed")
            self._persist_task_queue()
        MainWindow._notify_task_failed(self)

    def _on_cancelled(self, tid: int) -> None:
        cancelling = getattr(self, "_cancelling_task_ids", None)
        if cancelling is None:
            cancelling = set()
            self._cancelling_task_ids = cancelling
        cancelling.add(tid)
        t = self._task(tid)
        if t and t.state == TaskState.RUNNING:
            t.message = "正在取消…"
            t.progress = -1.0

    def _on_transcribe_worker_finished(self, tid: int, worker) -> None:
        """Let every QThread ``finished`` observer run before queue cleanup."""
        trace = getattr(worker, "_attempt_trace", None)
        if trace:
            trace.returned("thread_finished")
        if isinstance(worker, QThread):
            if getattr(worker, "_lulu_finish_settlement_scheduled", False):
                return
            worker._lulu_finish_settlement_scheduled = True

            def settle_finished_event() -> None:
                self._settle_transcribe_worker_finished(tid, worker)

            QTimer.singleShot(0, settle_finished_event)
            return
        self._settle_transcribe_worker_finished(tid, worker)

    def _settle_transcribe_worker_finished(self, tid: int, worker) -> None:
        """Advance the serial queue only after the finished event is delivered."""
        current = self.current
        if current is None or current[1] is not worker:
            worker.deleteLater()
            return

        task = self._task(tid)
        if task is not None and task.state == TaskState.RUNNING:
            cancelling = tid in getattr(self, "_cancelling_task_ids", set())
            task.state = (
                TaskState.CANCELLED if cancelling else TaskState.FAILED
            )
            task.error = None if cancelling else "转写线程意外结束"
            task.message = "已取消" if cancelling else "转写失败"
            if not cancelling:
                MainWindow._notify_task_failed(self)

        self._persist_task_queue()

        getattr(self, "_cancelling_task_ids", set()).discard(tid)
        self.current = None
        worker.deleteLater()
        self._refresh_badge()
        MainWindow._sync_runtime_preferences(self)
        group_id = MainWindow._task_group_id_for_task(self, tid)
        if group_id in getattr(self, "_deleting_task_group_ids", set()):
            MainWindow._finalize_task_group_delete(self, group_id)
        self._pump()

    def _cancel_task(self, tid: int) -> None:
        group_id = MainWindow._task_group_id_for_task(self, tid)
        if not MainWindow._request_task_cancel(self, tid):
            return
        if group_id is not None:
            paused = getattr(self, "_paused_task_group_ids", None)
            if paused is None:
                paused = set()
                self._paused_task_group_ids = paused
            paused.add(group_id)
        self._publish_extraction_state()
        self._toast(
            "已暂停该批次，正在安全取消当前任务"
            if group_id is not None
            else "正在安全取消当前任务"
        )

    def _request_task_cancel(self, tid: int | str) -> bool:
        if not self.current or self.current[0].id != tid:
            task = self._task(tid)
            if (task is not None and not getattr(task, "_core_view_only", False)
                    and (task.state in (TaskState.PENDING, TaskState.QUEUED)
                         or tid == getattr(self, "_core_starting_task_id", None))):
                task.state = TaskState.CANCELLED
                task.message = "已取消"
                if tid == getattr(self, "_core_starting_task_id", None):
                    self._core_starting_task_id = None
                self._persist_task_queue()
                self._refresh_badge()
                return True
            return False
        cancelling = getattr(self, "_cancelling_task_ids", None)
        if cancelling is None:
            cancelling = set()
            self._cancelling_task_ids = cancelling
        if tid in cancelling:
            return False
        cancelling.add(tid)
        task = self.current[0]
        task.message = "正在取消…"
        task.progress = -1.0
        self.current[1].request_cancel()
        self._refresh_badge()
        return True

    def _task_group_id_for_task(self, tid: int | str) -> int | None:
        for group in getattr(self, "task_groups", ()):
            if any(member.task_id == tid for member in group.members):
                return group.id
        return None

    def _task_group(self, group_id: int) -> TaskGroup | None:
        return next(
            (group for group in self.task_groups if group.id == group_id),
            None,
        )


    def _publish_extraction_state(self) -> None:
        workspace = getattr(self, "extraction_workspace", None)
        if workspace is not None:
            workspace.publish()

    def _collection_task_states(self):
        """采集页只读运行态出口；不缓存、不写第二套任务状态。"""
        return derive_collection_item_runtime(self.tasks)

    def _queue_pending_task_group_members(self, group: TaskGroup) -> int:
        """只把本组尚未开始的成员送入既有串行队列。"""
        if MainWindow._task_start_blocked(self):
            return 0
        queued = 0
        for member in group.members:
            if member.is_skipped or member.task_id is None:
                continue
            task = self._task(member.task_id)
            if task is None or task.state != TaskState.PENDING:
                continue
            # Batch policies bind their model before admission to the queue.
            # The ledger deliberately forbids changing a frozen queued policy.
            MainWindow._freeze_task_policy(self, task)
            task.state = TaskState.QUEUED
            task.message = "排队中…"
            queued += 1
        if queued:
            self._persist_task_queue()
        return queued

    def _start_task_group(self, group_id: int) -> None:
        """“开始整组”仅排入本组 PENDING 成员，执行仍交给全局队列泵。"""
        if MainWindow._task_start_blocked(self):
            return
        group = self._task_group(group_id)
        if (
            group is None
            or group_id in getattr(self, "_paused_task_group_ids", set())
            or group_id in getattr(self, "_deleting_task_group_ids", set())
        ):
            return
        selected = [self._task(member.task_id) for member in group.members if not member.is_skipped]
        if MainWindow._reconnect_before_task_start(self, [t for t in selected if t is not None]):
            return
        queued = MainWindow._queue_pending_task_group_members(self, group)
        resumed = 0
        for member in group.members:
            task = self._task(member.task_id)
            if task is not None and getattr(task, "_core_recovered_queue", False):
                task._core_recovered_queue = False
                resumed += 1
        queued += resumed
        if not queued:
            return
        self._refresh_badge()
        self._toast(f"已提交本批次 {queued} 条任务，保存后开始转录")
        self._pump()

    def _cancel_task_group(self, group_id: int) -> None:
        """终止尚未启动成员；运行中成员只走既有单任务取消入口。"""
        group = self._task_group(group_id)
        if group is None:
            return
        getattr(self, "_paused_task_group_ids", set()).discard(group_id)
        for member in group.members:
            if member.is_skipped or member.task_id is None:
                continue
            task = self._task(member.task_id)
            if task is None:
                continue
            if task.state in (TaskState.PENDING, TaskState.QUEUED):
                task.state = TaskState.CANCELLED
                task.message = "已取消"
            elif task.state == TaskState.RUNNING:
                MainWindow._request_task_cancel(self, task.id)
        self._refresh_badge()
        self._persist_task_queue()
        self._pump()

    def _toggle_task_group_pause(self, group_id: int) -> None:
        group = self._task_group(group_id)
        if group is None:
            return
        paused = getattr(self, "_paused_task_group_ids", None)
        if paused is None:
            paused = set()
            self._paused_task_group_ids = paused
        if group_id in paused:
            if MainWindow._task_start_blocked(self):
                return
            paused.remove(group_id)
            MainWindow._queue_pending_task_group_members(self, group)
            self._publish_extraction_state()
            self._toast("已从下一条尚未处理的任务继续")
            self._pump()
            return

        current_task = self.current[0] if self.current else self._task(getattr(self, "_core_starting_task_id", None))
        if (
            current_task is None
            or MainWindow._task_group_id_for_task(self, current_task.id)
            != group_id
        ):
            return
        paused.add(group_id)
        MainWindow._request_task_cancel(self, current_task.id)
        self._publish_extraction_state()
        self._toast("已暂停该批次，后续任务不会继续启动")

    def _retry_task_group(self, group_id: int) -> None:
        """安全复用失败/取消成员，保持一成员一张当前卡。"""
        if MainWindow._task_start_blocked(self):
            return
        group = self._task_group(group_id)
        if (
            group is None
            or group_id in getattr(self, "_deleting_task_group_ids", set())
        ):
            return
        retried = 0
        for member in group.members:
            if member.is_skipped or not member.source:
                continue
            task = self._task(member.task_id) if member.task_id is not None else None
            if task is None or task.state not in (TaskState.FAILED, TaskState.CANCELLED):
                continue
            if getattr(task, "_core_view_only", False):
                task = self._prepare_history_retry(task)
            if (
                task.state == TaskState.FAILED
                and task.transcript is not None
                and not task.library_id
            ):
                self._retry_result_save(task.id)
                continue
            task.state = TaskState.PENDING
            task.progress = 0.0
            task.message = "待转录"
            task.error = None
            task.transcript = None
            task.library_id = None
            task._pending_library_id = ""
            retried += 1
        self._refresh_badge()
        self._persist_task_queue()
        if retried:
            self._toast(f"已将 {retried} 条任务恢复为待转录")

    def _prepare_history_retry(self, previous: Task) -> Task:
        """显式重试升级历史时建立新任务，保留旧 Core 收据。"""
        task = self._add_task(previous.source, previous.kind, previous.title,
            folder_id=previous.folder_id, stable_key=previous.stable_key,
            execution_policy=previous.execution_policy,
            defer_queue_refresh=True)
        self.tasks.remove(previous)
        if not hasattr(self, "_core_hidden_intents"):
            self._core_hidden_intents = set()
        self._core_hidden_intents.add(previous.id)
        for group in self.task_groups:
            for member in group.members:
                if member.task_id == previous.id:
                    member.attempt_task_ids.append(previous.id)
                    member.task_id = task.id
        return task

    def _delete_task_group(self, group_id: int) -> None:
        """运行项先安全取消；只有 worker finished 后才销毁整组 UI。"""
        group = self._task_group(group_id)
        if (
            group is None
            or group_id in getattr(self, "_deleting_task_group_ids", set())
        ):
            return
        member_ids = {
            task_id
            for member in group.members
            for task_id in member.attempt_task_ids + (
                [member.task_id] if member.task_id is not None else []
            )
        }
        running = [
            task
            for task in self.tasks
            if task.id in member_ids and task.state == TaskState.RUNNING
        ]
        current_task = self.current[0] if self.current else None
        active_current = (
            current_task
            if current_task is not None and current_task.id in member_ids
            else None
        )
        if running and (
            len(running) != 1
            or active_current is None
            or running[0].id != active_current.id
        ):
            self._toast("任务仍在运行，暂时不能删除，请稍后重试")
            return

        deleting = getattr(self, "_deleting_task_group_ids", None)
        if deleting is None:
            deleting = set()
            self._deleting_task_group_ids = deleting
        deleting.add(group_id)
        paused = getattr(self, "_paused_task_group_ids", None)
        if paused is None:
            paused = set()
            self._paused_task_group_ids = paused
        paused.add(group_id)

        for task in self.tasks:
            if (
                task.id in member_ids
                and task.state in (TaskState.PENDING, TaskState.QUEUED)
            ):
                task.state = TaskState.CANCELLED
                task.message = "已取消"

        if active_current is not None:
            if active_current.state == TaskState.RUNNING:
                MainWindow._request_task_cancel(self, active_current.id)
            self._publish_extraction_state()
            self._toast("正在安全停止当前任务，线程结束后会删除整个任务组")
            return

        if MainWindow._finalize_task_group_delete(self, group_id):
            self._toast("任务组已删除")

    def _finalize_task_group_delete(self, group_id: int) -> bool:
        """仅在组内没有运行线程时移除聚合卡、成员卡和运行期任务。"""
        group = self._task_group(group_id)
        if group is None:
            getattr(self, "_deleting_task_group_ids", set()).discard(group_id)
            return False
        member_ids = {
            task_id
            for member in group.members
            for task_id in member.attempt_task_ids + (
                [member.task_id] if member.task_id is not None else []
            )
        }
        if (
            self.current is not None
            and self.current[0].id in member_ids
        ) or any(
            task.id in member_ids and task.state == TaskState.RUNNING
            for task in self.tasks
        ):
            return False

        for task_id in member_ids:
            getattr(self, "_cancelling_task_ids", set()).discard(task_id)
        if getattr(self._task_queue_store, "async_write", False):
            if not hasattr(self, "_core_hidden_intents"):
                self._core_hidden_intents = set()
            self._core_hidden_intents.update(member_ids)
        self.tasks = [task for task in self.tasks if task.id not in member_ids]
        self.task_groups = [
            item for item in self.task_groups if item.id != group_id
        ]
        getattr(self, "_paused_task_group_ids", set()).discard(group_id)
        getattr(self, "_deleting_task_group_ids", set()).discard(group_id)
        self._refresh_badge()
        self._persist_task_queue()
        return True

    # ---------------- 任务管理（更多菜单）---------------- #


    def _delete_task(self, tid: int) -> None:
        t = self._task(tid)
        if not t:
            return
        if t.state == TaskState.RUNNING:
            self._toast("请先取消正在进行的任务，再删除")
            return
        if getattr(self._task_queue_store, "async_write", False):
            if not hasattr(self, "_core_hidden_intents"):
                self._core_hidden_intents = set()
            self._core_hidden_intents.add(tid)
        self.tasks = [x for x in self.tasks if x.id != tid]
        self._refresh_badge()
        self._persist_task_queue()

    def _rerun_task(self, tid: int) -> None:
        t = self._task(tid)
        if t:
            if t.state == TaskState.FAILED and t.transcript is not None and not t.library_id:
                self._retry_result_save(tid)
                return
            self._add_task(
                t.source,
                t.kind,
                t.title,
                folder_id=t.folder_id,
                stable_key=t.stable_key,
                execution_policy=t.execution_policy,
            )

    def _reveal(self, path: str) -> None:
        try:
            subprocess.run(["open", "-R", path], check=False)
        except Exception:  # noqa: BLE001
            self._toast("无法在访达中定位该文件")

    def _refresh_badge(self) -> None:
        active = sum(1 for task in self.tasks if task.is_active)
        if self._queue_count_label is not None:
            self._queue_count_label.setText(str(active))
            self._queue_count_chip.setVisible(active > 0)
        self._publish_extraction_state()

    # ---------------- 过滤 ---------------- #


    def _open_task_link(self, tid: int) -> None:
        """打开原链接：用默认浏览器跳转该任务的来源网址。"""
        t = self._task(tid)
        if not t or t.kind != "link" or not (t.source or "").strip():
            return
        # 链接任务允许用户直接粘贴整段平台分享文案。转录链会先从文案
        # 中提取 URL，这里必须使用同一规则，不能把整段文案交给系统浏览器。
        from ..core.audio.extractor import extract_url
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        url = extract_url(t.source)
        if not url:
            self._toast("没有找到可打开的原链接")
            return
        try:
            opened = bool(QDesktopServices.openUrl(QUrl.fromUserInput(url)))
        except Exception:  # noqa: BLE001 - 系统 URL 服务失败须转为用户可见反馈
            opened = False
        if not opened:
            self._toast("原链接暂时无法打开，请稍后重试")


    def _voice_task(self, tid: int) -> None:
        t = self._task(tid)
        if t and t.transcript:
            self.open_voice_studio(t.transcript.text, self._voice_source_for_task(t))

    def _voice_source_for_task(self, t: Task) -> dict:
        return {
            "source_type": "transcript",
            "source_id": t.library_id,
            "source_title": t.title,
            "source_text_snapshot": t.transcript.text if t.transcript else "",
            "source_chain": [],
        }

    # ---------------- Obsidian ---------------- #
    def _ensure_obsidian_dir(self) -> str | None:
        """返回已配置的 Obsidian 目标文件夹；未设则弹出选择，选好记住。取消返回 None。"""
        if self.cfg.obsidian_dir and Path(self.cfg.obsidian_dir).is_dir():
            return self.cfg.obsidian_dir
        d = QFileDialog.getExistingDirectory(
            self, "选择 Obsidian 仓库里的目标文件夹", str(Path.home())
        )
        if not d:
            return None
        self.cfg.obsidian_dir = d
        self.cfg.save()
        return d

    def _config_obsidian(self) -> None:
        """侧栏「Obsidian 知识库」：设置/更改目标文件夹。"""
        cur = self.cfg.obsidian_dir
        start = cur if cur and Path(cur).is_dir() else str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "选择 Obsidian 仓库里的目标文件夹", start)
        if d:
            self.cfg.obsidian_dir = d
            self.cfg.save()
            self._toast(f"Obsidian 目标文件夹已设为：{Path(d).name}")

    def _obsidian_task(self, tid: int) -> None:
        t = self._task(tid)
        if not t or not t.transcript:
            return
        folder = self._ensure_obsidian_dir()
        if not folder:
            return
        summary = None
        if t.library_id:
            try:
                from ..core import library
                summary = library.get_summary(t.library_id)
            except Exception:  # noqa: BLE001
                summary = None
        try:
            from ..core import obsidian
            p = obsidian.save_note(t.transcript, folder, t.title, summary=summary)
            self._toast(f"已存入 Obsidian:{p.name}")
        except Exception as exc:  # noqa: BLE001
            dialogs.alert(self, "存入 Obsidian 失败", str(exc))

    # ---------------- 飞书作品库 ---------------- #
    def _config_feishu(self) -> None:
        self.open_feishu_connection()

    def open_feishu_connection(self, save_dialog=None) -> bool:
        """Keep a save draft alive while the user visits connection settings."""
        self._switch_page(6)
        if self.settings_page is not None:
            self.settings_page.set_category("feishu")
            if getattr(self, "feishu_nav", None) is not None:
                self.feishu_nav.setFocus(Qt.FocusReason.OtherFocusReason)
            if save_dialog is not None:
                current = self.settings_page.feishu_connection._return_dialog
                if current is not None and current is not save_dialog and current._connection_suspended:
                    return False
                save_dialog._connection_suspended = True
                save_dialog.hide()
                self.settings_page.feishu_connection.offer_return(save_dialog)
            return True
        return False

    def _send_to_feishu(
        self,
        transcript,
        title: str,
        *,
        summary: str | None = None,
        text_override: str | None = None,
    ) -> None:
        """任务卡共用作品库入口；完整作品上下文交给统一存储层。"""
        from .feishu_dialog import send_to_feishu
        self._feishu_worker = send_to_feishu(
            self,
            self.cfg,
            transcript,
            title,
            summary=summary,
            text_override=text_override,
            toast=self._toast,
        )

    def _feishu_task(self, tid: int) -> None:
        t = self._task(tid)
        if not t or not t.transcript:
            return
        summary = None
        if t.library_id:
            try:
                from ..core import library
                summary = library.get_summary(t.library_id)
            except Exception:  # noqa: BLE001
                summary = None
        from dataclasses import replace
        transcript = replace(t.transcript, meta={**(t.transcript.meta or {}), "lulu_entry_id": t.library_id}) if t.library_id else t.transcript
        self._send_to_feishu(transcript, t.title, summary=summary)

    # ---------------- 文稿动作 ---------------- #
    def _ensure_task_transcript(self, task, action) -> bool:
        if task.transcript is not None:
            return True
        if task.library_id:
            from ..core import library
            task.transcript = library.load(task.library_id)
            if task.transcript is not None:
                return True
        result_ref = getattr(task, "_core_result_ref", None)
        if not isinstance(result_ref, str) or not result_ref.startswith("lulu-result:"):
            self._toast("这条任务的文稿暂时不可用")
            return False
        worker = self._core_result_worker
        if worker is not None and worker.isRunning():
            self._toast("正在读取文稿，请稍候")
            return False
        if worker is not None:
            worker.deleteLater()
        from .worker import CoreTaskResultWorker
        worker = CoreTaskResultWorker(self._task_queue_store, task.id, result_ref, self)
        self._core_result_worker = worker
        def ready(transcript):
            if self._close_settling or self._task(task.id) is not task or getattr(task, "_core_result_ref", None) != result_ref:
                return
            task.transcript = transcript
            action(task.id)
        worker.ready.connect(ready)
        worker.failed.connect(lambda: self._toast("文稿读取失败，请刷新任务后重试") if not self._close_settling else None)
        self._toast("正在读取文稿…")
        worker.start()
        return False

    def _view_task(self, tid: int) -> None:
        t = self._task(tid)
        if not t or not self._ensure_task_transcript(t, self._view_task):
            return
        t.library_id = self.open_transcript_in_library(t.transcript, t.title, t.library_id)

    def _copy_task(self, tid: int) -> None:
        t = self._task(tid)
        if t and self._ensure_task_transcript(t, self._copy_task):
            QGuiApplication.clipboard().setText(t.transcript.text)
            self._toast("已复制到剪贴板")

    def _export_task(self, tid: int) -> None:
        t = self._task(tid)
        if not t or not self._ensure_task_transcript(t, self._export_task):
            return
        default = str(Path(self.cfg.last_output_dir) / f"{t.title[:40]}.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出逐字稿", default,
            "文本 (*.txt);;Markdown (*.md);;字幕 (*.srt);;所有文件 (*)",
        )
        if not path:
            return
        try:
            from ..core.output.writer import write_transcript

            written = write_transcript(t.transcript, Path(path))
            self.cfg.last_output_dir = str(Path(written).parent)
            self.cfg.save()
            self._toast(f"已导出：{Path(written).name}")
        except Exception as exc:  # noqa: BLE001
            dialogs.alert(self, "导出失败", str(exc))

    # ---------------- toast ---------------- #
    def _toast(self, text: str) -> None:
        workspace = getattr(self, "extraction_workspace", None)
        if workspace is not None and workspace.ready and workspace.isVisible():
            workspace.bridge.notice.emit(text)
            return
        lab = QLabel(text, self)
        adopt_toast(lab)
        lab.setMaximumWidth(320)
        lab.setWordWrap(True)
        lab.adjustSize()
        lab.move((self.width() - lab.width()) // 2, self.height() - lab.height() - 28)
        lab.show()
        QTimer.singleShot(1600, lab.deleteLater)


    def has_active_work(self) -> bool:
        """Single install gate for every user-visible long-running operation."""
        inheritance = getattr(self, '_legacy_inheritance', None)
        if inheritance is not None and inheritance.active:
            return True
        if getattr(self, '_storage_relocation_in_progress', False):
            return True
        live = getattr(self, "live_controller", None)
        if live is not None and live.active:
            return True
        if any(task.is_active for task in self.tasks):
            return True
        recorder = getattr(self, "_recorder_obj", None)
        if recorder is not None:
            try:
                if recorder.is_recording(): return True
            except Exception:
                return True
        if self._mixed_rec_active or self._mixed_pending_stop:
            return True
        session = self._system_rec_session
        if session is not None:
            try:
                if session.is_active(): return True
            except Exception:
                return True
        owners = (self,) + tuple(getattr(self, name, None) for name in ("library_page", "voice_page", "collection_page", "settings_page"))
        connection = getattr(getattr(self, "settings_page", None), "feishu_connection", None)
        if connection is not None and any(MainWindow._update_activity_running(w) for w in connection.owned_workers()):
            return True
        for owner in owners:
            if owner is None: continue
            for name, value in vars(owner).items():
                if "worker" not in name.lower() or value is None: continue
                if owner is self and name in {"_task_queue_startup_worker", "_core_result_worker"}:
                    # 被动状态读取不属于用户任务；退出由 closeEvent 单独取消并等待。
                    continue
                if MainWindow._update_activity_running(value): return True
        models_page = getattr(self, "models_page", None)
        if models_page is not None and models_page.has_active_work():
            return True
        voice_page = getattr(self, "voice_page", None)
        qwen = getattr(voice_page, "_qwen3", None)
        if qwen is not None:
            if qwen.busy or any(MainWindow._update_activity_running(getattr(qwen, name, None)) for name in ("worker", "export_worker", "release_worker")):
                return True
        return False

    def _model_usage_reason(self, model_id: str) -> str:
        """Return an exact same-model operation conflict; unrelated models remain independent."""
        page = getattr(self, "models_page", None)
        if page is not None and model_id in page.active_model_ids():
            return "这个模型仍有任务正在处理，请等完成或取消后再操作。"
        current = getattr(self, "current", None)
        if current:
            worker = current[1]
            if (
                getattr(worker, "_engine_name", None) == model_id
                and MainWindow._update_activity_running(worker)
            ):
                return "这个模型正在转录。请先取消当前转录或等它完成，再删除。"

        voice_page = getattr(self, "voice_page", None)
        if voice_page is not None:
            worker_names = {
                "local_voxcpm2": ("_worker",),
                "local_indextts2": ("_indextts2_worker", "_indextts2_release_worker"),
                "local_indextts2_0": ("_indextts2_worker", "_indextts2_release_worker"),
            }
            for name in worker_names.get(model_id, ()):
                worker = getattr(voice_page, name, None)
                if name == "_indextts2_worker":
                    expected = "2.0" if model_id == "local_indextts2_0" else "2.5"
                    actual = getattr(worker, "_model_version", None)
                    if actual in ("2.0", "2.5") and actual != expected:
                        continue
                if MainWindow._update_activity_running(worker):
                    return "这个模型正在生成或释放后台。请先取消当前任务或等它完成，再卸载。"
            if model_id == "local_sensevoice":
                reference_workers = (
                    getattr(voice_page, "_ref_worker", None),
                    getattr(voice_page, "_new_voice_worker", None),
                )
                if any(
                    type(worker).__name__ == "ReferenceTranscribeWorker"
                    and MainWindow._update_activity_running(worker)
                    for worker in reference_workers
                    if worker is not None
                ):
                    return "SenseVoice 正在识别参考音频。请等识别完成，再删除。"

        if model_id == "local_qwen3_tts":
            try:
                qwen = getattr(voice_page, "_qwen3", None)
                if qwen is not None and (qwen.busy or any(MainWindow._update_activity_running(getattr(qwen, name, None)) for name in ("worker", "release_worker"))):
                    return "Qwen3 正在生成或释放后台，请等任务完成后再卸载。"
                from ..core.synthesis.qwen3_subprocess import persistent_worker_status
                if persistent_worker_status().get("alive"):
                    return "Qwen3 仍在内存中，请先到配音页释放内存，再卸载模型。"
            except Exception:
                return "暂时无法确认 Qwen3 后台状态，请先释放内存后再试。"
        if model_id in {"local_indextts2", "local_indextts2_0"}:
            try:
                from ..core.synthesis.indextts2_subprocess import persistent_worker_status

                background = persistent_worker_status()
                expected = "2.0" if model_id == "local_indextts2_0" else "2.5"
                actual = background.get("model_version")
                if background.get("alive") and (actual not in ("2.0", "2.5") or actual == expected):
                    return "IndexTTS2 后台仍在内存中。请先到文案配音页点“释放内存”，再卸载模型。"
            except Exception:  # noqa: BLE001 - status uncertainty must fail closed
                return "暂时无法确认 IndexTTS2 后台状态。请先到文案配音页释放内存后再试。"
        return ""

    @staticmethod
    def _update_activity_running(value) -> bool:
        """Inspect only explicit worker containers; ordinary page browsing stays idle."""
        if value is None: return False
        if isinstance(value, dict):
            return any(MainWindow._update_activity_running(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(MainWindow._update_activity_running(item) for item in value)
        try:
            return bool(value.isRunning())
        except Exception:
            return False

    @staticmethod
    def _collect_qthreads(value, workers: list[QThread]) -> None:
        if isinstance(value, QThread):
            if value not in workers:
                workers.append(value)
            return
        if isinstance(value, dict):
            for item in value.values():
                MainWindow._collect_qthreads(item, workers)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                MainWindow._collect_qthreads(item, workers)

    def _registered_qthreads(self) -> list[QThread]:
        """枚举 App 页面显式持有的 QThread，不依赖对象名猜测运行状态。"""
        owners = (self,) + tuple(
            getattr(self, name, None)
            for name in (
                "library_page",
                "voice_page",
                "collection_page",
                "settings_page",
            )
        )
        workers: list[QThread] = []
        live_worker = getattr(getattr(self, "live_controller", None), "worker", None)
        MainWindow._collect_qthreads(live_worker, workers)
        connection = getattr(getattr(self, "settings_page", None), "feishu_connection", None)
        if connection is not None:
            MainWindow._collect_qthreads(connection.owned_workers(), workers)
        models_page = getattr(self, "models_page", None)
        if models_page is not None:
            MainWindow._collect_qthreads(models_page.owned_workers(), workers)
        for owner in owners:
            if owner is None:
                continue
            for value in vars(owner).values():
                MainWindow._collect_qthreads(value, workers)
        qwen = getattr(getattr(self, "voice_page", None), "_qwen3", None)
        if qwen is not None:
            for value in vars(qwen).values():
                MainWindow._collect_qthreads(value, workers)
        # Unified Feishu forms own their metadata/upload threads beneath the dialog.
        from .feishu_save_dialog import FeishuSaveDialog
        for dialog in self.findChildren(FeishuSaveDialog):
            worker = dialog._worker
            if worker is not None:
                MainWindow._collect_qthreads(worker, workers)
        return workers

    def _settle_registered_qthreads(self, timeout_ms: int = 0) -> bool:
        """请求全部登记线程停止；任何线程仍活跃时保留引用并失败关闭。"""
        workers = self._registered_qthreads()

        def running(worker: QThread) -> bool:
            try:
                return bool(worker.isRunning())
            except RuntimeError:
                return False

        for worker in workers:
            if not running(worker):
                continue
            try:
                if hasattr(worker, "request_cancel"):
                    worker.request_cancel()
                else:
                    worker.requestInterruption()
            except RuntimeError:
                continue
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        for worker in workers:
            if not running(worker) or timeout_ms <= 0:
                continue
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining_ms:
                worker.wait(remaining_ms)
        return not any(running(worker) for worker in workers)

    def has_unsaved_work(self) -> bool:
        if getattr(self.library_page, "dirty", False):
            return True
        if any(has_unsaved_result(t) for t in self.tasks):
            return True
        for editor_type in (QTextEdit, QPlainTextEdit):
            for editor in self.findChildren(editor_type):
                try:
                    if editor.document().isModified(): return True
                except Exception:
                    continue
        return False


    def _choose_xhs_exit_action(self) -> str:
        """仅当专用浏览器在运行时给出保留/关闭/取消三选项。"""
        status = self._collection_service.xhs_browser_status(refresh=True)
        if not status.running:
            return "none"
        choice = dialogs.choose(
            self,
            "小红书专用浏览器仍在运行",
            "退出 Lulu 时，你可以保留专用浏览器继续登录，"
            "也可以请求它正常关闭。关闭不会删除登录状态。",
            ["保留浏览器并退出", "同时关闭并退出"],
            current=0,
            cancel="取消",
        )
        if choice is None:
            return "cancel"
        return "keep" if choice == 0 else "close"

    def _settle_xhs_exit(self, action: str) -> bool:
        """完成退出选择；False 表示保持 Lulu 打开。"""
        if action == "cancel":
            return False
        if action == "close":
            status = self._collection_service.close_xhs_browser()
            if status.code == "collection_busy":
                dialogs.alert(
                    self,
                    "仍有采集任务",
                    "本次采集尚未停止，专用浏览器没有关闭。请稍后重试退出。",
                )
                return False
            if status.code == "close_failed_browser_alive":
                choice = dialogs.choose(
                    self,
                    "专用浏览器仍在运行",
                    "Lulu 没有强制结束浏览器，也没有删除专用登录状态。",
                    ["返回处理", "浏览器保持运行，仍退出 Lulu"],
                    current=0,
                    cancel="取消",
                )
                if choice != 1:
                    return False
        if action != "close" or self._collection_service.xhs_browser_status().running:
            self._collection_service.release_xhs_browser_control()
        return True

    def closeEvent(self, e):  # noqa: N802
        if self.library_page is not None and hasattr(self.library_page, "flush_pending"):
            if not self.library_page.flush_pending(self.close):
                e.ignore()
                return
        first_attempt = not bool(getattr(self, "_close_settling", False))
        if (
            first_attempt
            and bool(getattr(self.cfg, "confirm_exit_during_tasks", True))
            and self.has_active_work()
        ):
            if not dialogs.confirm(
                self,
                "还有任务正在处理",
                "现在退出会中断正在处理的任务。确定退出 Lulu？",
                ok="退出 Lulu",
                cancel="继续任务",
            ):
                e.ignore()
                return
        if first_attempt:
            xhs_exit_action = self._choose_xhs_exit_action()
            if xhs_exit_action == "cancel":
                e.ignore()
                return
            self._close_xhs_action = xhs_exit_action
            self._close_settling = True
            if hasattr(self, "_update_check_timer"):
                self._update_check_timer.stop()
        else:
            xhs_exit_action = getattr(self, "_close_xhs_action", "keep")
        if not self.live_controller.request_stop():
            self._defer_close_until_workers_settle(e)
            return
        update_controller = getattr(self, "update_controller", None)
        if update_controller is not None:
            update_controller.cancel()
        if self.models_page is not None:
            self.models_page.shutdown_workers(0)
        connection = getattr(getattr(self, "settings_page", None), "feishu_connection", None)
        if connection is not None:
            connection.shutdown()
        if not self._settle_registered_qthreads(500):
            self._defer_close_until_workers_settle(e)
            return
        if self.collection_page is not None:
            self.collection_page.shutdown_workers()
        if not self._settle_xhs_exit(xhs_exit_action):
            e.ignore()
            return
        # Core 先结算实际线程再确认最终状态；旧文件恢复队列保留原快照语义。
        if getattr(self._task_queue_store, "async_write", False):
            if self.current:
                self._request_task_cancel(self.current[0].id)
                self._defer_close_until_workers_settle(e)
                return
            if not MainWindow._persist_task_queue(self, force=True):
                if getattr(self, "_result_recovery_failed", False):
                    self._close_settling = False
                    e.ignore()
                    return
                if self._task_queue_store_failed:
                    recovery = getattr(self, "_task_queue_recovery", None)
                    try:
                        if recovery is None:
                            from ..core.gui_task_recovery import GuiTaskRecoveryStore
                            recovery = self._task_queue_recovery = GuiTaskRecoveryStore.production()
                        recovery.save(self.tasks, self.task_groups,
                            preserve_existing=not self._task_queue_recovery_checked,
                            hidden_ids=getattr(self, "_core_hidden_intents", set()))
                    except (OSError, RuntimeError, ValueError, TypeError):
                        self._close_settling = False
                        self._toast("恢复记录无法写入磁盘，请检查可用空间或保存位置后再退出")
                        e.ignore()
                        return
                else:
                    self._defer_close_until_workers_settle(e)
                    return
        else:
            MainWindow._persist_task_queue(self, force=True)
        self._task_queue_snapshot_locked = True
        if hasattr(self, "_runtime_preferences_timer"):
            self._runtime_preferences_timer.stop()
        if self.current:
            self.current[1].request_cancel()
            self.current[1].wait(3000)
            if self.current[1].isRunning():
                self._defer_close_until_workers_settle(e)
                return
        if (
            self._transcript_cover_retry_worker is not None
            and self._transcript_cover_retry_worker.isRunning()
        ):
            self._transcript_cover_retry_worker.request_cancel()
            self._transcript_cover_retry_worker.wait(3000)
            if self._transcript_cover_retry_worker.isRunning():
                self._defer_close_until_workers_settle(e)
                return
        if self._transcript_cover_retry_worker is not None:
            self._transcript_cover_retry_worker = None
        if self.models_page is not None:
            if not self.models_page.shutdown_workers(500):
                self._defer_close_until_workers_settle(e)
                return
        if update_controller is not None and not update_controller.shutdown(500):
            self._defer_close_until_workers_settle(e)
            return
        inheritance = getattr(self, '_legacy_inheritance', None)
        if inheritance is not None:
            inheritance.shutdown()
        if self.voice_page is not None:
            self.voice_page.shutdown_workers()
        if self.settings_page is not None:
            self.settings_page.shutdown_workers()
        if self.library_page is not None and hasattr(self.library_page, "shutdown"):
            if not self.library_page.shutdown(500):
                self._defer_close_until_workers_settle(e)
                return
        if hasattr(self, "extraction_workspace"):
            self.extraction_workspace.shutdown()
        self.power_manager.shutdown()
        if not self.isMaximized() and not self.isMinimized():
            self.cfg.window_geometry=dict(x=self.x(),y=self.y(),width=self.width(),height=self.height())
        self.cfg.save()  # 偏好已在各处实时写入，这里收尾再存一次
        self._close_settling = False
        self._close_retry_scheduled = False
        super().closeEvent(e)

    def _defer_close_until_workers_settle(self, event) -> None:
        event.ignore()
        if hasattr(self, "_toast"):
            self._toast("正在安全停止后台任务")
        if getattr(self, "_close_retry_scheduled", False):
            return
        self._close_retry_scheduled = True

        def retry() -> None:
            self._close_retry_scheduled = False
            if hasattr(self, "close"):
                self.close()

        QTimer.singleShot(120, retry)
