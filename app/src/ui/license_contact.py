from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QTimer, QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedLayout,
    QVBoxLayout,
)

from . import icons, theme as T
from .control_foundation import PaperTextureFrame


CONTACT_QR_SHA256 = "adaea8a3f66373bb7572dae08e798aa708698e010fcfbea24892055d82e1d88f"
CONTACT_QR_SOURCE_SIZE = QSize(280, 262)
CONTACT_QR_MAX_LOGICAL = QSize(224, 210)


class LicenseContactDialog(QDialog):
    """Local-only contact QR modal. It never decodes QR content or uses network APIs."""

    def __init__(
        self,
        parent=None,
        *,
        asset_path: str | None = None,
        expected_sha256: str = CONTACT_QR_SHA256,
    ):
        super().__init__(parent)
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.setObjectName("licenseContactDialog")
        self.setWindowTitle("联系授权方")
        self.setAccessibleName("联系授权方")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(388, 432)
        self.setStyleSheet(_build_qss(self._consumer))
        self._asset_path = asset_path or T.asset("license_contact_wechat.png")
        self._expected_sha256 = expected_sha256
        self.qr_available = False
        self._build()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None:
            frame = self.frameGeometry()
            frame.moveCenter(parent.frameGeometry().center())
            self.move(frame.topLeft())
        QTimer.singleShot(
            0,
            lambda: self.close_button.setFocus(Qt.FocusReason.OtherFocusReason),
        )

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)

        card = PaperTextureFrame(
            self,
            strength=self._consumer.paper_texture_opacity,
            corner_radius=18,
            layer="licenseContactCard",
        )
        card.setObjectName("licenseContactCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        outer.addWidget(card)
        self.card = card

        header = QFrame(card)
        header.setObjectName("licenseContactHeader")
        header.setFixedHeight(72)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(12)

        icon_well = QFrame(header)
        icon_well.setObjectName("licenseContactIconWell")
        icon_well.setFixedSize(40, 40)
        icon_layout = QVBoxLayout(icon_well)
        icon_layout.setContentsMargins(10, 10, 10, 10)
        contact_icon = QLabel(icon_well)
        contact_icon.setObjectName("licenseContactIcon")
        contact_icon.setFixedSize(20, 20)
        contact_icon.setPixmap(icons.pixmap("user", self._consumer.accent, 20, 1.8))
        contact_icon.setAccessibleName("联系授权方")
        icon_layout.addWidget(contact_icon)
        header_layout.addWidget(icon_well, 0, Qt.AlignmentFlag.AlignTop)

        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(4)
        title = QLabel("联系授权方", header)
        title.setObjectName("licenseContactTitle")
        title.setAccessibleName("联系授权方")
        description = QLabel("微信扫码联系，添加时备注「Lulu 授权」。", header)
        description.setObjectName("licenseContactDescription")
        description.setWordWrap(True)
        copy.addWidget(title)
        copy.addWidget(description)
        header_layout.addLayout(copy, 1)
        layout.addWidget(header)

        well = QFrame(card)
        well.setObjectName("licenseQrWell")
        well.setProperty("fallback", False)
        well.setFixedSize(248, 232)
        stack = QStackedLayout(well)
        stack.setContentsMargins(8, 8, 8, 8)
        stack.setStackingMode(QStackedLayout.StackingMode.StackOne)

        self.qr_image = QLabel(well)
        self.qr_image.setObjectName("licenseQrImage")
        self.qr_image.setFixedSize(232, 216)
        self.qr_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_image.setScaledContents(False)
        self.qr_image.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.qr_image.setAccessibleName("微信联系二维码")
        stack.addWidget(self.qr_image)

        self.qr_fallback = QLabel("联系二维码暂不可用，请稍后再试。", well)
        self.qr_fallback.setObjectName("licenseQrFallback")
        self.qr_fallback.setFixedSize(232, 216)
        self.qr_fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_fallback.setWordWrap(True)
        self.qr_fallback.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        stack.addWidget(self.qr_fallback)

        pixmap = _load_qr_pixmap(
            self._asset_path,
            expected_sha256=self._expected_sha256,
            target_dpr=max(1.0, float(self.devicePixelRatioF())),
        )
        if pixmap is None:
            well.setProperty("fallback", True)
            stack.setCurrentWidget(self.qr_fallback)
        else:
            self.qr_image.setPixmap(pixmap)
            stack.setCurrentWidget(self.qr_image)
            self.qr_available = True
        layout.addWidget(well, 0, Qt.AlignmentFlag.AlignHCenter)
        self.qr_well = well

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addStretch(1)
        self.close_button = QPushButton("关闭", card)
        self.close_button.setObjectName("licenseContactClose")
        self.close_button.setAccessibleName("关闭联系授权方")
        self.close_button.setFixedSize(104, 40)
        self.close_button.setDefault(True)
        self.close_button.setAutoDefault(True)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setIcon(
            QIcon(icons.pixmap("x", self._consumer.ink_soft, 14, 1.8))
        )
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        layout.addLayout(row)

        self.setTabOrder(self.close_button, self.close_button)
        self.close_button.setFocus(Qt.FocusReason.OtherFocusReason)


def _load_qr_pixmap(
    path: str,
    *,
    expected_sha256: str,
    target_dpr: float,
) -> QPixmap | None:
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        return None

    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        return None
    if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
        return None
    if pixmap.size() != CONTACT_QR_SOURCE_SIZE:
        return None

    target_pixels = QSize(
        max(1, round(CONTACT_QR_MAX_LOGICAL.width() * target_dpr)),
        max(1, round(CONTACT_QR_MAX_LOGICAL.height() * target_dpr)),
    )
    scaled = pixmap.scaled(
        target_pixels,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    if scaled.isNull() or scaled.width() <= 0 or scaled.height() <= 0:
        return None
    # Detach the scaled pixels from the platform pixmap cache before assigning
    # a device ratio. This keeps offscreen and Retina paint paths deterministic.
    scaled = QPixmap.fromImage(scaled.toImage())
    scaled.setDevicePixelRatio(target_dpr)
    return scaled


def _build_qss(c: T.ConsumerTheme) -> str:
    return f"""
QDialog#licenseContactDialog {{ background: {c.canvas}; color: {c.ink}; }}
QFrame#licenseContactCard {{
    background: {c.surface_focal}; border: 1px solid {c.line}; border-radius: 18px;
}}
QFrame#licenseContactHeader {{ background: transparent; border: none; }}
QFrame#licenseContactIconWell {{
    background: {c.accent_wash}; border: 1px solid {c.accent_hairline}; border-radius: 12px;
}}
QLabel#licenseContactIcon {{ background: transparent; border: none; }}
QLabel#licenseContactTitle {{
    background: transparent; color: {c.ink}; font-family: "{c.font_display}";
    font-size: 21px; font-weight: 650;
}}
QLabel#licenseContactDescription {{
    background: transparent; color: {c.ink_muted}; font-size: 12px;
}}
QFrame#licenseQrWell {{ border: 1px solid {c.line}; border-radius: 14px; }}
QFrame#licenseQrWell[fallback="false"] {{ background: #FFFFFF; }}
QFrame#licenseQrWell[fallback="true"] {{ background: {c.surface_work}; }}
QLabel#licenseQrImage {{ background: #FFFFFF; border: none; }}
QLabel#licenseQrFallback {{
    background: {c.surface_work}; color: {c.ink_soft}; border: none; font-size: 13px;
}}
QPushButton#licenseContactClose {{
    background: {c.surface}; color: {c.ink}; border: 1px solid {c.line}; border-radius: 10px;
    font-size: 13px; font-weight: 600;
}}
QPushButton#licenseContactClose:hover {{
    background: {c.secondary_hover}; color: {c.ink}; border-color: {c.accent_hairline};
}}
QPushButton#licenseContactClose:pressed {{
    background: {c.accent_wash}; color: {c.accent}; border-color: {c.accent};
}}
QPushButton#licenseContactClose:focus {{
    background: {c.secondary}; border: 2px solid {c.accent_hairline_active};
}}
"""
