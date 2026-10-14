from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.license._service_core import LicenseService
from ..core.license.models import LicenseStatus
from ..core.models_manager import app_data_dir
from . import icons, theme as T
from .control_foundation import PaperTextureFrame
from .license_contact import LicenseContactDialog


_WARNING_CODES = {
    "empty_code",
    "invalid",
    "expired",
    "revoked",
    "suspended",
    "device_limit",
    "rate_limited",
    "network_unavailable",
}
_ERROR_CODES = {
    "time_anomaly",
    "no_entitlement",
    "grace_expired",
    "lease_invalid",
    "invalid_server_response",
    "secure_store_error",
    "server_error",
}
_INPUT_ERROR_CODES = {"empty_code", "invalid"}
_SUCCESS_TRANSITION_MS = 360


class _ActivationThread(QThread):
    """Run the existing synchronous LicenseService contract away from the UI thread."""

    completed = Signal(object)

    def __init__(self, service: LicenseService, code: str):
        super().__init__()
        self._service = service
        self._code = code

    def run(self) -> None:
        try:
            status = self._service.activate(self._code)
        except Exception:
            # Unexpected implementation failures still fail closed without exposing
            # transport, storage or credential details to the user.
            status = LicenseStatus(
                False,
                "server_error",
                "授权服务暂时不可用，请稍后重试。",
            )
        self.completed.emit(status)


# A user may close the gate while an activation request is finishing. Keeping a
# module-level owner prevents QThread destruction from turning a safe exit into a
# Qt fatal; the thread removes itself as soon as it finishes.
_RUNNING_ACTIVATIONS: set[_ActivationThread] = set()


class LicenseGateWindow(QWidget):
    """Low-privilege activation/lock window shown before MainWindow exists."""

    def __init__(self, service: LicenseService, *, on_unlocked, parent=None):
        super().__init__(parent)
        self.service = service
        self.on_unlocked = on_unlocked
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self._contact_dialog: LicenseContactDialog | None = None
        self._activation_worker: _ActivationThread | None = None
        self._pending_unlock_status: LicenseStatus | None = None
        self._busy = False
        self._closing = False
        self.setObjectName("licenseGate")
        self.setWindowTitle("Lulu 授权激活")
        self.setAccessibleName("Lulu 授权激活")
        self.setMinimumSize(760, 520)
        self.resize(820, 560)
        self.setStyleSheet(T.QSS + _build_qss(self._consumer))
        self._build()
        self.apply_status(service.startup_status())

    def _build(self) -> None:
        window_layout = QVBoxLayout(self)
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.setSpacing(0)

        canvas = PaperTextureFrame(
            self,
            strength=self._consumer.paper_canvas_opacity,
            layer="licenseCanvas",
        )
        canvas.setObjectName("licenseCanvas")
        root = QVBoxLayout(canvas)
        root.setContentsMargins(40, 18, 40, 18)
        root.setSpacing(0)
        window_layout.addWidget(canvas)
        self.canvas = canvas

        shell = QFrame(canvas)
        shell.setObjectName("licenseShell")
        shell.setFixedWidth(620)
        shell.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        sv = QVBoxLayout(shell)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(10)

        brand = QFrame(shell)
        brand.setObjectName("licenseBrand")
        brand.setFixedHeight(68)
        bh = QHBoxLayout(brand)
        bh.setContentsMargins(2, 0, 2, 0)
        bh.setSpacing(14)

        logo_well = QFrame(brand)
        logo_well.setObjectName("licenseLogoWell")
        logo_well.setFixedSize(62, 62)
        logo_layout = QVBoxLayout(logo_well)
        logo_layout.setContentsMargins(0, 0, 0, 0)
        logo = QLabel(logo_well)
        logo.setObjectName("licenseLogo")
        logo.setProperty("brandAsset", "logo_mascot_transparent.png")
        logo.setFixedSize(62, 62)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(
            icons.image_pixmap(T.asset("logo_mascot_transparent.png"), 62)
        )
        logo.setAccessibleName("Lulu 品牌形象")
        logo_layout.addWidget(logo)
        bh.addWidget(logo_well, 0, Qt.AlignmentFlag.AlignVCenter)

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(0)
        title = QLabel("激活 Lulu", brand)
        title.setObjectName("licenseTitle")
        title.setAccessibleName("激活 Lulu")
        texts.addWidget(title)
        bh.addLayout(texts, 1)
        sv.addWidget(brand)

        panel = PaperTextureFrame(
            shell,
            strength=self._consumer.paper_texture_opacity,
            corner_radius=18,
            layer="licenseCard",
        )
        panel.setObjectName("licensePanel")
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(20, 17, 20, 17)
        pv.setSpacing(10)

        input_label = QLabel("授权码", panel)
        input_label.setObjectName("licenseInputLabel")
        pv.addWidget(input_label)

        self.code = QLineEdit(panel)
        self.code.setObjectName("licenseInput")
        self.code.setPlaceholderText("LULU-XXXX-XXXX-XXXX-XXXX")
        self.code.setAccessibleName("授权码")
        self.code.setAccessibleDescription("输入授权方提供的 Lulu 授权码")
        self.code.setClearButtonEnabled(True)
        self.code.setFixedHeight(48)
        self.code.setProperty("invalid", False)
        self.code.setProperty("busy", False)
        self.code.returnPressed.connect(self._activate)
        self.code.textEdited.connect(self._clear_input_error)
        pv.addWidget(self.code)

        self.status_box = QFrame(panel)
        self.status_box.setObjectName("licenseStatus")
        self.status_box.setProperty("tone", "neutral")
        self.status_box.setMinimumHeight(54)
        sh = QHBoxLayout(self.status_box)
        sh.setContentsMargins(12, 9, 12, 9)
        sh.setSpacing(10)
        self.status_icon = QLabel(self.status_box)
        self.status_icon.setObjectName("licenseStatusIcon")
        self.status_icon.setFixedSize(18, 18)
        self.status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_icon.setAccessibleName("授权状态")
        sh.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        self.message = QLabel(self.status_box)
        self.message.setObjectName("licenseMessage")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.message.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sh.addWidget(self.message, 1)
        pv.addWidget(self.status_box)

        self.activate_btn = QPushButton("激活 Lulu", panel)
        self.activate_btn.setObjectName("licensePrimary")
        self.activate_btn.setAccessibleName("激活 Lulu")
        self.activate_btn.setAccessibleDescription("联网验证授权码并进入 Lulu")
        self.activate_btn.setFixedHeight(46)
        self.activate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.activate_btn.setIcon(QIcon(icons.pixmap("check", "#FFFFFF", 15, 1.9)))
        self.activate_btn.clicked.connect(self._activate)
        pv.addWidget(self.activate_btn)
        T.shadow(panel, blur=32, dy=9, alpha=24)
        sv.addWidget(panel)

        actions = QFrame(shell)
        actions.setObjectName("licenseAuxActions")
        actions.setFixedHeight(38)
        ah = QHBoxLayout(actions)
        ah.setContentsMargins(0, 0, 0, 0)
        ah.setSpacing(8)

        self.contact_btn = self._aux_button("licenseContact", "联系授权方", "user")
        self.contact_btn.setAccessibleDescription("查看本机联系二维码")
        self.contact_btn.clicked.connect(self._show_contact_dialog)
        ah.addWidget(self.contact_btn, 1)

        self.folder_btn = self._aux_button("licenseFolder", "打开本地数据文件夹", "folder")
        self.folder_btn.setAccessibleDescription("在访达中打开 Lulu 本地数据文件夹")
        self.folder_btn.clicked.connect(self._open_data_folder)
        ah.addWidget(self.folder_btn, 1)

        self.exit_btn = self._aux_button("licenseExit", "退出", "x")
        self.exit_btn.setAccessibleDescription("退出 Lulu，也可按 Escape")
        self.exit_btn.clicked.connect(self.close)
        ah.addWidget(self.exit_btn, 1)
        sv.addWidget(actions)

        root.addWidget(shell, 0, Qt.AlignmentFlag.AlignCenter)
        self.shell = shell
        self.setTabOrder(self.code, self.activate_btn)
        self.setTabOrder(self.activate_btn, self.contact_btn)
        self.setTabOrder(self.contact_btn, self.folder_btn)
        self.setTabOrder(self.folder_btn, self.exit_btn)
        QTimer.singleShot(0, self._focus_code)

    def _aux_button(self, object_name: str, text: str, icon_name: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(object_name)
        button.setAccessibleName(text)
        button.setFixedHeight(38)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIcon(QIcon(icons.pixmap(icon_name, self._consumer.ink_muted, 14, 1.8)))
        return button

    def _show_contact_dialog(self) -> None:
        if self._contact_dialog is not None and self._contact_dialog.isVisible():
            self._contact_dialog.raise_()
            self._contact_dialog.activateWindow()
            return
        dialog = LicenseContactDialog(self)
        self._contact_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._contact_dialog = None
            self.contact_btn.setFocus(Qt.FocusReason.OtherFocusReason)
            QTimer.singleShot(
                0,
                lambda: self.contact_btn.setFocus(Qt.FocusReason.OtherFocusReason),
            )

    def apply_status(self, status: LicenseStatus) -> None:
        if status.allowed:
            self.code.setProperty("invalid", False)
            self.code.setEnabled(False)
            _refresh_style(self.code)
            self._set_status("授权已激活，正在进入 Lulu…", "success")
            self.activate_btn.setText("激活成功")
            self.activate_btn.setEnabled(False)
            self.activate_btn.setIcon(QIcon(icons.pixmap("check", "#FFFFFF", 15, 1.9)))
            _refresh_style(self.activate_btn)
            self._pending_unlock_status = status
            QTimer.singleShot(_SUCCESS_TRANSITION_MS, self._complete_unlock)
            return

        tone = _tone_for_code(status.code)
        self._set_status(status.message, tone)
        self.code.setProperty("invalid", status.code in _INPUT_ERROR_CODES)
        _refresh_style(self.code)
        QTimer.singleShot(0, lambda: self._focus_code(select_all=status.code in _INPUT_ERROR_CODES))

    def _activate(self) -> None:
        if self._busy or self._activation_worker is not None:
            return
        self._set_busy(True)
        worker = _ActivationThread(self.service, self.code.text())
        self._activation_worker = worker
        _RUNNING_ACTIVATIONS.add(worker)
        worker.completed.connect(self._activation_completed)
        worker.finished.connect(self._activation_thread_finished)
        worker.finished.connect(lambda current=worker: _RUNNING_ACTIVATIONS.discard(current))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _activation_completed(self, status: LicenseStatus) -> None:
        worker = self.sender()
        if worker is not self._activation_worker:
            return
        if self._closing:
            return
        self._set_busy(False)
        self.apply_status(status)

    def _activation_thread_finished(self) -> None:
        worker = self.sender()
        if worker is self._activation_worker:
            self._activation_worker = None
        if self._closing:
            QTimer.singleShot(0, self._finish_deferred_close)

    def _finish_deferred_close(self) -> None:
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _complete_unlock(self) -> None:
        status = self._pending_unlock_status
        self._pending_unlock_status = None
        if status is None or self._closing:
            return
        self.on_unlocked(status)
        self.close()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.code.setEnabled(not busy)
        self.code.setProperty("busy", busy)
        self.activate_btn.setEnabled(not busy)
        self.activate_btn.setProperty("busy", busy)
        if busy:
            self.activate_btn.setText("正在激活…")
            self.activate_btn.setIcon(
                QIcon(icons.pixmap("clock", "#FFFFFF", 15, 1.9))
            )
            self._set_status("正在验证授权，请稍候。", "busy")
        else:
            self.activate_btn.setText("激活 Lulu")
            self.activate_btn.setIcon(
                QIcon(icons.pixmap("check", "#FFFFFF", 15, 1.9))
            )
        _refresh_style(self.code)
        _refresh_style(self.activate_btn)

    def _clear_input_error(self) -> None:
        if not bool(self.code.property("invalid")):
            return
        self.code.setProperty("invalid", False)
        _refresh_style(self.code)

    def _focus_code(self, *, select_all: bool = False) -> None:
        if self._busy or not self.code.isEnabled():
            return
        self.code.setFocus(Qt.FocusReason.OtherFocusReason)
        if select_all and self.code.text():
            self.code.selectAll()

    def _set_status(self, text: str, tone: str) -> None:
        self.message.setText(text)
        self.message.setAccessibleDescription(text)
        self.status_box.setProperty("tone", tone)
        _refresh_style(self.status_box)
        if tone == "busy":
            icon_name, color = "clock", self._consumer.accent
        elif tone == "success":
            icon_name, color = "check", T.GREEN
        elif tone == "warning":
            icon_name, color = "clock", T.AMBER
        elif tone == "error":
            icon_name, color = "x", self._consumer.danger
        else:
            icon_name, color = "check", self._consumer.accent
        self.status_icon.setPixmap(icons.pixmap(icon_name, color, 16, 1.9))

    def _open_data_folder(self) -> None:
        path = app_data_dir()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path))))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        # Signals connected to this widget are automatically disconnected on
        # destruction; _RUNNING_ACTIVATIONS keeps any in-flight thread owned.
        self._closing = True
        self._pending_unlock_status = None
        worker = self._activation_worker
        if worker is not None and worker.isRunning():
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)


def _tone_for_code(code: str) -> str:
    if code == "not_activated":
        return "neutral"
    if code in _WARNING_CODES:
        return "warning"
    if code in _ERROR_CODES:
        return "error"
    return "error"


def _refresh_style(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _build_qss(c: T.ConsumerTheme) -> str:
    return f"""
QWidget#licenseGate {{ background: {c.canvas}; color: {c.ink}; }}
QFrame#licenseCanvas {{ background: {c.canvas}; border: none; }}
QFrame#licenseShell, QFrame#licenseBrand, QFrame#licenseAuxActions {{
    background: transparent; border: none;
}}
QFrame#licenseLogoWell {{
    background: transparent; border: none;
}}
QLabel#licenseLogo {{ background: transparent; border: none; }}
QLabel#licenseTitle {{
    background: transparent; color: {c.ink}; font-family: "{c.font_display}";
    font-size: 26px; font-weight: 650;
}}
QFrame#licensePanel {{
    background: {c.surface_focal}; border: 1px solid {c.line}; border-radius: 18px;
}}
QLabel#licenseInputLabel {{
    background: transparent; color: {c.ink}; font-size: 13px; font-weight: 650;
}}
QLineEdit#licenseInput {{
    background: {c.surface}; color: {c.ink}; border: 1px solid {c.line}; border-radius: 11px;
    padding: 0 14px; font-size: 16px; selection-background-color: {c.accent_wash};
    selection-color: {c.ink};
}}
QLineEdit#licenseInput:hover {{
    background: {c.surface_focal}; border-color: {c.accent_hairline};
}}
QLineEdit#licenseInput:focus {{
    background: {c.surface_focal}; border: 2px solid {c.accent}; padding: 0 13px;
}}
QLineEdit#licenseInput[invalid="true"] {{
    border: 1px solid {c.danger};
}}
QLineEdit#licenseInput[invalid="true"]:focus {{
    border: 2px solid {c.danger}; padding: 0 13px;
}}
QLineEdit#licenseInput:disabled {{
    background: {c.surface_work}; color: {c.ink_muted}; border: 1px solid {c.line};
}}
QFrame#licenseStatus {{
    border-radius: 11px; border: 1px solid transparent;
}}
QFrame#licenseStatus[tone="neutral"] {{
    background: {c.secondary}; border-color: {c.line};
}}
QFrame#licenseStatus[tone="busy"] {{
    background: {c.accent_wash}; border-color: {c.accent_hairline};
}}
QFrame#licenseStatus[tone="success"] {{
    background: {T.GREEN_WASH}; border-color: {T.GREEN};
}}
QFrame#licenseStatus[tone="warning"] {{
    background: {T.AMBER_WASH}; border-color: {T.AMBER};
}}
QFrame#licenseStatus[tone="error"] {{
    background: {c.secondary}; border-color: {c.danger};
}}
QLabel#licenseStatusIcon {{ background: transparent; border: none; }}
QLabel#licenseMessage {{
    background: transparent; color: {c.ink_soft}; font-size: 13px;
}}
QFrame#licenseStatus[tone="busy"] QLabel#licenseMessage {{ color: {c.accent}; }}
QFrame#licenseStatus[tone="success"] QLabel#licenseMessage {{ color: {T.GREEN}; }}
QFrame#licenseStatus[tone="warning"] QLabel#licenseMessage {{ color: {T.AMBER}; }}
QFrame#licenseStatus[tone="error"] QLabel#licenseMessage {{ color: {c.danger}; }}
QPushButton#licensePrimary {{
    background: {c.accent}; color: #FFFFFF; border: 2px solid {c.accent};
    border-radius: 11px; padding: 0 14px; font-size: 14px; font-weight: 700;
}}
QPushButton#licensePrimary:hover {{
    background: {c.accent_hover}; border-color: {c.accent_hover};
}}
QPushButton#licensePrimary:pressed {{
    background: {c.accent_hover}; border-color: {c.paper_edge};
}}
QPushButton#licensePrimary:focus {{
    border: 2px solid {c.accent_hairline_active};
}}
QPushButton#licensePrimary:disabled {{
    background: {c.surface_work}; color: {c.ink_muted}; border-color: {c.line};
}}
QPushButton#licenseContact, QPushButton#licenseFolder, QPushButton#licenseExit {{
    background: transparent; color: {c.ink_soft}; border: 1px solid transparent;
    border-radius: 9px; padding: 0 8px; font-size: 12px; font-weight: 550;
}}
QPushButton#licenseContact:hover, QPushButton#licenseFolder:hover, QPushButton#licenseExit:hover {{
    background: {c.secondary_hover}; color: {c.ink};
}}
QPushButton#licenseContact:pressed, QPushButton#licenseFolder:pressed, QPushButton#licenseExit:pressed {{
    background: {c.accent_wash}; color: {c.accent};
}}
QPushButton#licenseContact:focus, QPushButton#licenseFolder:focus, QPushButton#licenseExit:focus {{
    background: {c.secondary}; border-color: {c.accent_hairline_active};
}}
QPushButton#licenseContact:disabled, QPushButton#licenseFolder:disabled, QPushButton#licenseExit:disabled {{
    background: transparent; color: {c.ink_muted};
}}
"""
