from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QObject, QRectF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.updater.models import UpdateError, UpdateSnapshot
from ..core.updater.service import UpdateService
from ..core.updater.constants import MANUAL_DOWNLOAD_URL
from ..version import __build__, __version__
from . import theme as T
from .consumer_switch import ConsumerSwitch
from .progress_feedback import present_progress, reset_progress
from .widgets import ElideLabel


class AccessibleBuddyLabel(QLabel):
    """保留 QLabel 原生 buddy 语义，并让点击可见标签也能聚焦关联控件。"""

    def mouseReleaseEvent(self, event):  # noqa: N802
        buddy = self.buddy()
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()) and buddy is not None and buddy.isEnabled():
            buddy.setFocus(Qt.MouseFocusReason)
        super().mouseReleaseEvent(event)


class UpdateSwitch(ConsumerSwitch):
    """兼容既有更新 UI 名称的消费级动画开关。"""

    def __init__(self, parent=None):
        super().__init__(accessible_name="自动检查更新", parent=parent)
        self.setObjectName("updateAutoCheck")


class UpdateStatusDot(QWidget):
    """10px 固定占位中居中绘制 6px 状态点。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._color = T.INK_FAINT
        self.setFixedSize(10, 14)

    def setColor(self, color: str) -> None:  # noqa: N802
        self._color = color
        self.update()

    def paintEvent(self, event):  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._color))
        painter.drawEllipse(QRectF(2, 4, 6, 6))


class UpdateSpinner(QWidget):
    """14px 轻量检查中 spinner；隐藏时自动停止计时器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._angle = 0
        self.setFixedSize(14, 14)
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._angle = (self._angle + 36) % 360
        self.update()

    def setSpinning(self, spinning: bool) -> None:  # noqa: N802
        self.setVisible(spinning)
        if spinning and not self._timer.isActive():
            self._timer.start()
        elif not spinning:
            self._timer.stop()

    def paintEvent(self, event):  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(T.INK_FAINT), 1.5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(QRectF(2, 2, 10, 10), int((-self._angle) * 16), 270 * 16)


class _UpdateWorker(QThread):
    completed = Signal(object)
    failed = Signal(str, str)
    progress = Signal(int, int)

    def __init__(self, service: UpdateService, action: str):
        super().__init__()
        self.service = service; self.action = action; self.cancel_requested = False

    def run(self):
        try:
            if self.action.startswith("check:"):
                result = self.service.check(manual=self.action.endswith("manual"))
            elif self.action == "download":
                path = self.service.download(progress=self.progress.emit, cancelled=lambda: self.cancel_requested)
                result = self.service.stage(path)
            else:
                raise UpdateError("action", "更新操作无效")
            self.completed.emit(result)
        except UpdateError as exc:
            self.failed.emit(exc.code, str(exc))
        except Exception:
            self.failed.emit("failed", "更新暂时无法完成")


class _InstallWorker(QThread):
    completed = Signal(object)
    failed = Signal(str, str)

    def __init__(self, service, snapshot):
        super().__init__()
        self.service, self.snapshot = service, replace(snapshot)
        self.action, self.cancel_requested = 'install', False

    def run(self):
        import sys
        from ..core.updater.launch import prepare_launch
        try:
            launch = prepare_launch(sys.executable, self.snapshot, self.service.store.root,
                                    cancelled=lambda: self.cancel_requested)
            self.completed.emit(launch)
        except Exception as exc:
            self.failed.emit(getattr(exc, 'code', 'helper_launch'), '安装准备未完成，原版本和资料已保留。请重试。')


class _InstallPollWorker(QThread):
    def __init__(self, launch):
        super().__init__()
        self.launch, self.result = launch, 'waiting'

    def run(self):
        try:
            self.result = self.launch.poll()
        except (OSError, UpdateError):
            self.result = 'waiting' if self.launch.child.poll() is None else 'failed'


class UpdateController(QObject):
    changed = Signal(object)
    install_accepted = Signal()

    def __init__(self, *, service: UpdateService | None = None, active_work=None, unsaved_work=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.snapshot = service.snapshot if service is not None else UpdateSnapshot(current_version=__version__, current_build=__build__)
        self.active_work = active_work or (lambda: False)
        self.unsaved_work = unsaved_work or (lambda: False)
        self._worker = None
        self._install_launch = None
        self._install_poll_worker = None
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(100)
        self._install_timer.timeout.connect(self._poll_install)
        self._result_timer = QTimer(self)
        self._result_timer.setInterval(1000)
        self._result_timer.timeout.connect(self.refresh_external)
        self._result_timer.start()

    def refresh_external(self):
        if self.service is None or (self._worker and self._worker.isRunning()):
            return
        try:
            if self.service.refresh_external():
                self.snapshot = self.service.snapshot
                self.publish()
        except (UpdateError, OSError):
            # A transient state-file failure must not replace a helper result.
            return

    def acknowledge(self):
        if self.service is not None:
            self.service._save(state="idle", ui_state="up_to_date", message="", error_code="")
        self.snapshot.state = "idle"
        self.snapshot.ui_state = "up_to_date"
        self.publish()

    def publish(self):
        if self.snapshot.ui_state == "ready" and self.active_work():
            self.snapshot.ui_state = "ready_blocked"
        elif self.snapshot.ui_state == "ready_blocked" and not self.active_work():
            self.snapshot.ui_state = "ready"
        self.changed.emit(self.snapshot)

    def check(self, *, manual=True):
        if not self._ensure_service(): return
        if self.snapshot.state in {"installing", "waiting_ready", "recovering"}:
            self.refresh_external()
            return
        if not self.service.public_keys:
            self.snapshot.state="failed_signature"; self.snapshot.ui_state="manifest_verify_error"; self.snapshot.source="manual" if manual else "auto"; self.snapshot.verified_manifest=False; self.service.store.save(self.snapshot); self.publish(); return
        self._start(f"check:{'manual' if manual else 'auto'}")

    def maybe_auto_check(self, enabled: bool) -> bool:
        if not self._ensure_service(): return False
        self.refresh_external()
        if not enabled or self.snapshot.state in {"installing", "waiting_ready", "recovering"}: return False
        from datetime import datetime, timezone
        from ..core.updater.constants import CHECK_INTERVAL_SECONDS
        value=self.snapshot.last_checked_at
        if value:
            try:
                if (datetime.now(timezone.utc)-datetime.fromisoformat(value)).total_seconds()<CHECK_INTERVAL_SECONDS: return False
            except Exception:
                pass
        self.check(manual=False); return True

    def download(self): self._start("download")

    def cancel(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel_requested = True
            if getattr(self._worker, "action", None) == "download":
                self.snapshot = replace(self.snapshot, ui_state="pausing_download")
                self.publish()

    def shutdown(self, timeout_ms: int = 0) -> bool:
        """请求更新 worker 自然停止；超时后保留引用并诚实返回 False。"""
        poll_worker = self._install_poll_worker
        if poll_worker is not None and poll_worker.isRunning():
            if timeout_ms > 0:
                poll_worker.wait(timeout_ms)
            if poll_worker.isRunning():
                return False
        worker = self._worker
        if worker is None:
            return True
        if worker.isRunning():
            worker.cancel_requested = True
            if timeout_ms > 0:
                worker.wait(timeout_ms)
        if worker.isRunning():
            return False
        self._release_worker(worker)
        return True

    def install_gate(self) -> str:
        if self.active_work():
            self.snapshot.ui_state = "ready_blocked"; self.publish(); return "active"
        if self.unsaved_work(): return "unsaved"
        return "ok"

    def launch_install(self) -> str:
        if (self.install_gate() != "ok" or self.service is None or self._install_launch is not None
                or (self._worker and self._worker.isRunning())
                or self.snapshot.state in {'installing', 'waiting_ready', 'recovering'}):
            return "blocked"
        worker = _InstallWorker(self.service, self.snapshot)
        self._worker = worker
        worker.completed.connect(self._install_started)
        worker.failed.connect(self._install_failed)
        worker.finished.connect(lambda: self._release_worker(worker))
        self.snapshot = replace(self.snapshot, ui_state='preparing', message='正在准备安装，确认接管后将自动重新打开。')
        self.publish()
        worker.start()
        return 'launching'

    def _install_started(self, launch):
        self._install_launch = launch
        try:
            self.service.snapshot = self.service.store.load()
            self.snapshot = self.service.snapshot
        except (OSError, UpdateError):
            self.snapshot = replace(self.snapshot, state='installing', ui_state='preparing', txn=launch.plan['txn'])
        self.publish()
        self._install_timer.start()

    def _install_failed(self, code, message):
        try:
            snapshot = self.service.store.load()
            if snapshot.state in {'installing','waiting_ready','recovering'} and (self.service.store.root/'transactions/update.lock').exists():
                # A failed write or a different owner still needs startup recovery.
                # Do not replace its durable installing state from the GUI.
                self.snapshot = replace(snapshot, ui_state='preparation_error', message='安装中断，恢复记录已保留。请重新打开 Lulu。')
                self.publish()
                return
            if snapshot.ui_state != 'preparation_error':
                snapshot = replace(snapshot, state='failed', ui_state='preparation_error', error_code=code, message=message)
                self.service.store.save(snapshot)
            self.service.snapshot = self.snapshot = snapshot
        except (OSError, UpdateError):
            self.snapshot = replace(self.snapshot, state='failed', ui_state='preparation_error',
                                    error_code=code, message='安装中断，恢复记录已保留。请重新打开 Lulu。')
        self.publish()

    def _poll_install(self):
        launch = self._install_launch
        if launch is None:
            self._install_timer.stop()
            return
        if self._install_poll_worker is not None:
            return
        worker = _InstallPollWorker(launch)
        self._install_poll_worker = worker
        worker.finished.connect(lambda: self._install_polled(worker))
        worker.start()

    def _install_polled(self, worker):
        if self._install_poll_worker is worker:
            self._install_poll_worker = None
        worker.deleteLater()
        if self._install_launch is not worker.launch:
            return
        result = worker.result
        if result == 'waiting':
            return
        self._install_timer.stop()
        self._install_launch = None
        if result == 'accepted' and worker.launch.child.poll() is None:
            # The UI event may have waited behind a modal dialog. Never act
            # on an acceptance from a helper that has since exited.
            self.install_accepted.emit()
        else:
            self._install_failed('helper_exit', '安装助手未能接管，原版本保留。请重新检查更新。')

    def _start(self, action):
        if not self._ensure_service(): return
        if self.snapshot.state in {"installing", "waiting_ready", "recovering"}:
            self.refresh_external()
            return
        if self._worker and self._worker.isRunning(): return
        if self._worker is not None:
            self._release_worker(self._worker)
        self._worker = _UpdateWorker(self.service, action)
        self._worker.progress.connect(self._progress)
        self._worker.completed.connect(self._done)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(
            lambda worker=self._worker: self._release_worker(worker)
        )
        if action.startswith("check:"):
            # Publish on the UI thread before the worker can block. Keep this
            # preview separate from durable state (including a paused download).
            self.snapshot = replace(self.service.snapshot,
                ui_state="checking_manual" if action.endswith("manual") else "checking_auto",
                source="manual" if action.endswith("manual") else "auto",
                error_code="", message="", verified_manifest=False)
        else:
            self.snapshot = replace(self.service.snapshot, ui_state="downloading",
                                    error_code="", message="")
        self.publish()
        self._worker.start()

    def _release_worker(self, worker) -> None:
        if worker is None or worker.isRunning():
            return
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _progress(self, done, total):
        self.snapshot = replace(self.service.snapshot, downloaded=done, total=total)
        if self._worker is not None and self._worker.cancel_requested:
            self.snapshot.ui_state = "pausing_download"
        self.publish()

    def _done(self, _result): self.snapshot = self.service.snapshot; self.publish()

    def _failed(self, code, message):
        self.snapshot = self.service.snapshot
        if code == "recheck": self.publish(); return
        if self.snapshot.ui_state not in ("manifest_verify_error", "verify_error", "zip_structure_error", "preparation_error", "download_paused", "download_error"): self.snapshot.ui_state = "download_error" if self.snapshot.state.startswith("download") else "check_error"
        self.snapshot.message = message
        self.snapshot.error_code = code
        self.service.store.save(self.snapshot)
        self.publish()

    def _ensure_service(self):
        if self.service is None:
            try:
                self.service = UpdateService()
                self.snapshot = self.service.snapshot
            except Exception:
                self.snapshot.state="failed"; self.snapshot.ui_state="check_error"; self.snapshot.error_code="state_unavailable"; self.snapshot.message="更新状态目录暂时不可用"; self.publish(); return False
        return True


_STATE = {
    "idle": ("neutral", "准备就绪", "Lulu 会按你的设置检查新版本。", "检查更新", ""),
    "install_interrupted": ("warning", "上次安装已中断", "上次安装没有留下完整结果。请重新检查，或下载最新版覆盖安装；原资料保留。", "重新检查", "稍后"),
    "checking_auto": ("progress", "正在检查更新…", "正在安全读取版本信息。", "", ""),
    "checking_manual": ("progress", "正在检查更新…", "正在安全读取版本信息。", "正在检查…", ""),
    "up_to_date": ("success", "当前已是最新版本", f"你正在使用 Lulu {__version__}。", "再次检查", ""),
    "update_available": ("accent", "发现新版本", "更新已准备下载，你可以继续使用当前版本。", "下载更新", "稍后"),
    "manual_required": ("warning", "需要手动更新 Lulu", "下载后退出 Lulu，将新版拖入“应用程序”并选择“替换”，无需先卸载。", "查看手动下载包", "稍后"),
    "downloading": ("progress", "正在下载更新", "下载期间可以继续使用 Lulu。", "暂停下载", ""),
    "pausing_download": ("progress", "正在暂停下载…", "正在等待当前连接结束。", "正在暂停…", ""),
    "verifying": ("progress", "正在校验更新…", "正在确认更新包完整且可信。", "正在校验…", ""),
    "preparing": ("progress", "正在准备安装…", "完成后再由你决定何时重启。", "正在准备…", ""),
    "ready": ("success", "更新已准备好", "Lulu 将重启安装。如出现 macOS 授权窗口，请手动处理。你的本地数据不会被移动。", "立即重启安装", "稍后"),
    "ready_blocked": ("warning", "更新已下载，等待任务完成", "Lulu 正在处理任务。任务完成后才能重启安装，当前任务不会被取消。", "完成任务后重启", "稍后"),
    "download_paused": ("neutral", "下载已暂停", "下载已暂停，当前版本没有变化。下次可以继续下载。", "继续下载", "稍后"),
    "check_error": ("warning", "暂时无法检查更新", "暂时无法连接更新服务器，请稍后重试。", "重试", ""),
    "manifest_verify_error": ("error", "更新清单无法验证", "更新清单无法验证，本次不会安装。", "重新检查", "稍后"),
    "download_error": ("warning", "下载没有完成", "下载中断，当前版本不受影响。你可以稍后重新下载。", "重新下载", "稍后"),
    "verify_error": ("error", "更新包未通过安全校验", "安装包校验失败，已删除下载文件。当前版本保持不变。", "重新检查", "稍后"),
    "zip_structure_error": ("error", "安装包结构异常", "安装包结构异常，已取消更新。当前版本保持不变。", "重新检查", "稍后"),
    "preparation_error": ("warning", "更新准备未完成", "无法写入更新临时目录，请检查可用空间和目录权限后重试。当前版本保持不变。", "重新检查", "稍后"),
    "location_error": ("warning", "当前安装位置无法自动更新", "当前 Lulu 似乎运行在磁盘映像或临时目录中，请先拖到“应用程序”后再更新。", "重新检查位置", "稍后"),
    "install_error_rollback": ("error", "安装没有完成", "Lulu 已恢复到更新前的版本，你的本地数据没有变化。", "重新下载", "稍后"),
    "rollback_failed": ("error", "更新和自动恢复均未完成", "更新未完成，且自动恢复失败。请手动重新安装 Lulu。你的本地数据仍保留在应用支持目录。", "查看手动下载包", "稍后"),
    "old_version_after_restart": ("error", "更新未生效", f"更新没有生效，你仍在使用 Lulu {__version__}。", "重新检查", "稍后"),
    "installed": ("success", "已完成更新", "Lulu 已更新到新版本。", "知道了", ""),
}
_GATEKEEPER = "当前 Lulu 仍是未签名、未公证的信任用户内测包。更新后如 macOS 提示无法验证开发者，请按内测说明手动允许打开。"


def _state_presentation(snapshot):
    if snapshot.ui_state == "download_error" and snapshot.error_code == "cancelled":
        return ("neutral", "下载已暂停", "下载已暂停。下次将重新下载，当前版本不受影响。", "重新下载", "稍后")
    if snapshot.ui_state == "download_paused" and snapshot.error_code in {"network", "timeout", "http", "download"}:
        return ("warning", "下载已暂停", f"连接中断，已保留 {_size(snapshot.downloaded)}。网络恢复后可继续下载。", "继续下载", "稍后")
    if snapshot.ui_state == 'preparation_error' and snapshot.error_code.startswith('handover_'):
        return ('warning', '旧任务交接未完成', snapshot.message or
                '尚未替换程序，请查看当前任务状态后重试。', '重新检查', '稍后')
    # Older clients persisted local staging conflicts as package corruption.
    if snapshot.error_code == "staged_exists" and snapshot.ui_state in ("zip_structure_error", "preparation_error"):
        return ("warning", "更新临时目录冲突", "上次安装的临时目录仍存在。请重新检查更新后再试，原程序和资料已保留。", "重新检查", "稍后")
    if snapshot.ui_state == "preparation_error" and snapshot.error_code in {"storage_full", "install_space"}:
        return ("warning", "更新所需空间不足", "可用空间不足，暂时无法准备安装。请腾出空间后重新检查更新。", "重新检查", "稍后")
    return _STATE.get(snapshot.ui_state, _STATE["check_error"])


class UpdatePanel(QFrame):
    manual_download = Signal(str)
    install_requested = Signal()

    def __init__(self, cfg, controller: UpdateController, parent=None):
        super().__init__(parent); self.cfg = cfg; self.controller = controller
        self.setObjectName("updatePanel"); self._build(); controller.changed.connect(self.render); self.render(controller.snapshot)

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(18,15,18,15); root.setSpacing(9)
        header = QHBoxLayout(); title=QLabel("软件更新"); title.setObjectName("updateTitle"); header.addWidget(title); header.addStretch(1)
        self.badge=QLabel(f"当前 {__version__}"); self.badge.setObjectName("updateVersionBadge"); self.badge.setFixedHeight(22); self.badge.setToolTip(f"Build {__build__}"); header.addWidget(self.badge); root.addLayout(header)

        # 开关、当前状态和操作归并为一条紧凑操作行。
        self.body=QFrame(); self.body.setObjectName("updateStateBody"); body=QHBoxLayout(self.body); body.setContentsMargins(12,9,10,9); body.setSpacing(9)
        self.auto_title=AccessibleBuddyLabel("自动检查更新"); self.auto_title.setObjectName("updateAutoTitle"); self.auto_title.setAccessibleName("自动检查更新"); self.auto_title.setToolTip("每 24 小时在后台检查一次；只读取版本信息，不上传使用数据。"); self.auto_title.setCursor(Qt.PointingHandCursor); body.addWidget(self.auto_title)
        self.auto=UpdateSwitch(); self.auto.setAccessibleName("自动检查更新"); self.auto.setAccessibleDescription("开启后，Lulu 每 24 小时在后台检查一次更新；只读取版本信息，不上传使用数据。"); self.auto.setToolTip(self.auto_title.toolTip()); self.auto.setChecked(bool(getattr(self.cfg,"auto_update_enabled",True))); self.auto.toggled.connect(self._toggle_auto); self.auto_title.setBuddy(self.auto); body.addWidget(self.auto)
        divider=QFrame(); divider.setObjectName("updateDivider"); divider.setFixedWidth(1); divider.setFixedHeight(24); body.addWidget(divider)
        indicator=QWidget(); indicator.setObjectName("updateIndicatorSlot"); indicator.setFixedSize(14,14)
        self.status_dot=UpdateStatusDot(indicator); self.status_dot.setObjectName("updateStatusDot"); self.status_dot.move(2,0)
        self.spinner=UpdateSpinner(indicator); self.spinner.setObjectName("updateSpinner"); self.spinner.move(0,0); self.spinner.setSpinning(False)
        body.addWidget(indicator)
        self.state_title=ElideLabel(); self.state_title.setObjectName("updateStateTitle"); body.addWidget(self.state_title,1)
        self.meta=QLabel(); self.meta.setObjectName("updateStateMeta"); self.meta.setVisible(False)
        self.actions=QBoxLayout(QBoxLayout.LeftToRight); self.actions.setSpacing(7)
        self.tertiary=QPushButton("手动下载"); self.tertiary.setObjectName("updateTertiary"); self.tertiary.setFixedHeight(34); self.tertiary.clicked.connect(lambda: self.manual_download.emit(MANUAL_DOWNLOAD_URL)); self.actions.addWidget(self.tertiary)
        self.primary=QPushButton(); self.primary.setObjectName("updatePrimary"); self.primary.setProperty("tier","secondary"); self.primary.setFixedHeight(34); self.primary.setMinimumWidth(112); self.primary.clicked.connect(self._primary); self.actions.addWidget(self.primary)
        self.secondary=QPushButton(); self.secondary.setObjectName("updateSecondary"); self.secondary.setFixedHeight(34); self.secondary.setMinimumWidth(92); self.secondary.clicked.connect(self._secondary); self.actions.addWidget(self.secondary)
        body.addLayout(self.actions); root.addWidget(self.body)

        self.message=ElideLabel(); self.message.setObjectName("updateStateMessage"); root.addWidget(self.message)
        self.notes=QTextBrowser(); self.notes.setObjectName("updateNotes"); self.notes.setOpenExternalLinks(False); self.notes.setOpenLinks(False); self.notes.setMaximumHeight(112); self.notes.setMinimumHeight(56); root.addWidget(self.notes)
        self.progress=QProgressBar(); self.progress.setObjectName("updateProgress"); self.progress.setFixedHeight(6); self.progress.setTextVisible(False); self.progress_text=QLabel(); self.progress_text.setObjectName("updateProgressText"); reset_progress(self.progress); root.addWidget(self.progress); root.addWidget(self.progress_text)
        self.gatekeeper=QLabel(_GATEKEEPER); self.gatekeeper.setObjectName("updateGatekeeperNote"); self.gatekeeper.setWordWrap(True); root.addWidget(self.gatekeeper)
        self.setStyleSheet(_qss())

    def resizeEvent(self, event):
        # 紧凑区在常见宽度下始终保持一行，状态文字由 ElideLabel 安全省略。
        self.actions.setDirection(QBoxLayout.LeftToRight)
        super().resizeEvent(event)

    def _toggle_auto(self, checked):
        old=getattr(self.cfg,"auto_update_enabled",True); self.cfg.auto_update_enabled=checked
        try: saved=self.cfg.save()
        except Exception: saved=False
        if saved is False:
            self.cfg.auto_update_enabled=old
            self.auto.animate_rollback(bool(old))
            self.message.setText("设置暂未保存，请重试。")

    def _primary(self):
        state=self.controller.snapshot.ui_state
        if state in ("idle","up_to_date","check_error","manifest_verify_error","verify_error","zip_structure_error","preparation_error","old_version_after_restart","location_error","install_interrupted"): self.controller.check(manual=True)
        elif state in ("update_available","download_error","download_paused","install_error_rollback"): self.controller.download()
        elif state in ("manual_required","rollback_failed"): self.manual_download.emit(MANUAL_DOWNLOAD_URL)
        elif state=="installed": self.controller.acknowledge()
        elif state=="downloading": self.controller.cancel()
        elif state=="ready":
            if self.controller.install_gate()=="ok": self.install_requested.emit()

    def _secondary(self): self.controller.snapshot.deferred=True; self.controller.publish()

    def render(self, snapshot: UpdateSnapshot):
        tone,title,message,primary,secondary=_state_presentation(snapshot)
        if snapshot.ui_state == "check_error":
            message = _check_error_message(snapshot.error_code)
            if snapshot.error_code == "manifest_recheck_required":
                primary = "重新检查更新"
        self.body.setProperty("tone",tone)
        title=(title+f" {snapshot.latest_version}") if snapshot.ui_state=="update_available" and snapshot.latest_version else title
        self.state_title.setText(title); self.state_title.setToolTip(title)
        self.message.setText(message); self.message.setToolTip(message)
        self.meta.setText(f"当前 {__version__} ({__build__})" + (f" · 更新包 {_size(snapshot.asset_size)}" if snapshot.asset_size else "")); self.notes.setPlainText(snapshot.notes); self.notes.setVisible(snapshot.ui_state in ("update_available","download_error") and bool(snapshot.notes))
        in_progress=snapshot.ui_state in ("downloading","verifying","preparing"); self.progress.setVisible(in_progress); self.progress_text.setVisible(in_progress)
        if not in_progress: reset_progress(self.progress)
        if snapshot.ui_state=="downloading" and snapshot.total: present_progress(self.progress,0.90*snapshot.downloaded/max(snapshot.total,1)); self.progress_text.setText(f"正在下载 {int(snapshot.downloaded*100/max(snapshot.total,1))}% · {_size(snapshot.downloaded)} / {_size(snapshot.total)}")
        elif snapshot.ui_state=="downloading": present_progress(self.progress,None,unknown_text="下载中"); self.progress_text.setText(f"正在下载… · 已完成 {_size(snapshot.downloaded)}")
        elif snapshot.ui_state=="verifying": present_progress(self.progress,0.94,approximate=True); self.progress_text.setText(title)
        elif snapshot.ui_state=="preparing": present_progress(self.progress,0.97,approximate=True); self.progress_text.setText(title)
        verbose_states=("update_available","manual_required","ready","ready_blocked","download_paused","download_error","check_error","manifest_verify_error","verify_error","zip_structure_error","preparation_error","location_error","install_error_rollback","rollback_failed","old_version_after_restart")
        self.message.setVisible(snapshot.ui_state in verbose_states and bool(message))
        checking=snapshot.ui_state in ("checking_auto","checking_manual")
        self.spinner.setSpinning(checking); self.status_dot.setVisible(not checking)
        tier="primary" if snapshot.ui_state in ("update_available","download_paused","download_error","ready","install_error_rollback") else "secondary"
        self.primary.setProperty("tier",tier)
        self.gatekeeper.setVisible(snapshot.ui_state in ("update_available","ready","ready_blocked")); self.primary.setText(primary)
        tertiary_only=snapshot.ui_state in ("manual_required","rollback_failed")
        self.primary.setVisible(bool(primary) and not tertiary_only); self.primary.setEnabled(snapshot.ui_state not in ("checking_auto","checking_manual","pausing_download","verifying","preparing","ready_blocked")); self.primary.setCursor(Qt.PointingHandCursor if self.primary.isEnabled() else Qt.ArrowCursor)
        self.secondary.setText(secondary); self.secondary.setVisible(bool(secondary)); self.secondary.setCursor(Qt.PointingHandCursor if self.secondary.isEnabled() else Qt.ArrowCursor)
        self.tertiary.setText("查看手动下载包" if tertiary_only else "手动下载"); self.tertiary.setVisible(snapshot.ui_state in ("update_available","manual_required","location_error","rollback_failed"))
        tone_color={"neutral":T.INK_FAINT,"progress":T.BLUE,"success":T.GREEN,"warning":T.AMBER,"error":T.DANGER}.get(tone,T.INK_FAINT)
        self.status_dot.setColor(T.TERRA if tone=="accent" else tone_color)
        self.style().unpolish(self.primary); self.style().polish(self.primary)
        self.style().unpolish(self.body); self.style().polish(self.body)


def _check_error_message(code: str) -> str:
    """把已分类的更新失败转成准确、不泄露路径的用户文案。"""
    return {
        "unsafe_url": "更新地址未通过安全校验，本次检查已停止。",
        "unsafe_redirect": "更新跳转未通过安全校验，本次检查已停止。",
        "http": "更新信息暂未就绪，请稍后重试。",
        "timeout": "检查更新超时，请稍后重试。",
        "network": "网络连接暂时不可用，请检查网络后重试。",
        "state_unavailable": "更新状态暂时无法保存，请稍后重试。",
        "state_corrupt": "更新状态需要重新初始化，请重试。",
    }.get(code, "暂时无法检查更新，请稍后重试。")


class AboutUpdatePanel(QFrame):
    """关于页按更新、产品与作者、说明分组，复用现有更新状态机。"""

    _INFO_ROW_MIN_HEIGHT = 58

    manual_download = Signal(str)
    install_requested = Signal()

    def __init__(
        self,
        cfg,
        controller: UpdateController,
        parent=None,
        *,
        repository_url: str,
        author_url: str,
        product_boundary: str,
        open_url,
        show_contact,
    ):
        super().__init__(parent)
        self.cfg = cfg
        self.controller = controller
        self.repository_url = repository_url
        self.author_url = author_url
        self._open_url = open_url
        self._show_contact = show_contact
        self._consumer = T.consumer_theme(getattr(cfg, "theme", None))
        self.setObjectName("aboutUpdatePanel")
        self._build(product_boundary)
        controller.changed.connect(self.render)
        self.render(controller.snapshot)

    def _build(self, boundary: str) -> None:
        consumer = self._consumer
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)
        root.setAlignment(Qt.AlignTop)

        self.software_group = QFrame()
        self.software_group.setObjectName("aboutSoftwareGroup")
        self.software_group.setAccessibleName("软件更新")
        software = QVBoxLayout(self.software_group)
        software.setContentsMargins(0, 0, 0, 0)
        software.setSpacing(0)

        self.status_hero = QFrame()
        self.status_hero.setObjectName("aboutUpdateStatusHero")
        self.status_hero.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._status_layout = QVBoxLayout(self.status_hero)
        self._status_layout.setContentsMargins(18, 12, 18, 12)
        self._status_layout.setSpacing(8)
        self._summary_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self._summary_layout.setSpacing(12)

        self.state_slot = QWidget()
        state = QVBoxLayout(self.state_slot)
        state.setContentsMargins(0, 0, 0, 0)
        state.setSpacing(3)
        title = QLabel("软件更新")
        title.setObjectName("aboutInfoTitle")
        state.addWidget(title)
        state_line = QHBoxLayout()
        state_line.setContentsMargins(0, 0, 0, 0)
        state_line.setSpacing(7)
        indicator = QWidget()
        indicator.setFixedSize(14, 14)
        self.status_dot = UpdateStatusDot(indicator)
        self.status_dot.move(2, 0)
        self.spinner = UpdateSpinner(indicator)
        self.spinner.move(0, 0)
        self.spinner.setSpinning(False)
        state_line.addWidget(indicator, 0, Qt.AlignVCenter)
        self.state_title = QLabel()
        self.state_title.setObjectName("aboutUpdateStateTitle")
        self.state_title.setWordWrap(True)
        state_line.addWidget(self.state_title, 1)
        state.addLayout(state_line)
        self._summary_layout.addWidget(self.state_slot, 1)

        self.action_slot = QWidget()
        actions = QHBoxLayout(self.action_slot)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.tertiary = self._row_button("手动下载", "手动下载安装包")
        self.tertiary.setObjectName("aboutUpdateTertiary")
        self.tertiary.setVisible(False)
        self.tertiary.clicked.connect(lambda: self.manual_download.emit(MANUAL_DOWNLOAD_URL))
        self.secondary = self._row_button("稍后", "稍后更新")
        self.secondary.setObjectName("aboutUpdateSecondary")
        self.secondary.setVisible(False)
        self.secondary.clicked.connect(self._secondary)
        self.primary = self._row_button("检查更新", "检查更新")
        self.primary.setObjectName("aboutUpdatePrimary")
        self.primary.setFixedWidth(132)
        self.primary.clicked.connect(self._primary)
        actions.addWidget(self.tertiary)
        actions.addWidget(self.secondary)
        actions.addWidget(self.primary)
        self._summary_layout.addWidget(self.action_slot, 0)
        self._status_layout.addLayout(self._summary_layout)

        self.detail_slot = QWidget()
        detail = QVBoxLayout(self.detail_slot)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(6)
        self.message = QLabel()
        self.message.setObjectName("aboutUpdateMessage")
        self.message.setWordWrap(True)
        detail.addWidget(self.message)
        self.progress = QProgressBar()
        self.progress.setObjectName("aboutUpdateProgress")
        self.progress.setFixedHeight(5)
        self.progress.setTextVisible(False)
        self.progress_text = QLabel()
        self.progress_text.setObjectName("aboutUpdateProgressText")
        reset_progress(self.progress)
        detail.addWidget(self.progress)
        detail.addWidget(self.progress_text)
        self._status_layout.addWidget(self.detail_slot)
        software.addWidget(self.status_hero)
        software.addWidget(self._divider())

        auto_row = self._row("自动检查更新", "每 24 小时读取一次版本信息，不上传使用数据。")
        auto_lay = auto_row.layout()
        self.auto_title = auto_row.findChild(QLabel, "aboutInfoTitle")
        self.inline_error = QLabel()
        self.inline_error.setObjectName("aboutUpdateInlineError")
        self.inline_error.setWordWrap(True)
        self.inline_error.setVisible(False)
        auto_lay.itemAt(0).layout().addWidget(self.inline_error)
        self.auto = UpdateSwitch()
        self.auto.setAccessibleName("自动检查更新")
        self.auto.setAccessibleDescription("每 24 小时读取一次版本信息，不上传使用数据。")
        self.auto.setChecked(bool(getattr(self.cfg, "auto_update_enabled", True)))
        self.auto.toggled.connect(self._toggle_auto)
        self.auto_title.setBuddy(self.auto)
        self.auto_title.setCursor(Qt.PointingHandCursor)
        auto_lay.addWidget(self.auto, 0, Qt.AlignVCenter)
        software.addWidget(auto_row)
        root.addWidget(self.software_group)

        self.info_group = QFrame()
        self.info_group.setObjectName("aboutInfoGroup")
        self.info_group.setAccessibleName("产品与作者")
        info = QVBoxLayout(self.info_group)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(0)
        repo_row = self._row("GitHub 仓库", self.repository_url)
        self.repository_button = self._row_button("打开仓库", "打开 Lulu GitHub 仓库")
        self.repository_button.clicked.connect(lambda: self._open_url(self.repository_url))
        repo_row.layout().addWidget(self.repository_button, 0, Qt.AlignVCenter)
        info.addWidget(repo_row)
        info.addWidget(self._divider())
        author_row = self._row("作者 GitHub 主页", self.author_url)
        self.author_button = self._row_button("打开主页", "打开作者 GitHub 主页")
        self.author_button.clicked.connect(lambda: self._open_url(self.author_url))
        author_row.layout().addWidget(self.author_button, 0, Qt.AlignVCenter)
        info.addWidget(author_row)
        info.addWidget(self._divider())
        contact_row = self._row("作者联系方式", "微信扫码联系兆基")
        self.contact_button = self._row_button("查看微信二维码", "查看作者微信二维码")
        self.contact_button.clicked.connect(self._show_contact)
        contact_row.layout().addWidget(self.contact_button, 0, Qt.AlignVCenter)
        info.addWidget(contact_row)
        root.addWidget(self.info_group)

        self.boundary_group = QFrame()
        self.boundary_group.setObjectName("aboutProductNote")
        note = QVBoxLayout(self.boundary_group)
        note.setContentsMargins(0, 0, 0, 0)
        note.setSpacing(4)
        boundary_title = QLabel("产品说明")
        boundary_title.setObjectName("aboutInfoTitle")
        note.addWidget(boundary_title)
        self.product_boundary = QLabel(boundary)
        self.product_boundary.setObjectName("aboutProductBoundary")
        self.product_boundary.setWordWrap(True)
        self.product_boundary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        note.addWidget(self.product_boundary)
        root.addWidget(self.boundary_group)

        self.setStyleSheet(
            "QFrame#aboutUpdatePanel,QFrame#aboutUpdateStatusHero,QFrame#aboutProductNote{background:transparent;border:none;}"
            f"QFrame#aboutInfoGroup,QFrame#aboutSoftwareGroup{{background:{consumer.surface_focal};border:1px solid {consumer.line};border-radius:12px;}}"
            "QFrame#aboutInfoRow{background:transparent;border:none;}"
            f"QFrame#aboutInfoDivider{{background:{T.LINE_SOFT};border:none;}}"
            f"QLabel#aboutUpdateStateTitle{{color:{consumer.ink_soft};font-size:13px;background:transparent;}}"
            f"QLabel#aboutUpdateMessage,QLabel#aboutUpdateProgressText,QLabel#aboutInfoMeta,QLabel#aboutProductBoundary{{color:{consumer.ink_muted};font-size:12px;background:transparent;}}"
            f"QLabel#aboutInfoTitle{{color:{consumer.ink};font-size:13px;font-weight:600;background:transparent;}}"
            f"QLabel#aboutUpdateInlineError{{color:{consumer.danger};font-size:12px;background:transparent;}}"
            f"QProgressBar#aboutUpdateProgress{{background:{T.BG_DEEP};border:none;border-radius:2px;}}"
            f"QProgressBar#aboutUpdateProgress::chunk{{background:{consumer.accent};border-radius:2px;}}"
            "QPushButton#aboutUpdatePrimary,QPushButton#aboutUpdateSecondary,QPushButton#aboutUpdateTertiary,QPushButton#aboutInfoAction{border-radius:8px;font-size:12px;padding:0 13px;}"
            f"QPushButton#aboutUpdatePrimary[tier=\"primary\"]{{background:{consumer.accent};color:#FFFFFF;border:1px solid {consumer.accent};font-weight:600;}}"
            f"QPushButton#aboutUpdatePrimary[tier=\"secondary\"],QPushButton#aboutUpdateSecondary,QPushButton#aboutInfoAction{{background:{consumer.surface_focal};color:{consumer.ink};border:1px solid {consumer.line};}}"
            f"QPushButton#aboutUpdateTertiary{{background:transparent;color:{consumer.ink_soft};border:1px solid transparent;}}"
            f'QPushButton#aboutUpdatePrimary[tier="primary"]:hover{{background:{consumer.accent_hover};border-color:{consumer.accent_hover};}}'
            f'QPushButton#aboutUpdatePrimary[tier="primary"]:pressed{{background:{QColor(consumer.accent_hover).darker(106).name()};}}'
            f'QPushButton#aboutUpdatePrimary[tier="secondary"]:hover,QPushButton#aboutUpdateSecondary:hover,QPushButton#aboutInfoAction:hover,QPushButton#aboutUpdateTertiary:hover{{background:{consumer.accent_wash};border-color:{consumer.accent_hairline};color:{consumer.accent};}}'
            f'QPushButton#aboutUpdatePrimary[tier="secondary"]:pressed,QPushButton#aboutUpdateSecondary:pressed,QPushButton#aboutInfoAction:pressed,QPushButton#aboutUpdateTertiary:pressed{{background:{consumer.secondary_hover};border-color:{consumer.line};color:{consumer.ink};}}'
            f'QPushButton#aboutUpdatePrimary[tier]:focus,QPushButton#aboutUpdateSecondary:focus,QPushButton#aboutInfoAction:focus,QPushButton#aboutUpdateTertiary:focus{{border:2px solid {consumer.accent_hairline_active};}}'
            f'QPushButton#aboutUpdatePrimary[tier]:disabled{{background:{consumer.surface_alt};color:{consumer.ink_muted};border:1px solid {consumer.line};}}'
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_summary_layout()

    def _sync_summary_layout(self) -> None:
        # Real detail controls grow with their content; a narrow card stacks the
        # action below the status instead of clipping text or hiding recovery.
        direction = QBoxLayout.TopToBottom if self.width() < 460 else QBoxLayout.LeftToRight
        if self._summary_layout.direction() != direction:
            self._summary_layout.setDirection(direction)

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setObjectName("aboutInfoDivider")
        divider.setFixedHeight(1)
        return divider

    @staticmethod
    def _row(title: str, description: str) -> QFrame:
        row = QFrame()
        row.setObjectName("aboutInfoRow")
        row.setMinimumHeight(AboutUpdatePanel._INFO_ROW_MIN_HEIGHT)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.setAlignment(Qt.AlignVCenter)
        title_label = AccessibleBuddyLabel(title)
        title_label.setObjectName("aboutInfoTitle")
        text.addWidget(title_label)
        detail = QLabel(description)
        detail.setObjectName("aboutInfoMeta")
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(detail)
        layout.addLayout(text, 1)
        return row

    @staticmethod
    def _row_button(text: str, accessible_name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("aboutInfoAction")
        button.setFixedHeight(34)
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.StrongFocus)
        button.setAccessibleName(accessible_name)
        return button

    def _toggle_auto(self, checked: bool) -> None:
        old = bool(getattr(self.cfg, "auto_update_enabled", True))
        self.cfg.auto_update_enabled = bool(checked)
        try:
            saved = bool(self.cfg.save())
        except Exception:  # noqa: BLE001
            saved = False
        if not saved:
            self.cfg.auto_update_enabled = old
            self.auto.animate_rollback(old)
            self.inline_error.setText("设置暂未保存，请重试。")
            self.inline_error.setVisible(True)
            return
        self.inline_error.clear()
        self.inline_error.setVisible(False)

    def _primary(self) -> None:
        state = self.controller.snapshot.ui_state
        if state in (
            "idle", "up_to_date", "check_error", "manifest_verify_error", "verify_error",
            "zip_structure_error", "preparation_error", "old_version_after_restart", "location_error", "install_interrupted",
        ):
            self.controller.check(manual=True)
        elif state in ("update_available", "download_error", "download_paused", "install_error_rollback"):
            self.controller.download()
        elif state in ("manual_required", "rollback_failed"):
            self.manual_download.emit(MANUAL_DOWNLOAD_URL)
        elif state == "installed":
            self.controller.acknowledge()
        elif state == "downloading":
            self.controller.cancel()
        elif state == "ready" and self.controller.install_gate() == "ok":
            self.install_requested.emit()

    def _secondary(self) -> None:
        self.controller.snapshot.deferred = True
        self.controller.publish()

    def render(self, snapshot: UpdateSnapshot) -> None:
        tone, title, message, primary, secondary = _state_presentation(snapshot)
        if snapshot.ui_state == "idle":
            title, message, primary = "尚未检查更新", "", "检查更新"
        elif snapshot.ui_state == "up_to_date":
            title, message, primary = "已是最新版本", "", "检查更新"
        elif snapshot.ui_state in ("checking_auto", "checking_manual"):
            primary = "正在检查…"
        elif snapshot.ui_state == "check_error":
            message = _check_error_message(snapshot.error_code)
            if snapshot.error_code == "manifest_recheck_required":
                primary = "重新检查更新"
        elif snapshot.ui_state == "update_available" and snapshot.latest_version:
            title, primary = f"最新版本 v{snapshot.latest_version}", "立即更新"

        self.state_title.setText(title)
        self.message.setText(message)
        show_message = bool(message) and snapshot.ui_state != "installed"
        in_progress = snapshot.ui_state in ("downloading", "verifying", "preparing")
        self.message.setVisible(show_message)
        self.detail_slot.setMaximumHeight(16777215 if show_message or in_progress else 0)
        self.detail_slot.setVisible(show_message or in_progress)
        self._sync_summary_layout()
        checking = snapshot.ui_state in ("checking_auto", "checking_manual")
        self.spinner.setSpinning(checking)
        self.status_dot.setVisible(not checking)
        tone_color = {
            "neutral": T.INK_FAINT, "progress": T.BLUE, "success": T.GREEN,
            "warning": T.AMBER, "error": T.DANGER,
        }.get(tone, T.TERRA if tone == "accent" else T.INK_FAINT)
        if snapshot.ui_state == "update_available":
            tone_color = T.DANGER
        self.status_dot.setColor(tone_color)

        in_progress = snapshot.ui_state in ("downloading", "verifying", "preparing")
        self.progress.setVisible(in_progress)
        self.progress_text.setVisible(in_progress)
        if not in_progress:
            reset_progress(self.progress)
        if snapshot.ui_state == "downloading" and snapshot.total:
            present_progress(
                self.progress,
                0.90 * snapshot.downloaded / max(snapshot.total, 1),
            )
            self.progress_text.setText(
                f"{int(snapshot.downloaded * 100 / max(snapshot.total, 1))}% · "
                f"{_size(snapshot.downloaded)} / {_size(snapshot.total)}"
            )
        elif snapshot.ui_state == "downloading":
            present_progress(self.progress, None, unknown_text="下载中")
            self.progress_text.setText(f"已完成 {_size(snapshot.downloaded)}")
        elif snapshot.ui_state == "verifying":
            present_progress(self.progress, 0.94, approximate=True)
            self.progress_text.setText(title)
        elif snapshot.ui_state == "preparing":
            present_progress(self.progress, 0.97, approximate=True)
            self.progress_text.setText(title)

        tier = "primary" if snapshot.ui_state in (
            "update_available", "download_paused", "download_error", "ready", "install_error_rollback"
        ) else "secondary"
        self.primary.setProperty("tier", tier)
        self.primary.setText(primary)
        self.primary.setAccessibleName(primary)
        self.primary.setVisible(True)
        self.primary.setEnabled(snapshot.ui_state not in (
            "checking_auto", "checking_manual", "pausing_download", "verifying", "preparing", "ready_blocked"
        ))
        self.secondary.setText(secondary)
        self.secondary.setVisible(False)
        self.tertiary.setVisible(snapshot.ui_state == "location_error")
        for widget in (self.primary, self.secondary, self.tertiary):
            widget.setCursor(Qt.PointingHandCursor if widget.isEnabled() else Qt.ArrowCursor)
            widget.style().unpolish(widget)
            widget.style().polish(widget)



def _size(value):
    if value<0:return ""
    if value==0:return "0 B"
    for unit in ("B","KB","MB","GB"):
        if value<1024 or unit=="GB": return f"{value:.0f} {unit}" if unit=="B" else f"{value:.1f} {unit}"
        value/=1024


def _qss():
    primary_pressed=QColor(T.TERRA_DEEP).darker(106).name()
    badge_border="transparent"
    rules = [
        ("QFrame#updatePanel", f"background:{T.CARD};border:1px solid {T.LINE};border-radius:14px"),
        ("QLabel#updateTitle", f'font-family:"{T.SERIF}";font-size:16px;font-weight:600;color:{T.INK}'),
        ("QLabel#updateStateMessage,QLabel#updateStateMeta,QLabel#updateProgressText,QLabel#updateGatekeeperNote", f"color:{T.INK_SOFT};background:transparent"),
        ("QLabel#updateVersionBadge", f"background:{T.BG_DEEP};color:{T.INK_FAINT};border:1px solid {badge_border};border-radius:6px;padding:0 8px;font-size:11px;font-weight:500"),
        ("QLabel#updateAutoTitle", f"color:{T.INK_SOFT};font-size:12px;background:transparent"),
        ("QLabel#updateStateTitle", f"color:{T.INK};font-size:12px;font-weight:600;background:transparent"),
        ("QFrame#updateDivider", f"background:{T.LINE};border:none"),
        ("QFrame#updateStateBody", f"background:{T.BG_DEEP};border:1px solid {T.LINE_SOFT};border-radius:10px"),
        ("QTextBrowser#updateNotes", f"background:{T.BG};color:{T.INK};border:1px solid {T.LINE};border-radius:8px"),
        ("QProgressBar#updateProgress", f"background:{T.BG_DEEP};border:none;border-radius:3px"),
        ("QProgressBar#updateProgress::chunk", f"background:{T.TERRA};border-radius:3px"),
        ("QPushButton#updatePrimary,QPushButton#updateSecondary,QPushButton#updateTertiary", "border-radius:8px;font-size:12px;padding:0 14px"),
        ('QPushButton#updatePrimary[tier="primary"]', f"background:{T.TERRA};color:#FFFFFF;border:1px solid {T.TERRA};font-weight:600"),
        ('QPushButton#updatePrimary[tier="primary"]:hover', f"background:{T.TERRA_DEEP};border-color:{T.TERRA_DEEP}"),
        ('QPushButton#updatePrimary[tier="primary"]:pressed', f"background:{primary_pressed};border-color:{primary_pressed}"),
        ('QPushButton#updatePrimary[tier="primary"]:focus', f"border:2px solid {T.TERRA_SOFT}"),
        ('QPushButton#updatePrimary[tier="primary"]:disabled', f"background:{T.TERRA_SOFT};color:{T.INK_FAINT};border:1px solid {T.TERRA_SOFT}"),
        ('QPushButton#updatePrimary[tier="secondary"],QPushButton#updateSecondary', f"background:{T.CARD};color:{T.INK_SOFT};border:1px solid {T.LINE};font-weight:500"),
        ('QPushButton#updatePrimary[tier="secondary"]:hover,QPushButton#updateSecondary:hover', f"background:{T.TERRA_WASH};color:{T.INK};border-color:{T.TERRA_SOFT}"),
        ('QPushButton#updatePrimary[tier="secondary"]:pressed,QPushButton#updateSecondary:pressed', f"background:{T.BG_DEEP};color:{T.INK};border-color:{T.LINE}"),
        ('QPushButton#updatePrimary[tier="secondary"]:focus,QPushButton#updateSecondary:focus', f"border:2px solid {T.TERRA_SOFT}"),
        ('QPushButton#updatePrimary[tier="secondary"]:disabled,QPushButton#updateSecondary:disabled', f"background:{T.BG_DEEP};color:{T.INK_FAINT};border:1px solid {T.LINE_SOFT}"),
        ("QPushButton#updateTertiary", f"background:transparent;color:{T.INK_SOFT};border:1px solid transparent;font-weight:500;padding:0 8px"),
        ("QPushButton#updateTertiary:hover", f"background:{T.TERRA_WASH};color:{T.TERRA};border-color:transparent"),
        ("QPushButton#updateTertiary:pressed", f"background:{T.BG_DEEP};color:{T.INK}"),
        ("QPushButton#updateTertiary:focus", f"border:2px solid {T.TERRA_SOFT}"),
        ("QPushButton#updateTertiary:disabled", f"background:transparent;color:{T.INK_FAINT};border-color:transparent"),
    ]
    return "\n".join(selector + " {" + body + "}" for selector, body in rules)
