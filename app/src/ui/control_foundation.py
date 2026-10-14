"""Lulu consumer control foundation.

Only widgets created from this module receive the ``lulu*`` selectors. Existing
business pages keep their current object-name styles until a later migration.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from . import icons


CONTROL_HEIGHTS = {"sm": 28, "md": 34, "lg": 40}
ICON_SIZES = {"sm": 14, "md": 16, "lg": 16}
BUTTON_ROLES = {"primary", "secondary", "tertiary", "destructive", "icon"}
SURFACE_ROLES = {"page", "section", "card", "well", "popover"}
STATUS_KINDS = {"neutral", "info", "success", "warning", "error"}
CONSUMER_THEME_SCOPES = {"transcriptionWorkbench", "globalShell", "coreWorkspace"}
_repolish_suspensions = 0


@contextmanager
def defer_repolish():
    """Batch construction-time property changes until Qt's first real polish."""
    global _repolish_suspensions
    _repolish_suspensions += 1
    try:
        yield
    finally:
        _repolish_suspensions -= 1


def _repolish(widget) -> None:
    # Dynamic properties set during construction are consumed by Qt's first
    # automatic polish. Forcing an unpolish/polish pair on every not-yet-shown
    # child made a 52-card group pay hundreds of redundant style passes on the
    # main thread. Already-polished live controls still refresh immediately.
    if _repolish_suspensions or not widget.testAttribute(
        Qt.WidgetAttribute.WA_WState_Polished
    ):
        widget.update()
        return
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def _set_property(widget, name: str, value) -> None:
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    _repolish(widget)


def adopt_consumer_theme_scope(widget, scope: str):
    """Explicitly opt one consumer surface into a supported theme scope."""
    if scope not in CONSUMER_THEME_SCOPES:
        raise ValueError(f"unsupported consumer theme scope: {scope}")
    _set_property(widget, "consumerThemeScope", scope)
    return widget




def build_consumer_page_header(
    title: str,
    subtitle: str,
    *,
    trailing: QWidget | None = None,
    parent: QWidget | None = None,
) -> tuple[QFrame, QLabel, QLabel]:
    """Build the same editorial title lockup used by the transcription sample.

    The helper owns presentation only: callers keep their existing title and
    subtitle copy, while the 3px accent rule and spacing stay identical across
    every opted-in core workspace.
    """
    header = QFrame(parent)
    header.setObjectName("consumerPageHeader")
    header.setProperty("luluControl", "pageHeader")
    header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)

    rule = QFrame(header)
    rule.setObjectName("studioTitleRule")
    rule.setFixedSize(3, 46)
    rule.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    rule.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    row.addWidget(rule, 0, Qt.AlignmentFlag.AlignTop)

    copy = QVBoxLayout()
    copy.setContentsMargins(0, 0, 0, 0)
    copy.setSpacing(6)
    title_label = QLabel(title, header)
    title_label.setObjectName("pageTitle")
    title_label.setAccessibleName(title)
    subtitle_label = QLabel(subtitle, header)
    subtitle_label.setObjectName("pageMeta")
    subtitle_label.setWordWrap(True)
    copy.addWidget(title_label)
    copy.addWidget(subtitle_label)
    row.addLayout(copy, 1)

    if trailing is not None:
        row.addWidget(trailing, 0, Qt.AlignmentFlag.AlignTop)
    return header, title_label, subtitle_label


def _adopt_name(widget, canonical_name: str) -> None:
    """Move a migrated widget out of legacy ``#id`` QSS without losing traceability."""
    old_name = widget.objectName()
    if old_name and old_name != canonical_name:
        widget.setProperty("legacyObjectName", old_name)
    widget.setObjectName(canonical_name)


def adopt_button(
    button: QPushButton,
    *,
    role: str = "secondary",
    size: str = "md",
    variant: str = "",
    accessible_name: str | None = None,
    tooltip: str | None = None,
) -> QPushButton:
    """Attach the foundation contract without replacing an existing business widget."""
    if role not in BUTTON_ROLES:
        raise ValueError(f"unsupported button role: {role}")
    if size not in CONTROL_HEIGHTS:
        raise ValueError(f"unsupported button size: {size}")
    _adopt_name(button, "luluButton")
    button.setProperty("luluControl", "button")
    button.setProperty("role", role)
    button.setProperty("controlSize", size)
    button.setProperty("controlVariant", variant)
    button.setProperty("state", "default")
    button.setFixedHeight(CONTROL_HEIGHTS[size])
    button.setIconSize(QSize(ICON_SIZES[size], ICON_SIZES[size]))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    name = (accessible_name or button.text().strip()).strip()
    if role == "icon" and not name:
        raise ValueError("icon button requires an accessible name")
    if name:
        button.setAccessibleName(name)
    if tooltip is not None:
        button.setToolTip(tooltip)
        button.setAccessibleDescription(tooltip)
    _repolish(button)
    return button


def adopt_text_field(field: QLineEdit, *, accessible_name: str) -> QLineEdit:
    _adopt_name(field, "luluTextField")
    field.setProperty("luluControl", "textField")
    field.setProperty("state", "default")
    field.setFixedHeight(38)
    field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    field.setAccessibleName(accessible_name)
    _repolish(field)
    return field


class _CheckBoxFocusAdapter(QObject):
    """Keep keyboard focus separate from a checkbox's checked state."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.FocusIn:
            keyboard = event.reason() in {
                Qt.FocusReason.TabFocusReason,
                Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason,
            }
            _set_property(watched, "keyboardFocus", keyboard)
        elif event.type() == QEvent.Type.FocusOut:
            _set_property(watched, "keyboardFocus", False)
        elif event.type() == QEvent.Type.MouseButtonPress:
            _set_property(watched, "keyboardFocus", False)
        return False


def adopt_checkbox(
    checkbox: QCheckBox,
    *,
    accessible_name: str | None = None,
) -> QCheckBox:
    """Adopt a legacy QCheckBox without changing its value or signals."""
    from . import theme as T

    _adopt_name(checkbox, "luluCheckBox")
    checkbox.setProperty("luluControl", "checkbox")
    checkbox.setProperty("keyboardFocus", False)
    checkbox.setFixedHeight(28)
    checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
    checkbox.setAccessibleName(accessible_name or checkbox.text())
    checkbox.setStyleSheet(
        "QCheckBox#luluCheckBox{border:1px solid transparent;border-radius:7px;}"
        f"QCheckBox#luluCheckBox[keyboardFocus=\"true\"]{{"
        f"border-color:{T.INK_FAINT};}}"
    )
    adapter = getattr(checkbox, "_lulu_checkbox_focus_adapter", None)
    if adapter is None:
        adapter = _CheckBoxFocusAdapter(checkbox)
        checkbox._lulu_checkbox_focus_adapter = adapter
        checkbox.installEventFilter(adapter)
    _repolish(checkbox)
    return checkbox


class _SelectChevron(QLabel):
    """Keep the foundation caret visible on an adopted business QComboBox."""

    def __init__(self, combo: QComboBox):
        super().__init__(combo)
        self.setObjectName("luluSelectChevron")
        self.setFixedSize(14, 14)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        from . import theme as T
        self.setPixmap(icons.pixmap("caret_down", T.INK_FAINT, 14))
        combo.installEventFilter(self)
        self._sync_geometry()

    def _sync_geometry(self) -> None:
        combo = self.parentWidget()
        if combo is None:
            return
        self.move(max(0, combo.width() - 26), max(0, (combo.height() - self.height()) // 2))
        self.raise_()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            self._sync_geometry()
        return False


def adopt_select_field(combo: QComboBox, *, accessible_name: str) -> QComboBox:
    """Adopt an existing QComboBox without replacing its items, signals or value."""
    _adopt_name(combo, "luluSelectField")
    combo.setProperty("luluControl", "selectField")
    combo.setProperty("state", "default")
    combo.setFixedHeight(38)
    combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    combo.setAccessibleName(accessible_name)
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(12)
    chevron = getattr(combo, "_lulu_select_chevron", None)
    if chevron is None:
        chevron = _SelectChevron(combo)
        combo._lulu_select_chevron = chevron

    def _sync_tooltip(text: str) -> None:
        combo.setToolTip(str(text or ""))

    combo.currentTextChanged.connect(_sync_tooltip)
    _sync_tooltip(combo.currentText())
    _repolish(combo)
    return combo


def adopt_segment(container: QFrame, buttons, *, accessible_name: str) -> QFrame:
    _adopt_name(container, "luluSegment")
    container.setProperty("luluControl", "segment")
    container.setFixedHeight(36)
    container.setAccessibleName(accessible_name)
    layout = container.layout()
    if layout is not None:
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(3)
    for button in buttons:
        _adopt_name(button, "luluSegmentItem")
        button.setProperty("luluControl", "segmentItem")
        button.setFixedHeight(30)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(f"{accessible_name}：{button.text()}")
        _repolish(button)
    _repolish(container)
    return container


def adopt_surface(widget: QFrame, surface: str, *, variant: str = "") -> QFrame:
    if surface not in SURFACE_ROLES:
        raise ValueError(f"unsupported surface: {surface}")
    _adopt_name(widget, "luluSurface")
    widget.setProperty("luluControl", "surface")
    widget.setProperty("surface", surface)
    widget.setProperty("surfaceVariant", variant)
    _repolish(widget)
    return widget


class PaperTextureFrame(QFrame):
    """Paint the sample's paper grain under child controls, never over their text."""

    _texture: QPixmap | None = None
    _fabric_texture: QPixmap | None = None

    def __init__(
        self,
        parent=None,
        *,
        strength: float = 0.0,
        corner_radius: float = 0.0,
        layer: str = "canvas",
        base_color: str = "",
    ):
        super().__init__(parent)
        self._paper_texture_strength = max(0.0, min(1.0, float(strength)))
        self._paper_texture_radius = max(0.0, float(corner_radius))
        self._paper_base_color = QColor(str(base_color or ""))
        self.setProperty("paperTextureLayer", layer)
        self.setProperty("paperTextureStrength", self._paper_texture_strength)
        self.setProperty("paperTextureRadius", self._paper_texture_radius)
        self.setProperty(
            "paperBaseColor",
            self._paper_base_color.name().upper()
            if self._paper_base_color.isValid()
            else "",
        )

    @classmethod
    def texture_path(cls) -> Path:
        return Path(__file__).resolve().parent / "assets" / "paper_texture.png"

    @classmethod
    def texture_pixmap(cls) -> QPixmap:
        if cls._texture is None:
            cls._texture = QPixmap(str(cls.texture_path()))
        return cls._texture

    @classmethod
    def fabric_texture_path(cls) -> Path:
        return Path(__file__).resolve().parent / "assets" / "model_tag_fabric_texture.png"

    @classmethod
    def fabric_texture_pixmap(cls) -> QPixmap:
        if cls._fabric_texture is None:
            cls._fabric_texture = QPixmap(str(cls.fabric_texture_path()))
        return cls._fabric_texture

    def set_paper_texture_strength(self, strength: float) -> None:
        normalized = max(0.0, min(1.0, float(strength)))
        if self._paper_texture_strength == normalized:
            return
        self._paper_texture_strength = normalized
        self.setProperty("paperTextureStrength", self._paper_texture_strength)
        self.update()

    def set_paper_base_color(self, color: str) -> None:
        candidate = QColor(str(color or ""))
        normalized = candidate.name().upper() if candidate.isValid() else ""
        if self.property("paperBaseColor") == normalized:
            return
        self._paper_base_color = candidate
        self.setProperty("paperBaseColor", normalized)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._paper_base_color.isValid():
            base_painter = QPainter(self)
            if self._paper_texture_radius > 0:
                base_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                base_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
                base_path = QPainterPath()
                base_path.addRoundedRect(
                    base_rect,
                    self._paper_texture_radius,
                    self._paper_texture_radius,
                )
                base_painter.fillPath(base_path, self._paper_base_color)
            else:
                base_painter.fillRect(event.rect(), self._paper_base_color)
            base_painter.end()
        super().paintEvent(event)
        fabric_tag_enabled = bool(self.property("fabricTag"))
        texture = self.fabric_texture_pixmap() if fabric_tag_enabled else self.texture_pixmap()
        texture_enabled = self._paper_texture_strength > 0.0 and not texture.isNull()
        anchor_enabled = bool(self.property("paperAccentAnchor"))
        layered_tag_enabled = bool(self.property("paperTagLayered"))
        if (
            not texture_enabled
            and not anchor_enabled
            and not layered_tag_enabled
            and not fabric_tag_enabled
        ):
            return
        painter = QPainter(self)

        def _tag_divider_y(inner_rect: QRectF) -> float:
            divider_ratio = max(
                0.35,
                min(0.72, float(self.property("paperTagDividerRatio") or 0.64)),
            )
            return inner_rect.top() + inner_rect.height() * divider_ratio

        if layered_tag_enabled:
            upper_color = QColor(str(self.property("paperTagUpperColor") or ""))
            if upper_color.isValid():
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                inner_inset = float(self.property("paperTagInnerInset") or 4.0)
                inner_rect = QRectF(self.rect()).adjusted(
                    inner_inset,
                    inner_inset,
                    -inner_inset,
                    -inner_inset,
                )
                inner_radius = max(2.0, self._paper_texture_radius - inner_inset)
                inner_clip = QPainterPath()
                inner_clip.addRoundedRect(inner_rect, inner_radius, inner_radius)
                painter.setClipPath(inner_clip)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                painter.setOpacity(float(self.property("paperTagUpperOpacity") or 0.76))
                divider_y = _tag_divider_y(inner_rect)
                painter.fillRect(
                    QRectF(
                        inner_rect.left(),
                        inner_rect.top(),
                        inner_rect.width(),
                        divider_y - inner_rect.top(),
                    ),
                    upper_color,
                )
                painter.restore()

        if texture_enabled:
            painter.save()
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform,
                fabric_tag_enabled,
            )
            if self._paper_texture_radius > 0:
                # Align the clip to the device-pixel centre. The old 1px inset left
                # a bright ring between the paper grain and the rounded QSS border.
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                clip_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
                clip = QPainterPath()
                clip.addRoundedRect(
                    clip_rect,
                    self._paper_texture_radius,
                    self._paper_texture_radius,
                )
                painter.setClipPath(clip)
            else:
                painter.setClipRect(event.rect())
            # A low-opacity multiply keeps the fibre relief visible on pale paper.
            # Soft-light made a mid-grey texture mathematically near-neutral and the
            # material disappeared at normal screenshot scale.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            painter.setOpacity(self._paper_texture_strength)
            if fabric_tag_enabled:
                # The model tag is smaller than a texture tile. Scaling one clean
                # material scan avoids seams and keeps its dense pile legible.
                painter.drawPixmap(self.rect(), texture, texture.rect())
            else:
                painter.drawTiledPixmap(self.rect(), texture)
            painter.restore()

        if fabric_tag_enabled:
            edge_color = QColor(str(self.property("fabricEdgeColor") or ""))
            stitch_color = QColor(str(self.property("fabricStitchColor") or ""))
            accent_color = QColor(str(self.property("fabricLabelColor") or ""))
            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            outer_rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
            if edge_color.isValid():
                painter.setBrush(Qt.BrushStyle.NoBrush)
                edge_pen = QPen(edge_color)
                edge_pen.setWidthF(2.0)
                painter.setPen(edge_pen)
                painter.setOpacity(float(self.property("fabricEdgeOpacity") or 0.42))
                painter.drawRoundedRect(
                    outer_rect,
                    max(3.0, self._paper_texture_radius - 1.5),
                    max(3.0, self._paper_texture_radius - 1.5),
                )
            if stitch_color.isValid():
                stitch_pen = QPen(stitch_color)
                stitch_pen.setWidthF(1.0)
                stitch_pen.setStyle(Qt.PenStyle.DotLine)
                stitch_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(stitch_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setOpacity(float(self.property("fabricStitchOpacity") or 0.52))
                stitch_rect = QRectF(self.rect()).adjusted(5.5, 5.5, -5.5, -5.5)
                painter.drawRoundedRect(
                    stitch_rect,
                    max(2.0, self._paper_texture_radius - 5.0),
                    max(2.0, self._paper_texture_radius - 5.0),
                )
            if accent_color.isValid():
                # Five short diagonal stitches carry the theme colour as yarn
                # woven into the blanket binding. This stays legible at sidebar
                # scale without reintroducing a pasted-on blue rectangle.
                label_opacity = float(self.property("fabricLabelOpacity") or 0.52)
                thread_pen = QPen(accent_color)
                thread_pen.setWidthF(1.15)
                thread_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(thread_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setOpacity(label_opacity)
                for index in range(5):
                    x = 8.5 + index * 3.4
                    painter.drawLine(
                        QPointF(x, 12.8),
                        QPointF(x + 2.0, 9.2),
                    )
            painter.restore()

        if layered_tag_enabled:
            line_color = QColor(str(self.property("paperTagLineColor") or ""))
            accent_color = QColor(str(self.property("paperTagAccentColor") or ""))
            inner_inset = float(self.property("paperTagInnerInset") or 4.0)
            inner_rect = QRectF(self.rect()).adjusted(
                inner_inset + 0.5,
                inner_inset + 0.5,
                -inner_inset - 0.5,
                -inner_inset - 0.5,
            )
            divider_y = _tag_divider_y(inner_rect)
            painter.save()
            painter.setClipping(False)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if line_color.isValid():
                painter.setOpacity(float(self.property("paperTagLineOpacity") or 0.72))
                line_pen = QPen(line_color)
                line_pen.setWidthF(1.0)
                painter.setPen(line_pen)
                inner_radius = max(2.0, self._paper_texture_radius - inner_inset)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(inner_rect, inner_radius, inner_radius)
                painter.drawLine(
                    QPointF(inner_rect.left() + 1.0, divider_y),
                    QPointF(inner_rect.right() - 1.0, divider_y),
                )
            if accent_color.isValid():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(accent_color)
                painter.setOpacity(float(self.property("paperTagAccentOpacity") or 0.82))
                accent_width = float(self.property("paperTagAccentWidth") or 9.0)
                accent_height = min(
                    float(self.property("paperTagAccentHeight") or 18.0),
                    max(8.0, divider_y - inner_rect.top() - 8.0),
                )
                accent_rect = QRectF(
                    inner_rect.left() + 6.0,
                    inner_rect.top() + 5.0,
                    accent_width,
                    accent_height,
                )
                painter.drawRoundedRect(accent_rect, 2.5, 2.5)
            painter.restore()

        if anchor_enabled:
            color = QColor(str(self.property("paperAccentColor") or ""))
            if color.isValid():
                painter.save()
                painter.setClipping(False)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setOpacity(float(self.property("paperAccentOpacity") or 1.0))
                pen = QPen(color)
                pen.setWidthF(float(self.property("paperAccentWidth") or 2.0))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                x = float(self.property("paperAccentInset") or 8.0)
                vertical_pad = float(self.property("paperAccentVerticalPad") or 13.0)
                painter.drawLine(
                    QPointF(x, vertical_pad),
                    QPointF(x, max(vertical_pad, self.height() - vertical_pad)),
                )
                painter.restore()


def adopt_consumer_hover_motion(widget: QFrame, *, color: str, duration_ms: int = 160) -> QFrame:
    """声明样板 hover 语义；实际反馈仅由范围化 QSS 改色，不扩大绘制边界。"""
    widget.setProperty("consumerMotion", "colorOnly")
    widget.setProperty("consumerMotionDuration", max(0, int(duration_ms)))
    widget.setProperty("consumerMotionColor", color)
    return widget


def adopt_nav_row(
    widget: QFrame,
    *,
    selected: bool = False,
    accessible_name: str = "",
    on_activate=None,
) -> QFrame:
    _adopt_name(widget, "luluNavRow")
    widget.setProperty("luluControl", "navRow")
    widget.setProperty("selected", selected)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if accessible_name:
        widget.setAccessibleName(accessible_name)
    if on_activate is not None:
        widget.mousePressEvent = lambda event: on_activate()

        def _key_press(event: QKeyEvent) -> None:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                on_activate()
                event.accept()
                return
            QFrame.keyPressEvent(widget, event)

        widget.keyPressEvent = _key_press
    _repolish(widget)
    return widget


def adopt_badge(widget: QFrame, *, kind: str = "info") -> QFrame:
    if kind not in STATUS_KINDS:
        raise ValueError(f"unsupported badge kind: {kind}")
    _adopt_name(widget, "luluBadge")
    widget.setProperty("luluControl", "badge")
    widget.setProperty("statusKind", kind)
    widget.setFixedHeight(22)
    _repolish(widget)
    return widget


def set_status_kind(widget, kind: str, *, control: str = "statusText"):
    if kind not in STATUS_KINDS:
        raise ValueError(f"unsupported status kind: {kind}")
    canonical_names = {
        "badge": "luluBadge",
        "statusIcon": "luluStatusIcon",
        "statusDot": "luluStatusDot",
        "statusText": "luluStatusText",
    }
    canonical_name = canonical_names.get(control, "luluStatus")
    changed = (
        widget.objectName() != canonical_name
        or widget.property("luluControl") != control
        or widget.property("statusKind") != kind
    )
    if not changed:
        return widget
    _adopt_name(widget, canonical_name)
    widget.setProperty("luluControl", control)
    widget.setProperty("statusKind", kind)
    _repolish(widget)
    return widget


class _StatusTextSemanticWatcher(QLabel):
    """Derive presentation tone from existing user-facing status text only."""

    def __init__(self, status: QLabel):
        super().__init__(status)
        self.hide()
        status.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.Paint, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            text = watched.text().strip()
            if any(word in text for word in ("失败", "错误", "无法")):
                kind = "error"
            elif any(word in text for word in ("完成", "已生成", "已保存")):
                kind = "success"
            elif any(word in text for word in ("正在", "准备", "运行中", "加载")):
                kind = "info"
            else:
                kind = "neutral"
            if watched.property("statusKind") != kind:
                _set_property(watched, "statusKind", kind)
        return False


def track_status_text_semantics(widget: QLabel) -> QLabel:
    if getattr(widget, "_lulu_status_watcher", None) is None:
        widget._lulu_status_watcher = _StatusTextSemanticWatcher(widget)
    return widget


def adopt_progress(widget, *, kind: str = "info"):
    if kind not in STATUS_KINDS:
        raise ValueError(f"unsupported progress kind: {kind}")
    changed = (
        widget.objectName() != "luluProgress"
        or widget.property("luluControl") != "progress"
        or widget.property("statusKind") != kind
    )
    if not changed:
        return widget
    _adopt_name(widget, "luluProgress")
    widget.setProperty("luluControl", "progress")
    widget.setProperty("statusKind", kind)
    _repolish(widget)
    return widget


def set_progress_kind(widget: QProgressBar, kind: str) -> QProgressBar:
    """Change only the semantic paint of an adopted progress track."""
    if kind not in STATUS_KINDS:
        raise ValueError(f"unsupported progress kind: {kind}")
    _set_property(widget, "statusKind", kind)
    return widget


def set_button_loading(button: QPushButton, loading: bool, *, text: str = "正在生成…") -> QPushButton:
    """Give a business-owned button a stable loading presentation.

    Enabled state remains owned by the existing handler.  This adapter only
    swaps paint/text and freezes the widest natural width.
    """
    state = getattr(button, "_lulu_loading_state", None)
    if state is None:
        state = {
            "loading": False,
            "text": button.text(),
            "icon": button.icon(),
            "angle": 0,
            "timer": QTimer(button),
            "stable_width": 0,
        }
        button._lulu_loading_state = state

        def _advance() -> None:
            if not state["loading"]:
                return
            state["angle"] = (int(state["angle"]) - 36) % 360
            from . import theme as T
            role = str(button.property("role") or "secondary")
            color = "#FFFFFF" if role == "primary" else T.INK_FAINT
            size = ICON_SIZES.get(str(button.property("controlSize") or "md"), 16)
            button.setIcon(QIcon(_spinner_pixmap(size, int(state["angle"]), color)))

        state["timer"].setInterval(80)
        state["timer"].timeout.connect(_advance)

    if bool(state["loading"]) == bool(loading):
        return button
    button.ensurePolished()
    state["stable_width"] = max(int(state["stable_width"]), button.sizeHint().width())
    if loading:
        state["loading"] = True
        state["text"] = button.text()
        state["icon"] = button.icon()
        button.setText(text)
        _set_property(button, "state", "loading")
        state["stable_width"] = max(int(state["stable_width"]), button.sizeHint().width())
        button.setMinimumWidth(int(state["stable_width"]))
        state["timer"].start()
        state["timer"].timeout.emit()
    else:
        state["timer"].stop()
        state["loading"] = False
        button.setText(str(state["text"]))
        button.setIcon(state["icon"])
        _set_property(button, "state", "default")
        button.setMinimumWidth(max(int(state["stable_width"]), button.sizeHint().width()))
    return button


def adopt_dialog(dialog: QDialog, *, accessible_name: str) -> QDialog:
    """Apply the consumer modal shell without changing dialog behavior."""
    _adopt_name(dialog, "luluDialog")
    dialog.setProperty("luluControl", "dialog")
    dialog.setAccessibleName(accessible_name)
    dialog.setModal(True)
    dialog.setMinimumWidth(max(420, dialog.minimumWidth()))
    dialog.setMaximumWidth(max(520, dialog.minimumWidth()))
    _repolish(dialog)
    return dialog


def adopt_text_area(widget, *, surface: str = "page", accessible_name: str = ""):
    _adopt_name(widget, "luluTextArea")
    widget.setProperty("luluControl", "textArea")
    widget.setProperty("surface", surface)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    if accessible_name:
        widget.setAccessibleName(accessible_name)
    _repolish(widget)
    return widget


def adopt_action_surface(
    widget: QFrame,
    *,
    surface: str = "well",
    variant: str = "",
    accessible_name: str,
    on_activate,
) -> QFrame:
    """Make an existing source/selection surface keyboard-equivalent to its mouse action."""
    adopt_surface(widget, surface, variant=variant)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setAccessibleName(accessible_name)
    widget.mousePressEvent = lambda _event: on_activate()

    def _key_press(event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            on_activate()
            event.accept()
            return
        QFrame.keyPressEvent(widget, event)

    widget.keyPressEvent = _key_press
    _repolish(widget)
    return widget


def adopt_empty_state(widget):
    _adopt_name(widget, "luluEmptyState")
    widget.setProperty("luluControl", "emptyState")
    _repolish(widget)
    return widget


def adopt_toast(widget):
    _adopt_name(widget, "luluToast")
    widget.setProperty("luluControl", "toast")
    _repolish(widget)
    return widget


def adopt_scroll_area(widget):
    _adopt_name(widget, "luluScrollArea")
    widget.setProperty("luluControl", "scrollArea")
    _repolish(widget)
    return widget


def adopt_select_field_frame(widget: QFrame, *, accessible_name: str) -> QFrame:
    """Style a business-owned select trigger without replacing its value/signals."""
    _adopt_name(widget, "luluSelectFieldFrame")
    widget.setProperty("luluControl", "selectFieldFrame")
    widget.setProperty("state", "default")
    widget.setFixedHeight(38)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setAccessibleName(accessible_name)
    _repolish(widget)
    return widget


def adopt_popover(widget: QFrame, *, accessible_name: str = "") -> QFrame:
    adopt_surface(widget, "popover")
    if accessible_name:
        widget.setAccessibleName(accessible_name)
    return widget


def adopt_list_row(
    widget: QFrame,
    *,
    accessible_name: str = "",
    on_activate=None,
    selected: bool = False,
) -> QFrame:
    """Adopt a scan-friendly list row while preserving its business layout."""
    _adopt_name(widget, "luluListRow")
    widget.setProperty("luluControl", "listRow")
    widget.setProperty("selected", bool(selected))
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if accessible_name:
        widget.setAccessibleName(accessible_name)
    if on_activate is not None:
        widget.mousePressEvent = lambda event: on_activate()

        def _key_press(event: QKeyEvent) -> None:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                on_activate()
                event.accept()
                return
            QFrame.keyPressEvent(widget, event)

        widget.keyPressEvent = _key_press
    _repolish(widget)
    return widget


def adopt_list_widget(widget: QListWidget, *, accessible_name: str) -> QListWidget:
    _adopt_name(widget, "luluList")
    widget.setProperty("luluControl", "list")
    widget.setAccessibleName(accessible_name)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    _repolish(widget)
    return widget


def adopt_table(widget: QTableWidget, *, accessible_name: str) -> QTableWidget:
    """Adopt a business-owned table without changing its columns or item data."""
    _adopt_name(widget, "luluTable")
    widget.setProperty("luluControl", "table")
    widget.setAccessibleName(accessible_name)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setWordWrap(False)
    widget.setShowGrid(False)
    widget.horizontalHeader().setFixedHeight(36)
    widget.verticalHeader().setDefaultSectionSize(40)
    widget.verticalHeader().setMinimumSectionSize(40)
    _repolish(widget)
    return widget


def adopt_bulk_bar(widget: QFrame, *, accessible_name: str) -> QFrame:
    """Keep the bulk-action slot stable while the page owns action visibility."""
    _adopt_name(widget, "luluBulkBar")
    widget.setProperty("luluControl", "bulkBar")
    widget.setAccessibleName(accessible_name)
    widget.setFixedHeight(44)
    _repolish(widget)
    return widget


def adopt_spin_box(widget: QSpinBox, *, accessible_name: str) -> QSpinBox:
    _adopt_name(widget, "luluSpinBox")
    widget.setProperty("luluControl", "spinBox")
    widget.setProperty("state", "default")
    widget.setFixedHeight(38)
    widget.setAccessibleName(accessible_name)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    _repolish(widget)
    return widget


def adopt_slider(
    widget: QSlider,
    *,
    accessible_name: str,
    value_formatter=None,
) -> QSlider:
    """Adopt a business-owned slider without touching its value contract."""
    _adopt_name(widget, "luluSlider")
    widget.setProperty("luluControl", "slider")
    widget.setProperty("state", "default")
    widget.setFixedHeight(28)
    widget.setAccessibleName(accessible_name)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _sync_accessible_value(value: int) -> None:
        shown = value_formatter(value) if value_formatter is not None else str(value)
        widget.setAccessibleDescription(f"当前值：{shown}")

    widget.valueChanged.connect(_sync_accessible_value)
    _sync_accessible_value(widget.value())
    _repolish(widget)
    return widget


def adopt_menu(widget: QMenu, *, accessible_name: str) -> QMenu:
    _adopt_name(widget, "luluSelectMenu")
    widget.setProperty("luluControl", "selectMenu")
    widget.setAccessibleName(accessible_name)
    _repolish(widget)
    return widget


def adopt_divider(widget: QFrame, *, orientation: str = "horizontal") -> QFrame:
    if orientation not in {"horizontal", "vertical"}:
        raise ValueError(f"unsupported divider orientation: {orientation}")
    _adopt_name(widget, "luluDivider")
    widget.setProperty("luluControl", "divider")
    widget.setProperty("orientation", orientation)
    if orientation == "vertical":
        widget.setFixedWidth(1)
        widget.setMinimumHeight(1)
        widget.setMaximumHeight(16777215)
    else:
        widget.setFixedHeight(1)
        widget.setMinimumWidth(1)
        widget.setMaximumWidth(16777215)
    _repolish(widget)
    return widget


def set_control_state(widget, state: str):
    """Expose a visual state without changing a widget's business value."""
    _set_property(widget, "state", state)
    return widget


def set_surface_status(widget, kind: str):
    if kind not in STATUS_KINDS:
        raise ValueError(f"unsupported surface status: {kind}")
    _set_property(widget, "statusKind", kind)
    return widget


def _spinner_pixmap(size: int, angle: int, color: str) -> QPixmap:
    dpr = 2
    pixmap = QPixmap(size * dpr, size * dpr)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color), 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    inset = 2.0
    painter.drawArc(QRectF(inset, inset, size - inset * 2, size - inset * 2), angle * 16, 280 * 16)
    painter.end()
    return pixmap


class LuluButton(QPushButton):
    """Semantic button with stable geometry and optional loading feedback."""

    def __init__(self, text: str, *, role: str = "secondary", size: str = "md", icon: str | None = None, parent=None):
        if role not in BUTTON_ROLES:
            raise ValueError(f"unsupported button role: {role}")
        if size not in CONTROL_HEIGHTS:
            raise ValueError(f"unsupported button size: {size}")
        super().__init__(text, parent)
        self.setObjectName("luluButton")
        self.setProperty("luluControl", "button")
        self.setProperty("role", role)
        self.setProperty("controlSize", size)
        self.setProperty("state", "default")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(CONTROL_HEIGHTS[size])
        self.setIconSize(QSize(ICON_SIZES[size], ICON_SIZES[size]))
        self.setAccessibleName(text)
        self._icon_name = icon
        self._loading = False
        self._loading_angle = 0
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(80)
        self._loading_timer.timeout.connect(self._advance_spinner)
        self._normal_text = text
        self._normal_icon = QIcon()
        self._normal_enabled = True
        self._stable_width = 0
        if icon:
            self.set_icon_name(icon)
        self._freeze_natural_width()

    @property
    def role(self) -> str:
        return str(self.property("role"))

    @property
    def control_size(self) -> str:
        return str(self.property("controlSize"))

    def set_icon_name(self, name: str) -> None:
        self._icon_name = name
        from . import theme as T
        color = "#FFFFFF" if self.role == "primary" else T.INK_SOFT
        self.setIcon(QIcon(icons.pixmap(name, color, ICON_SIZES[self.control_size])))
        self._normal_icon = self.icon()
        self._freeze_natural_width()

    def _freeze_natural_width(self) -> None:
        self.ensurePolished()
        self._stable_width = max(self._stable_width, self.sizeHint().width())
        if self._stable_width > 0:
            self.setMinimumWidth(self._stable_width)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_loading(self, loading: bool, text: str | None = None) -> None:
        if self._loading == loading:
            return
        self._loading = loading
        if loading:
            self._normal_text = self.text()
            self._normal_icon = self.icon()
            self._normal_enabled = self.isEnabled()
            self._freeze_natural_width()
            self.setText(text or "正在处理…")
            self.setEnabled(False)
            _set_property(self, "state", "loading")
            self._loading_timer.start()
            self._advance_spinner()
        else:
            self._loading_timer.stop()
            self.setText(self._normal_text)
            self.setIcon(self._normal_icon)
            self.setEnabled(self._normal_enabled)
            _set_property(self, "state", "default")
        self._freeze_natural_width()

    def _advance_spinner(self) -> None:
        if not self._loading:
            return
        self._loading_angle = (self._loading_angle - 36) % 360
        from . import theme as T
        color = "#FFFFFF" if self.role == "primary" else T.INK_FAINT
        self.setIcon(QIcon(_spinner_pixmap(ICON_SIZES[self.control_size], self._loading_angle, color)))


class LuluIconButton(LuluButton):
    """Square icon action. Tooltip and accessible name are mandatory."""

    def __init__(self, icon: str, *, accessible_name: str, tooltip: str, size: str = "md", parent=None):
        if not accessible_name.strip() or not tooltip.strip():
            raise ValueError("icon button requires accessible_name and tooltip")
        super().__init__("", role="icon", size=size, icon=icon, parent=parent)
        edge = CONTROL_HEIGHTS[size]
        self.setFixedSize(edge, edge)
        self.setToolTip(tooltip)
        self.setAccessibleName(accessible_name)
        self.setAccessibleDescription(tooltip)


class LuluTextField(QLineEdit):
    def __init__(self, *, accessible_name: str, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("luluTextField")
        self.setProperty("luluControl", "textField")
        self.setProperty("state", "default")
        self.setFixedHeight(38)
        self.setPlaceholderText(placeholder)
        self.setAccessibleName(accessible_name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_error(self, message: str | None) -> None:
        _set_property(self, "state", "error" if message else "default")
        self.setAccessibleDescription(message or "")


class LuluSelectField(QComboBox):
    def __init__(self, *, accessible_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("luluSelectField")
        self.setProperty("luluControl", "selectField")
        self.setFixedHeight(38)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(accessible_name)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self._chevron = QLabel(self)
        self._chevron.setFixedSize(14, 14)
        self._chevron.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        from . import theme as T
        self._chevron.setPixmap(icons.pixmap("caret_down", T.INK_FAINT, 14))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._chevron.move(max(0, self.width() - 26), max(0, (self.height() - 14) // 2))
        self._chevron.raise_()


class LuluSegmentedControl(QFrame):
    selectionChanged = Signal(str)

    def __init__(self, options: list[tuple[str, str]], *, accessible_name: str, parent=None):
        super().__init__(parent)
        if not options:
            raise ValueError("segmented control requires at least one option")
        self.setObjectName("luluSegment")
        self.setProperty("luluControl", "segment")
        self.setFixedHeight(36)
        self.setAccessibleName(accessible_name)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(3)
        for index, (value, label) in enumerate(options):
            button = QPushButton(label)
            button.setObjectName("luluSegmentItem")
            button.setProperty("luluControl", "segmentItem")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(f"{accessible_name}：{label}")
            button.setProperty("segmentValue", value)
            button.clicked.connect(lambda _checked=False, v=value: self.selectionChanged.emit(v))
            self._group.addButton(button)
            self._buttons[value] = button
            layout.addWidget(button, 1)
            if index == 0:
                button.setChecked(True)

    def value(self) -> str:
        checked = self._group.checkedButton()
        return str(checked.property("segmentValue")) if checked else ""

    def set_value(self, value: str) -> None:
        if value not in self._buttons:
            raise ValueError(f"unknown segment value: {value}")
        self._buttons[value].setChecked(True)
        self.selectionChanged.emit(value)

    def buttons(self) -> tuple[QPushButton, ...]:
        return tuple(self._buttons.values())


class LuluCheckBox(QCheckBox):
    def __init__(self, text: str, *, accessible_name: str | None = None, parent=None):
        super().__init__(text, parent)
        self.setObjectName("luluCheckBox")
        self.setProperty("luluControl", "checkbox")
        self.setFixedHeight(28)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(accessible_name or text)
        self._keyboard_focus = False

    def set_consumer_accent(self, color: str | None) -> None:
        """Opt one page instance into its scoped accent without changing defaults."""
        self.setProperty("consumerAccent", str(color or ""))
        self.update()

    def _accent_colors(self):
        from . import theme as T
        scoped = QColor(str(self.property("consumerAccent") or ""))
        if scoped.isValid():
            return scoped, scoped
        return QColor(T.TERRA), QColor(T.TERRA_SOFT)

    def sizeHint(self) -> QSize:  # noqa: N802
        width = 32 + self.fontMetrics().horizontalAdvance(self.text())
        return QSize(width, 28)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._keyboard_focus = event.reason() in {
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        }
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._keyboard_focus = False
        self.update()
        super().focusOutEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._keyboard_focus = False
        self.update()
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from . import theme as T
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        indicator = QRectF(5, 5, 18, 18)
        accent, accent_soft = self._accent_colors()
        # A visually consistent placeholder can remain non-interactive while an
        # independent feature TASK supplies its business wiring.  The opt-in
        # property avoids changing disabled checkboxes anywhere else in Lulu.
        visual_enabled = self.isEnabled() or bool(self.property("visualPlaceholder"))
        if self.isChecked():
            border = accent_soft
        elif self.underMouse():
            border = QColor(T.INK_FAINT)
        else:
            border = QColor(T.LINE)
        if not visual_enabled:
            border = QColor(T.LINE_SOFT)
        if self.hasFocus() and self._keyboard_focus:
            painter.setPen(QPen(QColor(T.INK_FAINT), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.75, 1.75, self.width() - 1.5, 24.5), 7, 7)
        painter.setPen(QPen(border, 1))
        painter.setBrush(
            accent
            if self.isChecked() and visual_enabled
            else QColor(T.CARD if visual_enabled else T.BG_DEEP)
        )
        painter.drawRoundedRect(indicator, 4, 4)
        if self.isChecked():
            path = QPainterPath(QPointF(9, 14))
            path.lineTo(QPointF(13, 18))
            path.lineTo(QPointF(20, 10))
            painter.setPen(QPen(QColor("#FFFFFF" if visual_enabled else T.INK_FAINT), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.setPen(QColor(T.INK if visual_enabled else T.INK_FAINT))
        painter.drawText(QRectF(31, 0, max(0, self.width() - 31), self.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        painter.end()


class LuluSwitch(QAbstractButton):
    """38×22 semantic switch using the verified Lulu update-switch geometry."""

    def __init__(self, *, accessible_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("luluSwitch")
        self.setProperty("luluControl", "switch")
        self.setCheckable(True)
        self.setFixedSize(38, 22)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(accessible_name)
        self.toggled.connect(self._sync_accessible_description)
        self._sync_accessible_description(self.isChecked())

    def _sync_accessible_description(self, checked: bool) -> None:
        self.setAccessibleDescription("已开启" if checked else "已关闭")

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from . import theme as T
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        outer = QRectF(1, 1, 36, 20)
        if self.hasFocus():
            painter.setPen(QPen(QColor(T.TERRA_SOFT), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(1, 1, 36, 20), 10, 10)
            outer = QRectF(3, 3, 32, 16)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        if not self.isEnabled():
            track = QColor(T.LINE_SOFT)
            thumb = QColor(T.INK_FAINT)
        elif self.isChecked():
            track = QColor(T.TERRA_DEEP if self.isDown() else T.TERRA)
            thumb = QColor("#FFFFFF")
        else:
            track = QColor(T.LINE if not self.isDown() else T.INK_FAINT)
            thumb = QColor(T.CARD)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(outer, outer.height() / 2, outer.height() / 2)
        diameter = min(16.0, outer.height() - 2)
        x = outer.right() - diameter - 1 if self.isChecked() else outer.left() + 1
        y = outer.center().y() - diameter / 2
        painter.setBrush(thumb)
        painter.drawEllipse(QRectF(x, y, diameter, diameter))
        painter.end()


class LuluSurface(QFrame):
    def __init__(self, surface: str = "card", parent=None):
        if surface not in SURFACE_ROLES:
            raise ValueError(f"unsupported surface: {surface}")
        super().__init__(parent)
        self.setObjectName("luluSurface")
        self.setProperty("luluControl", "surface")
        self.setProperty("surface", surface)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)


def foundation_qss(p) -> str:
    """QSS scoped to the new foundation object names; old pages are untouched."""
    return f"""

/* ===== Lulu consumer control foundation (new components only) ===== */
QWidget#controlGallery, QScrollArea#controlGalleryScroll,
QScrollArea#controlGalleryScroll > QWidget > QWidget {{
    background: {p.BG}; color: {p.INK};
}}
QWidget#controlGallery QLabel {{ color: {p.INK}; background: transparent; }}
QWidget#controlGallery QLabel#pageMeta {{ color: {p.INK_SOFT}; }}
QPushButton[luluControl="button"] {{
    border-radius: 8px; padding: 0 14px; font-size: 12px; font-weight: 500;
    outline: none;
}}
QPushButton[luluControl="button"][controlSize="sm"] {{ padding: 0 10px; }}
QPushButton[luluControl="button"][controlSize="lg"] {{ padding: 0 16px; }}
QPushButton[luluControl="button"][role="primary"] {{ background: {p.TERRA}; color: #FFFFFF; border: 1px solid {p.TERRA}; font-weight: 600; }}
QPushButton[luluControl="button"][role="primary"]:hover, QPushButton[luluControl="button"][role="primary"][demoState="hover"] {{ background: {p.TERRA_DEEP}; border-color: {p.TERRA_DEEP}; }}
QPushButton[luluControl="button"][role="primary"]:pressed, QPushButton[luluControl="button"][role="primary"][demoState="pressed"] {{ background: {p.TERRA_DEEP}; border: 2px solid {p.TERRA_SOFT}; }}
QPushButton[luluControl="button"][role="secondary"] {{ background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; }}
QPushButton[luluControl="button"][role="secondary"]:hover, QPushButton[luluControl="button"][role="secondary"][demoState="hover"] {{ background: {p.TERRA_WASH}; border-color: {p.TERRA_SOFT}; }}
QPushButton[luluControl="button"][role="secondary"]:pressed, QPushButton[luluControl="button"][role="secondary"][demoState="pressed"] {{ background: {p.BG_DEEP}; border-color: {p.TERRA}; }}
QPushButton[luluControl="button"][role="tertiary"] {{ background: transparent; color: {p.INK_SOFT}; border: 1px solid transparent; }}
QPushButton[luluControl="button"][role="tertiary"]:hover {{ background: {p.BG_DEEP}; color: {p.INK}; }}
QPushButton[luluControl="button"][role="destructive"] {{ background: {p.CARD}; color: {p.DANGER}; border: 1px solid {p.DANGER}; }}
QPushButton[luluControl="button"][role="destructive"]:hover {{ background: {p.BG_DEEP}; }}
QPushButton[luluControl="button"][role="icon"] {{ background: transparent; color: {p.INK_SOFT}; border: 1px solid {p.LINE}; padding: 0; }}
QPushButton[luluControl="button"][role="icon"]:hover {{ background: {p.TERRA_WASH}; border-color: {p.TERRA_SOFT}; }}
QPushButton[luluControl="button"]:focus, QPushButton[luluControl="button"][demoState="focus"] {{ border: 2px solid {p.TERRA_SOFT}; }}
QPushButton[luluControl="button"]:disabled {{ background: {p.BG_DEEP}; color: {p.INK_FAINT}; border: 1px solid {p.LINE_SOFT}; }}
QPushButton[luluControl="button"][state="loading"] {{ font-weight: 500; }}
QPushButton[luluControl="button"][role="primary"][state="loading"] {{ background: {p.TERRA}; color: #FFFFFF; border-color: {p.TERRA}; }}
QPushButton[luluControl="button"][role="secondary"][state="loading"] {{ background: {p.CARD}; color: {p.INK_SOFT}; border-color: {p.LINE}; }}
QPushButton[luluControl="button"][controlVariant="pane"] {{ background: transparent; border: 2px solid transparent; border-radius: 7px; padding: 0; }}
QPushButton[luluControl="button"][controlVariant="pane"]:hover {{ background: {p.RAIL_HOVER}; border-color: transparent; }}
QPushButton[luluControl="button"][controlVariant="pane"]:pressed {{ background: {p.BG_DEEP}; border-color: transparent; }}
QPushButton[luluControl="button"][controlVariant="settings"] {{ background: transparent; border: 2px solid transparent; border-radius: 7px; padding: 0; }}
QPushButton[luluControl="button"][controlVariant="settings"]:checked {{ background: transparent; border-color: transparent; }}
QPushButton[luluControl="button"][controlVariant="settings"]:hover {{ background: {p.RAIL_HOVER}; border-color: transparent; }}
QPushButton[luluControl="button"][controlVariant="settings"]:pressed, QPushButton[luluControl="button"][controlVariant="settings"]:checked:pressed {{ background: {p.BG_DEEP}; border-color: transparent; }}
QPushButton[luluControl="button"][controlVariant="settings"]:focus, QPushButton[luluControl="button"][controlVariant="settings"][demoState="focus"] {{ background: {p.TERRA_WASH}; border: 2px solid transparent; }}
QPushButton[luluControl="button"][controlVariant="selection"] {{
    background: {p.CARD}; color: {p.INK_SOFT}; border: 1px solid {p.LINE}; padding: 0;
}}
QPushButton[luluControl="button"][controlVariant="selection"]:hover {{ background: {p.TERRA_WASH}; border-color: {p.TERRA_SOFT}; }}
QPushButton[luluControl="button"][controlVariant="selection"]:checked {{ background: {p.TERRA}; color: #FFFFFF; border-color: {p.TERRA}; }}
QPushButton[luluControl="button"][controlVariant="engine"] {{
    background: transparent; color: {p.INK_SOFT}; border: 1px solid transparent;
    border-radius: 6px; text-align: left; padding: 0 12px; font-weight: 500;
}}
QPushButton[luluControl="button"][controlVariant="engine"]:hover {{ background: {p.CARD}; color: {p.INK}; border-color: {p.LINE}; }}
QPushButton[luluControl="button"][controlVariant="engine"]:pressed {{ background: {p.TERRA_WASH}; border-color: {p.TERRA_SOFT}; }}
QPushButton[luluControl="button"][controlVariant="engine"]:checked {{ background: {p.CARD}; color: {p.INK}; border-color: {p.TERRA_SOFT}; font-weight: 600; }}
QPushButton[luluControl="button"][controlVariant="engine"]:focus {{ border: 2px solid {p.TERRA}; }}
QPushButton[luluControl="button"][controlVariant="engine"]:disabled {{ background: transparent; color: {p.INK_FAINT}; border-color: transparent; }}

QLineEdit[luluControl="textField"], QComboBox[luluControl="selectField"] {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px;
    padding: 0 12px; font-size: 12px; selection-background-color: {p.TERRA_WASH};
}}
QLineEdit[luluControl="textField"]:hover, QComboBox[luluControl="selectField"]:hover {{ border-color: {p.INK_FAINT}; }}
QLineEdit[luluControl="textField"]:focus, QComboBox[luluControl="selectField"]:focus,
QLineEdit[luluControl="textField"][demoState="focus"], QComboBox[luluControl="selectField"][demoState="focus"] {{ border: 2px solid {p.TERRA}; }}
QLineEdit[luluControl="textField"][state="error"] {{ border: 1px solid {p.DANGER}; }}
QLineEdit[luluControl="textField"]:disabled, QComboBox[luluControl="selectField"]:disabled {{ background: {p.BG_DEEP}; color: {p.INK_FAINT}; border-color: {p.LINE_SOFT}; }}
QLabel#luluErrorText {{ color: {p.DANGER}; font-size: 11px; background: transparent; }}
QComboBox[luluControl="selectField"] {{ padding-right: 34px; }}
QComboBox[luluControl="selectField"]::drop-down {{ width: 30px; border: none; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox[luluControl="selectField"]::down-arrow {{ image: none; width: 0; height: 0; }}
QComboBox[luluControl="selectField"] QAbstractItemView {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px;
    padding: 6px; outline: none; selection-background-color: {p.TERRA_WASH};
}}
QFrame[luluControl="selectFieldFrame"] {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px;
}}
QFrame[luluControl="selectFieldFrame"]:hover {{ background: {p.TERRA_WASH}; border-color: {p.INK_FAINT}; }}
QFrame[luluControl="selectFieldFrame"]:focus {{ border: 2px solid {p.TERRA}; }}
QFrame[luluControl="selectFieldFrame"][pressed="true"] {{ background: {p.BG_DEEP}; border-color: {p.TERRA}; }}
QFrame[luluControl="selectFieldFrame"][menuOpen="true"] {{ border: 2px solid {p.TERRA}; }}
QMenu[luluControl="selectMenu"] {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px; padding: 6px;
}}
QMenu[luluControl="selectMenu"]::item {{ min-height: 30px; padding: 0 12px; border-radius: 6px; }}
QMenu[luluControl="selectMenu"]::item:selected {{ background: {p.TERRA_WASH}; color: {p.INK}; }}

QFrame[luluControl="segment"] {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 10px; }}
QPushButton[luluControl="segmentItem"] {{
    background: {p.BG_DEEP}; color: {p.INK_SOFT}; border: 1px solid {p.LINE};
    border-radius: 7px; padding: 0 12px; font-size: 12px; font-weight: 600;
}}
QPushButton[luluControl="segmentItem"]:hover {{
    background: {p.TERRA_WASH}; color: {p.INK}; border-color: {p.TERRA_SOFT};
}}
QPushButton[luluControl="segmentItem"]:pressed {{
    background: {p.TERRA_DEEP}; color: #FFFFFF; border: 2px solid {p.TERRA_DEEP};
}}
QPushButton[luluControl="segmentItem"]:checked {{
    background: {p.TERRA}; color: #FFFFFF; border: 2px solid {p.TERRA_DEEP}; font-weight: 700;
}}
QPushButton[luluControl="segmentItem"]:checked:pressed {{
    background: {p.TERRA_DEEP}; color: #FFFFFF; border: 2px solid {p.TERRA_DEEP};
}}
QPushButton[luluControl="segmentItem"]:focus {{ border: 2px solid {p.TERRA}; }}
QPushButton[luluControl="segmentItem"]:disabled {{
    background: {p.BG_DEEP}; color: {p.INK_FAINT}; border-color: {p.LINE_SOFT};
}}

QCheckBox#luluCheckBox {{ color: {p.INK}; font-size: 12px; spacing: 8px; }}

QFrame[luluControl="surface"][surface="page"], QFrame[luluControl="surface"][surface="section"] {{ background: transparent; border: none; }}
QFrame[luluControl="surface"][surface="card"] {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 8px; }}
QFrame[luluControl="surface"][surface="well"] {{ background: {p.BG_DEEP}; border: 1px solid {p.LINE_SOFT}; border-radius: 6px; }}
QFrame[luluControl="surface"][surface="popover"] {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 8px; }}
QFrame[luluControl="surface"][surfaceVariant="drop"] {{ background: {p.DROP_BG}; border: 1px dashed {p.TERRA_SOFT}; border-radius: 8px; }}
QFrame[luluControl="surface"][surfaceVariant="drop"]:hover {{ background: {p.TERRA_WASH}; border-color: {p.TERRA}; }}
QFrame[luluControl="surface"][surfaceVariant="uploadHero"] {{ background: {p.DROP_BG}; border: 2px dashed {p.TERRA_SOFT}; border-radius: 14px; }}
QFrame[luluControl="surface"][surfaceVariant="uploadHero"]:hover {{ background: {p.TERRA_WASH}; border: 2px solid {p.TERRA}; }}
QFrame[luluControl="surface"][surfaceVariant="uploadHero"]:focus {{ background: {p.TERRA_WASH}; border: 2px solid {p.TERRA}; }}
QFrame[luluControl="surface"][surfaceVariant="uploadGlyph"] {{ background: {p.CARD}; border: 1px solid {p.TERRA_SOFT}; border-radius: 11px; }}
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"] {{
    background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 14px;
}}
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"]:hover {{
    background: {p.TERRA_WASH}; border-color: {p.TERRA_SOFT};
}}
QFrame[luluControl="surface"][surfaceVariant="sourcePicker"]:focus {{
    background: {p.CARD}; border: 2px solid {p.TERRA};
}}
QFrame[luluControl="surface"][surfaceVariant="sourcePickerGlyph"] {{
    background: {p.TERRA_WASH}; border: 1px solid {p.TERRA_SOFT}; border-radius: 16px;
}}
QFrame[luluControl="surface"][surfaceVariant="optionBar"] {{ background: {p.BG_DEEP}; border: 1px solid {p.LINE_SOFT}; border-radius: 10px; }}
QFrame[luluControl="surface"][surfaceVariant="recordGlyph"] {{ background: {p.TERRA_WASH}; border: 1px solid {p.TERRA_SOFT}; border-radius: 18px; }}
QFrame[luluControl="surface"][surfaceVariant="recordingCard"] {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 12px; }}
QFrame[luluControl="surface"][surfaceVariant="recordControls"] {{ background: {p.BG_DEEP}; border: 1px solid {p.LINE_SOFT}; border-radius: 10px; }}
QFrame[luluControl="surface"][surfaceVariant="resultCanvas"] {{
    background: {p.CARD}; border: 1px solid {p.LINE_SOFT}; border-radius: 18px;
}}
QFrame[luluControl="surface"][surfaceVariant="resultBody"] {{ background: transparent; border: none; }}
QFrame[luluControl="surface"][surfaceVariant="resultToolbar"] {{
    background: {p.BG_DEEP}; border: none; border-radius: 12px;
}}
QFrame[luluControl="surface"][surfaceVariant="resultGlyph"] {{
    background: {p.TERRA_WASH}; border: none; border-radius: 22px;
}}
QLabel#recordTitle {{ color: {p.INK}; font-size: 14px; font-weight: 650; background: transparent; }}
QLabel#sourcePickerTitle {{ color: {p.INK}; font-size: 14px; font-weight: 650; background: transparent; }}
QLabel#sourcePickerMeta {{ color: {p.INK_SOFT}; font-size: 12px; background: transparent; }}
QLabel#sourcePickerAction {{ color: {p.TERRA}; font-size: 12px; font-weight: 650; background: transparent; }}
QLabel#recordRowLabel {{ color: {p.INK_SOFT}; font-size: 12px; font-weight: 600; background: transparent; }}
QFrame[luluControl="surface"][surfaceVariant="taskQueue"] {{ background: {p.CARD}; border: 1px solid {p.LINE}; border-radius: 12px; }}
QFrame[luluControl="surface"][surfaceVariant="taskQueue"][focusMode="true"] {{ border-color: {p.TERRA_SOFT}; }}
QFrame[luluControl="surface"][surfaceVariant="selected"] {{ background: {p.TERRA_WASH}; border-color: {p.TERRA_SOFT}; }}
QFrame[luluControl="surface"][surfaceVariant="status"][statusKind="neutral"] {{ background: {p.BG_DEEP}; border-color: {p.LINE_SOFT}; }}
QFrame[luluControl="surface"][surfaceVariant="status"][statusKind="info"] {{ background: {p.BLUE_WASH}; border-color: {p.BLUE}; }}
QFrame[luluControl="surface"][surfaceVariant="status"][statusKind="success"] {{ background: {p.GREEN_WASH}; border-color: {p.GREEN}; }}
QFrame[luluControl="surface"][surfaceVariant="status"][statusKind="warning"] {{ background: {p.AMBER_WASH}; border-color: {p.AMBER}; }}
QFrame[luluControl="surface"][surfaceVariant="status"][statusKind="error"] {{ background: {p.BG_DEEP}; border-color: {p.DANGER}; }}

QFrame[luluControl="listRow"] {{ background: transparent; border: none; border-bottom: 1px solid {p.LINE_SOFT}; border-radius: 0; }}
QFrame[luluControl="listRow"]:hover {{ background: {p.NAV_HOVER}; }}
QFrame[luluControl="listRow"]:focus {{ border: 2px solid {p.TERRA_SOFT}; }}
QFrame[luluControl="listRow"][selected="true"] {{ background: {p.TERRA_WASH}; border-left: 2px solid {p.TERRA}; }}
QFrame[luluControl="listRow"][dropActive="true"] {{ background: {p.TERRA_WASH}; border: 1px solid {p.TERRA}; }}
QListWidget[luluControl="list"] {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px;
    padding: 4px; outline: none;
}}
QListWidget[luluControl="list"]::item {{ min-height: 34px; border-radius: 6px; padding: 0 8px; }}
QListWidget[luluControl="list"]::item:hover {{ background: {p.NAV_HOVER}; }}
QListWidget[luluControl="list"]::item:selected {{ background: {p.TERRA_WASH}; color: {p.INK}; }}
QFrame[luluControl="bulkBar"] {{
    background: {p.BG_DEEP}; border: 1px solid {p.LINE_SOFT}; border-radius: 8px;
}}
QTableWidget[luluControl="table"] {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px;
    gridline-color: transparent; outline: none; selection-background-color: {p.TERRA_WASH};
    selection-color: {p.INK};
}}
QTableWidget[luluControl="table"] QHeaderView::section {{
    background: {p.BG_DEEP}; color: {p.INK_SOFT}; border: none;
    border-bottom: 1px solid {p.LINE_SOFT}; padding: 0 10px; font-size: 11px; font-weight: 600;
}}
QTableWidget[luluControl="table"] QHeaderView::section:hover {{ color: {p.TERRA}; background: {p.TERRA_WASH}; }}
QTableWidget[luluControl="table"]::item {{
    color: {p.INK}; border: none; border-bottom: 1px solid {p.LINE_SOFT}; padding: 0 10px;
}}
QTableWidget[luluControl="table"]::item:hover {{ background: {p.NAV_HOVER}; }}
QTableWidget[luluControl="table"]::item:selected {{ background: {p.TERRA_WASH}; color: {p.INK}; }}
QTableWidget[luluControl="table"]:focus {{ border: 2px solid {p.TERRA_SOFT}; }}

QSpinBox[luluControl="spinBox"] {{
    background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px; padding: 0 10px;
}}
QSpinBox[luluControl="spinBox"]:hover {{ border-color: {p.INK_FAINT}; }}
QSpinBox[luluControl="spinBox"]:focus {{ border: 2px solid {p.TERRA}; }}
QSlider#luluSlider {{ outline: none; }}
QSlider#luluSlider::groove:horizontal {{
    background: {p.LINE_SOFT}; height: 4px; border-radius: 2px;
}}
QSlider#luluSlider::sub-page:horizontal {{
    background: {p.TERRA}; height: 4px; border-radius: 2px;
}}
QSlider#luluSlider::add-page:horizontal {{
    background: {p.LINE_SOFT}; height: 4px; border-radius: 2px;
}}
QSlider#luluSlider::handle:horizontal {{
    background: {p.CARD}; border: 2px solid {p.TERRA}; width: 14px; height: 14px;
    margin: -6px 0; border-radius: 8px;
}}
QSlider#luluSlider::handle:horizontal:hover {{ border-color: {p.TERRA_DEEP}; }}
QSlider#luluSlider::handle:horizontal:pressed {{ background: {p.TERRA_WASH}; border-color: {p.TERRA_DEEP}; }}
QSlider#luluSlider::handle:horizontal:focus {{ border: 3px solid {p.TERRA_SOFT}; }}
QSlider#luluSlider::groove:horizontal:disabled,
QSlider#luluSlider::add-page:horizontal:disabled,
QSlider#luluSlider::sub-page:horizontal:disabled {{ background: {p.LINE_SOFT}; }}
QSlider#luluSlider::handle:horizontal:disabled {{ background: {p.BG_DEEP}; border-color: {p.INK_FAINT}; }}
QFrame[luluControl="divider"] {{ background: {p.LINE_SOFT}; border: none; }}
QFrame[luluControl="divider"][orientation="horizontal"] {{ max-height: 1px; min-height: 1px; }}
QFrame[luluControl="divider"][orientation="vertical"] {{ max-width: 1px; min-width: 1px; }}

QFrame[luluControl="navRow"] {{ background: transparent; border: none; border-left: 2px solid transparent; border-radius: 8px; }}
QFrame[luluControl="navRow"]:hover {{ background: {p.NAV_HOVER}; }}
QFrame[luluControl="navRow"][selected="true"] {{ background: {p.CARD}; border-left-color: {p.TERRA}; }}
QFrame[luluControl="navRow"]:focus {{ border: 2px solid {p.TERRA_SOFT}; }}

QFrame[luluControl="badge"] {{ border: none; border-radius: 6px; }}
QFrame[luluControl="badge"][statusKind="neutral"] {{ background: {p.BG_DEEP}; }}
QFrame[luluControl="badge"][statusKind="info"] {{ background: {p.BLUE_WASH}; }}
QFrame[luluControl="badge"][statusKind="success"] {{ background: {p.GREEN_WASH}; }}
QFrame[luluControl="badge"][statusKind="warning"] {{ background: {p.AMBER_WASH}; }}
QFrame[luluControl="badge"][statusKind="error"] {{ background: {p.BG_DEEP}; border: 1px solid {p.DANGER}; }}
QFrame[luluControl="badge"][statusKind="neutral"] QLabel {{ color: {p.INK_SOFT}; }}
QFrame[luluControl="badge"][statusKind="info"] QLabel {{ color: {p.BLUE}; }}
QFrame[luluControl="badge"][statusKind="success"] QLabel {{ color: {p.GREEN}; }}
QFrame[luluControl="badge"][statusKind="warning"] QLabel {{ color: {p.AMBER}; }}
QFrame[luluControl="badge"][statusKind="error"] QLabel {{ color: {p.DANGER}; }}

QFrame[luluControl="statusIcon"] {{ border: none; border-radius: 8px; }}
QFrame[luluControl="statusIcon"][statusKind="neutral"] {{ background: {p.BG_DEEP}; }}
QFrame[luluControl="statusIcon"][statusKind="info"] {{ background: {p.BLUE_WASH}; }}
QFrame[luluControl="statusIcon"][statusKind="success"] {{ background: {p.GREEN_WASH}; }}
QFrame[luluControl="statusIcon"][statusKind="warning"] {{ background: {p.AMBER_WASH}; }}
QFrame[luluControl="statusIcon"][statusKind="error"] {{ background: {p.BG_DEEP}; }}
QLabel[luluControl="statusDot"] {{ border-radius: 3px; }}
QLabel[luluControl="statusText"] {{ font-size: 11px; font-weight: 500; background: transparent; }}
QLabel[luluControl="statusDot"][statusKind="neutral"] {{ background: {p.INK_FAINT}; }}
QLabel[luluControl="statusText"][statusKind="neutral"] {{ color: {p.INK_FAINT}; }}
QLabel[luluControl="statusDot"][statusKind="info"] {{ background: {p.BLUE}; }}
QLabel[luluControl="statusText"][statusKind="info"] {{ color: {p.BLUE}; }}
QLabel[luluControl="statusDot"][statusKind="success"] {{ background: {p.GREEN}; }}
QLabel[luluControl="statusText"][statusKind="success"] {{ color: {p.GREEN}; }}
QLabel[luluControl="statusDot"][statusKind="warning"] {{ background: {p.AMBER}; }}
QLabel[luluControl="statusText"][statusKind="warning"] {{ color: {p.AMBER}; }}
QLabel[luluControl="statusDot"][statusKind="error"] {{ background: {p.DANGER}; }}
QLabel[luluControl="statusText"][statusKind="error"] {{ color: {p.DANGER}; }}

QProgressBar[luluControl="progress"] {{ background: {p.BG_DEEP}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar[luluControl="progress"]::chunk {{ background: {p.TERRA}; border-radius: 3px; }}
QProgressBar[luluControl="progress"][statusKind="neutral"]::chunk {{ background: {p.INK_FAINT}; }}
QProgressBar[luluControl="progress"][statusKind="info"]::chunk {{ background: {p.BLUE}; }}
QProgressBar[luluControl="progress"][statusKind="success"]::chunk {{ background: {p.GREEN}; }}
QProgressBar[luluControl="progress"][statusKind="warning"]::chunk {{ background: {p.AMBER}; }}
QProgressBar[luluControl="progress"][statusKind="error"]::chunk {{ background: {p.DANGER}; }}

QDialog[luluControl="dialog"] {{
    background: {p.BG}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px;
}}
QDialog[luluControl="dialog"] QLabel {{ background: transparent; color: {p.INK}; }}
QDialog[luluControl="dialog"] QLabel#pageMeta {{ color: {p.INK_SOFT}; }}

QLineEdit[luluControl="textField"], QPlainTextEdit[luluControl="textArea"], QTextBrowser[luluControl="textArea"] {{ outline: none; }}
QPlainTextEdit[luluControl="textArea"][surface="card"] {{ background: {p.CARD}; color: {p.INK}; border: 1px solid {p.LINE}; border-radius: 8px; padding: 12px; font-size: 12px; selection-background-color: {p.TERRA_WASH}; }}
QPlainTextEdit[luluControl="textArea"][surface="card"][surfaceVariant="promptInput"] {{ padding: 8px 12px; }}
QPlainTextEdit[luluControl="textArea"][surface="page"] {{ background: {p.BG}; color: {p.INK}; border: none; border-radius: 0; padding: 8px; font-size: 14px; selection-background-color: {p.TERRA_WASH}; }}
QTextBrowser[luluControl="textArea"][surface="page"] {{ background: {p.BG}; color: {p.INK}; border: none; border-radius: 0; padding: 0; font-size: 13px; selection-background-color: {p.TERRA_WASH}; }}
QPlainTextEdit[luluControl="textArea"]:focus, QTextBrowser[luluControl="textArea"]:focus {{ border-color: {p.TERRA}; }}
QPlainTextEdit[luluControl="textArea"][state="running"] {{ border-color: {p.BLUE}; }}
QPlainTextEdit[luluControl="textArea"][state="success"] {{ border-color: {p.GREEN}; }}
QPlainTextEdit[luluControl="textArea"][state="error"] {{ border-color: {p.DANGER}; }}
QPlainTextEdit[luluControl="textArea"][surfaceVariant="resultBody"] {{
    background: transparent; border: 1px solid transparent; border-radius: 12px;
    padding: 14px 10px; font-size: 13px;
}}
QPlainTextEdit[luluControl="textArea"][surfaceVariant="resultBody"]:focus {{
    background: {p.BG_DEEP}; border-color: {p.TERRA_SOFT};
}}
QPlainTextEdit[luluControl="textArea"][surfaceVariant="resultBody"][state="running"] {{
    background: {p.BG_DEEP}; border-color: {p.BLUE};
}}
QPlainTextEdit[luluControl="textArea"][surfaceVariant="resultBody"][state="success"] {{
    background: transparent; border-color: transparent;
}}
QPlainTextEdit[luluControl="textArea"][surfaceVariant="resultBody"][state="error"] {{
    background: {p.BG_DEEP}; border-color: {p.DANGER};
}}
QLabel[luluControl="emptyState"] {{ color: {p.INK_FAINT}; font-size: 12px; background: transparent; }}
QLabel[luluControl="toast"] {{ background: {p.TOOLTIP_BG}; color: {p.TOOLTIP_FG}; border: none; border-radius: 8px; padding: 8px 12px; font-size: 12px; }}
QScrollArea[luluControl="scrollArea"] {{ background: transparent; border: none; }}
"""
