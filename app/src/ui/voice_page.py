"""文案配音页：统一的消费级工作台，承载四种已有配音引擎。

页面固定遵循「配音文案 → 选择音色 → 表达与输出 → 生成结果」的使用顺序；
模型差异仅在必要的进阶控件中呈现，所有生成载荷、信号和运行时契约保持不变。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QObject,
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QSignalBlocker,
    QTimer,
    QUrl,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from .audio_output import create_audio_output as QAudioOutput
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListView,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import library, textfile
from ..core.synthesis import glossary, local_models, style_presets, voices
from ..core.synthesis.errors import user_error_message
from ..core.synthesis.indextts2_presets import (
    DEFAULT_EMOTION_INTENSITY,
    EMOTION_VECTOR_LABELS,
    MAX_EMOTION_VECTOR_TOTAL,
    emotion_vector_for,
    emotion_vector_total,
    emotion_weight_for,
    normalize_emotion_intensity,
    normalize_emotion_vector,
)
from ..core.synthesis.indextts2_results import build_result_summary, export_target_path, unique_output_path
from ..core.synthesis.indextts2_segments import assess_segment_plan
from ..core.synthesis.indextts2_subprocess import persistent_worker_status, shutdown_persistent_worker
from ..core.synthesis.indextts2_text_guard import assess_text_length
from . import dialogs, icons, theme as T
from .control_foundation import (
    _repolish,
    LuluCheckBox,
    adopt_action_surface,
    adopt_button,
    adopt_dialog,
    adopt_progress,
    adopt_scroll_area,
    adopt_segment,
    adopt_select_field,
    adopt_slider,
    adopt_surface,
    adopt_text_area,
    adopt_text_field,
    adopt_consumer_theme_scope,
    build_consumer_page_header,
    PaperTextureFrame,
    set_button_loading,
    set_progress_kind,
    set_status_kind,
    set_surface_status,
    track_status_text_semantics,
)
from .glossary_panel import GlossaryPanel
from .consumer_select import ConsumerSelect
from .document_source_controls import PaperEnvelopePicker as _PaperEnvelopePicker
from . import voice_result_style as result_style
from .voice_wave_slider import VoiceWaveSlider
from .widgets import ElideLabel, FileDropArea
from .waveform import WaveformSelector
from .worker import (
    IndexTTS2BackgroundReleaseWorker,
    IndexTTS2ConfigSyncWorker,
    IndexTTS2StatusWorker,
    IndexTTS2SynthesisWorker,
    ReferenceTranscribeWorker,
    SynthesisWorker,
)


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB" if n < 1024**3 else f"{n / 1024**3:.1f} GB"


def _time_label(ms: int) -> str:
    seconds = max(0, int(ms / 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB" if n else "未统计"


def _readiness_text(ready: bool, *, checking: bool = False, failed: bool = False, state: str = "") -> str:
    if checking:
        return "检查中"
    if failed:
        return "检查失败"
    if state == "needs_fix":
        return "需修复"
    if state == "error":
        return "检查失败"
    return "可用" if ready else "未准备"


def _finder_reveal_failed_message(_error: BaseException | None = None, _output: Path | None = None) -> str:
    return "无法在 Finder 中显示该文件，请确认文件仍在本机且当前用户有权限访问。"


VOICE_ENVELOPE_ASSET = (
    Path(__file__).resolve().parent / "assets" / "voice_envelope_vintage_complete.png"
)
VOICE_UPLOAD_PAPER_BOX_ASSET = (
    Path(__file__).resolve().parent / "assets" / "voice_upload_paper_box.png"
)
VOICE_SOURCE_TAB_PAPER_ASSET = (
    Path(__file__).resolve().parent / "assets" / "voice_source_tabs_cotton_paper.png"
)


class _EnvelopeMark(QWidget):
    """Hidden animation state for the envelope's gentle material lift."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(42, 42)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._flap_progress = 0.0
        self._animation = QPropertyAnimation(self, b"flapProgress", self)
        self._animation.setDuration(210)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_flap_progress(self) -> float:
        return self._flap_progress

    def _set_flap_progress(self, value: float) -> None:
        self._flap_progress = max(0.0, min(1.0, float(value)))
        self.update()

    flapProgress = Property(float, _get_flap_progress, _set_flap_progress)

    def set_opened(self, opened: bool, *, immediate: bool = False) -> None:
        target = 1.0 if opened else 0.0
        self._animation.stop()
        if immediate:
            self._set_flap_progress(target)
            return
        self._animation.setStartValue(self._flap_progress)
        self._animation.setEndValue(target)
        self._animation.start()

class _EnvelopeTrigger(QPushButton):
    """Library-only manuscript trigger backed by a tactile paper photograph."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("voiceEnvelopeTrigger")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(88)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("从文稿库选择配音稿件")
        self.setToolTip("选择一篇稿件")
        self.setProperty("envelopeVisual", "completeVintageEnvelope")
        self.setProperty("envelopeMaterial", "cottonRagPaper")
        self.setProperty("envelopeScaling", "cachedHeightThreeSlice")
        self.setProperty("envelopeEdgeContinuity", True)
        self.setProperty("envelopeCopySafeArea", True)
        self.setProperty("envelopeHoverFeedback", "liftGlowShadow")
        self._paper_asset = QPixmap(str(VOICE_ENVELOPE_ASSET))
        self._paper_height_cache: dict[int, tuple[QPixmap, QPixmap, QPixmap]] = {}
        self.setProperty("envelopeAssetLoaded", not self._paper_asset.isNull())
        self.mark = _EnvelopeMark(self)
        self.mark.hide()
        self.mark._animation.valueChanged.connect(self._on_flap_progress)
        self.copy_block = QWidget(self)
        self.copy_block.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        copy_layout = QVBoxLayout(self.copy_block)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(4)
        self.title = QLabel("选择一篇稿件", self.copy_block)
        self.title.setObjectName("voiceEnvelopeTitle")
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.title.setAlignment(Qt.AlignCenter)
        copy_layout.addWidget(self.title, 0, Qt.AlignHCenter)
        self.action = QLabel("点击选择", self.copy_block)
        self.action.setObjectName("voiceEnvelopeAction")
        self.action.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.action.setAlignment(Qt.AlignCenter)
        copy_layout.addWidget(self.action, 0, Qt.AlignHCenter)
        self.copy_block.adjustSize()
        hint = self.copy_block.sizeHint()
        self._copy_natural_size = QSize(hint.width() + 24, hint.height() + 10)
        self.copy_block.setFixedSize(self._copy_natural_size)
        self._center_copy_block()

    def _on_flap_progress(self, _value) -> None:
        self._center_copy_block()
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._center_copy_block()

    def _center_copy_block(self) -> None:
        """Move the fixed copy group without forcing a layout pass on every resize frame."""
        natural = self._copy_natural_size
        width = min(natural.width(), max(0, self.width() - 44))
        height = min(natural.height(), max(0, self.height() - 24))
        self.copy_block.setFixedSize(width, height)
        lift = round(4.0 * float(getattr(self.mark, "_flap_progress", 0.0)))
        self.copy_block.move(
            round((self.width() - width) / 2.0),
            max(0, round((self.height() - height) / 2.0) - lift),
        )

    def _paper_slices(self, body: QRectF) -> tuple[tuple[QRectF, QRectF], ...]:
        """Map full-height paper into three horizontal slices with an undistorted seal."""
        # Parent layouts can assign the same final size without a second resize event.
        # Moving a fixed child is cheap; unlike adjustSize(), it does not relayout on animation frames.
        self._center_copy_block()
        if self._paper_asset.isNull() or body.width() <= 0.0 or body.height() <= 0.0:
            return ()
        source = QRectF(self._paper_asset.rect())
        center_source_width = min(420.0, source.width() * 0.28)
        center_source_left = source.center().x() - center_source_width / 2.0
        center_scale = body.height() / source.height()
        center_target_width = min(body.width(), center_source_width * center_scale)
        side_target_width = max(0.0, (body.width() - center_target_width) / 2.0)
        center_target_left = body.center().x() - center_target_width / 2.0
        return (
            (
                QRectF(source.left(), source.top(), center_source_left - source.left(), source.height()),
                QRectF(body.left(), body.top(), side_target_width, body.height()),
            ),
            (
                QRectF(center_source_left, source.top(), center_source_width, source.height()),
                QRectF(center_target_left, body.top(), center_target_width, body.height()),
            ),
            (
                QRectF(
                    center_source_left + center_source_width,
                    source.top(),
                    source.right() - (center_source_left + center_source_width),
                    source.height(),
                ),
                QRectF(center_target_left + center_target_width, body.top(), side_target_width, body.height()),
            ),
        )

    def _paper_height_slices(self, height: int) -> tuple[QPixmap, QPixmap, QPixmap]:
        """Cache vertical resampling so sidebar width animation only stretches horizontally."""
        height = max(1, int(height))
        cached = self._paper_height_cache.get(height)
        if cached is not None:
            return cached
        source = self._paper_asset.rect()
        center_width = min(420, round(source.width() * 0.28))
        center_left = round(source.center().x() - center_width / 2.0)
        source_rects = (
            QRect(0, 0, center_left, source.height()),
            QRect(center_left, 0, center_width, source.height()),
            QRect(center_left + center_width, 0, source.width() - center_left - center_width, source.height()),
        )
        slices = tuple(
            self._paper_asset.copy(rect).scaledToHeight(height, Qt.SmoothTransformation)
            for rect in source_rects
        )
        self._paper_height_cache[height] = slices
        if len(self._paper_height_cache) > 4:
            oldest = next(iter(self._paper_height_cache))
            if oldest != height:
                self._paper_height_cache.pop(oldest, None)
        return slices

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        """Render the legacy-compatible trigger as a quiet consumer source card."""
        super().paintEvent(event)
        if self.width() < 80 or self.height() < 60:
            return
        consumer = T.consumer_theme(T.ACTIVE.name)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            progress = float(getattr(self.mark, "_flap_progress", 0.0))
            hovered = self.underMouse() or bool(self.property("open"))
            body = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
            fill = T.consumer_qcolor(consumer.accent_wash if hovered else consumer.surface_focal)
            if self.isDown():
                fill = T.consumer_qcolor(consumer.secondary_hover)
            painter.setBrush(fill)
            edge = T.consumer_qcolor(
                consumer.accent_hairline_active
                if self.hasFocus()
                else consumer.accent_hairline if hovered or progress > 0.0 else consumer.line
            )
            painter.setPen(QPen(edge, 2.0 if self.hasFocus() else 1.0))
            painter.drawRoundedRect(body, consumer.radius_surface, consumer.radius_surface)
            painter.setPen(Qt.NoPen)
            painter.setBrush(T.consumer_qcolor(consumer.accent))
            painter.drawRoundedRect(
                QRectF(body.left() + 11.0, body.top() + 18.0, 3.0, body.height() - 36.0),
                1.5,
                1.5,
            )
        finally:
            painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.mark.set_opened(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not bool(self.property("open")):
            self.mark.set_opened(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton:
            self.mark.set_opened(True)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.mark.set_opened(True)
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_opened(self, opened: bool) -> None:
        self.setProperty("open", bool(opened))
        self.mark.set_opened(bool(opened))
        self.style().unpolish(self)
        self.style().polish(self)


class _PaperBoxFileDropArea(FileDropArea):
    """FileDropArea behavior presented as a complete top-down document box."""

    def __init__(self, on_click, on_drop, sub: str = "", *, title: str = "拖入配音文案文件"):
        super().__init__(on_click, on_drop, sub=sub, title=title)
        self.setObjectName("voicePaperBoxDropArea")
        self.setProperty("uploadVisual", "topDownPaperDocumentBox")
        self.setProperty("uploadMaterial", "layeredMatteCardboard")
        self.setProperty("uploadScaling", "cachedHeightThreeSlice")
        self.setProperty("paperBoxEdgeContinuity", True)
        self.setProperty("uploadCenterRendering", "highResolutionPaperLayer")
        self._paper_box_asset = QPixmap(str(VOICE_UPLOAD_PAPER_BOX_ASSET))
        self._paper_box_height_cache: dict[int, tuple[QPixmap, QPixmap, QPixmap]] = {}
        self.setProperty("uploadAssetLoaded", not self._paper_box_asset.isNull())
        consumer = T.consumer_theme(T.ACTIVE.name)
        self.layout().setContentsMargins(16, 10, 16, 10)
        self.layout().setSpacing(4)
        self.glyph.show()
        glyph_label = self.glyph.layout().itemAt(0).widget()
        if isinstance(glyph_label, QLabel):
            glyph_label.setPixmap(icons.pixmap("download", consumer.accent, 20))
        self.setStyleSheet(
            "QFrame#voicePaperBoxDropArea { background: transparent; border: none; }"
            f"QFrame#voicePaperBoxDropArea QLabel#dropTitle {{ color: {consumer.ink}; font-weight: 650; }}"
            f"QFrame#voicePaperBoxDropArea QLabel#dropSub {{ color: {consumer.ink_muted}; font-weight: 500; }}"
            f"QFrame#voicePaperBoxDropArea QLabel#dropFmt {{ color: {consumer.ink_muted}; }}"
        )

    def _paper_box_height_slices(self, height: int) -> tuple[QPixmap, QPixmap, QPixmap]:
        """Cache vertical resampling while preserving horizontal source detail."""
        height = max(1, int(height))
        cached = self._paper_box_height_cache.get(height)
        if cached is not None:
            return cached
        source = self._paper_box_asset.rect().adjusted(68, 30, -68, -42)
        edge_width = min(250, max(1, source.width() // 4))
        rects = (
            QRect(source.left(), source.top(), edge_width, source.height()),
            QRect(source.left() + edge_width, source.top(), source.width() - (2 * edge_width), source.height()),
            QRect(source.right() - edge_width + 1, source.top(), edge_width, source.height()),
        )
        slices = tuple(
            self._paper_box_asset.copy(rect).scaled(
                rect.width(), height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
            )
            for rect in rects
        )
        self._paper_box_height_cache[height] = slices
        if len(self._paper_box_height_cache) > 4:
            oldest = next(iter(self._paper_box_height_cache))
            if oldest != height:
                self._paper_box_height_cache.pop(oldest, None)
        return slices

    def _draw_paper_box(self, painter: QPainter, body: QRectF) -> None:
        left, center, right = self._paper_box_height_slices(round(body.height()))
        edge_target = min(max(64.0, body.height() * 0.68), body.width() / 4.0)
        center_width = max(1.0, body.width() - (2.0 * edge_target))
        targets = (
            QRectF(body.left(), body.top(), edge_target, body.height()),
            QRectF(body.left() + edge_target, body.top(), center_width, body.height()),
            QRectF(body.right() - edge_target, body.top(), edge_target, body.height()),
        )
        for target, piece in zip(targets, (left, center, right)):
            painter.drawPixmap(target, piece, QRectF(piece.rect()))

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        if self.width() < 80 or self.height() < 60:
            return
        consumer = T.consumer_theme(T.ACTIVE.name)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            body = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
            hovered = self.underMouse()
            pressed = bool(QApplication.mouseButtons() & Qt.LeftButton) and hovered
            fill = T.consumer_qcolor(
                consumer.secondary_hover if pressed else consumer.accent_wash if hovered else consumer.secondary
            )
            painter.setBrush(fill)
            edge = T.consumer_qcolor(
                consumer.accent_hairline_active
                if self.hasFocus() or pressed
                else consumer.accent_hairline
            )
            pen = QPen(edge, 2.0 if self.hasFocus() else 1.0)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(body, consumer.radius_surface, consumer.radius_surface)
        finally:
            painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.update()
        super().leaveEvent(event)


class _PaperSourceTab(QPushButton):
    """Independent letterpress tab with its own archival cotton-paper material."""

    def __init__(self, label: str, texture_index: int, parent=None):
        super().__init__(label, parent)
        self._texture_index = max(0, int(texture_index))
        self._paper_asset = QPixmap(str(VOICE_SOURCE_TAB_PAPER_ASSET))
        self._paper_texture = QPixmap()
        if not self._paper_asset.isNull():
            source_width = max(1, self._paper_asset.width() // 3)
            source_left = min(self._texture_index, 2) * source_width
            self._paper_texture = self._paper_asset.copy(
                QRect(source_left, 0, source_width, self._paper_asset.height())
            ).scaled(360, 120, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(86)
        self.setFixedHeight(40)
        self.setAccessibleName(f"配音文案来源：{label}")
        self.setProperty("sourceTabVisual", "archivalLetterpressTab")
        self.setProperty("sourceTabTexture", "dedicatedCottonPaper")
        self.setProperty("sourceTabAssetLoaded", not self._paper_asset.isNull())
        self.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; }")

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        consumer = T.consumer_theme(T.ACTIVE.name)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            body = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
            hovered = self.underMouse()
            checked = self.isChecked()
            pressed = self.isDown()
            if pressed:
                body.translate(0.0, 1.0)
            fill = T.consumer_qcolor(
                consumer.surface_focal
                if checked
                else consumer.accent_wash if hovered
                else consumer.secondary
            )
            painter.setBrush(fill)
            edge = T.consumer_qcolor(
                consumer.paper_edge
                if checked else consumer.accent_hairline if hovered else "rgba(0,0,0,0)"
            )
            painter.setPen(QPen(edge, 1.0))
            painter.drawRoundedRect(body, consumer.radius_compact, consumer.radius_compact)

            font = self.font()
            font.setBold(checked)
            painter.setFont(font)
            ink = T.consumer_qcolor(consumer.accent if checked else consumer.ink_soft)
            painter.setPen(ink)
            painter.drawText(body, Qt.AlignCenter, self.text())

            if self.hasFocus():
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(T.consumer_qcolor(consumer.accent_hairline_active), 1.5))
                painter.drawRoundedRect(body.adjusted(1.5, 1.5, -1.5, -1.5), 7.5, 7.5)
        finally:
            painter.end()


class _VoiceWaveVisual(QWidget):
    """Theme-aware horizontal waveform for the voice-result empty state."""

    def __init__(self, parent=None, *, compact: bool = False):
        super().__init__(parent)
        self._compact = compact
        self.setFixedSize(176 if not compact else 112, 62 if not compact else 44)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        consumer = T.consumer_theme(T.ACTIVE.name)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        wash = T.consumer_qcolor(consumer.surface_focal)
        line = T.consumer_qcolor(consumer.accent)
        line.setAlpha(86)
        accent = T.consumer_qcolor(consumer.accent)
        accent.setAlpha(218)
        box = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        painter.setPen(QPen(T.consumer_qcolor(consumer.accent_hairline), 1.0))
        painter.setBrush(wash)
        painter.drawRoundedRect(box, 15.0, 15.0)
        heights = (10, 18, 28, 20, 36, 24, 44, 30, 18, 38, 50, 34, 22, 40, 27, 17, 31, 21, 12)
        if self._compact:
            heights = tuple(max(8, int(value * 0.72)) for value in heights)
        gap = 4.0 if not self._compact else 3.0
        bar_w = 3.0
        total = len(heights) * bar_w + (len(heights) - 1) * gap
        x = (self.width() - total) / 2.0
        for index, height in enumerate(heights):
            top = (self.height() - height) / 2.0
            color = accent if index in (4, 6, 9, 10, 13) else line
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, top, bar_w, height), bar_w / 2.0, bar_w / 2.0)
            x += bar_w + gap


class _GardenFollowPanel(QFrame):
    """Quiet paper ground for the parameter-free follow mode."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("voiceFollowGarden")
        self.setAccessibleName("自然跟随说明")
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        consumer = T.consumer_theme(T.ACTIVE.name)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            body = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
            paper = QLinearGradient(body.topLeft(), body.bottomRight())
            paper.setColorAt(0.0, T.consumer_qcolor(consumer.surface_focal))
            paper.setColorAt(0.58, T.consumer_qcolor(consumer.secondary))
            paper.setColorAt(1.0, T.consumer_qcolor(consumer.accent_wash))
            painter.setBrush(paper)
            painter.setPen(QPen(T.consumer_qcolor(consumer.line), 1.0))
            painter.drawRoundedRect(body, consumer.radius_surface, consumer.radius_surface)

            clip = QPainterPath()
            clip.addRoundedRect(body, consumer.radius_surface, consumer.radius_surface)
            painter.setClipPath(clip)

            # Deterministic short fibres keep the panel tactile without visual noise.
            fibre = T.consumer_qcolor(consumer.paper_edge)
            fibre.setAlpha(28)
            painter.setPen(QPen(fibre, 0.65))
            for index in range(34):
                x = body.left() + 11 + ((index * 47) % max(12, int(body.width() - 22)))
                y = body.top() + 9 + ((index * 29) % max(12, int(body.height() - 18)))
                painter.drawLine(round(x), round(y), round(x + 4 + index % 5), round(y + (index % 3) - 1))

        finally:
            painter.end()


class _VoiceSourceDialog(QDialog):
    """Non-blocking source chooser with full-shell blur and outside dismissal."""

    source_selected = Signal(str)

    def __init__(self, *, current_mode: str, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.setObjectName("voiceSourceDialog")
        self.setWindowModality(Qt.NonModal)
        self.setModal(False)
        self.setMinimumSize(420, 248)
        self.resize(520, 270)
        self._blur_effect = None
        self._blur_target = None
        self._current_mode = current_mode
        self.setStyleSheet(T.QSS + f"""
QDialog#voiceSourceDialog {{ background:{T.CARD}; border:1px solid {T.LINE}; border-radius:18px; }}
QLabel#voiceSourceDialogTitle {{ color:{T.INK}; font-size:18px; font-weight:700; background:transparent; }}
QLabel#voiceSourceDialogSub {{ color:{T.INK_SOFT}; font-size:12px; background:transparent; }}
QPushButton[voiceSourceChoice="true"] {{
    background:{T.CARD}; color:{T.INK}; border:1px solid {T.LINE}; border-radius:12px;
    padding:12px 14px; text-align:left; font-size:13px; font-weight:650;
}}
QPushButton[voiceSourceChoice="true"]:hover {{ background:{T.TERRA_WASH}; border-color:{T.TERRA_SOFT}; }}
QPushButton[voiceSourceChoice="true"]:pressed {{ background:{T.BG_DEEP}; border-color:{T.TERRA}; }}
QPushButton[voiceSourceChoice="true"][current="true"] {{ background:{T.TERRA_WASH}; border-color:{T.TERRA}; }}
""")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("选择配音文案")
        title.setObjectName("voiceSourceDialogTitle")
        title_box.addWidget(title)
        sub = QLabel("选择来源后，现有内容和业务状态会继续保留")
        sub.setObjectName("voiceSourceDialogSub")
        title_box.addWidget(sub)
        head.addLayout(title_box, 1)
        close = QPushButton()
        adopt_button(close, role="icon", accessible_name="关闭文案来源选择", tooltip="关闭")
        close.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 14)))
        close.clicked.connect(self.close)
        head.addWidget(close, 0, Qt.AlignTop)
        root.addLayout(head)

        choices = QHBoxLayout()
        choices.setSpacing(10)
        for key, title_text, detail in (
            ("library", "素材库", "选择已保存文稿"),
            ("upload", "上传文件", "读取本地文本"),
            ("custom", "自定义", "直接输入文案"),
        ):
            button = QPushButton(f"{title_text}\n{detail}")
            button.setProperty("voiceSourceChoice", True)
            button.setProperty("current", key == current_mode)
            button.setMinimumHeight(88)
            button.setCursor(Qt.PointingHandCursor)
            button.setAccessibleName(f"配音文案来源：{title_text}")
            button.clicked.connect(lambda _checked=False, value=key: self._choose(value))
            choices.addWidget(button, 1)
        root.addLayout(choices)
        foot = QLabel("点击卡片外区域或按 Esc 返回")
        foot.setObjectName("voiceSourceDialogSub")
        foot.setAlignment(Qt.AlignCenter)
        root.addWidget(foot)

    def _choose(self, value: str) -> None:
        self.source_selected.emit(value)
        self.close()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        parent = self.parentWidget()
        if parent is not None:
            target = parent.window()
            if target is not self and target.graphicsEffect() is None:
                self._blur_effect = QGraphicsBlurEffect(target)
                self._blur_effect.setBlurRadius(5.0)
                target.setGraphicsEffect(self._blur_effect)
                self._blur_target = target
            self.resize(
                min(520, max(420, target.width() - 56)),
                min(270, max(248, target.height() - 56)),
            )
            center = target.mapToGlobal(target.rect().center())
            self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if (
            self._blur_target is not None
            and self._blur_effect is not None
            and self._blur_target.graphicsEffect() is self._blur_effect
        ):
            self._blur_target.setGraphicsEffect(None)
        self._blur_effect = None
        self._blur_target = None
        super().closeEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.MouseButtonPress and self.isVisible():
            if not self.frameGeometry().contains(event.globalPosition().toPoint()):
                self.close()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)


class _DialogBackdrop(QObject):
    """Keeps an existing dialog non-blocking while owning full-shell blur safely."""

    def __init__(self, dialog: QDialog, shell: QWidget):
        super().__init__(dialog)
        self.dialog = dialog
        self.shell = shell.window()
        self.effect = None
        if self.shell.graphicsEffect() is None:
            self.effect = QGraphicsBlurEffect(self.shell)
            self.effect.setBlurRadius(5.0)
            self.shell.setGraphicsEffect(self.effect)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        dialog.finished.connect(self.cleanup)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.MouseButtonPress and self.dialog.isVisible():
            if not self.dialog.frameGeometry().contains(event.globalPosition().toPoint()):
                self.dialog.reject()
        return super().eventFilter(watched, event)

    def cleanup(self, *_args) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self.effect is not None and self.shell.graphicsEffect() is self.effect:
            self.shell.setGraphicsEffect(None)
        self.effect = None


class _VoiceLibraryModal(QDialog):
    """Centered voice library card over a blurred, dismissible shell backdrop."""

    def __init__(self, parent: QWidget):
        shell = parent.window()
        super().__init__(shell, Qt.Dialog | Qt.FramelessWindowHint)
        self.shell = shell
        self.setObjectName("voiceLibraryModal")
        self.setWindowTitle("音色库")
        # Keep this dialog non-native-modal so the in-window backdrop can own
        # outside-click dismissal without macOS replacing the shell with a
        # separate full-window white dialog surface.
        self.setWindowModality(Qt.NonModal)
        self.setModal(False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setAccessibleName("音色库")
        self._blur_effect: QGraphicsBlurEffect | None = None
        self._blur_target: QWidget | None = None
        self._cleaned_up = False

        self.backdrop = QWidget(shell)
        self.backdrop.setObjectName("voiceLibraryBackdrop")
        self.backdrop.setAttribute(Qt.WA_StyledBackground, True)
        self.backdrop.setAccessibleName("关闭音色库")
        self.backdrop.hide()

        mask = QColor(T.INK)
        mask_red, mask_green, mask_blue, mask_alpha = mask.red(), mask.green(), mask.blue(), 62
        self.setStyleSheet(
            T.QSS
            + "QDialog#voiceLibraryModal{background:transparent;border:none;}"
            f"QFrame#voiceLibrarySurface{{background:{T.CARD};border:1px solid {T.LINE};border-radius:20px;}}"
            f"QLabel#voiceLibraryTitle{{background:transparent;color:{T.INK};font-size:20px;font-weight:750;}}"
            f"QLabel#voiceLibrarySub{{background:transparent;color:{T.INK_SOFT};font-size:12px;}}"
            f"QFrame#voiceLibraryStore{{background:{T.BG};border:1px solid {T.LINE_SOFT};border-radius:14px;}}"
            f"QFrame#voiceLibraryRow{{background:{T.CARD};border:1px solid {T.LINE_SOFT};border-radius:12px;}}"
            f"QFrame#voiceLibraryRow:hover{{background:{T.HOVER_RAISE};border-color:{T.TERRA_SOFT};}}"
        )
        self.backdrop.setStyleSheet(
            f"QWidget#voiceLibraryBackdrop{{background:rgba({mask_red},{mask_green},{mask_blue},{mask_alpha});}}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.surface = QFrame(self)
        self.surface.setObjectName("voiceLibrarySurface")
        self.surface.setAccessibleName("音色库内容")
        self.surface_layout = QVBoxLayout(self.surface)
        self.surface_layout.setContentsMargins(24, 22, 24, 20)
        self.surface_layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        header_copy = QVBoxLayout()
        header_copy.setSpacing(3)
        title = QLabel("我的音色")
        title.setObjectName("voiceLibraryTitle")
        header_copy.addWidget(title)
        subtitle = QLabel("保存在本机，随时试听和使用")
        subtitle.setObjectName("voiceLibrarySub")
        subtitle.setWordWrap(True)
        header_copy.addWidget(subtitle)
        header.addLayout(header_copy, 1)
        self.close_button = QPushButton()
        adopt_button(
            self.close_button,
            role="icon",
            accessible_name="关闭音色库",
            tooltip="关闭",
        )
        self.close_button.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 14)))
        self.close_button.setFixedSize(32, 32)
        self.close_button.setStyleSheet('QPushButton{padding:0;border:0;background:transparent;}')
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button, 0, Qt.AlignTop)
        self.surface_layout.addLayout(header)

        self.body_layout = QVBoxLayout()
        self.body_layout.setSpacing(12)
        self.surface_layout.addLayout(self.body_layout, 1)
        outer.addWidget(self.surface, 1)
        self.finished.connect(self._cleanup)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._cleaned_up = False
        self.backdrop.setGeometry(self.shell.rect())
        self.backdrop.show()
        self.backdrop.raise_()
        if self.shell.graphicsEffect() is None:
            self._blur_effect = QGraphicsBlurEffect(self.shell)
            self._blur_effect.setBlurRadius(6.0)
            self.shell.setGraphicsEffect(self._blur_effect)
            self._blur_target = self.shell
        self._fit_to_shell()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        super().showEvent(event)
        self.raise_()
        self.activateWindow()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key_Escape:
            self.reject()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if self.property("childWorkflow"):
            return super().eventFilter(watched, event)
        if watched is self.shell and event.type() in (QEvent.Move, QEvent.Resize):
            self.backdrop.setGeometry(self.shell.rect())
            self._fit_to_shell()
            return False
        if event.type() == QEvent.MouseButtonPress and self.isVisible():
            if watched is self.backdrop:
                self.reject()
                event.accept()
                return True
            point = event.globalPosition().toPoint()
            if watched is self and not self.surface.rect().contains(self.surface.mapFromGlobal(point)):
                self.reject()
                event.accept()
                return True
            if not self.frameGeometry().contains(point):
                self.reject()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._cleanup()
        super().closeEvent(event)

    def _fit_to_shell(self) -> None:
        horizontal_margin = 46 if self.shell.width() >= 720 else 18
        vertical_margin = 38 if self.shell.height() >= 620 else 18
        target_width = min(920, max(280, self.shell.width() - horizontal_margin * 2))
        target_height = min(700, max(280, self.shell.height() - vertical_margin * 2))
        self.setFixedSize(target_width, target_height)
        shell_center = self.shell.mapToGlobal(self.shell.rect().center())
        self.move(
            shell_center.x() - target_width // 2,
            shell_center.y() - target_height // 2,
        )

    def _cleanup(self, *_args) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.backdrop.hide()
        self.backdrop.deleteLater()
        if (
            self._blur_target is not None
            and self._blur_effect is not None
            and self._blur_target.graphicsEffect() is self._blur_effect
        ):
            self._blur_target.setGraphicsEffect(None)
        self._blur_effect = None
        self._blur_target = None


# 情感控制方式（五选一，按产品入口顺序固定）
EMOTION_MODE_ORDER = ("follow", "emoaudio", "vector", "template", "emotext")
EMOTION_MODE_LABELS = {
    "follow": "跟随音色",
    "emoaudio": "情感参考音频",
    "template": "情感模板",
    "vector": "高级情感控制",
    "emotext": "情感描述文本",
}

# 8 维滑杆的显示名（对齐用，语义不变）：反感/厌恶→厌恶，自然/平静→平静
VECTOR_DISPLAY_LABELS = {"反感/厌恶": "厌恶", "自然/平静": "平静"}

EMOTION_LABELS = {
    "natural": "自然",
    "happy": "开心",
    "firm": "坚定",
    "gentle": "温柔",
    "excited": "激动",
    "calm": "平静",
}

INTENSITY_LABELS = {
    "restrained": "克制",
    "standard": "标准",
    "vivid": "鲜明",
}

DEFAULT_EMOTION_WEIGHT = 0.65
VOICE_SOURCE_CANVAS_HEIGHT = 142
VOICE_SOURCE_INNER_BOTTOM_GAP = 10


class FlowLayout(QLayout):
    """自动换行的流式布局，用于情感模板 chips（数量增长时换行，不出现横向滚动条）。"""

    def __init__(self, parent=None, hspacing: int = 8, vspacing: int = 8):
        super().__init__(parent)
        self._items: list = []
        self._h = hspacing
        self._v = vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        right = rect.right() - m.right()
        line_h = 0
        for it in self._items:
            hint = it.sizeHint()
            w, h = hint.width(), hint.height()
            if x + w > right and line_h > 0:
                x = rect.x() + m.left()
                y += line_h + self._v
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += w + self._h
            line_h = max(line_h, h)
        return y + line_h - rect.y() + m.bottom()


class _CurrentPageLayout(QStackedLayout):
    """Qt's parent layout asks this layout, not the widget, for height-for-width."""
    def sizeHint(self):
        widget = self.currentWidget()
        return widget.sizeHint() if widget is not None else super().sizeHint()

    def minimumSize(self):
        widget = self.currentWidget()
        return widget.minimumSizeHint() if widget is not None else super().minimumSize()

    def hasHeightForWidth(self):
        widget = self.currentWidget()
        return widget.hasHeightForWidth() if widget is not None else False

    def heightForWidth(self, width):
        widget = self.currentWidget()
        if widget is None:
            return -1
        return widget.heightForWidth(width) if widget.hasHeightForWidth() else widget.sizeHint().height()


class _FittedStack(QWidget):
    """A standard Qt stack whose hidden pages do not reserve canvas space."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pages = _CurrentPageLayout(self)
        self._pages.setContentsMargins(0, 0, 0, 0)
        self._pages.currentChanged.connect(lambda _i: self.updateGeometry())

    def addWidget(self, widget):
        return self._pages.addWidget(widget)

    def currentWidget(self):
        return self._pages.currentWidget()

    def currentIndex(self):
        return self._pages.currentIndex()

    def setCurrentIndex(self, index):
        self._pages.setCurrentIndex(index)

    def widget(self, index):
        return self._pages.widget(index)

    def count(self):
        return self._pages.count()


class _VoiceRecordingDialog(QDialog):
    """Small in-page recorder used by voice sample and emotion reference flows."""

    def __init__(self, parent, *, purpose: str):
        super().__init__(parent)
        self.page = parent
        self.purpose = purpose
        self.result_path: str | None = None
        self.result_meta: dict[str, object] = {}
        self._source = "microphone"
        self._mic_rec = None
        self._system_session = None
        self._mixed_system_result = None
        self._mixed_paths: dict[str, Path] = {}
        self._cancel_requested = False
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        title = "录制音色样本" if purpose == "voice_sample" else "录制情感参考"
        self.setWindowTitle(title)
        adopt_dialog(self, accessible_name=title)
        adopt_consumer_theme_scope(self, "coreWorkspace")
        self.setMinimumWidth(480)
        if purpose == "voice_sample":
            from .voice_library import library_style
            self.setStyleSheet(library_style(parent._consumer) + f'''
                QPushButton#seg:checked {{background:{parent._consumer.accent_wash};border-color:{parent._consumer.accent};}}
            ''')
        else:
            self.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS + parent._extra_qss())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(12)
        head = QLabel(title)
        head.setObjectName("sectionTitle")
        lay.addWidget(head)
        desc = QLabel(self._purpose_hint())
        desc.setObjectName("pageMeta")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        seg = QFrame()
        seg.setObjectName("segBox")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(4, 4, 4, 4)
        sh.setSpacing(3)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, text, tip in (
            ("microphone", "麦克风", "推荐：录本人授权、干净单人讲话"),
            ("system_audio", "系统声音", "录电脑正在播放的声音，请确认有权使用"),
            ("mixed", "麦克风+系统", "同时录两路声音，音色样本不推荐直接使用混合音频"),
        ):
            btn = QPushButton(text)
            btn.setObjectName("seg")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _checked=False, s=key: self._set_source(s))
            self._group.addButton(btn)
            self._buttons[key] = btn
            sh.addWidget(btn, 1)
        self._buttons["microphone"].setChecked(True)
        lay.addWidget(seg)

        self.hint = QLabel("")
        self.hint.setObjectName("pageMeta")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        self.status = QLabel("准备录音")
        self.status.setObjectName("voiceSub")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("act")
        self.cancel_btn.clicked.connect(self._cancel)
        adopt_button(self.cancel_btn, role="secondary", accessible_name="取消录音")
        row.addWidget(self.cancel_btn)
        self.start_btn = QPushButton("  开始录音")
        self.start_btn.setObjectName("grabBtn")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setIcon(QIcon(icons.pixmap("mic", "#FFFFFF", 13)))
        self.start_btn.clicked.connect(self._toggle)
        adopt_button(self.start_btn, role="primary", size="lg", accessible_name="开始或停止录音")
        row.addWidget(self.start_btn)
        lay.addLayout(row)
        self._set_source("microphone")
        if purpose == "voice_sample":
            head.setObjectName("libraryTitle")
            for label in (desc, self.hint, self.status):
                label.setObjectName("libraryMeta")
            for button in (*self._buttons.values(), self.start_btn, self.cancel_btn):
                button.setFixedHeight(34)
                button.setAutoDefault(False)

    def _purpose_hint(self) -> str:
        if self.purpose == "voice_sample":
            return "建议录 10-30 秒、干净单人讲话。请确认你有权使用这段声音创建音色。"
        return "建议录 3-10 秒、有明显情绪的短音频。它只影响情绪表达，音色仍以上方选择的音色为准。"

    def _set_source(self, source: str) -> None:
        self._source = source
        self._buttons[source].setChecked(True)
        if source == "microphone":
            self.hint.setText("推荐来源：只录麦克风输入，适合音色样本和情感参考。")
            self.start_btn.setIcon(QIcon(icons.pixmap("mic", "#FFFFFF", 13)))
        elif source == "system_audio":
            self.hint.setText("会录到电脑正在播放的声音。请确认你有权使用这段声音。")
            self.start_btn.setIcon(QIcon(icons.pixmap("waveform", "#FFFFFF", 13)))
        else:
            self.hint.setText("会同时录麦克风和电脑声音；混合音频不推荐直接作为音色样本。")
            self.start_btn.setIcon(QIcon(icons.pixmap("waveform", "#FFFFFF", 13)))

    def _toggle(self) -> None:
        if self._mic_rec is not None and self._mic_rec.is_recording():
            self._stop()
            return
        if self._system_session is not None and self._system_session.is_active():
            self._stop()
            return
        self._start()

    def _recording_path(self, prefix: str) -> Path:
        root = Path(tempfile.gettempdir()) / "lulu-voice-recordings"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.m4a"

    def _start(self) -> None:
        self._cancel_requested = False
        try:
            if self._source == "microphone":
                path = self._recording_path("音色样本" if self.purpose == "voice_sample" else "情感参考")
                self._start_microphone(path)
            elif self._source == "system_audio":
                path = self._recording_path("系统声音样本" if self.purpose == "voice_sample" else "系统声音参考")
                self._start_system(path)
            else:
                output = self._recording_path("混合样本" if self.purpose == "voice_sample" else "混合参考")
                self._start_mixed(output)
        except Exception as exc:
            self._fail(self._friendly_recording_error(exc))

    def _start_microphone(self, path: Path) -> None:
        from .recorder import MicRecorder

        rec = MicRecorder()
        if not rec.has_input():
            raise RuntimeError("麦克风权限或设备不可用")
        rec.tick.connect(self._on_mic_tick)
        rec.finished.connect(lambda p: self._finish_single(Path(p), "麦克风"))
        rec.failed.connect(lambda _msg: self._fail("麦克风录制失败，请检查麦克风权限后重试。"))
        self._mic_rec = rec
        rec.start(str(path))
        self._set_running_ui("麦克风录音中")

    def _start_system(self, path: Path) -> None:
        from ..core.recording import MacOSSystemAudioBackend, RecordingRequest, RecordingSource, RecordingUse

        request = RecordingRequest(source=RecordingSource.SYSTEM_AUDIO, output_path=path, use=RecordingUse.VOICE_SAMPLE)
        self._system_session = MacOSSystemAudioBackend().start(request)
        self._set_running_ui("系统声音录制中")

    def _start_mixed(self, output: Path) -> None:
        from .recorder import MicRecorder
        from ..core.recording import MacOSSystemAudioBackend, RecordingRequest, RecordingSource, RecordingUse

        rec = MicRecorder()
        if not rec.has_input():
            raise RuntimeError("麦克风权限或设备不可用")
        mic_path = output.with_name(f"{output.stem}_麦克风轨.m4a")
        system_path = output.with_name(f"{output.stem}_系统声音轨.m4a")
        request = RecordingRequest(source=RecordingSource.SYSTEM_AUDIO, output_path=system_path, use=RecordingUse.VOICE_SAMPLE)
        self._system_session = MacOSSystemAudioBackend().start(request)
        self._mixed_paths = {"mic": mic_path, "system": system_path, "output": output}
        rec.tick.connect(self._on_mic_tick)
        rec.finished.connect(lambda p: self._finish_mixed(Path(p)))
        rec.failed.connect(lambda _msg: self._fail("麦克风录制失败，请检查麦克风权限后重试。"))
        self._mic_rec = rec
        rec.start(str(mic_path))
        self._set_running_ui("混合录制中")

    def _set_running_ui(self, text: str) -> None:
        for btn in self._buttons.values():
            btn.setEnabled(False)
        self.start_btn.setText("  停止并保存")
        self.start_btn.setIcon(QIcon(icons.pixmap("check", "#FFFFFF", 13)))
        self.status.setText(f"● {text} 00:00")
        self._elapsed = 0
        if self._source in {"system_audio", "mixed"}:
            self._timer.start(1000)

    def _stop(self) -> None:
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.status.setText("正在保存录音…")
        if self._source == "system_audio":
            self._stop_system()
            return
        if self._source == "mixed":
            self._stop_mixed()
            return
        if self._mic_rec is not None and self._mic_rec.is_recording():
            self._mic_rec.stop()

    def _stop_system(self) -> None:
        if self._system_session is None:
            self._fail("录音状态异常，请重新录制。")
            return
        try:
            result = self._system_session.stop()
        except Exception as exc:
            self._fail(self._friendly_recording_error(exc))
            return
        self._finish_single(Path(result.output_path), "系统声音")

    def _stop_mixed(self) -> None:
        if self._system_session is None:
            self._fail("录音状态异常，请重新录制。")
            return
        try:
            self._mixed_system_result = self._system_session.stop()
        except Exception as exc:
            self._fail(self._friendly_recording_error(exc))
            return
        if self._mic_rec is not None and self._mic_rec.is_recording():
            self._mic_rec.stop()
        else:
            self._finish_mixed(self._mixed_paths.get("mic", Path("")))

    def _finish_single(self, path: Path, source_label: str) -> None:
        if self._cancel_requested:
            self._cleanup(path)
            self.reject()
            return
        try:
            stats = self.page._analyze_voice_recording(path)
        except Exception:
            self._fail("录音文件无法读取，请重新录制。")
            return
        if stats.get("is_silent") is True:
            self._fail("这段录音没有清晰声音，请重新录制。")
            return
        self.result_path = str(path)
        self.result_meta = {"source": source_label, "stats": stats}
        self._timer.stop()
        self.accept()

    def _finish_mixed(self, mic_path: Path) -> None:
        if self._cancel_requested:
            self._cleanup_mixed()
            self.reject()
            return
        system_path = self._mixed_paths.get("system")
        output = self._mixed_paths.get("output")
        if not mic_path.exists():
            self._fail("没有录到麦克风声音，请检查麦克风权限后重试。")
            return
        if not system_path or not system_path.exists():
            self._fail("没有录到系统声音，请先播放电脑声音后重试。")
            return
        try:
            mic_stats = self.page._analyze_voice_recording(mic_path)
        except Exception:
            self._fail("麦克风录音无法读取，请重新录制。")
            return
        if mic_stats.get("is_silent") is True:
            self._fail("没有录到清晰的麦克风声音，请重新录制。")
            return
        try:
            stats = self.page._mix_voice_recording_tracks(mic_path, system_path, output)
        except Exception:
            self._fail("混合录音保存失败，请重新录制。")
            return
        self._cleanup(mic_path)
        self._cleanup(system_path)
        self.result_path = str(output)
        self.result_meta = {"source": "麦克风+系统声音", "stats": stats}
        self._timer.stop()
        self.accept()

    def _cancel(self) -> None:
        self._cancel_requested = True
        if self._system_session is not None and self._system_session.is_active():
            self._system_session.cancel()
        if self._mic_rec is not None and self._mic_rec.is_recording():
            self._mic_rec.stop()
        self._cleanup_mixed()
        self._timer.stop()
        self.reject()

    def _fail(self, text: str) -> None:
        self._timer.stop()
        if self._system_session is not None and self._system_session.is_active():
            self._system_session.cancel()
        if self._mic_rec is not None and self._mic_rec.is_recording():
            self._cancel_requested = True
            self._mic_rec.stop()
        self._cleanup_mixed()
        for btn in self._buttons.values():
            btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.start_btn.setText("  重新录制")
        self.start_btn.setIcon(QIcon(icons.pixmap("mic", "#FFFFFF", 13)))
        self.status.setText(text)

    def _friendly_recording_error(self, exc: Exception) -> str:
        text = str(exc) or "录音失败"
        if "权限" in text or "permission" in text.lower() or "not authorized" in text.lower():
            if self._source == "microphone":
                return "Lulu 需要麦克风权限才能录制你的声音。请在系统设置中允许后重试。"
            return "系统声音权限未打开。你可以先改为麦克风录音，或到系统设置允许 Lulu 录制电脑声音后重试。"
        if "超时" in text:
            return "录音启动超时。请检查权限后重试。"
        if "清晰" in text or "静音" in text or "没有录到" in text:
            return "这段录音没有清晰声音，请重新录制。"
        return "录音失败，请检查权限或保存位置后重试。"

    def _on_mic_tick(self, sec: int) -> None:
        m, s = divmod(int(sec), 60)
        prefix = "混合录制中" if self._source == "mixed" else "麦克风录音中"
        self.status.setText(f"● {prefix} {m:02d}:{s:02d}")

    def _tick(self) -> None:
        self._elapsed += 1
        m, s = divmod(self._elapsed, 60)
        prefix = "混合录制中" if self._source == "mixed" else "系统声音录制中"
        self.status.setText(f"● {prefix} {m:02d}:{s:02d}")

    def _cleanup(self, path: Path | None) -> None:
        try:
            if path and path.exists():
                path.unlink()
        except OSError:
            pass

    def _cleanup_mixed(self) -> None:
        for path in self._mixed_paths.values():
            self._cleanup(path)


class VoicePage(QWidget):
    model_context_changed = Signal(object)

    def __init__(self, cfg, main):
        super().__init__()
        self.setProperty("luluConsumerDialogs", True)
        self.cfg = cfg
        self.main = main
        self._model_status_cache = {}
        self._model_status_reads = {}
        self._model_status_epoch = 0
        self._model_status_closing = False
        self._ref_path = ""
        self._worker: SynthesisWorker | None = None
        # Passive status reads are not user work and must not trip MainWindow's
        # generic "*worker*" long-running task gate.
        self._indextts2_worker: IndexTTS2SynthesisWorker | None = None
        self._index_version = "2.5"
        self._index_pages = {}
        self._indextts2_release_worker: IndexTTS2BackgroundReleaseWorker | None = None
        self._indextts2_status_worker: IndexTTS2StatusWorker | None = None
        self._indextts2_config_worker: IndexTTS2ConfigSyncWorker | None = None
        self._ref_worker: ReferenceTranscribeWorker | None = None
        self._new_voice_worker: ReferenceTranscribeWorker | None = None
        self._last_output: str | None = None
        self._indextts2_last_output: str | None = None
        self._indextts2_results: list[dict] = []
        self._indextts2_glossary_applied = False
        self._indextts2_style_syncing = False
        self._indextts2_style_dirty = False
        self._indextts2_style_meta: dict = {}
        self._indextts2_current_style = style_presets.last_selected_style()
        self._indextts2_advanced_syncing = False
        self._indextts2_advanced_source_preset = "natural"
        self._indextts2_advanced_source_intensity = DEFAULT_EMOTION_INTENSITY
        self._indextts2_advanced_source_style_name = "通用自然"
        # IndexTTS 2.5 的核心价值是精细情绪控制，首次打开直接落在主能力上。
        self._indextts2_emotion_mode = "follow"
        self._indextts2_emotion_audio_path = ""
        self._indextts2_emotion_audio_recorded = False
        self._indextts2_emotion_audio_meta: dict[str, object] = {}
        self._src: dict[str, dict] = {}   # 文本三来源块状态（indextts2 / voxcpm2 共用一套构建+处理）
        self._voice_sel: dict[int, dict] = {}   # 音色选择器（选择卡 + 音色库面板）状态，按 combo 区分
        self._voice_library_dialog: _VoiceLibraryModal | None = None
        self._indextts2_status = None
        self._indextts2_status_error = ""
        self._indextts2_status_checking = False
        self._indextts2_text_has_content = False
        self._indextts2_text_length = assess_text_length("")
        self._indextts2_segment_plan = assess_segment_plan("")
        self._indextts2_last_length_meta: dict = {}
        self._voice_source: dict = {
            "source_type": "manual",
            "source_id": None,
            "source_title": "手动输入",
            "source_text_snapshot": "",
            "source_chain": [],
        }
        self._player_dragging = False
        self._indextts2_player_dragging = False
        self._voice_progress_timers: dict[QProgressBar, QTimer] = {}
        self._voice_progress_phases: dict[QProgressBar, int] = {}
        self._voice_progress_animations: dict[QProgressBar, QPropertyAnimation] = {}
        self._voice_progress_status: dict[QProgressBar, tuple[QLabel, str]] = {}
        self._voice_generate_buttons: dict[QProgressBar, QPushButton] = {}
        self._voice_status_labels: dict[QProgressBar, QLabel] = {}
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.setObjectName("root")
        self.setProperty("workspacePage", "voice")
        adopt_consumer_theme_scope(self, "coreWorkspace")
        voices.configure_voice_library_dir(getattr(self.cfg, "voice_library_dir", ""))
        self._build()
        selected = {"local_indextts2": 0, "local_voxcpm2": 1, "local_indextts2_0": 3, "local_qwen3_tts": 4}.get(getattr(self.cfg, "default_tts_model", "local_indextts2"), 0)
        if selected:
            self._switch_panel(selected)

    def _local_synthesis_busy_label(self, *, exclude: str = "") -> str:
        workers = (
            ("voxcpm2", self._worker, "VoxCPM2"),
            ("qwen3", getattr(getattr(self, "_qwen3", None), "worker", None), "Qwen3-TTS"),
            ("indextts2", self._indextts2_worker, f"{self._index_name}"),
        )
        for key, worker, label in workers:
            if key != exclude and worker is not None and worker.isRunning():
                return label
        return ""

    def _guard_local_synthesis_start(self, engine: str) -> bool:
        if self._indextts2_release_worker and self._indextts2_release_worker.isRunning():
            self.main._toast("正在释放上一个配音后台，请稍后生成。")
            return False
        busy = self._local_synthesis_busy_label(exclude=engine)
        if not busy:
            return True
        dialogs.alert(self, "本地配音正在运行", f"{busy} 正在生成。请等待完成或先取消，再启动另一个本地配音引擎。")
        return False

    def _save_voice_safely(self, output_path: str | Path, **kwargs) -> tuple[bool, str]:
        try:
            library.save_voice(audio_path=output_path, **kwargs)
            return True, ""
        except Exception:  # noqa: BLE001
            output = str(Path(output_path).expanduser().resolve())
            return False, f"音频已生成，但保存到素材库失败。音频文件仍在：{output}"

    # ------------------------------------------------------------------ 页面骨架

    def _voice_progress_percent(self, frac: float) -> int:
        return max(0, min(100, int(round(float(frac) * 100))))

    def _advance_voice_progress_activity(self, bar: QProgressBar) -> None:
        if not bar.isVisible():
            return
        phase = (self._voice_progress_phases.get(bar, 0) + 1) % 4
        self._voice_progress_phases[bar] = phase
        status_info = self._voice_progress_status.get(bar)
        if status_info is not None:
            status, base = status_info
            dots = "." * phase
            status.setText(f"{base}{dots}")

    def _start_voice_progress_activity(self, bar: QProgressBar) -> None:
        timer = self._voice_progress_timers.get(bar)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(650)
            timer.timeout.connect(lambda b=bar: self._advance_voice_progress_activity(b))
            self._voice_progress_timers[bar] = timer
        bar.setProperty("voiceRunningActivity", True)
        set_progress_kind(bar, "info")
        generate = self._voice_generate_buttons.get(bar)
        if generate is not None:
            set_button_loading(generate, True)
        self._advance_voice_progress_activity(bar)
        if not timer.isActive():
            timer.start()

    def _stop_voice_progress_activity(self, bar: QProgressBar) -> None:
        timer = self._voice_progress_timers.get(bar)
        if timer is not None:
            timer.stop()
        self._voice_progress_phases.pop(bar, None)
        self._voice_progress_status.pop(bar, None)
        bar.setProperty("voiceRunningActivity", False)
        generate = self._voice_generate_buttons.get(bar)
        if generate is not None:
            set_button_loading(generate, False)

    def _animate_voice_progress_to(self, bar: QProgressBar, target: int, *, max_target: int | None = None) -> int:
        previous_target = bar.property("voiceProgressTarget")
        try:
            previous_target = int(previous_target)
        except (TypeError, ValueError):
            previous_target = bar.value()
        target = max(0, min(100, int(target)))
        if max_target is not None:
            max_target = max(0, min(100, int(max_target)))
            target = min(target, max_target)
            previous_target = min(previous_target, max_target)
            if bar.value() > max_target:
                bar.setValue(max_target)
        target = max(previous_target, bar.value(), target)
        bar.setProperty("voiceProgressTarget", target)
        animation = self._voice_progress_animations.get(bar)
        if animation is None:
            animation = QPropertyAnimation(bar, b"value", self)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._voice_progress_animations[bar] = animation
        if animation.state() == QPropertyAnimation.State.Running:
            animation.stop()
        if bar.value() == target:
            return target
        distance = abs(target - bar.value())
        animation.setDuration(max(260, min(1200, 180 + distance * 12)))
        animation.setStartValue(bar.value())
        animation.setEndValue(target)
        animation.start()
        return target

    def _set_voice_progress_running(
        self,
        bar: QProgressBar,
        status: QLabel,
        msg: str,
        frac: float,
        *,
        prefix: str = "",
        finalizing_cap: int | None = None,
    ) -> None:
        bar.setVisible(True)
        clean_msg = (msg or "生成中").strip() or "生成中"
        if frac < 0:
            animation = self._voice_progress_animations.get(bar)
            if animation is not None and animation.state() == QPropertyAnimation.State.Running:
                animation.stop()
            new_run = not bool(bar.property("voiceRunningActivity"))
            bar.setRange(0, 100)
            if new_run:
                bar.setValue(0)
                bar.setProperty("voiceProgressTarget", 0)
            try:
                held = max(0, min(100, int(bar.property("voiceProgressTarget") or bar.value())))
            except (TypeError, ValueError):
                held = max(0, min(100, bar.value()))
            bar.setValue(held)
            bar.setFormat("运行中")
            base = (
                f"{prefix}{clean_msg} · 整体已到 {held}% · 当前阶段处理中"
                if held > 0
                else f"{prefix}{clean_msg} · 处理中"
            )
            status.setText(base)
            self._voice_progress_status[bar] = (status, base)
        else:
            pct = self._voice_progress_percent(frac)
            bar.setRange(0, 100)
            if bar.value() < 0:
                bar.setValue(0)
            is_finalizing = finalizing_cap is not None and pct >= 100
            target = self._animate_voice_progress_to(
                bar,
                pct,
                max_target=finalizing_cap if is_finalizing else None,
            )
            bar.setFormat(f"{target}%")
            if is_finalizing:
                base = f"{prefix}{clean_msg} · 整体进度 {target}% · 正在收尾，准备试听"
            else:
                base = f"{prefix}{clean_msg} · 已到真实进度 {target}% · 模型内部生成中"
            status.setText(base)
            self._voice_progress_status[bar] = (status, base)
        self._start_voice_progress_activity(bar)

    def _set_voice_progress_complete(self, bar: QProgressBar) -> None:
        self._stop_voice_progress_activity(bar)
        animation = self._voice_progress_animations.get(bar)
        if animation is not None and animation.state() == QPropertyAnimation.State.Running:
            animation.stop()
        bar.setVisible(True)
        bar.setRange(0, 100)
        bar.setProperty("voiceProgressTarget", 100)
        bar.setValue(100)
        bar.setFormat("100%")
        set_progress_kind(bar, "success")
        status = self._voice_status_labels.get(bar)
        if status is not None:
            set_status_kind(status, "success")

    def _set_voice_progress_stopped(self, bar: QProgressBar) -> None:
        self._stop_voice_progress_activity(bar)
        animation = self._voice_progress_animations.get(bar)
        if animation is not None and animation.state() == QPropertyAnimation.State.Running:
            animation.stop()
        bar.setProperty("voiceProgressTarget", None)
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setVisible(False)
        set_progress_kind(bar, "neutral")
        status = self._voice_status_labels.get(bar)
        if status is not None:
            set_status_kind(status, "neutral")
            track_status_text_semantics(status)

    def _build(self) -> None:
        self.setStyleSheet(self._extra_qss())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        adopt_scroll_area(scroll)
        scroll.setObjectName("voicePageScroll")
        self.page_scroll = scroll
        wrap = PaperTextureFrame(
            strength=self._consumer.paper_canvas_opacity,
            layer="canvas",
        )
        adopt_surface(wrap, "page")
        wrap.setObjectName("coreCanvas")
        scroll.setWidget(wrap)
        # 标准窗口尽量不滚动：页根纵向撑满，表单整体 Maximum + 末尾 addStretch(0)。
        root = QVBoxLayout(wrap)
        root.setContentsMargins(24, 0, 24, 28)
        root.setSpacing(14)

        self.page_privacy_badge = QLabel()
        self.page_privacy_badge.setObjectName("voicePrivacyBadge")
        self.page_privacy_badge.setAlignment(Qt.AlignCenter)
        # 窗口较窄时，中心工作区的最小内容宽度为 620px。
        # 这里少占 2px，避免页头最小宽度反向把滚动内容撑宽。
        self.page_privacy_badge.setMinimumWidth(140)
        header_tools = QWidget()
        header_tools.setObjectName("voiceHeaderTools")
        header_tools_layout = QHBoxLayout(header_tools)
        header_tools_layout.setContentsMargins(0, 0, 0, 0)
        header_tools_layout.setSpacing(12)
        header_tools_layout.addWidget(self._build_engine_switch(), 0, Qt.AlignTop)
        header_tools_layout.addWidget(self.page_privacy_badge, 0, Qt.AlignTop)
        header, self.page_title, self.page_meta = build_consumer_page_header(
            "文案配音",
            "把文案变成可试听、可导出的声音",
        )
        self.page_header = header
        self.page_header_tools = header_tools
        self.page_header_shell = QWidget()
        self.page_header_shell.setObjectName("voicePageHeaderShell")
        self.page_header_layout = QGridLayout(self.page_header_shell)
        self.page_header_layout.setContentsMargins(0, 0, 0, 0)
        self.page_header_layout.setHorizontalSpacing(12)
        self.page_header_layout.setVerticalSpacing(10)
        self.page_header_layout.addWidget(header, 0, 0, Qt.AlignTop)
        self.page_header_layout.addWidget(header_tools, 0, 1, Qt.AlignTop)
        self.page_header_layout.setColumnStretch(0, 1)
        root.addWidget(self.page_header_shell)

        self.panel_stack = _FittedStack()
        self.panel_stack.addWidget(self._build_indextts2_panel())   # 0 IndexTTS 2.5（默认）
        self.panel_stack.addWidget(self._build_voxcpm2_panel())     # 1 VoxCPM2
        self.panel_stack.addWidget(QWidget())                      # 2 retired engine slot; preserve local IDs
        # QStackedWidget 默认按最高页定高，会让矮的 IndexTTS 2.5 页也被高的 VoxCPM2 页撑出滚动。
        # 把非当前页设为 Ignored，使栈高度只跟随当前页，标准窗口下当前页不被另一页拖出滚动。
        self._size_panel_stack(0)
        root.addWidget(self.panel_stack)
        root.addStretch(0)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)
        self.indextts2_action_host = QWidget()
        actions = QVBoxLayout(self.indextts2_action_host)
        actions.setContentsMargins(24, 8, 24, 12)
        actions.addWidget(self.indextts2_generation_dock)
        self.indextts2_status.setWordWrap(True)
        self.indextts2_generation_dock.layout().addWidget(self.indextts2_status)
        self.indextts2_generation_dock.layout().addWidget(self.indextts2_progress)
        lay.addWidget(self.indextts2_action_host)
        self._reload_indextts2_styles(apply_last=True)
        self._set_indextts2_emotion_mode(self._indextts2_emotion_mode)
        self.indextts2_advanced_toggle.setChecked(self._indextts2_emotion_mode != "follow")
        self._update_page_trust(0)
        self._update_flow_shell()
        self._adopt_generation_result_controls()
        for button in (
            self.btn_indextts2_release_bg,
            self.btn_indextts2_cancel,
            self.btn_indextts2_generate,
        ):
            button.setFixedSize(142, 44)
        from .indextts25_workbench import IndexTTS25Workbench
        approved = IndexTTS25Workbench(self)
        index_layout = self.panel_stack.widget(0).layout()
        self._retired_index_surfaces = QWidget(self)
        self._retired_index_surfaces.hide()
        while index_layout.count():
            item = index_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(self._retired_index_surfaces)
        index_layout.addWidget(approved)
        self._approved_index = approved
        from .voxcpm2_workbench import VoxCPM2Workbench
        vox = VoxCPM2Workbench(self)
        vox_layout = self.panel_stack.widget(1).layout()
        self._retired_vox_surfaces = QWidget(self)
        self._retired_vox_surfaces.hide()
        while vox_layout.count():
            item = vox_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(self._retired_vox_surfaces)
        vox_layout.addWidget(vox)
        self._approved_vox = vox
        from .qwen3_workbench import Qwen3Workbench
        self._qwen3 = Qwen3Workbench(self)
        self.panel_stack.addWidget(self._qwen3)
        self.indextts2_action_host.hide()
        self._update_indextts2_generate_state()

    def _adopt_generation_result_controls(self) -> None:
        """Migrate the four result lanes while preserving owned signals and values."""
        lanes = (
            (self.progress, self.status, self.btn_generate, self.btn_cancel,
             self.voxcpm2_player_bar, self.btn_play, self.player_slider,
             (self.btn_voxcpm2_library,)),
            (self.indextts2_progress, self.indextts2_status,
             self.btn_indextts2_generate, self.btn_indextts2_cancel,
             self.indextts2_player_bar, self.btn_indextts2_play,
             self.indextts2_player_slider,
             (self.btn_indextts2_reveal, self.btn_indextts2_export, self.btn_indextts2_library)),
        )
        for bar, status, generate, cancel, player_bar, play, slider, actions in lanes:
            adopt_progress(bar, kind="neutral")
            bar.setAccessibleName("配音生成进度")
            bar.setTextVisible(False)
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setVisible(False)
            set_status_kind(status, "neutral")
            status.setAccessibleName("配音生成状态")
            adopt_button(generate, role="primary", size="lg", accessible_name="生成配音")
            adopt_button(cancel, role="secondary", size="lg", accessible_name="取消生成")
            adopt_surface(player_bar, "well")
            player_bar.setAccessibleName("配音试听与结果操作")
            adopt_button(play, role="secondary", accessible_name="播放或暂停生成结果")
            adopt_slider(slider, accessible_name="试听进度", value_formatter=_time_label)
            for action in actions:
                name = action.text().strip() or action.toolTip() or "结果操作"
                adopt_button(action, role="tertiary", accessible_name=name, tooltip=name)
            self._voice_generate_buttons[bar] = generate
            self._voice_status_labels[bar] = status

    def _extra_qss(self) -> str:
        base = f"""
QLabel#voiceSub {{ font-size:11px; color:{T.INK_FAINT}; background:transparent; }}
QLabel#voicePageSubtitle {{ font-size:13px; color:{T.INK_SOFT}; background:transparent; }}
QLabel#voicePrivacyBadge {{
    background:{T.CARD}; color:{T.GREEN}; border:1px solid {T.LINE};
    border-radius:12px; padding:7px 12px; font-size:11px; font-weight:600;
}}
QLabel#voicePrivacyBadge[cloud="true"] {{
    background:{T.AMBER_WASH}; color:{T.AMBER}; border-color:{T.AMBER};
}}
QFrame#voiceWorkspaceCard {{
    background:{T.CARD}; border:1px solid {T.LINE}; border-radius:16px;
}}
QFrame#voiceWorkspaceCard[voiceCardRole="result"] {{
    background:{T.CARD}; border-color:{T.LINE_SOFT}; min-height:132px;
}}
QFrame#voiceWorkspaceCard[voiceCardRole="workbench"] {{ border-color:{T.LINE}; }}
QFrame#voiceWorkspaceCard[voiceCardRole="section"] {{
    background:{T.BG_DEEP}; border:none; border-radius:12px;
}}
QFrame#voiceWorkspaceCard[voiceCardRole="trust"] {{
    background:{T.AMBER_WASH}; border:1px solid {T.AMBER}; border-radius:12px;
}}
QLabel#voiceSectionTitle {{ font-size:14px; font-weight:700; color:{T.INK}; background:transparent; }}
QLabel#voiceSectionSub {{ font-size:11px; color:{T.INK_FAINT}; background:transparent; }}
QLabel#voiceStepBadge {{
    background:{T.TERRA_WASH}; color:{T.TERRA}; border:none; border-radius:11px;
    min-width:22px; max-width:22px; min-height:22px; max-height:22px;
    font-size:11px; font-weight:700;
}}
QLabel#voiceFieldLabel {{ font-size:12px; font-weight:650; color:{T.INK_SOFT}; background:transparent; }}
QLabel#voiceResultTitle {{ font-size:14px; font-weight:700; color:{T.INK}; background:transparent; }}
QPushButton[luluControl="button"][controlVariant="engine"] {{
    background:{T.CARD}; color:{T.INK_SOFT}; border:1px solid {T.LINE};
    border-radius:12px; text-align:left; padding:8px 12px; font-size:11px; font-weight:500;
}}
QPushButton[luluControl="button"][controlVariant="engine"]:hover {{
    background:{T.TERRA_WASH}; color:{T.INK}; border-color:{T.TERRA_SOFT};
}}
QPushButton[luluControl="button"][controlVariant="engine"]:pressed {{
    background:{T.BG_DEEP}; border-color:{T.TERRA};
}}
QPushButton[luluControl="button"][controlVariant="engine"]:checked {{
    background:{T.TERRA_WASH}; color:{T.INK}; border-color:{T.TERRA}; font-weight:650;
}}
QPushButton#voiceEnvelopeTrigger {{
    background:transparent; color:{T.INK}; border:none; border-radius:14px;
    padding:0; text-align:left;
}}
QPushButton#voiceEnvelopeTrigger:hover {{ background:transparent; }}
QPushButton#voiceEnvelopeTrigger:focus {{ background:transparent; }}
QPushButton#voiceEnvelopeTrigger:pressed,
QPushButton#voiceEnvelopeTrigger[open="true"] {{ background:transparent; }}
QFrame[voiceEnvelopeSelected="true"] {{
    background:{T.TERRA_WASH}; border:1px solid {T.TERRA_SOFT}; border-radius:11px;
}}
QLabel#voiceEnvelopeTitle {{ color:{T.INK}; font-size:12px; font-weight:650; background:transparent; }}
QLabel#voiceEnvelopeAction {{ color:{T.TERRA}; font-size:11px; font-weight:650; background:transparent; }}
QFrame#voiceToneCard {{
    background:{T.CARD}; border:1px solid {T.LINE}; border-radius:12px;
}}
QFrame#voiceToneCard:hover {{ background:{T.HOVER_RAISE}; border-color:{T.TERRA_SOFT}; }}
QFrame#voiceToneCard[voiceState="selected"] {{ background:{T.TERRA_WASH}; border-color:{T.TERRA_SOFT}; }}
QLabel#voiceToneIcon {{ background:{T.TERRA_WASH}; border:1px solid {T.TERRA_SOFT}; border-radius:18px; }}
QLabel#voiceToneName {{ color:{T.INK}; font-size:13px; font-weight:700; background:transparent; }}
QLabel#voiceToneMeta {{ color:{T.INK_SOFT}; font-size:11px; background:transparent; }}
QPushButton#voiceEmotionChoice {{
    background:{T.CARD}; color:{T.INK_SOFT}; border:1px solid {T.LINE}; border-radius:10px;
    padding:0 8px; font-size:12px; font-weight:600;
}}
QPushButton#voiceEmotionChoice:hover {{ background:{T.HOVER_RAISE}; color:{T.INK}; border-color:{T.TERRA_SOFT}; }}
QPushButton#voiceEmotionChoice:pressed {{ background:{T.BG_DEEP}; border-color:{T.TERRA}; }}
QPushButton#voiceEmotionChoice:checked {{ background:{T.TERRA_WASH}; color:{T.TERRA}; border:2px solid {T.TERRA}; font-weight:700; }}
QFrame[luluControl="surface"][surfaceVariant="emotionStudio"] {{
    background:{T.CARD}; border:1px solid {T.LINE}; border-radius:14px;
}}
QFrame#voiceFollowPanel {{
    background:{T.BG}; border:1px solid {T.LINE_SOFT}; border-radius:11px;
}}
QLabel#voiceFollowTitle {{ color:{T.INK}; font-size:14px; font-weight:700; background:transparent; }}
QLabel#voiceFollowMeta {{ color:{T.INK_SOFT}; font-size:11px; background:transparent; }}
QLabel#voiceFollowTag {{
    background:{T.CARD}; color:{T.INK_SOFT}; border:1px solid {T.LINE_SOFT};
    border-radius:8px; padding:3px 10px; font-size:10px; font-weight:650;
}}
QFrame#voiceFollowFooter {{
    background:{T.BG}; border:1px solid {T.LINE_SOFT}; border-radius:10px;
}}
QLabel#voiceFollowFooterMeta {{ color:{T.INK_FAINT}; font-size:11px; background:transparent; }}
QLabel#voiceTemplateLabel {{ color:{T.INK}; font-size:12px; font-weight:700; background:transparent; }}
QPushButton[luluControl="button"][templateChip="true"] {{
    background:{T.BG}; color:{T.INK_SOFT}; border:1px solid {T.LINE};
    border-radius:10px; font-weight:600; padding:0 13px;
}}
QPushButton[luluControl="button"][templateChip="true"]:hover {{
    background:{T.HOVER_RAISE}; color:{T.INK}; border-color:{T.TERRA_SOFT};
}}
QPushButton[luluControl="button"][templateChip="true"]:pressed {{
    background:{T.BG_DEEP}; border-color:{T.TERRA};
}}
QPushButton[luluControl="button"][templateChip="true"]:checked {{
    background:{T.TERRA_WASH}; color:{T.TERRA}; border:1px solid {T.TERRA}; font-weight:700;
}}
QPushButton[luluControl="button"][controlVariant="templateManage"] {{
    background:{T.CARD}; color:{T.TERRA}; border:1px solid {T.TERRA_SOFT};
    border-radius:9px; font-weight:650;
}}
QPushButton[luluControl="button"][controlVariant="templateManage"]:hover {{
    background:{T.TERRA_WASH}; border-color:{T.TERRA};
}}
QFrame#voiceTemplateCurrent {{
    background:{T.BG}; border:1px solid {T.LINE_SOFT}; border-radius:11px;
}}
QLabel#voiceTemplateGlyph {{
    background:{T.TERRA_WASH}; border:1px solid {T.TERRA_SOFT}; border-radius:16px;
}}
QLabel#voiceTemplateName {{ color:{T.INK}; font-size:12px; font-weight:700; background:transparent; }}
QLabel#voiceTemplateSummary {{ color:{T.INK_SOFT}; font-size:11px; background:transparent; }}
QFrame#voiceStrengthBar {{
    background:{T.BG}; border:1px solid {T.LINE_SOFT}; border-radius:10px;
}}
QLabel#voiceStrengthTitle {{ color:{T.INK_SOFT}; font-size:11px; font-weight:650; background:transparent; }}
QLabel#voiceStrengthValue {{
    background:{T.TERRA_WASH}; color:{T.TERRA}; border:1px solid {T.TERRA_SOFT};
    border-radius:8px; padding:3px 8px; font-size:11px; font-weight:700;
}}
QFrame#voiceGenerationDock {{
    background:{T.BG}; border:1px solid {T.LINE}; border-radius:14px;
}}
QFrame#voiceOutputDestination {{
    background:{T.CARD}; border:1px solid {T.LINE_SOFT}; border-radius:11px;
}}
QLabel#voiceOutputGlyph {{
    background:{T.TERRA_WASH}; border:1px solid {T.TERRA_SOFT}; border-radius:17px;
}}
QLabel#voiceOutputTitle {{ color:{T.INK}; font-size:12px; font-weight:700; background:transparent; }}
QLabel#voiceOutputMeta {{ color:{T.INK_FAINT}; font-size:10px; background:transparent; }}
QLabel#voiceEngineState {{ color:{T.INK_SOFT}; font-size:11px; font-weight:600; background:transparent; }}
QLabel#voiceGateHint {{
    background:{T.TERRA_WASH}; color:{T.TERRA}; border:1px solid {T.TERRA_SOFT};
    border-radius:9px; padding:7px 10px; font-size:11px; font-weight:650;
}}
QPushButton[luluControl="button"][controlVariant="releaseMemory"] {{
    background:{T.CARD}; color:{T.INK_SOFT}; border:1px solid {T.LINE}; border-radius:10px; font-weight:650;
}}
QPushButton[luluControl="button"][controlVariant="releaseMemory"]:hover {{
    background:{T.HOVER_RAISE}; color:{T.INK}; border-color:{T.TERRA_SOFT};
}}
QPushButton[luluControl="button"][controlVariant="releaseMemory"]:pressed {{
    background:{T.BG_DEEP}; border-color:{T.TERRA};
}}
QPushButton[luluControl="button"][controlVariant="releaseMemory"]:disabled {{
    background:{T.BG}; color:{T.INK_FAINT}; border-color:{T.LINE_SOFT};
}}
QLabel#voiceResultStatus {{ color:{T.INK_SOFT}; font-size:11px; font-weight:650; background:transparent; }}
QFrame#voiceResultEmpty {{
    background:{T.BG}; border:1px solid {T.LINE_SOFT}; border-radius:13px;
}}
QLabel#voiceResultEmptyTitle {{ color:{T.INK}; font-size:13px; font-weight:700; background:transparent; }}
QLabel#voiceResultEmptyMeta {{ color:{T.INK_FAINT}; font-size:11px; background:transparent; }}
QLabel#expChip {{ background:{T.AMBER_WASH}; color:{T.AMBER}; font-size:10px; font-weight:600; border-radius:8px; padding:2px 8px; }}
QLabel#emoDropTitle {{ font-size:13px; font-weight:600; color:{T.INK}; }}
QLabel#emoDropSub {{ font-size:11px; color:{T.INK_FAINT}; }}
QPushButton#collapseBtn {{ background:{T.BG}; border:1px solid {T.LINE_SOFT}; border-radius:12px; color:{T.INK}; font-size:13px; font-weight:600; padding:8px 14px; text-align:left; }}
QPushButton#collapseBtn:hover {{ border-color:{T.TERRA_SOFT}; }}
QFrame#hr {{ background:{T.LINE_SOFT}; }}
QLabel#dim {{ font-size:12px; color:{T.INK_SOFT}; }}
QLabel#segIdx {{ font-size:11px; color:{T.INK_FAINT}; }}
/* 下拉：给 QComboBox 完整 styled 基底（关键：否则 macOS 用原生控件，QSS 箭头无效） */
QComboBox#linkInput {{
    background:{T.BG}; border:1px solid {T.LINE}; border-radius:10px;
    padding:8px 34px 8px 12px; font-size:13px; color:{T.INK}; min-height:18px;
}}
QComboBox#linkInput:hover {{ background:{T.HOVER_RAISE}; border:1px solid {T.TERRA_SOFT}; }}
QComboBox#linkInput:focus, QComboBox#linkInput:on {{ background:{T.HOVER_RAISE}; border:1px solid {T.TERRA_SOFT}; }}
QComboBox#linkInput::drop-down {{ border:none; background:transparent; width:30px; subcontrol-origin:padding; subcontrol-position:center right; }}
QComboBox#linkInput::down-arrow {{ image:url({icons.icon_file("caret_down", T.INK_SOFT, 12).replace(os.sep, "/")}); width:12px; height:12px; }}
QComboBox#linkInput::down-arrow:on {{ image:url({icons.icon_file("caret_down", T.TERRA, 12).replace(os.sep, "/")}); }}
QComboBox#linkInput QAbstractItemView {{
    background:{T.CARD}; border:1px solid {T.LINE}; border-radius:12px;
    padding:5px; margin-top:4px; outline:none; color:{T.INK};
    selection-background-color:{T.TERRA_WASH}; selection-color:{T.TERRA};
}}
QComboBox#linkInput QAbstractItemView::item {{ min-height:32px; padding:6px 12px; border-radius:8px; }}
QComboBox#linkInput QAbstractItemView::item:hover {{ background:{T.BG_DEEP}; color:{T.INK}; }}
QComboBox#linkInput QAbstractItemView::item:selected {{ background:{T.TERRA_WASH}; color:{T.TERRA}; }}
QPushButton#glossaryChip {{ background:{T.BG_DEEP}; border:1px solid transparent; border-radius:9px; color:{T.INK_SOFT}; font-size:12px; padding:7px 14px; }}
QPushButton#glossaryChip:hover {{ background:{T.TERRA_WASH}; color:{T.TERRA}; }}
QLabel#srcEmpty {{ font-size:12px; color:{T.INK_FAINT}; }}
"""
        semantic = base
        for old, new in (
            (T.TERRA_WASH, self._consumer.accent_wash),
            (T.TERRA_SOFT, self._consumer.accent_hairline),
            (T.TERRA_DEEP, self._consumer.accent_hover),
            (T.TERRA, self._consumer.accent),
            (T.BG_DEEP, self._consumer.surface_work),
            (T.BG, self._consumer.canvas),
            (T.CARD, self._consumer.surface_focal),
            (T.INK_FAINT, self._consumer.ink_muted),
            (T.INK_SOFT, self._consumer.ink_soft),
            (T.INK, self._consumer.ink),
            (T.LINE_SOFT, self._consumer.line),
            (T.LINE, self._consumer.line),
        ):
            semantic = semantic.replace(old, new)
        return semantic + T.build_consumer_core_workspace_qss(
            T.consumer_theme(T.ACTIVE.name)
        )

    def _card(self) -> QFrame:
        card = PaperTextureFrame(
            strength=self._consumer.paper_queue_opacity,
            corner_radius=self._consumer.radius_work,
            layer="work",
        )
        card.setObjectName("voiceWorkspaceCard")
        card.setProperty("voiceCardRole", "standard")
        return card

    def _section_header(self, step: int, title: str, subtitle: str = "") -> QWidget:
        """一个轻量工作流标题：只负责信息层级，不承载业务状态。"""
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        badge = QLabel(str(step))
        badge.setObjectName("voiceStepBadge")
        badge.setAlignment(Qt.AlignCenter)
        row.addWidget(badge, 0, Qt.AlignVCenter)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        heading = QLabel(title)
        heading.setObjectName("voiceSectionTitle")
        labels.addWidget(heading)
        if subtitle:
            detail = QLabel(subtitle)
            detail.setObjectName("voiceSectionSub")
            detail.setWordWrap(True)
            labels.addWidget(detail)
        row.addLayout(labels, 1)
        return wrap

    def _hr(self) -> QFrame:
        line = QFrame()
        line.setObjectName("hr")
        line.setFixedHeight(1)
        return line

    def _build_engine_switch(self) -> QWidget:
        box = QFrame()
        box.setObjectName("voiceEngineSwitch")
        box.setAccessibleName("配音引擎")
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        label = QLabel("配音引擎")
        label.setObjectName("voiceFieldLabel")
        row.addWidget(label, 0, Qt.AlignVCenter)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.engine_buttons = []
        engines = (
            ("自然情感", "IndexTTS 2.5 · 推荐", "IndexTTS 2.5 自然情感配音"),
            ("声音克隆", "VoxCPM2", "VoxCPM2 配音工作台"),
            ("", "", ""),  # retired ID 2; never offered as an engine
            ("经典情感", "IndexTTS 2", "IndexTTS 2 经典情感配音"),
            ("多语言声音", "Qwen3-TTS", "Qwen3-TTS 本地声音工作台"),
        )
        for idx, (name, model, accessible_name) in enumerate(engines):
            # Keep the established button/group truth for existing business callers;
            # the consumer SelectField is now the only visible presentation.
            btn = QPushButton(f"{name}\n{model}", box)
            btn.setCheckable(True)
            btn.setAccessibleName(f"配音方式：{accessible_name}")
            btn.setToolTip(accessible_name)
            btn.setVisible(False)
            btn.setEnabled(idx != 2)
            btn.clicked.connect(lambda _checked=False, i=idx: self._switch_panel(i))
            self.mode_group.addButton(btn, idx)
            self.engine_buttons.append(btn)
            if idx == 0:
                btn.setChecked(True)
        self.engine_select = ConsumerSelect(
            (
                ("3", "IndexTTS 2"),
                ("0", "IndexTTS 2.5"),
                ("1", "VoxCPM2"),
                ("4", "Qwen3-TTS"),
            ),
            value="0",
            accessible_name="配音引擎",
            corner_radius=15,
            popup_radius=18,
            clip_popup_corners=True,
            consumer_theme_scope="coreWorkspace",
            consumer_theme_name=T.ACTIVE.name,
        )
        self.engine_select.trigger.setObjectName("consumerSelectField")
        self.engine_select.setFixedHeight(44)
        self.engine_select.setProperty("voiceSelectVisual", "softRoundedCard")
        self.engine_select.setMinimumWidth(280)
        self.engine_select.setMaximumWidth(420)
        self.engine_select.value_changed.connect(lambda value: self._switch_panel(int(value)))
        row.addWidget(self.engine_select, 1)
        row.addStretch(1)
        self.engine_switch_box = box
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(box)
        wl.addStretch(1)
        return wrap

    def _reflow_engine_buttons(self) -> None:
        # Compatibility hook retained for callers from the first workbench pass.
        # The new SelectField has a stable single-row geometry and needs no reflow.
        return

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_page_header_layout()
        self._reflow_emotion_buttons()
        QTimer.singleShot(0, self._reflow_indextts2_workbench)

    def _reflow_indextts2_workbench(self) -> None:
        self._sync_page_header_layout()
        if hasattr(self, "_approved_index"):
            self._approved_index.reflow()
            return
        if not hasattr(self, "indextts2_workspace_layout"):
            return
        wide = self.page_scroll.viewport().width() >= 1150
        if self.indextts2_settings.property("wideLayout") == wide:
            return
        self.indextts2_settings.setProperty("wideLayout", wide)
        grid = self.indextts2_workspace_layout
        grid.removeWidget(self.indextts2_settings)
        grid.addWidget(self.indextts2_settings, 0 if wide else 1, 1 if wide else 0, Qt.AlignTop)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2 if wide else 0)
        self.indextts2_settings.setMaximumWidth(480 if wide else 16777215)
        self._refit_panel()

    def _sync_page_header_layout(self) -> None:
        """At the 620px center workspace, keep header tools on a second row."""
        if not hasattr(self, "page_header_layout"):
            return
        viewport_width = self.page_scroll.viewport().width()
        stacked = viewport_width < 720
        if bool(self.page_header_shell.property("stacked")) == stacked:
            return
        self.page_header_shell.setProperty("stacked", stacked)
        self.page_header_layout.removeWidget(self.page_header_tools)
        if stacked:
            self.page_header_layout.addWidget(
                self.page_header_tools,
                1,
                0,
                1,
                2,
                Qt.AlignLeft | Qt.AlignTop,
            )
        else:
            self.page_header_layout.addWidget(
                self.page_header_tools,
                0,
                1,
                Qt.AlignRight | Qt.AlignTop,
            )
        self.page_header_layout.invalidate()
        self.page_header_shell.updateGeometry()

    def _size_panel_stack(self, idx: int) -> None:
        # _FittedStack 已只返回当前页的 sizeHint。不再封死 maximumHeight，
        # 避免文案/情感面板展开时被旧高度裁切；外层滚动区负责窄窗口的纵向滚动。
        cur = self.panel_stack.widget(idx)
        if cur is None:
            return
        # 强制即时重算布局，确保展开/收起折叠区后读到的是新的真实高度（否则缓存导致裁切或残留滚动）
        cur_layout = cur.layout()
        if cur_layout is not None:
            cur_layout.invalidate()
            cur_layout.activate()
        self.panel_stack.setMaximumHeight(16777215)
        self.panel_stack.updateGeometry()
        canvas = self.page_scroll.widget()
        canvas.layout().invalidate()
        canvas.updateGeometry()

    def _refit_panel(self) -> None:
        # 当前页内容高度变化（切情感模式、切文本来源、状态横幅、结果区）后，同步把栈高度
        # 钉到当前页真实需要。同步计算（invalidate+activate）避免切换时高度跳变闪烁。
        if hasattr(self, "panel_stack"):
            self._size_panel_stack(self.panel_stack.currentIndex())

    def _switch_panel(self, idx: int) -> None:
        requested = max(0, min(4, int(idx)))
        if requested == 2:requested = 0  # old cloud links fall back to local IndexTTS
        current = 3 if self.panel_stack.currentIndex() == 0 and self._index_version == "2.0" else (4 if self.panel_stack.currentIndex() == 3 else self.panel_stack.currentIndex())
        if requested != current and (
            self._indextts2_status_checking
            or bool(getattr(getattr(self, "_qwen3", None), "busy", False))
            or any(worker is not None and worker.isRunning() for worker in (
                self._indextts2_worker, self._indextts2_release_worker, self._indextts2_config_worker))
        ):
            self.engine_select.set_value(str(current), emit=False)
            self.engine_buttons[current].setChecked(True)
            self.engine_select.setToolTip("当前正在检查、生成或释放模型，请完成后再切换")
            return
        if requested in (0, 3):
            version = "2.0" if requested == 3 else "2.5"
            if version != self._index_version:
                self._switch_index_version(version)
        idx = 0 if requested == 3 else (3 if requested == 4 else requested)
        if hasattr(self, "_approved_index"):
            self._approved_index.stop_playback()
        if hasattr(self, "_qwen3"):
            self._qwen3.stop_playback()
        # 切面板先停掉各引擎的试听，避免切走后旧面板音频继续播
        if hasattr(self, "_approved_vox"):
            self._approved_vox.voice_player.stop()
        for player in (getattr(self, "player", None), getattr(self, "indextts2_player", None)):
            if player is not None:
                player.stop()
        idx = max(0, min(self.panel_stack.count() - 1, int(idx)))
        if hasattr(self, "engine_select"):
            self.engine_select.set_value(str(requested), emit=False)
        if hasattr(self, "engine_buttons"):
            self.engine_buttons[requested].setChecked(True)
        self.panel_stack.setCurrentIndex(idx)
        self.engine_select.setToolTip("")
        self.indextts2_action_host.setVisible(idx == 0 and not hasattr(self, "_approved_index"))
        if idx != 0 and not (self._indextts2_worker and self._indextts2_worker.isRunning()):
            if self._indextts2_background_status_snapshot().get('alive'):
                self._release_indextts2_background()
        self._size_panel_stack(idx)
        self._update_page_trust(idx)
        self._update_flow_shell()
        obj = self.panel_stack.currentWidget().objectName()
        if obj == "indextts2ModelPanel":
            self._refresh_indextts2_status(force=False)
            self._update_indextts2_voice_status()
        elif obj == "qwen3ModelPanel":
            self._qwen3.refresh()
        self._emit_model_context_changed()

    @property
    def _index_name(self) -> str:
        return "IndexTTS 2" if self._index_version == "2.0" else "IndexTTS 2.5"

    def _switch_index_version(self, version: str) -> None:
        """Two versioned workspaces reuse the approved layout and shared input."""
        import copy
        self._index_pages[self._index_version] = {
            "source_mode": self._src["indextts2"]["mode"],
            "mode": self._indextts2_emotion_mode,
            "vector": [slider.value() for slider in self.indextts2_advanced_sliders],
            "weight": self.indextts2_weight_slider.value(),
            "emotion_text": self.indextts2_emotion_text_edit.toPlainText(),
            "audio": self._indextts2_emotion_audio_path,
            "audio_meta": copy.deepcopy(self._indextts2_emotion_audio_meta),
            "recorded": self._indextts2_emotion_audio_recorded,
            "language": self.indextts2_language_combo.currentData(),
            "speed": self.indextts2_speed_slider.value(),
            "random": self._approved_index.random_emotion,
            "results": copy.deepcopy(self._indextts2_results),
            "last": self._indextts2_last_output,
            "message": self.indextts2_status.text(),
            "style": copy.deepcopy(self._indextts2_current_style),
            "style_dirty": self._indextts2_style_dirty,
        }
        self._approved_index.stop_playback()
        if self._indextts2_background_status_snapshot().get("alive"):
            self._release_indextts2_background()
        self._index_version = version
        saved = self._index_pages.get(version, {})
        self._src_set_mode("indextts2", saved.get("source_mode", "library"))
        self._indextts2_status = None
        self._indextts2_status_error = ""
        self._indextts2_emotion_audio_path = saved.get("audio", "")
        self._indextts2_emotion_audio_meta = copy.deepcopy(saved.get("audio_meta", {}))
        self._indextts2_emotion_audio_recorded = saved.get("recorded", False)
        self.indextts2_emotion_text_edit.setPlainText(saved.get("emotion_text", ""))
        for slider, value in zip(self.indextts2_advanced_sliders, saved.get("vector", [0] * 8)):
            slider.setValue(value)
        self.indextts2_weight_slider.setValue(saved.get("weight", 80))
        self.indextts2_language_combo.setCurrentIndex(self.indextts2_language_combo.findData(saved.get("language", "ZH")))
        self.indextts2_speed_slider.setValue(saved.get("speed", 100))
        self._approved_index.random_emotion = saved.get("random", False)
        self._indextts2_current_style = copy.deepcopy(saved.get("style", {}))
        self._indextts2_style_dirty = saved.get("style_dirty", False)
        self._set_indextts2_emotion_mode(saved.get("mode", "follow"))
        self._render_indextts2_emoaudio_state()
        self._reset_indextts2_player()
        self._indextts2_results = copy.deepcopy(saved.get("results", []))
        self._indextts2_last_output = saved.get("last")
        self._render_indextts2_results()
        selected = next((item for item in self._indextts2_results if item.get("output_path") == self._indextts2_last_output), None)
        if selected:
            self._select_indextts2_result(selected)
        self._set_voice_progress_stopped(self.indextts2_progress)
        self.indextts2_status.setText(saved.get("message", "等待生成"))
        modern = version == "2.5"
        self._approved_index.sync_settings()
        self._approved_index.sound_cells[1].setEnabled(modern)
        self._approved_index.sound_cells[2].setEnabled(modern)
        self._approved_index.reflow()
        self.indextts2_glossary_panel.set_model_version(version)
        self.btn_indextts2_glossary.setEnabled(True)
        self.btn_indextts2_glossary.setToolTip("设置术语读音" if modern else "官方中文拼音与 Lulu 文字替换；原始文案不变")
        self._src["indextts2"]["segment"].setAccessibleName(self._index_name + "文本来源")
        self._approved_index.setAccessibleName(self._index_name + "配音工作台")
        self.btn_indextts2_release_bg.setToolTip("释放 " + self._index_name + " 后台；下次生成会重新加载")

    def _update_page_trust(self, idx: int) -> None:
        if not hasattr(self, "page_privacy_badge"):
            return
        self.page_privacy_badge.setProperty("cloud", False)
        self.page_privacy_badge.setText("● 本地离线 · 不上传")
        self.page_privacy_badge.setToolTip("当前引擎在本机生成，文本和音频不上传。")
        _repolish(self.page_privacy_badge)

    def _active_model_name(self) -> str:
        if not hasattr(self, "panel_stack"):
            return self._index_name
        obj = self.panel_stack.currentWidget().objectName()
        if obj == "qwen3ModelPanel":
            return "Qwen3-TTS"
        if obj == "voxcpm2ModelPanel":
            return "VoxCPM2"
        return self._index_name

    def current_model_context(self):
        """当前配音面板的模型状态快照；只读取已有就绪真相，不启动检查或联网。"""
        from .model_context import local_model_context

        if not hasattr(self, "panel_stack"):
            return local_model_context(
                "文案配音", f"{self._index_name}", ready=False, purpose_index=1
            )
        obj = self.panel_stack.currentWidget().objectName()
        if obj == "qwen3ModelPanel":
            ready, _ = self.model_readiness(self._qwen3.mode)
            return local_model_context("文案配音", "Qwen3-TTS", ready=ready, purpose_index=1)
        if obj == "voxcpm2ModelPanel":
            ready, runtime_state = self.model_readiness('voxcpm2')
            clean_missing = runtime_state in {'not_installed', '正在检查模型…'}
            return local_model_context(
                "文案配音",
                "VoxCPM2",
                ready=ready,
                broken=not ready and not clean_missing,
                purpose_index=1,
            )
        status = self._indextts2_status
        return local_model_context(
            "文案配音",
            f"{self._index_name}",
            ready=self._indextts2_environment_ready(),
            broken=bool(self._indextts2_status_error),
            purpose_index=1,
        )

    def _emit_model_context_changed(self) -> None:
        self.model_context_changed.emit(self.current_model_context())

    def _update_flow_shell(self, *, source: str | None = None) -> None:
        if source and hasattr(self, "indextts2_source_hint"):
            self.indextts2_source_hint.setText(source)

    def _open_voice_library(self) -> None:
        if hasattr(self.main, "open_voice_library"):
            self.main.open_voice_library()
            return
        path = voices.current_voice_library_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(path)], check=False)
        except OSError:
            dialogs.alert(self, "配音素材库", "无法打开配音素材库，请确认该文件夹仍在本机且当前用户有权限访问。")

    def _open_voice_library_dir(self) -> None:
        path = voices.current_voice_library_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(path)], check=False)
        except OSError:
            dialogs.alert(self, "音色库", "无法打开音色库位置，请确认该文件夹仍在本机且当前用户有权限访问。")

    def _change_voice_library_dir(self) -> bool:
        busy = getattr(self.main, "storage_relocation_active", None)
        try:
            storage_busy = bool(busy()) if callable(busy) else bool(
                getattr(self.main, "_storage_relocation_in_progress", False)
            )
        except Exception:  # noqa: BLE001 - a broken gate must fail closed
            storage_busy = True
        if storage_busy:
            dialogs.alert(
                self,
                "音色库暂不可操作",
                "设置中心正在迁移本地模型或音色库，请等迁移结束后再继续。",
            )
            return False
        current = str(voices.current_voice_library_dir())
        chosen = QFileDialog.getExistingDirectory(self, "选择音色库默认保存位置", current)
        if not chosen:
            return False
        try:
            new_root = voices.relocate_voice_library(chosen)
        except ValueError as exc:
            dialogs.alert(self, "音色库保存位置", str(exc))
            return False
        self.cfg.voice_library_dir = str(new_root)
        self.cfg.save()
        self.refresh()
        return True

    # ====================================================== VoxCPM2 面板（轻量精修）

    def _build_voxcpm2_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("voxcpm2ModelPanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(self._build_synthesis_card())
        v.addWidget(self._build_voxcpm2_result_card())
        return panel

    def _build_voice_card(self) -> QWidget:
        # 音色卡与 IndexTTS 2.5 统一：只放下拉 + 新建/上传 + 删除；新建走弹窗，不在卡里铺一堆字段。
        card = self._card()
        card.setProperty("voiceCardRole", "section")
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 14, 20, 14)
        v.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.voice_combo = QComboBox()  # 隐藏数据源；选择走音色库面板
        self.voice_combo.setVisible(False)
        row.addWidget(self._build_voice_selector(self.voice_combo), 1)
        self.voxcpm2_ready_chip = QLabel("● 检查中")
        set_status_kind(self.voxcpm2_ready_chip, "info")
        row.addWidget(self.voxcpm2_ready_chip)
        v.addLayout(row)
        return card

    def _voxcpm2_format(self) -> str:
        for key, btn in getattr(self, "voxcpm2_format_buttons", {}).items():
            if btn.isChecked():
                return key
        return "wav"

    def _build_synthesis_card(self) -> QWidget:
        card = self._card()
        card.setProperty("voiceCardRole", "workbench")
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)
        v.addWidget(self._section_header(1, "配音文案"))

        # 文本三来源（与 IndexTTS 2.5 一致）
        text_row = QHBoxLayout()
        text_row.setSpacing(10)
        text_col = QVBoxLayout()
        text_col.setSpacing(8)
        block = self._build_source_block("voxcpm2", on_text=None, placeholder="输入要生成配音的文本…", custom_height=110)
        text_col.addWidget(block)
        self.text_edit = self._src["voxcpm2"]["text_edit"]
        text_row.addLayout(text_col, 1)
        v.addLayout(text_row)

        v.addWidget(self._hr())
        v.addWidget(self._section_header(2, "选择音色", "轻量克隆会直接模仿参考声音"))
        v.addWidget(self._build_voice_card())
        v.addWidget(self._hr())

        # 输出：格式分段 + 紧凑目录按钮 + 模型目录（重做原被挤压的下拉与长条）
        out_row = QHBoxLayout()
        out_row.setSpacing(10)
        fmt_label = QLabel("输出")
        fmt_label.setObjectName("voiceFieldLabel")
        out_row.addWidget(fmt_label)
        fmt_seg = QFrame()
        fmt_seg.setObjectName("segBox")
        fmt_h = QHBoxLayout(fmt_seg)
        fmt_h.setContentsMargins(4, 4, 4, 4)
        fmt_h.setSpacing(2)
        self.voxcpm2_format_group = QButtonGroup(self)
        self.voxcpm2_format_group.setExclusive(True)
        self.voxcpm2_format_buttons: dict[str, QPushButton] = {}
        for key in ("wav", "mp3"):
            b = QPushButton(key.upper())
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.voxcpm2_format_group.addButton(b)
            self.voxcpm2_format_buttons[key] = b
            fmt_h.addWidget(b)
            if key == "wav":
                b.setChecked(True)
        out_row.addWidget(fmt_seg)
        out_row.addStretch(1)
        self.out_dir_btn = result_style.OutputLocationButton()
        self.out_dir_btn.setObjectName("act")
        self.out_dir_btn.setCursor(Qt.PointingHandCursor)
        self.out_dir_btn.setIcon(QIcon(icons.pixmap("folder", T.INK_SOFT, 13)))
        self.out_dir_btn.clicked.connect(self._pick_output_dir)
        out_row.addWidget(self.out_dir_btn)
        model_dir_btn = QPushButton("  模型目录")
        model_dir_btn.setObjectName("act")
        model_dir_btn.setCursor(Qt.PointingHandCursor)
        model_dir_btn.setIcon(QIcon(icons.pixmap("hdd", T.INK_SOFT, 13)))
        model_dir_btn.clicked.connect(self._pick_model_dir)
        out_row.addWidget(model_dir_btn)
        v.addLayout(out_row)
        self._update_output_dir_btn()

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("  取消")
        self.btn_cancel.setObjectName("act")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 13)))
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setVisible(False)
        btns.addWidget(self.btn_cancel)
        self.btn_generate = QPushButton("  生成配音")
        self.btn_generate.setObjectName("grabBtn")
        self.btn_generate.setCursor(Qt.PointingHandCursor)
        self.btn_generate.setIcon(QIcon(icons.pixmap("play", "#FFFFFF", 13)))
        self.btn_generate.clicked.connect(self._generate)
        btns.addWidget(self.btn_generate)
        v.addLayout(btns)
        return card

    def _build_voxcpm2_result_card(self) -> QWidget:
        # 与 IndexTTS 2.5 一致：生成结果/试听做成单独一块
        card = self._card()
        card.setProperty("voiceCardRole", "result")
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(9)
        result_title = QLabel("生成结果")
        result_title.setObjectName("voiceResultTitle")
        v.addWidget(result_title)
        self.progress = QProgressBar()
        self.progress.setObjectName("taskBar")
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        v.addWidget(self.progress)
        self.status = QLabel("等待生成…")
        self.status.setObjectName("pageMeta")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.voxcpm2_result_hint = QLabel("选择文案和音色，然后生成配音")
        self.voxcpm2_result_hint.setObjectName("voiceSub")
        self.voxcpm2_result_hint.setWordWrap(True)
        v.addWidget(self.voxcpm2_result_hint)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.durationChanged.connect(self._on_player_duration)
        self.player.positionChanged.connect(self._on_player_position)
        self.player.playbackStateChanged.connect(self._on_player_state)

        self.voxcpm2_player_bar = QFrame()
        self.voxcpm2_player_bar.setObjectName("voicePlayerBar")
        self.player_row = QHBoxLayout(self.voxcpm2_player_bar)
        self.player_row.setContentsMargins(12, 9, 12, 9)
        self.player_row.setSpacing(12)
        self.btn_play = QPushButton("  播放")
        self.btn_play.setObjectName("voicePlayBtn")
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setIcon(QIcon(icons.pixmap("play", self._consumer.accent, 13)))
        self.btn_play.clicked.connect(self._toggle_play)
        self.player_row.addWidget(self.btn_play)
        self.player_slider = QSlider(Qt.Horizontal)
        self.player_slider.setObjectName("voiceProgress")
        self.player_slider.setRange(0, 0)
        self.player_slider.sliderPressed.connect(self._on_slider_pressed)
        self.player_slider.sliderReleased.connect(self._on_slider_released)
        self.player_row.addWidget(self.player_slider, 1)
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("pageMeta")
        self.player_row.addWidget(self.time_label)
        self.btn_voxcpm2_library = QPushButton("  素材库")
        self.btn_voxcpm2_library.setObjectName("act")
        self.btn_voxcpm2_library.setCursor(Qt.PointingHandCursor)
        self.btn_voxcpm2_library.setIcon(QIcon(icons.pixmap("layers", T.INK_SOFT, 13)))
        self.btn_voxcpm2_library.clicked.connect(self._open_voice_library)
        self.player_row.addWidget(self.btn_voxcpm2_library)
        v.addWidget(self.voxcpm2_player_bar)
        self._set_player_visible(False)
        return card


    def _build_indextts2_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("indextts2ModelPanel")
        root = QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addWidget(self._build_indextts2_main_card())
        root.addWidget(self._build_indextts2_result_card())
        return panel

    def _row_label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("voiceFieldLabel")
        lab.setMinimumWidth(32)
        return lab

    def _build_indextts2_main_card(self) -> QWidget:
        card = self._card()
        card.setProperty("voiceCardRole", "workbench")
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)

        self.indextts2_workspace_layout = QGridLayout()
        self.indextts2_workspace_layout.setHorizontalSpacing(24)
        self.indextts2_workspace_layout.setVerticalSpacing(20)
        editor = QWidget()
        text_layout = QVBoxLayout(editor)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(12)
        text_layout.addWidget(self._section_header(1, "配音文案"))
        text_layout.addLayout(self._build_indextts2_text_row())
        text_hint = QLabel("选中一小段先试听，确认声音后再生成全文。原文和历史音频都会保留。")
        text_hint.setObjectName("pageMeta")
        text_hint.setWordWrap(True)
        text_layout.addWidget(text_hint)
        text_layout.addStretch(1)
        self.indextts2_workspace_layout.addWidget(editor, 0, 0)
        self.indextts2_settings = QWidget()
        settings = QVBoxLayout(self.indextts2_settings)
        settings.setContentsMargins(0, 0, 0, 0)
        settings.setSpacing(12)
        settings.addWidget(self._section_header(2, "声音设置"))
        settings.addLayout(self._build_indextts2_voice_row())
        settings.addWidget(self._build_indextts25_speech_controls())
        # Readiness is already expressed beside「音色管理」and in the generation
        # gate. Keep the detailed diagnostic widgets alive for existing status
        # logic, but remove their duplicated full-width block from the canvas.
        self.indextts2_status_banner_host = self._build_indextts2_status_banner()
        self.indextts2_status_banner_host.setVisible(False)
        settings.addWidget(self.indextts2_status_banner_host)
        self.indextts2_advanced_toggle = QPushButton("表达方式 · 自然跟随")
        self.indextts2_advanced_toggle.setCheckable(True)
        adopt_button(self.indextts2_advanced_toggle, role="secondary", accessible_name="展开或收起表达方式设置")
        settings.addWidget(self.indextts2_advanced_toggle)
        self.indextts2_advanced_body = QWidget()
        advanced = QVBoxLayout(self.indextts2_advanced_body)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.addLayout(self._build_indextts2_emotion_row())
        self.indextts2_advanced_body.hide()
        self.indextts2_advanced_toggle.toggled.connect(self.indextts2_advanced_body.setVisible)
        self.indextts2_advanced_toggle.toggled.connect(lambda _checked: self._refit_panel())
        settings.addWidget(self.indextts2_advanced_body)
        self.indextts2_workspace_layout.addWidget(self.indextts2_settings, 1, 0, Qt.AlignTop)
        v.addLayout(self.indextts2_workspace_layout)

        generation_dock = QFrame()
        self.indextts2_generation_dock = generation_dock
        generation_dock.setObjectName("voiceGenerationDock")
        generation_dock.setAccessibleName("IndexTTS 2.5保存与生成操作")
        generation_layout = QVBoxLayout(generation_dock)
        generation_layout.setContentsMargins(12, 12, 12, 12)
        generation_layout.setSpacing(10)

        output_bar = QFrame()
        output_bar.setObjectName("voiceOutputDestination")
        output_bar.setAccessibleName("IndexTTS 2.5输出设置")
        output = QHBoxLayout(output_bar)
        output.setContentsMargins(12, 9, 10, 9)
        output.setSpacing(11)
        output_glyph = QLabel()
        output_glyph.setObjectName("voiceOutputGlyph")
        output_glyph.setFixedSize(34, 34)
        output_glyph.setAlignment(Qt.AlignCenter)
        output_glyph.setPixmap(icons.pixmap("folder", self._consumer.accent, 15))
        output.addWidget(output_glyph)
        output_copy = QVBoxLayout()
        output_copy.setSpacing(1)
        output_label = QLabel("保存位置")
        output_label.setObjectName("voiceOutputTitle")
        output_copy.addWidget(output_label)
        output_format = QLabel("WAV · 本机")
        output_format.setObjectName("voiceOutputMeta")
        output_copy.addWidget(output_format)
        output.addLayout(output_copy)
        output.addStretch(1)
        self.btn_indextts2_output_dir = result_style.OutputLocationButton()
        adopt_button(
            self.btn_indextts2_output_dir,
            role="tertiary",
            accessible_name="选择IndexTTS 2.5配音保存位置",
            tooltip="更改配音保存位置",
        )
        self.btn_indextts2_output_dir.setIcon(QIcon(icons.pixmap("folder", T.INK_SOFT, 13)))
        self.btn_indextts2_output_dir.clicked.connect(self._pick_output_dir)
        output.addWidget(self.btn_indextts2_output_dir)
        generation_layout.addWidget(output_bar)
        self._update_indextts2_output_dir_btn()

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.indextts2_bg_status = ElideLabel("后台未加载")
        self.indextts2_bg_status.setObjectName("voiceEngineState")
        self.indextts2_bg_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.indextts2_bg_status.setToolTip("连续生成时会复用已加载模型；也可以手动释放内存。")
        foot.addWidget(self.indextts2_bg_status, 1)
        self.btn_indextts2_release_bg = QPushButton("  释放内存")
        self.btn_indextts2_release_bg.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_release_bg.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 13)))
        self.btn_indextts2_release_bg.setToolTip("关闭 IndexTTS 2.5 常驻后台并释放内存；下次生成会重新加载，可能会慢一些。")
        self.btn_indextts2_release_bg.clicked.connect(self._release_indextts2_background)
        self.btn_indextts2_release_bg.setEnabled(False)
        adopt_button(
            self.btn_indextts2_release_bg,
            role="secondary",
            size="lg",
            variant="releaseMemory",
            accessible_name="释放IndexTTS 2.5后台内存",
        )
        self.btn_indextts2_release_bg.setFixedSize(142, 44)
        foot.addWidget(self.btn_indextts2_release_bg)
        self.btn_indextts2_preview = QPushButton("先试听")
        adopt_button(self.btn_indextts2_preview, role="secondary", size="lg",
                     accessible_name="生成选中内容试听，没有选中时试听开头",
                     tooltip="有选中内容时试听选段；否则试听开头最多 80 字。试听不会自动加入素材库。")
        self.btn_indextts2_preview.setFixedSize(110, 44)
        self.btn_indextts2_preview.clicked.connect(lambda: self._generate_indextts2(preview=True))
        foot.addWidget(self.btn_indextts2_preview)
        self.btn_indextts2_cancel = QPushButton("  取消")
        self.btn_indextts2_cancel.setObjectName("act")
        self.btn_indextts2_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_cancel.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 13)))
        self.btn_indextts2_cancel.clicked.connect(self._cancel_indextts2)
        self.btn_indextts2_cancel.setVisible(False)
        self.btn_indextts2_cancel.setFixedSize(142, 44)
        foot.addWidget(self.btn_indextts2_cancel)
        self.btn_indextts2_generate = QPushButton("  生成配音")
        self.btn_indextts2_generate.setObjectName("grabBtn")
        self.btn_indextts2_generate.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_generate.setIcon(QIcon(icons.pixmap("play", "#FFFFFF", 13)))
        self.btn_indextts2_generate.clicked.connect(self._generate_indextts2)
        self.btn_indextts2_generate.setFixedSize(142, 44)
        foot.addWidget(self.btn_indextts2_generate)

        self.indextts2_gate_hint = QLabel("")
        self.indextts2_gate_hint.setObjectName("voiceGateHint")
        self.indextts2_gate_hint.setWordWrap(True)
        self.indextts2_gate_hint.setVisible(False)
        generation_layout.addWidget(self.indextts2_gate_hint)
        generation_layout.addLayout(foot)
        return card

    def _build_indextts25_speech_controls(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        language_row = QHBoxLayout()
        language_row.addWidget(self._row_label("语言"))
        self.indextts2_language_combo = QComboBox()
        for label, code in (("中文", "ZH"), ("English", "EN"), ("日本語", "JA"),
                            ("Español", "ES"), ("العربية", "AR")):
            self.indextts2_language_combo.addItem(label, code)
        self.indextts2_language_combo.setAccessibleName("配音语言，不翻译原文")
        adopt_select_field(self.indextts2_language_combo, accessible_name="配音语言，不翻译原文")
        language_row.addWidget(self.indextts2_language_combo, 1)
        layout.addLayout(language_row)
        language_hint = QLabel("按所选语言朗读，不会翻译文案。")
        language_hint.setObjectName("pageMeta")
        language_hint.setWordWrap(True)
        layout.addWidget(language_hint)
        speed_row = QHBoxLayout()
        speed_row.addWidget(self._row_label("语速"))
        self.indextts2_speed_slider = QSlider(Qt.Horizontal)
        self.indextts2_speed_slider.setRange(75, 125)
        self.indextts2_speed_slider.setSingleStep(5)
        self.indextts2_speed_slider.setPageStep(5)
        self.indextts2_speed_slider.setValue(100)
        adopt_slider(self.indextts2_speed_slider, accessible_name="相对语速，越大越快",
                     value_formatter=lambda value: f"{value / 100:.2f} 倍")
        self.indextts2_speed_value = QLabel("1.00 倍")
        self.indextts2_speed_value.setObjectName("pageMeta")
        self.indextts2_speed_slider.valueChanged.connect(
            lambda value: self.indextts2_speed_value.setText(f"{value / 100:.2f} 倍"))
        speed_row.addWidget(self.indextts2_speed_slider, 1)
        speed_row.addWidget(self.indextts2_speed_value)
        reset = QPushButton("还原")
        adopt_button(reset, role="tertiary", accessible_name="恢复正常语速")
        reset.clicked.connect(lambda: self.indextts2_speed_slider.setValue(100))
        speed_row.addWidget(reset)
        layout.addLayout(speed_row)
        hint = QLabel("慢一些  0.75 · 正常  1.00 · 快一些  1.25\n相对调整节奏，实际时长取决于文案和音色。")
        hint.setObjectName("pageMeta")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    def _build_indextts2_voice_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.indextts2_voice_combo = QComboBox()  # 隐藏，仅作数据源；真实选择走音色库面板
        self.indextts2_voice_combo.setVisible(False)
        self.indextts2_voice_combo.currentIndexChanged.connect(self._update_indextts2_voice_status)
        row.addWidget(self._build_voice_selector(self.indextts2_voice_combo), 1)
        self.indextts2_ready_chip = QLabel("● 检查中")
        set_status_kind(self.indextts2_ready_chip, "info")
        row.addWidget(self.indextts2_ready_chip)
        return row

    # ------------------------------------------------------------------ 音色库选择器

    def _build_voice_selector(self, combo: QComboBox) -> QWidget:
        """Pair the shared consumer dropdown with a distinct management action."""
        row = QFrame()
        row.setObjectName("voiceToneRow")
        row.setProperty("voiceState", "empty")
        row.setFixedHeight(42)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        select_host = QWidget(row)
        select_layout = QVBoxLayout(select_host)
        select_layout.setContentsMargins(0, 0, 0, 0)
        select_layout.setSpacing(0)
        h.addWidget(select_host, 1)
        manage_btn = QPushButton("音色管理")
        adopt_button(
            manage_btn,
            role="secondary",
            accessible_name="打开音色库",
            tooltip="打开音色库：选择 / 新建 / 上传 / 删除音色",
        )
        manage_btn.setIcon(QIcon(icons.pixmap("layers", self._consumer.accent, 14)))
        manage_btn.setFixedSize(124, 38)
        manage_btn.clicked.connect(lambda: self._open_voice_picker(combo))
        h.addWidget(manage_btn, 0, Qt.AlignVCenter)
        self._voice_sel[id(combo)] = {
            "root": row,
            "select_host": select_host,
            "select_layout": select_layout,
            "selector": None,
            "signature": (),
            "manage": manage_btn,
        }
        combo.currentIndexChanged.connect(lambda *_: self._update_voice_selector(combo))
        self._update_voice_selector(combo)
        return row

    def _update_voice_selector(self, combo: QComboBox) -> None:
        refs = self._voice_sel.get(id(combo))
        if not refs:
            return
        signature = tuple(
            (str(combo.itemData(index) or ""), combo.itemText(index))
            for index in range(combo.count())
            if str(combo.itemData(index) or "")
        )
        selected_value = str(combo.currentData() or "")
        if not signature:
            signature = (("", "暂无可用音色"),)
            selected_value = ""
        selector = refs.get("selector")
        if selector is None or refs.get("signature") != signature:
            replacement = ConsumerSelect(
                signature,
                value=selected_value,
                accessible_name="选择音色",
                consumer_theme_scope="coreWorkspace",
                consumer_theme_name=T.ACTIVE.name,
            )
            replacement.trigger.setObjectName("consumerSelectField")
            replacement.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            replacement.value_changed.connect(
                lambda value, source=combo: self._apply_voice_selection(source, value)
            )
            replacement.setEnabled(bool(selected_value or signature[0][0]))
            if selector is None:
                refs["select_layout"].addWidget(replacement)
            else:
                selector.popup.close()
                refs["select_layout"].replaceWidget(selector, replacement)
                selector.setParent(None)
                selector.deleteLater()
            refs["selector"] = replacement
            refs["signature"] = signature
            selector = replacement
        else:
            selector.set_value(selected_value, emit=False)
            selector.setEnabled(bool(selected_value or signature[0][0]))
        title = combo.currentText().strip() if selected_value else "暂无可用音色"
        detail = f"当前音色：{title}" if selected_value else "当前没有可用音色；可打开音色管理添加"
        selector.trigger.setToolTip(detail)
        selector.trigger.setAccessibleDescription(detail)
        refs["root"].setProperty("voiceState", "selected" if selected_value else "empty")
        refs["root"].setAccessibleName(detail)
        refs["root"].style().unpolish(refs["root"])
        refs["root"].style().polish(refs["root"])

    def _apply_voice_selection(self, combo: QComboBox, voice_id: str) -> None:
        index = combo.findData(voice_id)
        if index >= 0 and index != combo.currentIndex():
            combo.setCurrentIndex(index)

    def _open_voice_picker(self, combo: QComboBox) -> None:
        """大屏音色库：保留原音色业务，只升级选择与管理交互。"""
        existing = self._voice_library_dialog
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        dlg = _VoiceLibraryModal(self)
        # The library opened from the manage control is a true window-modal
        # workflow.  The dialog class stays reusable for non-modal previews,
        # while this product entry point blocks background interaction.
        dlg.setWindowModality(Qt.WindowModal)
        dlg.layout().setContentsMargins(22, 22, 22, 22)
        self._voice_library_dialog = dlg
        from .voice_library import VoiceLibraryController
        dlg.controller = VoiceLibraryController(self, combo, dlg)

        def clear_dialog_ref(*_args) -> None:
            if self._voice_library_dialog is dlg:
                self._voice_library_dialog = None

        dlg.finished.connect(clear_dialog_ref)
        dlg.show()

    def _build_indextts2_status_banner(self) -> QWidget:
        banner = QFrame()
        adopt_surface(banner, "well", variant="status")
        set_surface_status(banner, "info")
        self.indextts2_status_banner = banner
        v = QVBoxLayout(banner)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(6)
        self.indextts2_status_title = QLabel("正在检查 IndexTTS 2.5 本地环境")
        self.indextts2_status_title.setObjectName("taskName")
        self.indextts2_status_title.setWordWrap(True)
        v.addWidget(self.indextts2_status_title)
        self.indextts2_status_detail = QLabel("检查 runtime 与 model 是否已放入 Lulu 受管目录。")
        self.indextts2_status_detail.setObjectName("pageMeta")
        self.indextts2_status_detail.setWordWrap(True)
        v.addWidget(self.indextts2_status_detail)
        rows = QHBoxLayout()
        rows.setSpacing(14)
        self.indextts2_runtime_status = QLabel("runtime  检查中")
        self.indextts2_runtime_status.setObjectName("pageMeta")
        rows.addWidget(self.indextts2_runtime_status)
        self.indextts2_model_status = QLabel("model    检查中")
        self.indextts2_model_status.setObjectName("pageMeta")
        rows.addWidget(self.indextts2_model_status)
        self.indextts2_config_status = QLabel("config   检查中")
        self.indextts2_config_status.setObjectName("pageMeta")
        rows.addWidget(self.indextts2_config_status)
        rows.addStretch(1)
        v.addLayout(rows)
        act_row = QHBoxLayout()
        act_row.addStretch(1)
        self.btn_indextts2_refresh = QPushButton("  重新检查")
        self.btn_indextts2_refresh.setObjectName("act")
        self.btn_indextts2_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_refresh.setIcon(QIcon(icons.pixmap("search", T.INK_SOFT, 13)))
        self.btn_indextts2_refresh.clicked.connect(lambda: self._refresh_indextts2_status(force=True))
        act_row.addWidget(self.btn_indextts2_refresh)
        self.btn_indextts2_sync_config = QPushButton("  同步配置")
        self.btn_indextts2_sync_config.setObjectName("act")
        self.btn_indextts2_sync_config.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_sync_config.setIcon(QIcon(icons.pixmap("check", T.INK_SOFT, 13)))
        self.btn_indextts2_sync_config.clicked.connect(self._sync_indextts2_config)
        self.btn_indextts2_sync_config.setVisible(False)
        act_row.addWidget(self.btn_indextts2_sync_config)
        self.btn_indextts2_help = QPushButton("  准备说明")
        self.btn_indextts2_help.setObjectName("act")
        self.btn_indextts2_help.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_help.setIcon(QIcon(icons.pixmap("file", T.INK_SOFT, 13)))
        self.btn_indextts2_help.clicked.connect(self._show_indextts2_help)
        act_row.addWidget(self.btn_indextts2_help)
        v.addLayout(act_row)
        return banner

    def _build_indextts2_text_row(self) -> QHBoxLayout:
        # 文本三来源（素材库/上传/自定义），用通用 source block 构建，与 VoxCPM2 保持一致。
        row = QHBoxLayout()
        row.setSpacing(10)
        row.setAlignment(Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(8)
        block = self._build_source_block(
            "indextts2", on_text=self._on_indextts2_text_changed,
            placeholder="输入文案。选中一句可先试听，再生成全文…", custom_height=260)
        col.addWidget(block)
        block.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.indextts2_text_edit = self._src["indextts2"]["text_edit"]

        # 术语读音常驻面板（供生成时 apply_to_text），入口与来源切换同一工具行。
        self.indextts2_glossary_panel = GlossaryPanel(self, pronunciation_enabled=True)
        self.indextts2_glossary_panel.bind_preview_button(self._preview_indextts2_glossary)
        self.indextts2_glossary_panel.changed.connect(self._on_indextts2_glossary_changed)
        self.indextts2_glossary_panel.setVisible(False)

        tools = self._src["indextts2"]["toolbar"]
        self.indextts2_voice_status = QLabel("请选择音色。")
        self.indextts2_voice_status.setObjectName("voiceSub")
        self.indextts2_voice_status.setWordWrap(True)
        # Preserve the status object for generation gating, but remove the
        # repetitive helper sentence from the primary canvas.
        self.indextts2_voice_status.setVisible(False)
        self.indextts2_text_length_hint = QLabel("0 / 2000")
        self.indextts2_text_length_hint.setObjectName("pageMeta")
        tools.addWidget(self.indextts2_text_length_hint, 0, Qt.AlignVCenter)
        self.btn_indextts2_glossary = QPushButton("  术语读音")
        adopt_button(
            self.btn_indextts2_glossary,
            role="secondary",
            accessible_name="设置术语读音",
            tooltip="为生僻词 / 品牌名指定读音（生成前文本替换）",
        )
        self.btn_indextts2_glossary.setFixedHeight(44)
        self.btn_indextts2_glossary.setIcon(QIcon(icons.pixmap("pen", T.INK_SOFT, 13)))
        self.btn_indextts2_glossary.clicked.connect(self._open_indextts2_glossary_dialog)
        tools.addWidget(self.btn_indextts2_glossary)
        self.btn_indextts2_text_tips = QPushButton("  能力说明")
        adopt_button(self.btn_indextts2_text_tips, role="tertiary", accessible_name="查看文本能力说明")
        self.btn_indextts2_text_tips.clicked.connect(self._show_indextts2_text_tips)
        self.btn_indextts2_text_tips.setVisible(False)
        self.indextts2_source_hint = QLabel("")
        self.indextts2_source_hint.setVisible(False)
        row.addLayout(col, 1)
        return row

    # ------------------------------------------------------------------ 通用文本三来源块

    def _build_source_block(self, prefix: str, *, on_text=None, placeholder: str = "", custom_height: int = 90) -> QWidget:
        """构建「素材库选择 / 拖入·上传 / 自定义」三来源块，样式对齐文案处理页。
        widget/状态存进 self._src[prefix]；生成时读 self._src_text(prefix)。"""
        st: dict = {"mode": "library", "picked": None, "upload_text": "", "upload_name": "", "on_text": on_text}
        canvas_height = max(VOICE_SOURCE_CANVAS_HEIGHT, custom_height)
        self._src[prefix] = st
        wrap = QWidget()
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        source_label = {
            "voxcpm2": "VoxCPM2",
            "qwen3": "Qwen3-TTS",
            "indextts2": "IndexTTS 2.5",
        }.get(prefix, "配音")
        seg = QFrame(wrap)
        seg.setObjectName("voicePaperSourceTabs")
        seg.setProperty("sourceTabsVisual", "consumerSegment")
        seg.setAccessibleName(f"{source_label}文本来源")
        seg.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(2, 2, 2, 2)
        sh.setSpacing(3)
        st["buttons"] = {}
        st["group"] = QButtonGroup(self)
        st["group"].setExclusive(True)
        source_buttons = []
        for key, label in (("library", "素材库"), ("upload", "上传文件"), ("custom", "自定义")):
            btn = QPushButton(label, seg)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c=False, k=key: self._src_set_mode(prefix, k))
            st["group"].addButton(btn)
            st["buttons"][key] = btn
            sh.addWidget(btn)
            source_buttons.append(btn)
        adopt_segment(seg, source_buttons, accessible_name=f"{source_label}文本来源")
        st["segment"] = seg
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        toolbar.addWidget(seg, 0)
        toolbar.addStretch(1)
        st["toolbar"] = toolbar
        col.addLayout(toolbar)

        # 素材库：选中卡 / 空态（空态本身是醒目的点击入口，不再单独放远处的按钮）
        picked_card = QFrame()
        picked_card.setFixedHeight(canvas_height)
        picked_card.setProperty("voiceEnvelopeSelected", True)
        adopt_action_surface(
            picked_card,
            surface="well",
            variant="selected",
            accessible_name=f"{source_label}已选文稿",
            on_activate=lambda: self._src_open_picker(prefix),
        )
        pcl = QHBoxLayout(picked_card)
        pcl.setContentsMargins(14, 11, 11, 11)
        pcl.setSpacing(12)
        pcl.addWidget(icons.icon_label("file", self._consumer.accent, 20), 0, Qt.AlignVCenter)
        mid = QVBoxLayout()
        mid.setSpacing(3)
        st["picked_title"] = ElideLabel("")
        st["picked_title"].setObjectName("pickedTitle")
        mid.addWidget(st["picked_title"])
        metarow = QHBoxLayout()
        metarow.setContentsMargins(0, 0, 0, 0)
        metarow.setSpacing(8)
        st["picked_badge"] = QLabel("")
        st["picked_badge"].setObjectName("kindBadge")
        st["picked_meta"] = QLabel("")
        st["picked_meta"].setObjectName("pickedMeta")
        metarow.addWidget(st["picked_badge"], 0, Qt.AlignVCenter)
        metarow.addWidget(st["picked_meta"], 0, Qt.AlignVCenter)
        metarow.addStretch(1)
        mw = QWidget()
        mw.setLayout(metarow)
        mid.addWidget(mw)
        pcl.addLayout(mid, 1)
        pview = QPushButton("  查看原文")
        adopt_button(pview, role="tertiary", accessible_name="查看来源原文")
        pview.setIcon(QIcon(icons.pixmap("summary", T.INK_SOFT, 13)))
        pview.setToolTip("在右侧面板查看这篇文稿的全文、来源")
        pview.clicked.connect(lambda: self._src_view_picked(prefix))
        st["picked_view"] = pview
        pcl.addWidget(pview, 0, Qt.AlignVCenter)
        px = QPushButton()
        adopt_button(px, role="icon", size="sm", accessible_name="清除已选文稿", tooltip="清除已选文稿")
        px.setIcon(QIcon(icons.pixmap("x", T.INK_FAINT, 14)))
        px.clicked.connect(lambda: self._src_clear_pick(prefix))
        pcl.addWidget(px, 0, Qt.AlignVCenter)
        st["picked_card"] = picked_card
        # 素材库空态与已通过的文案处理/内容输出页共用同一纸质信封。
        empty = _PaperEnvelopePicker(self._consumer, wrap)
        # 共享信封默认服务于 130px 来源槽；配音页的消费级画布是 142px，
        # 必须在这里跟随本页合同，避免库/上传/自定义切换时留下高度缺口。
        empty.setFixedHeight(canvas_height)
        adopt_action_surface(
            empty,
            surface="card",
            variant="sourcePicker",
            accessible_name=f"{source_label}从文稿库选择内容",
            on_activate=lambda: self._src_open_picker(prefix),
        )
        empty_grid = QGridLayout(empty)
        empty_grid.setContentsMargins(18, 10, 18, 10)
        empty_grid.setHorizontalSpacing(0)
        empty_grid.setVerticalSpacing(0)
        empty_title = QLabel("选择一篇文稿")
        empty_title.setObjectName("sourcePickerTitle")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_title.setStyleSheet(
            "QLabel#sourcePickerTitle {"
            f" color: {self._consumer.ink}; background: transparent;"
            ' font-family: "HanziPen SC"; font-size: 15px; font-weight: 600;'
            " letter-spacing: 1px; }"
        )
        empty_meta = QLabel("从文稿库中选择内容进行配音")
        empty_meta.setObjectName("sourcePickerMeta")
        empty_meta.setAlignment(Qt.AlignCenter)
        empty_meta.setStyleSheet(
            "QLabel#sourcePickerMeta {"
            f" color: {self._consumer.ink_soft}; background: transparent;"
            ' font-family: "HanziPen SC"; font-size: 14px; font-weight: 500;'
            " letter-spacing: 1px; }"
        )
        empty_grid.addWidget(empty_title, 0, 0)
        empty_grid.addWidget(empty_meta, 0, 1)
        lower_space = QWidget(empty)
        lower_space.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        empty_grid.addWidget(lower_space, 1, 0, 1, 2)
        empty_grid.setColumnStretch(0, 1)
        empty_grid.setColumnStretch(1, 1)
        empty_grid.setRowStretch(0, 58)
        empty_grid.setRowStretch(1, 42)
        for child in empty.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        st["material_trigger"] = empty
        st["empty_envelope"] = empty
        st["library_picker_title"] = empty_title
        st["library_picker_meta"] = empty_meta
        st["pick_empty"] = empty
        lib_page = QWidget()
        lib_v = QVBoxLayout(lib_page)
        lib_v.setContentsMargins(0, 0, 0, 0)
        lib_v.setSpacing(0)
        lib_v.addWidget(picked_card)
        lib_v.addWidget(empty)

        # 上传：拖放区 / 文件卡
        st["drop_area"] = _PaperBoxFileDropArea(
            lambda: self._src_pick_file(prefix),
            lambda p: self._src_load_file(prefix, p),
            sub=textfile.FILE_LABEL,
            title="拖入配音文案文件",
        )
        adopt_action_surface(
            st["drop_area"],
            surface="well",
            variant="paperDocumentBox",
            accessible_name=f"{source_label}上传文本文件",
            on_activate=lambda: self._src_pick_file(prefix),
        )
        st["drop_area"].setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        st["drop_area"].setFixedHeight(canvas_height)
        st["uploaded_card"], st["uploaded_title"], st["uploaded_meta"] = self._build_file_card(
            "上传文本", on_open=lambda: self._src_pick_file(prefix), on_clear=lambda: self._src_clear_upload(prefix))
        up_wrap = QWidget()
        uv = QVBoxLayout(up_wrap)
        uv.setContentsMargins(0, 0, 0, 0)
        uv.setSpacing(0)
        uv.addWidget(st["uploaded_card"])
        up_wrap.layout().addStretch(1)
        st["upload_stack"] = QStackedWidget()
        st["upload_stack"].addWidget(st["drop_area"])
        st["upload_stack"].addWidget(up_wrap)

        # 自定义：文本框（填满整框）
        text_edit = QPlainTextEdit()
        adopt_text_area(text_edit, surface="card", accessible_name=f"{source_label}待合成文本")
        text_edit.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        text_edit.setFixedHeight(canvas_height)
        text_edit.setPlaceholderText(placeholder)
        if on_text is not None:
            text_edit.textChanged.connect(on_text)
        st["text_edit"] = text_edit

        # 三种来源维持稳定高度，压缩无效留白但不在切换时造成页面跳动。
        st["src_stack"] = QStackedWidget()
        st["src_stack"].setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        st["src_stack"].setFixedHeight(canvas_height)
        st["src_stack"].addWidget(lib_page)
        st["src_stack"].addWidget(st["upload_stack"])
        st["src_stack"].addWidget(text_edit)
        source_canvas_wrap = QWidget(wrap)
        source_canvas_layout = QVBoxLayout(source_canvas_wrap)
        source_canvas_layout.setContentsMargins(0, 0, 0, VOICE_SOURCE_INNER_BOTTOM_GAP)
        source_canvas_layout.setSpacing(0)
        source_canvas_layout.addWidget(st["src_stack"])
        source_canvas_wrap.setFixedHeight(
            canvas_height + VOICE_SOURCE_INNER_BOTTOM_GAP
        )
        st["source_canvas_wrap"] = source_canvas_wrap
        col.addWidget(source_canvas_wrap)
        wrap.setFixedHeight(
            seg.height()
            + col.spacing()
            + canvas_height
            + VOICE_SOURCE_INNER_BOTTOM_GAP
        )
        self._src_set_mode(prefix, "library")
        return wrap

    def _src_text(self, prefix: str) -> str:
        return self._src[prefix]["text_edit"].toPlainText()

    def _src_set_mode(self, prefix: str, mode: str) -> None:
        st = self._src[prefix]
        if mode not in ("library", "upload", "custom"):
            mode = "custom"
        preserve_draft = prefix == "qwen3" or (prefix == "indextts2" and hasattr(self, "_approved_index")) or (prefix == "voxcpm2" and hasattr(self, "_approved_vox"))
        if preserve_draft and st["mode"] == "custom" and mode != "custom":
            st["custom_draft"] = st["text_edit"].toPlainText()
        previous_mode = st["mode"]
        st["mode"] = mode
        btn = st["buttons"].get(mode)
        if btn is not None:
            btn.setChecked(True)
        st["src_stack"].setCurrentIndex({"library": 0, "upload": 1, "custom": 2}[mode])
        if mode == "library":
            picked = st["picked"] is not None
            st["picked_card"].setVisible(picked)
            st["pick_empty"].setVisible(not picked)
            if picked:
                self._src_sync_picked_text(prefix)
            elif preserve_draft:
                st["text_edit"].clear()
        elif mode == "upload":
            loaded = bool(st["upload_text"])
            st["upload_stack"].setCurrentIndex(1 if loaded else 0)
            if loaded:
                st["text_edit"].setPlainText(st["upload_text"])
            elif preserve_draft:
                st["text_edit"].clear()
        elif preserve_draft and previous_mode != "custom":
            st["text_edit"].setPlainText(st.get("custom_draft", ""))
        self._refit_panel()

    def _src_open_picker(self, prefix: str) -> None:
        from .doc_picker import DocPickerDialog
        st = self._src[prefix]
        current = st.get("picker_dialog")
        if current is not None and current.isVisible():
            current.raise_()
            current.activateWindow()
            return
        entries = []  # DocPickerDialog loads the shared query asynchronously.
        dlg = DocPickerDialog(entries, selected_id=(st["picked"] or {}).get("id"),
                              parent=self, on_go_workbench=lambda: self.main._switch_page(0))
        dlg.setWindowModality(Qt.NonModal)
        dlg.setModal(False)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        backdrop = _DialogBackdrop(dlg, self)
        st["picker_dialog"] = dlg
        st["picker_backdrop"] = backdrop
        trigger = st.get("material_trigger")
        set_trigger_opened = getattr(trigger, "set_opened", None)
        if callable(set_trigger_opened):
            set_trigger_opened(True)

        def _finished(result: int) -> None:
            if result == int(QDialog.DialogCode.Accepted) and dlg.payload:
                self._src_on_picked(prefix, dlg.payload)
            if st.get("picker_dialog") is dlg:
                st["picker_dialog"] = None
                st["picker_backdrop"] = None
            if callable(set_trigger_opened):
                set_trigger_opened(False)

        dlg.finished.connect(_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _src_on_picked(self, prefix: str, entry: dict) -> None:
        st = self._src[prefix]
        title = (entry.get("title") or "文稿").replace("\n", " ").strip()
        st["picked"] = {"id": entry["id"], "title": title, "kind": entry.get("kind"), "char_count": entry.get("char_count")}
        display_title = library.display_title(title)
        st["picked_title"].setText(display_title)
        st["picked_title"].setToolTip(display_title)
        st["picked_badge"].setText("改写文稿" if entry.get("kind") == "rewrite" else "转录文稿")
        cc = entry.get("char_count")
        st["picked_meta"].setText(f"{cc} 字" if cc else "")
        st["picked_meta"].setVisible(bool(cc))
        st["picked_card"].setVisible(True)
        st["pick_empty"].setVisible(False)
        self._src_sync_picked_text(prefix)

    def _src_sync_picked_text(self, prefix: str) -> None:
        st = self._src[prefix]
        if not st["picked"]:
            return
        transcript = library.load(str(st["picked"]["id"]))
        if transcript is None:
            st["text_edit"].clear()
            self._set_voice_source({
                "source_type": str(st["picked"].get("kind") or "transcript"),
                "source_id": str(st["picked"]["id"]),
                "source_title": st["picked"]["title"],
                "source_text_snapshot": "", "source_chain": [],
            })
            dialogs.alert(self, "素材库", "这篇文稿暂时读取不到，可能已被移动或删除。")
            return
        from ..core.document_text import voice_text_from_source

        source_type = str(st["picked"].get("kind") or "transcript")
        text = voice_text_from_source(getattr(transcript, "text", "") or "", source_type)
        if not text:
            self._set_voice_source({
                "source_type": source_type,
                "source_id": str(st["picked"]["id"]),
                "source_title": st["picked"]["title"],
                "source_text_snapshot": "",
                "source_chain": [],
            })
            st["text_edit"].setPlainText("")
            dialogs.alert(self, "素材库", "这篇成稿没有唯一可用的“逐字稿”正文，已停止带入配音。")
            return
        self._set_voice_source({
            "source_type": source_type,
            "source_id": str(st["picked"]["id"]),
            "source_title": st["picked"]["title"],
            "source_text_snapshot": text,
            "source_chain": [],
        })
        st["text_edit"].setPlainText(text)

        if prefix == "indextts2" and hasattr(self, "_approved_index"):
            from ..core.synthesis.indextts2_text_guard import count_text_chars
            cc = count_text_chars(text)
            st["picked_meta"].setText(f"配音正文 {cc} 字")
            st["picked_meta"].setToolTip("只统计用于配音的正文，不包含原文中的标题、来源等资料")
            st["picked_meta"].setVisible(True)

    def _src_view_picked(self, prefix: str) -> None:
        """在右侧面板打开当前选中的来源文稿（全文/来源/试听）。"""
        if prefix == "indextts2" and hasattr(self, "_approved_index"):
            self._approved_index._view_text()
            return
        st = self._src[prefix]
        picked = st.get("picked") or {}
        eid = picked.get("id")
        if not eid or getattr(self, "main", None) is None:
            return
        if library._load_raw(str(eid)) is None:   # 选中后被删/移走 → 明确提示，别静默无反应
            dialogs.alert(self, "查看原文", "这篇文稿已删除或不在原位置。")
            return
        self.main._view_entry(str(eid))

    def _src_clear_pick(self, prefix: str) -> None:
        st = self._src[prefix]
        st["picked"] = None
        st["picked_card"].setVisible(False)
        st["pick_empty"].setVisible(True)
        st["text_edit"].setPlainText("")
        self._refit_panel()

    def _src_pick_file(self, prefix: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文案文件", str(Path.home()), f"{textfile.FILE_FILTER};;所有文件 (*)")
        if path:
            self._src_load_file(prefix, path)

    def _src_load_file(self, prefix: str, path: str) -> None:
        st = self._src[prefix]
        try:
            text = textfile.read_text_file(path)
        except Exception as exc:  # noqa: BLE001
            dialogs.alert(self, "读取失败", str(exc))
            return
        if not text.strip():
            dialogs.alert(self, "读取失败", "这个文件里没读到文字内容。")
            return
        st["upload_text"] = text
        st["upload_name"] = Path(path).stem
        st["drop_area"].set_status(f"已选：{Path(path).name}  ·  {len(text)} 字")
        display_title = library.display_title(st["upload_name"])
        st["uploaded_title"].setText(display_title)
        st["uploaded_title"].setToolTip(display_title)
        st["uploaded_meta"].setText(f"{len(text)} 字")
        self._set_voice_source({
            "source_type": "manual", "source_id": None, "source_title": "上传文本",
            "source_text_snapshot": text.strip(), "source_chain": [],
        })
        st["text_edit"].setPlainText(text.strip())
        if st["mode"] == "upload":
            self._src_set_mode(prefix, "upload")

    def _src_clear_upload(self, prefix: str) -> None:
        st = self._src[prefix]
        st["upload_text"] = ""
        st["upload_name"] = ""
        st["drop_area"].set_status(textfile.FILE_LABEL)
        st["text_edit"].setPlainText("")
        if st["mode"] == "upload":
            self._src_set_mode(prefix, "upload")

    # IndexTTS 2.5 兼容别名（set_text 等仍调用旧名）
    def _set_indextts2_text_source(self, mode: str) -> None:
        self._src_set_mode("indextts2", mode)

    def _build_file_card(self, badge: str, *, on_open, on_clear):
        """与素材库选中卡同款的紧凑文件卡，返回 (card, title_label, meta_label)。"""
        card = QFrame()
        card.setFixedHeight(82)
        adopt_action_surface(
            card,
            surface="well",
            variant="selected",
            accessible_name=badge,
            on_activate=on_open,
        )
        cl = QHBoxLayout(card)
        cl.setContentsMargins(14, 11, 11, 11)
        cl.setSpacing(12)
        cl.addWidget(icons.icon_label("file", self._consumer.accent, 20), 0, Qt.AlignVCenter)
        mid = QVBoxLayout()
        mid.setSpacing(3)
        title = ElideLabel("")
        title.setObjectName("pickedTitle")
        mid.addWidget(title)
        metarow = QHBoxLayout()
        metarow.setContentsMargins(0, 0, 0, 0)
        metarow.setSpacing(8)
        badge_lab = QLabel(badge)
        badge_lab.setObjectName("kindBadge")
        meta = QLabel("")
        meta.setObjectName("pickedMeta")
        metarow.addWidget(badge_lab, 0, Qt.AlignVCenter)
        metarow.addWidget(meta, 0, Qt.AlignVCenter)
        metarow.addStretch(1)
        metawrap = QWidget()
        metawrap.setLayout(metarow)
        mid.addWidget(metawrap)
        cl.addLayout(mid, 1)
        x = QPushButton()
        adopt_button(x, role="icon", size="sm", accessible_name=f"清除{badge}", tooltip=f"清除{badge}")
        x.setIcon(QIcon(icons.pixmap("x", T.INK_FAINT, 14)))
        x.clicked.connect(on_clear)
        cl.addWidget(x, 0, Qt.AlignVCenter)
        return card, title, meta

    def _build_indextts2_emotion_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(10)
        # 五个业务模式各自拥有完整按钮边界；宽度足够时同排，窄窗自然 3+2。
        seg = QFrame()
        seg.setObjectName("emotionModeGrid")
        sh = QGridLayout(seg)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setHorizontalSpacing(8)
        sh.setVerticalSpacing(8)
        self.indextts2_emotion_mode_group = QButtonGroup(self)
        self.indextts2_emotion_mode_group.setExclusive(True)
        self.indextts2_emotion_mode_buttons: dict[str, QPushButton] = {}
        short_labels = {
            "follow": "自然跟随",
            "emoaudio": "情绪参考",
            "template": "情绪模板",
            "vector": "精细控制",
            "emotext": "情绪描述",
        }
        for key in EMOTION_MODE_ORDER:
            btn = QPushButton(short_labels[key])
            btn.setCheckable(True)
            btn.setMinimumWidth(1)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(EMOTION_MODE_LABELS[key])
            btn.clicked.connect(lambda _checked=False, k=key: self._on_indextts2_emotion_mode_selected(k))
            self.indextts2_emotion_mode_group.addButton(btn)
            self.indextts2_emotion_mode_buttons[key] = btn
            btn.setObjectName("voiceEmotionChoice")
            btn.setFixedHeight(44)
            btn.setFocusPolicy(Qt.StrongFocus)
            btn.setAccessibleName(f"IndexTTS 2.5情感方式：{EMOTION_MODE_LABELS[key]}")
            btn.setAccessibleDescription(EMOTION_MODE_LABELS[key])
        self.indextts2_emotion_mode_grid = sh
        self._reflow_emotion_buttons()
        col.addWidget(seg)

        surface = QFrame()
        adopt_surface(surface, "card", variant="emotionStudio")
        surface.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.indextts2_emo_surface = surface
        sv = QVBoxLayout(surface)
        sv.setContentsMargins(14, 12, 14, 12)
        sv.setSpacing(10)
        # 面板封顶（_size_panel_stack 的 setMaximumHeight）在窄窗口下偶发偏小，会把本框压短到
        # 放不下「情感栈（固定178）+整体情感权重行」时会重叠；锁住最小尺寸=内容，绝不被压缩。
        sv.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        # 五种模式共用同一只固定画布，切换时输出区与结果区不会跳动。
        self.indextts2_emotion_stack = QStackedWidget()
        self.indextts2_emotion_stack.setFixedHeight(178)
        self.indextts2_emotion_stack.addWidget(self._build_emo_follow())     # 0
        self.indextts2_emotion_stack.addWidget(self._build_emo_emoaudio())   # 1
        self.indextts2_emotion_stack.addWidget(self._build_emo_template())   # 2
        self.indextts2_emotion_stack.addWidget(self._build_emo_vector())     # 3
        self.indextts2_emotion_stack.addWidget(self._build_emo_emotext())    # 4
        sv.addWidget(self.indextts2_emotion_stack)

        # 跟随模式与可调模式占用同一条页脚高度。
        self.indextts2_follow_footer = QFrame()
        self.indextts2_follow_footer.setObjectName("voiceFollowFooter")
        self.indextts2_follow_footer.setFixedHeight(46)
        follow_footer_layout = QHBoxLayout(self.indextts2_follow_footer)
        follow_footer_layout.setContentsMargins(12, 7, 12, 7)
        follow_footer_layout.setSpacing(9)
        follow_footer_icon = QLabel()
        follow_footer_icon.setPixmap(icons.pixmap("waveform", self._consumer.accent, 14))
        follow_footer_icon.setFixedSize(20, 20)
        follow_footer_icon.setAlignment(Qt.AlignCenter)
        follow_footer_layout.addWidget(follow_footer_icon)
        follow_footer_title = QLabel("自动匹配")
        follow_footer_title.setObjectName("voiceStrengthTitle")
        follow_footer_layout.addWidget(follow_footer_title)
        follow_footer_meta = QLabel("跟随所选音色，无需额外参数")
        follow_footer_meta.setObjectName("voiceFollowFooterMeta")
        follow_footer_layout.addWidget(follow_footer_meta)
        follow_footer_layout.addStretch(1)
        self.indextts2_weight_row = QFrame()
        self.indextts2_weight_row.setObjectName("voiceStrengthBar")
        self.indextts2_weight_row.setFixedHeight(46)
        wr = QHBoxLayout(self.indextts2_weight_row)
        wr.setContentsMargins(12, 7, 10, 7)
        wr.setSpacing(12)
        self.indextts2_weight_title = QLabel("整体情感权重")
        self.indextts2_weight_title.setObjectName("voiceStrengthTitle")
        wr.addWidget(self.indextts2_weight_title)
        self.indextts2_weight_slider = QSlider(Qt.Horizontal)
        self.indextts2_weight_slider.setRange(30, 110)
        self.indextts2_weight_slider.setSingleStep(5)
        self.indextts2_weight_slider.setPageStep(5)
        self.indextts2_weight_slider.setValue(int(DEFAULT_EMOTION_WEIGHT * 100))
        self.indextts2_weight_slider.setMinimumWidth(120)
        self.indextts2_weight_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.indextts2_weight_slider.valueChanged.connect(self._on_indextts2_weight_changed)
        adopt_slider(
            self.indextts2_weight_slider,
            accessible_name="IndexTTS 2.5整体情感权重",
            value_formatter=lambda value: f"{value / 100:.2f}",
        )
        wr.addWidget(self.indextts2_weight_slider, 1)
        self.indextts2_weight_value = QLabel(f"{int(DEFAULT_EMOTION_WEIGHT * 100)}%")
        self.indextts2_weight_value.setObjectName("voiceStrengthValue")
        self.indextts2_weight_value.setAlignment(Qt.AlignCenter)
        self.indextts2_weight_value.setMinimumWidth(48)
        wr.addWidget(self.indextts2_weight_value)
        self.indextts2_emotion_footer_stack = QStackedWidget()
        self.indextts2_emotion_footer_stack.setFixedHeight(46)
        self.indextts2_emotion_footer_stack.addWidget(self.indextts2_follow_footer)
        self.indextts2_emotion_footer_stack.addWidget(self.indextts2_weight_row)
        sv.addWidget(self.indextts2_emotion_footer_stack)
        # 178 内容 + 46 强度条 + 10 间距 + 24 内边距 + 2px 实体边框。
        surface.setFixedHeight(260)
        col.addWidget(surface)
        row.addLayout(col, 1)
        return row

    def _build_emo_follow(self) -> QWidget:
        page = _GardenFollowPanel()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(22, 17, 22, 17)
        layout.setSpacing(18)
        note = QLabel("♪", page)
        note.setObjectName("voiceFollowNote")
        note.setAccessibleName("自然跟随音符")
        note.setAlignment(Qt.AlignCenter)
        note.setFixedWidth(150)
        note.setStyleSheet(
            "QLabel#voiceFollowNote {"
            f" color: {self._consumer.accent}; background: transparent;"
            ' font-family: "Times New Roman"; font-size: 58px; font-weight: 500;'
            " }"
        )
        layout.addWidget(note, 0, Qt.AlignVCenter)
        copy = QVBoxLayout()
        copy.setSpacing(7)
        copy.addStretch(1)
        title = QLabel("顺其声，自然成韵")
        title.setObjectName("voiceFollowTitle")
        copy.addWidget(title)
        self.indextts2_follow_hint = QLabel(
            "系统会沿用所选音色原本的语气、节奏与情感，不额外叠加强度或模板。"
        )
        self.indextts2_follow_hint.setObjectName("voiceFollowMeta")
        self.indextts2_follow_hint.setWordWrap(True)
        copy.addWidget(self.indextts2_follow_hint)
        tags = QHBoxLayout()
        tags.setSpacing(7)
        for text in ("语气随声", "节奏随声", "情感随声"):
            tag = QLabel(text)
            tag.setObjectName("voiceFollowTag")
            tag.setAlignment(Qt.AlignCenter)
            tags.addWidget(tag)
        tags.addStretch(1)
        copy.addLayout(tags)
        copy.addStretch(1)
        layout.addLayout(copy, 1)
        return page

    def _reflow_emotion_buttons(self) -> None:
        if hasattr(self, "_approved_index"):
            return
        grid = getattr(self, "indextts2_emotion_mode_grid", None)
        buttons = getattr(self, "indextts2_emotion_mode_buttons", None)
        if grid is None or not buttons:
            return
        columns = 5 if grid.parentWidget().width() >= 600 else 3
        for column in range(5):
            grid.setColumnStretch(column, 1 if column < columns else 0)
            grid.setColumnMinimumWidth(column, 0)
        for index, key in enumerate(EMOTION_MODE_ORDER):
            grid.addWidget(buttons[key], index // columns, index % columns)

    def _build_emo_emoaudio(self) -> QWidget:
        page = QWidget()
        l = QVBoxLayout(page)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(6)
        drop = _PaperBoxFileDropArea(
            self._pick_indextts2_emotion_audio,
            self._load_indextts2_emotion_audio,
            sub="WAV · MP3 · M4A · AAC · FLAC · AIFF",
            title="拖入情绪参考音频",
        )
        adopt_action_surface(
            drop,
            surface="well",
            variant="paperDocumentBox",
            accessible_name="上传或选择情感参考音频",
            on_activate=self._pick_indextts2_emotion_audio,
        )
        drop.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        drop.setMinimumHeight(150)
        drop.setAccessibleDescription("会模仿这段音频的情绪；音色仍使用上方所选音色")
        self.indextts2_emoaudio_drop = drop
        self.indextts2_emoaudio_title = drop.title
        dl = drop.layout()
        pick = QPushButton("  选择音频")
        pick.setCursor(Qt.PointingHandCursor)
        pick.setIcon(QIcon(icons.pixmap("file", T.INK_SOFT, 13)))
        pick.clicked.connect(self._pick_indextts2_emotion_audio)
        adopt_button(pick, role="secondary", size="sm", accessible_name="选择情感参考音频")
        prow = QHBoxLayout()
        prow.addStretch(1)
        prow.addWidget(pick)
        record = QPushButton("  录制参考")
        record.setCursor(Qt.PointingHandCursor)
        record.setIcon(QIcon(icons.pixmap("mic", T.INK_SOFT, 13)))
        record.setToolTip("录一段 3-10 秒有明显情绪的短音频")
        record.clicked.connect(self._record_indextts2_emotion_audio)
        adopt_button(record, role="secondary", size="sm", accessible_name="录制情感参考音频")
        prow.addWidget(record)
        clear = QPushButton("清除")
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(self._clear_indextts2_emotion_audio)
        adopt_button(clear, role="tertiary", size="sm", accessible_name="清除情感参考音频")
        prow.addWidget(clear)
        prow.addStretch(1)
        dl.addLayout(prow)
        l.addWidget(drop)
        self._render_indextts2_emoaudio_state()
        return page

    def _build_emo_template(self) -> QWidget:
        page = QWidget()
        l = QVBoxLayout(page)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("选择模板")
        title.setObjectName("voiceTemplateLabel")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_indextts2_template_manage = QPushButton("  管理模板")
        self.btn_indextts2_template_manage.setIcon(QIcon(icons.pixmap("gear", self._consumer.accent, 13)))
        self.btn_indextts2_template_manage.clicked.connect(self._open_indextts2_template_manager)
        adopt_button(
            self.btn_indextts2_template_manage,
            role="secondary",
            size="sm",
            variant="templateManage",
            accessible_name="管理情感模板",
        )
        head.addWidget(self.btn_indextts2_template_manage)
        l.addLayout(head)
        # 模板 chips 数量会随自定义增长；用流式布局自动换行，不出现横向滚动条/裁切。
        chip_wrap = QWidget()
        chip_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        chip_wrap.setFixedHeight(34)
        self.indextts2_template_chip_layout = FlowLayout(chip_wrap, hspacing=8, vspacing=8)
        self.indextts2_template_group = QButtonGroup(self)
        self.indextts2_template_group.setExclusive(True)
        l.addWidget(chip_wrap, 0, Qt.AlignTop)

        current = QFrame()
        current.setObjectName("voiceTemplateCurrent")
        current.setMinimumHeight(64)
        current.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        current_row = QHBoxLayout(current)
        current_row.setContentsMargins(10, 8, 11, 8)
        current_row.setSpacing(10)
        glyph = QLabel()
        glyph.setObjectName("voiceTemplateGlyph")
        glyph.setFixedSize(32, 32)
        glyph.setAlignment(Qt.AlignCenter)
        glyph.setPixmap(icons.pixmap("summary", self._consumer.accent, 15))
        current_row.addWidget(glyph, 0, Qt.AlignVCenter)
        current_copy = QVBoxLayout()
        current_copy.setSpacing(2)
        self.indextts2_template_name = ElideLabel("自然")
        self.indextts2_template_name.setObjectName("voiceTemplateName")
        current_copy.addWidget(self.indextts2_template_name)
        self.indextts2_template_summary = ElideLabel("")
        self.indextts2_template_summary.setObjectName("voiceTemplateSummary")
        current_copy.addWidget(self.indextts2_template_summary)
        current_row.addLayout(current_copy, 1)
        l.addWidget(current, 1)
        return page

    def _build_emo_vector(self) -> QWidget:
        page = QWidget()
        l = QVBoxLayout(page)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(8)
        grid = QWidget()
        gl = QHBoxLayout(grid)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.setSpacing(22)
        left_col = QVBoxLayout()
        left_col.setSpacing(8)
        right_col = QVBoxLayout()
        right_col.setSpacing(8)
        self.indextts2_advanced_sliders: list[QSlider] = []
        self.indextts2_advanced_value_labels: list[QLabel] = []
        for i, label in enumerate(EMOTION_VECTOR_LABELS):
            cell = QHBoxLayout()
            cell.setSpacing(8)
            name = QLabel(VECTOR_DISPLAY_LABELS.get(label, label))
            name.setObjectName("dim")
            name.setMinimumWidth(40)
            cell.addWidget(name)
            value = QLabel("0.00")
            value.setObjectName("segIdx")
            value.setMinimumWidth(30)
            cell.addWidget(value)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 80)
            slider.setSingleStep(1)
            slider.setPageStep(5)
            slider.valueChanged.connect(self._on_indextts2_vector_changed)
            adopt_slider(
                slider,
                accessible_name=f"IndexTTS 2.5情感向量：{VECTOR_DISPLAY_LABELS.get(label, label)}",
                value_formatter=lambda slider_value: f"{slider_value / 100:.2f}",
            )
            cell.addWidget(slider, 1)
            (left_col if i % 2 == 0 else right_col).addLayout(cell)
            self.indextts2_advanced_sliders.append(slider)
            self.indextts2_advanced_value_labels.append(value)
        gl.addLayout(left_col, 1)
        gl.addLayout(right_col, 1)
        l.addWidget(grid)

        bottom = QHBoxLayout()
        self.btn_indextts2_clear_vector = QPushButton("  清零")
        self.btn_indextts2_clear_vector.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_clear_vector.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 13)))
        self.btn_indextts2_clear_vector.clicked.connect(self._clear_indextts2_advanced_vector)
        adopt_button(self.btn_indextts2_clear_vector, role="tertiary", size="sm", accessible_name="清零情感向量")
        bottom.addWidget(self.btn_indextts2_clear_vector)
        self.btn_indextts2_save_style = QPushButton("  存为模板")
        self.btn_indextts2_save_style.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_save_style.setIcon(QIcon(icons.pixmap("plus", T.INK_SOFT, 13)))
        self.btn_indextts2_save_style.clicked.connect(self._save_indextts2_style)
        adopt_button(self.btn_indextts2_save_style, role="secondary", size="sm", accessible_name="将当前情感参数存为模板")
        bottom.addWidget(self.btn_indextts2_save_style)
        bottom.addStretch(1)
        self.indextts2_advanced_budget = QLabel("总量 0.00 / 0.80")
        self.indextts2_advanced_budget.setObjectName("voiceSub")
        bottom.addWidget(self.indextts2_advanced_budget)
        l.addLayout(bottom)
        return page

    def _build_emo_emotext(self) -> QWidget:
        # 去掉「实验」标和提示行，输入框填满整框（与其余三种框大小一致）
        page = QWidget()
        l = QVBoxLayout(page)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)
        self.indextts2_emotion_text_edit = QPlainTextEdit()
        self.indextts2_emotion_text_edit.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.indextts2_emotion_text_edit.setPlaceholderText("用文字描述想要的情绪，例：委屈巴巴、带一点不甘心 / 危险在悄悄逼近")
        self.indextts2_emotion_text_edit.textChanged.connect(self._update_indextts2_generate_state)
        adopt_text_area(self.indextts2_emotion_text_edit, surface="card", accessible_name="IndexTTS 2.5情感描述文本")
        l.addWidget(self.indextts2_emotion_text_edit)
        return page

    def _open_indextts2_glossary_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("术语读音")
        adopt_dialog(dlg, accessible_name="术语读音")
        adopt_consumer_theme_scope(dlg, "coreWorkspace")
        dlg.setMinimumWidth(560)
        dlg.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        head = QLabel("术语读音")
        head.setObjectName("sectionTitle")
        lay.addWidget(head)
        intro = QLabel("词典只存本机，原始文案不改写。文字替换由 Lulu 提供；指定读音使用官方模型能力。"
                       + ("当前 IndexTTS 2 支持中文拼音。" if self._index_version == "2.0"
                          else "当前 2.5 支持中文拼音、英文音素和日语假名。"))
        intro.setObjectName("pageMeta")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        # 把常驻面板临时挂进弹窗：弹窗已有标题，隐藏面板自带折叠头并强制展开正文。
        panel = self.indextts2_glossary_panel
        panel.setVisible(True)
        if hasattr(panel, "header"):
            panel.header.setVisible(False)
        if hasattr(panel, "body"):
            panel.body.setVisible(True)
        lay.addWidget(panel)
        close = QPushButton("完成")
        close.setObjectName("grabBtn")
        close.clicked.connect(dlg.accept)
        crow = QHBoxLayout()
        crow.addStretch(1)
        crow.addWidget(close)
        lay.addLayout(crow)
        dlg.exec()
        # 收回面板，避免被弹窗销毁；恢复折叠头（回到主面板时面板整体隐藏）
        if hasattr(panel, "header"):
            panel.header.setVisible(True)
        if hasattr(panel, "body"):
            panel.body.setVisible(False)
            panel.header.setChecked(False)
        panel.setParent(self)
        panel.setVisible(False)

    def _build_indextts2_result_card(self) -> QWidget:
        card = self._card()
        card.setProperty("voiceCardRole", "result")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        result_title = QLabel("生成结果")
        result_title.setObjectName("voiceResultTitle")
        head.addWidget(result_title)
        head.addStretch(1)
        self.indextts2_status = QLabel("等待生成")
        self.indextts2_status.setObjectName("voiceResultStatus")
        self.indextts2_status.setWordWrap(False)
        head.addWidget(self.indextts2_status)
        v.addLayout(head)
        self.indextts2_progress = QProgressBar()
        self.indextts2_progress.setObjectName("taskBar")
        self.indextts2_progress.setTextVisible(False)
        self.indextts2_progress.setVisible(False)
        v.addWidget(self.indextts2_progress)

        self.indextts2_result_empty = QFrame()
        self.indextts2_result_empty.setObjectName("voiceResultEmpty")
        self.indextts2_result_empty.setFixedHeight(112)
        empty_layout = QHBoxLayout(self.indextts2_result_empty)
        empty_layout.setContentsMargins(28, 14, 28, 14)
        empty_layout.setSpacing(28)
        empty_layout.addWidget(_VoiceWaveVisual(self.indextts2_result_empty), 0, Qt.AlignVCenter)
        empty_copy = QVBoxLayout()
        empty_copy.setSpacing(4)
        empty_copy.addStretch(1)
        self.indextts2_result_hint = QLabel("准备好后，声音会出现在这里")
        self.indextts2_result_hint.setObjectName("voiceResultEmptyTitle")
        self.indextts2_result_hint.setWordWrap(False)
        empty_copy.addWidget(self.indextts2_result_hint)
        empty_meta = QLabel("可在这里试听、导出并保存到素材库")
        empty_meta.setObjectName("voiceResultEmptyMeta")
        empty_copy.addWidget(empty_meta)
        empty_copy.addStretch(1)
        empty_layout.addLayout(empty_copy, 1)
        empty_layout.addStretch(1)
        v.addWidget(self.indextts2_result_empty)

        self.indextts2_result_summary = QLabel("生成后会在这里显示模型、音色、情感方式与术语读音状态。")
        self.indextts2_result_summary.setObjectName("voiceSub")
        self.indextts2_result_summary.setWordWrap(True)
        self.indextts2_result_summary.setVisible(False)
        v.addWidget(self.indextts2_result_summary)

        self.indextts2_player = QMediaPlayer(self)
        self.indextts2_audio_output = QAudioOutput(self)
        self.indextts2_player.setAudioOutput(self.indextts2_audio_output)
        self.indextts2_player.durationChanged.connect(self._on_indextts2_player_duration)
        self.indextts2_player.positionChanged.connect(self._on_indextts2_player_position)
        self.indextts2_player.playbackStateChanged.connect(self._on_indextts2_player_state)

        bar = QFrame()
        bar.setObjectName("voicePlayerBar")
        player_layout = QVBoxLayout(bar)
        player_layout.setContentsMargins(12, 10, 12, 10)
        player_layout.setSpacing(9)
        self.indextts2_player_row = QHBoxLayout()
        self.indextts2_player_row.setContentsMargins(0, 0, 0, 0)
        self.indextts2_player_row.setSpacing(12)
        self.btn_indextts2_play = QPushButton("  播放")
        self.btn_indextts2_play.setObjectName("voicePlayBtn")
        self.btn_indextts2_play.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_play.setIcon(QIcon(icons.pixmap("play", self._consumer.accent, 13)))
        self.btn_indextts2_play.clicked.connect(self._toggle_indextts2_play)
        self.indextts2_player_row.addWidget(self.btn_indextts2_play)
        self.indextts2_player_slider = QSlider(Qt.Horizontal)
        self.indextts2_player_slider.setObjectName("voiceProgress")
        self.indextts2_player_slider.setRange(0, 0)
        self.indextts2_player_slider.sliderPressed.connect(self._on_indextts2_slider_pressed)
        self.indextts2_player_slider.sliderReleased.connect(self._on_indextts2_slider_released)
        self.indextts2_player_row.addWidget(self.indextts2_player_slider, 1)
        self.indextts2_time_label = QLabel("0:00 / 0:00")
        self.indextts2_time_label.setObjectName("pageMeta")
        self.indextts2_player_row.addWidget(self.indextts2_time_label)
        player_layout.addLayout(self.indextts2_player_row)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        action_row.addStretch(1)
        self.btn_indextts2_reveal = QPushButton("  Finder")
        self.btn_indextts2_reveal.setObjectName("act")
        self.btn_indextts2_reveal.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_reveal.setIcon(QIcon(icons.pixmap("folder", T.INK_SOFT, 13)))
        self.btn_indextts2_reveal.clicked.connect(self._reveal_indextts2_output)
        action_row.addWidget(self.btn_indextts2_reveal)
        self.btn_indextts2_export = QPushButton("  导出 WAV")
        self.btn_indextts2_export.setObjectName("act")
        self.btn_indextts2_export.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_export.setIcon(QIcon(icons.pixmap("export", T.INK_SOFT, 13)))
        self.btn_indextts2_export.clicked.connect(self._export_indextts2_wav)
        action_row.addWidget(self.btn_indextts2_export)
        self.btn_indextts2_library = QPushButton("  素材库")
        self.btn_indextts2_library.setObjectName("act")
        self.btn_indextts2_library.setCursor(Qt.PointingHandCursor)
        self.btn_indextts2_library.setIcon(QIcon(icons.pixmap("layers", T.INK_SOFT, 13)))
        self.btn_indextts2_library.clicked.connect(self._open_voice_library)
        action_row.addWidget(self.btn_indextts2_library)
        player_layout.addLayout(action_row)
        self.indextts2_player_bar = bar
        v.addWidget(bar)

        self.indextts2_recent_title = QLabel("最近结果")
        self.indextts2_recent_title.setObjectName("sectionTitle")
        self.indextts2_recent_title.setVisible(False)
        v.addWidget(self.indextts2_recent_title)
        self.indextts2_recent_layout = QVBoxLayout()
        self.indextts2_recent_layout.setSpacing(8)
        v.addLayout(self.indextts2_recent_layout)
        self._set_indextts2_player_visible(False)
        return card

    # ------------------------------------------------------------------ 公共入口

    def invalidate_model_status(self):
        self._model_status_epoch += 1
        self._model_status_cache.clear()
        if self.isVisible():
            self.refresh()

    def model_readiness(self, key):
        """Display-only snapshot. Generation still performs its own full checks."""
        import time
        from shiboken6 import isValid
        from .model_settings.workers import ReadWorker
        cached = self._model_status_cache.get(key)
        if not self._model_status_closing and key not in self._model_status_reads and (
            cached is None or time.monotonic() - cached[0] > 15
        ):
            epoch = self._model_status_epoch
            def read():
                if key == 'voxcpm2':
                    from ..core.synthesis import runtime_packages
                    state = runtime_packages.status('local_voxcpm2')
                    return state == 'ready', state
                from ..core.synthesis import qwen3_runtime
                return qwen3_runtime.readiness(key)
            worker = ReadWorker(read)
            self._model_status_reads[key] = worker
            def received(value, error):
                if not isValid(self) or self._model_status_closing or epoch != self._model_status_epoch:
                    return
                self._model_status_cache[key] = (time.monotonic(), value if not error else (False, error))
                if key != 'voxcpm2' and hasattr(self, '_qwen3'):
                    editor = getattr(self._qwen3, 'text_edit', None)
                    if editor is not None and not isValid(editor):
                        return
                    self._qwen3.sync_text()
                if key == 'voxcpm2' and hasattr(self, 'voxcpm2_ready_chip'):
                    if not isValid(self.voxcpm2_ready_chip):
                        return
                    ready = bool(not error and value and value[0])
                    self.voxcpm2_ready_chip.setText('● AI大模型就绪' if ready else '● 待准备')
                    set_status_kind(self.voxcpm2_ready_chip, 'success' if ready else 'warning')
                self._emit_model_context_changed()
            def settled():
                self._model_status_reads.pop(key, None)
                worker.deleteLater()
                if isValid(self) and epoch != self._model_status_epoch and not self._model_status_closing and self.isVisible():
                    self.model_readiness(key)
            worker.result.connect(received)
            worker.finished.connect(settled)
            worker.start()
        return cached[1] if cached else (False, '正在检查模型…')

    def refresh(self) -> None:
        options = tuple((voice.name, voice.id) for voice in voices.list_voices())
        for combo in (self.voice_combo, self.indextts2_voice_combo):
            current_options = tuple((combo.itemText(i), combo.itemData(i)) for i in range(combo.count()))
            if current_options != options:
                selected = combo.currentData()
                # Publishing the intermediate empty/first-item selection reloads
                # the player and waveform and rebuilds the selector on every entry.
                # Refresh consumers once, after the final selection is restored.
                with QSignalBlocker(combo):
                    combo.clear()
                    for name, voice_id in options:
                        combo.addItem(name, voice_id)
                    index = combo.findData(selected)
                    if index >= 0:
                        combo.setCurrentIndex(index)
            self._update_voice_selector(combo)
        if hasattr(self, "voxcpm2_ready_chip"):
            ready, note = self.model_readiness('voxcpm2')
            self.voxcpm2_ready_chip.setText("● AI大模型就绪" if ready else "● 正在检查模型…" if note == '正在检查模型…' else "● 待准备")
            set_status_kind(self.voxcpm2_ready_chip, "success" if ready else "warning")
        if hasattr(self, "_approved_vox"):
            self._approved_vox.sync_voice()
        if hasattr(self, "_qwen3"):
            self._qwen3.refresh()
        self._update_flow_shell()
        self._refresh_indextts2_status(force=False)
        self._update_indextts2_voice_status()
        self._update_indextts2_generate_state()
        self._emit_model_context_changed()

    def set_text(self, text: str, source: dict | None = None) -> bool:
        from ..core.document_text import voice_text_from_source

        original_text = str(text or "")
        source_type = str((source or {}).get("source_type") or "manual")
        text = voice_text_from_source(original_text, source_type)
        if source is not None:
            normalized_source = dict(source)
            normalized_source["source_text_snapshot"] = text
            self._set_voice_source(normalized_source)
        if hasattr(self, "_qwen3"):
            self._src_set_mode("qwen3", "custom")
            self._qwen3.text_edit.setPlainText(text.strip())
        if original_text.strip() and not text:
            self.text_edit.setPlainText("")
            self.indextts2_text_edit.setPlainText("")
            self._indextts2_text_has_content = False
            self._update_indextts2_text_length_hint()
            self._update_indextts2_generate_state()
            return False
        if text.strip():
            self._src_set_mode("voxcpm2", "custom")
            self._set_indextts2_text_source("custom")
            self.text_edit.setPlainText(text.strip())
            self.indextts2_text_edit.setPlainText(text.strip())
            self._indextts2_text_has_content = True
            self._update_indextts2_text_length_hint()
            source_label = self._voice_source_label()
            self._update_flow_shell(source=f"来源：{source_label}，可在文本框继续编辑")
            return True
        return False

    def _set_voice_source(self, source: dict) -> None:
        self._voice_source = {
            "source_type": str(source.get("source_type") or "manual"),
            "source_id": source.get("source_id"),
            "source_title": source.get("source_title") or ("手动输入" if source.get("source_type") == "manual" else ""),
            "source_text_snapshot": source.get("source_text_snapshot") or "",
            "source_chain": list(source.get("source_chain") or []),
        }

    def _voice_source_label(self) -> str:
        source_type = self._voice_source.get("source_type") or "manual"
        title = str(self._voice_source.get("source_title") or "").strip()
        if source_type == "manual":
            return "手动输入"
        if source_type == "transcript":
            return f"文稿《{library.display_title(title)}》" if title else "文稿"
        if source_type == "rewrite":
            return f"输出《{library.display_title(title)}》" if title else "输出稿"
        return title or source_type

    def _source_payload_for_text(self, text: str) -> dict:
        payload = dict(self._voice_source)
        if payload.get("source_type") == "manual" or not payload.get("source_text_snapshot"):
            payload["source_text_snapshot"] = text
        return payload

    def _on_indextts2_text_changed(self) -> None:
        self._indextts2_text_has_content = not self.indextts2_text_edit.document().isEmpty()
        self._update_indextts2_text_length_hint()
        self._update_indextts2_generate_state()

    def _update_indextts2_text_length_hint(self) -> None:
        text = self.indextts2_text_edit.toPlainText()
        self._indextts2_text_length = assess_text_length(text)
        self._indextts2_segment_plan = assess_segment_plan(text)
        if not hasattr(self, "indextts2_text_length_hint"):
            return
        assessment = self._indextts2_text_length
        segment_plan = self._indextts2_segment_plan
        if segment_plan.is_segmented:
            self.indextts2_text_length_hint.setText(f"{segment_plan.label} · {segment_plan.char_count} 字")
            color = T.AMBER
        elif segment_plan.is_blocked and segment_plan.level == "too_long":
            self.indextts2_text_length_hint.setText(segment_plan.hint)
            color = self._consumer.accent
        else:
            self.indextts2_text_length_hint.setText(f"{assessment.char_count} 字 · 可先选中一句试听")
            if assessment.level == "blocked":
                color = self._consumer.accent
            elif assessment.level == "risky":
                color = T.AMBER
            else:
                color = T.INK_FAINT
        self.indextts2_text_length_hint.setStyleSheet(f"color:{color};font-size:12px;")

    def _show_indextts2_text_tips(self) -> None:
        capabilities = (
            "支持中、英、日、西班牙、阿拉伯语。语言选择不会翻译原文；语速控制不保证精确秒数。读音可通过术语词典或 <原词|读音> 标注。"
            if self._index_version == "2.5" else
            "支持中文和英文，自动识别语言，保留模型原生节奏。不使用 2.5 的语言、时长系数或读音标注参数。"
        )
        dialogs.alert(
            self,
            f"{self._index_name} 能力说明",
            "情绪表达提供跟随音色、参考音频、精细调节、文字描述四种方式。\n\n"
            + capabilities + "\n\n"
            "建议使用开头清晰、单人说话的参考音频。参考按官方最多使用前 15 秒，保存的原录音保持不变，不需要参考文本。先选一句试听，再生成全文。\n\n"
            "长文自动分段并合并。生成或取消都保留输入；试听不会自动加入音频库，完整配音会保存到音频库。",
        )

    # ------------------------------------------------------------------ 情感控制方式

    def _on_indextts2_emotion_mode_selected(self, mode: str) -> None:
        self._set_indextts2_emotion_mode(mode)
        self._update_indextts2_generate_state()

    def _set_indextts2_emotion_mode(self, mode: str) -> None:
        if hasattr(self, "_approved_index") and mode == "template":
            mode = "vector"
        if mode not in EMOTION_MODE_ORDER:
            mode = "template"
        self._indextts2_emotion_mode = mode
        if hasattr(self, "indextts2_advanced_toggle"):
            self.indextts2_advanced_toggle.setText(f"表达方式 · {EMOTION_MODE_LABELS[mode]}")
        btn = self.indextts2_emotion_mode_buttons.get(mode)
        if btn is not None:
            btn.setChecked(True)
        is_follow = mode == "follow"
        self.indextts2_emo_surface.setVisible(True)
        self.indextts2_emotion_stack.setCurrentIndex(
            {"follow": 0, "emoaudio": 1, "template": 2, "vector": 3, "emotext": 4}[mode])
        self.indextts2_emotion_footer_stack.setCurrentIndex(0 if is_follow else 1)
        if mode == "template":
            self._sync_indextts2_template_chip()
            self._update_indextts2_template_summary()
        elif mode == "vector":
            self._render_indextts2_vector_state()
        elif mode == "emoaudio":
            self._render_indextts2_emoaudio_state()
        if hasattr(self, "_approved_index"):
            self._approved_index.sync_mode()
        self._refit_panel()

    # ------------------------------------------------------------------ 情感模板（chips）

    def _reload_indextts2_styles(self, *, apply_last: bool = False, select_id: str | None = None) -> None:
        store = style_presets.load_store()
        styles = style_presets.list_styles(store)
        target_id = select_id or (store.get("last_selected_style_id") if apply_last else self._indextts2_current_style.get("id"))
        # 重建 chips
        while self.indextts2_template_chip_layout.count():
            item = self.indextts2_template_chip_layout.takeAt(0)
            widget = item.widget()
            if widget:
                self.indextts2_template_group.removeButton(widget) if isinstance(widget, QPushButton) else None
                widget.deleteLater()
        self.indextts2_template_chips: dict[str, QPushButton] = {}
        for item in styles:
            chip = QPushButton(str(item.get("name") or "模板"))
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setProperty("templateChip", True)
            sid = str(item.get("id"))
            chip.clicked.connect(lambda _checked=False, s=sid: self._on_indextts2_template_chip(s))
            adopt_button(
                chip,
                role="secondary",
                size="sm",
                variant="selection",
                accessible_name=f"情感模板：{chip.text()}",
                tooltip=f"选择情感模板：{chip.text()}",
            )
            self.indextts2_template_group.addButton(chip)
            self.indextts2_template_chip_layout.addWidget(chip)
            self.indextts2_template_chips[sid] = chip

        idx_style = style_presets.get_style(str(target_id), store) if target_id else None
        if idx_style is None:
            idx_style = style_presets.get_style(style_presets.DEFAULT_STYLE_ID, store)
        self._apply_indextts2_style(idx_style, persist=False)

    def _open_indextts2_template_manager(self) -> None:
        """单一入口：存为新模板 + 管理（删除）自己的模板。系统预设不可删除。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("模板管理")
        adopt_dialog(dlg, accessible_name="模板管理")
        adopt_consumer_theme_scope(dlg, "coreWorkspace")
        dlg.setMinimumWidth(460)
        dlg.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        head = QLabel("模板管理")
        head.setObjectName("sectionTitle")
        lay.addWidget(head)

        sub = QLabel("把当前情感参数（8 维向量 / 整体情感权重 / 术语开关）存为新的自定义模板：")
        sub.setObjectName("pageMeta")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        save_row = QHBoxLayout()
        name = QLineEdit()
        name.setObjectName("linkInput")
        name.setPlaceholderText("新模板名称")
        save_row.addWidget(name, 1)
        save_btn = QPushButton("  保存为新模板")
        save_btn.setObjectName("grabBtn")
        save_row.addWidget(save_btn)
        lay.addLayout(save_row)

        hr = self._hr()
        lay.addWidget(hr)
        mine_title = QLabel("我的模板")
        mine_title.setObjectName("pageMeta")
        lay.addWidget(mine_title)
        list_holder = QVBoxLayout()
        list_holder.setSpacing(6)
        lay.addLayout(list_holder)
        sys_names = " / ".join(str(s.get("name") or "") for s in style_presets.list_styles() if s.get("system", False))
        sys_note = QLabel(f"系统预设（{sys_names}）为内置，不可删除。")
        sys_note.setObjectName("voiceSub")
        sys_note.setWordWrap(True)
        lay.addWidget(sys_note)

        def rebuild_list() -> None:
            while list_holder.count():
                it = list_holder.takeAt(0)
                w = it.widget()
                if w:
                    w.deleteLater()
            customs = [s for s in style_presets.list_styles() if not s.get("system", False)]
            if not customs:
                empty = QLabel("还没有自定义模板。")
                empty.setObjectName("voiceSub")
                list_holder.addWidget(empty)
                return
            for item in customs:
                rowf = QFrame()
                rowf.setObjectName("segBox")
                rh = QHBoxLayout(rowf)
                rh.setContentsMargins(12, 7, 10, 7)
                nm = QLabel(str(item.get("name") or "模板"))
                nm.setObjectName("pageMeta")
                rh.addWidget(nm, 1)
                dele = QPushButton("  删除")
                dele.setObjectName("act")
                dele.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 13)))
                dele.clicked.connect(lambda _c=False, sid=str(item.get("id")): do_delete(sid))
                rh.addWidget(dele)
                list_holder.addWidget(rowf)

        def do_save() -> None:
            if self._indextts2_advanced_total() > MAX_EMOTION_VECTOR_TOTAL:
                dialogs.alert(self, "情感模板", "当前情感总量超出上限，请降低部分滑杆后再保存。")
                return
            target = self._resolve_indextts2_save_target(name.text())
            if target is None:
                return
            clean_name, style_id = target
            try:
                store = self._upsert_indextts2_style_from_current(clean_name, style_id=style_id)
            except ValueError as exc:
                dialogs.alert(self, "情感模板", str(exc))
                return
            except Exception:
                dialogs.alert(self, "情感模板", "保存失败，请重试。")
                return
            name.setText("")
            self._reload_indextts2_styles(select_id=str(store.get("last_selected_style_id") or style_presets.DEFAULT_STYLE_ID))
            self._set_indextts2_emotion_mode("template")
            self.main._toast("情感模板已保存")
            rebuild_list()

        def do_delete(style_id: str) -> None:
            style = style_presets.get_style(style_id)
            if style.get("system", False):
                dialogs.alert(self, "情感模板", "系统预设不可删除。")
                return
            if not dialogs.confirm(self, "删除这个情感模板？", "删除后不会影响已经生成的音频，也不会删除术语读音词条。", ok="删除"):
                return
            try:
                store = style_presets.delete_custom_style(style_id)
            except Exception:
                dialogs.alert(self, "情感模板", "删除失败，请重试。")
                return
            self._reload_indextts2_styles(select_id=str(store.get("last_selected_style_id") or style_presets.DEFAULT_STYLE_ID))
            rebuild_list()

        save_btn.clicked.connect(do_save)
        rebuild_list()
        close = QPushButton("完成")
        close.setObjectName("act")
        close.clicked.connect(dlg.accept)
        crow = QHBoxLayout()
        crow.addStretch(1)
        crow.addWidget(close)
        lay.addLayout(crow)
        dlg.exec()

    def _sync_indextts2_template_chip(self) -> None:
        if not hasattr(self, "indextts2_template_chips"):
            return
        sid = str(self._indextts2_current_style.get("id") or "")
        chip = self.indextts2_template_chips.get(sid)
        if chip is not None:
            chip.setChecked(True)

    def _on_indextts2_template_chip(self, style_id: str) -> None:
        if self._indextts2_style_dirty and not dialogs.confirm(
            self,
            "切换情感模板？",
            "当前情感配置还没有保存。切换模板会放弃这些未保存改动，但不会影响已生成音频。",
            ok="放弃改动并切换",
        ):
            self._sync_indextts2_template_chip()
            return
        self._apply_indextts2_style(style_presets.get_style(str(style_id)))
        self.indextts2_status.setText(f"已切换到情感模板「{self._indextts2_current_style.get('name', '自然')}」。")

    def _apply_indextts2_style(self, style: dict, *, persist: bool = True) -> None:
        self._indextts2_style_syncing = True
        self._indextts2_current_style = dict(style)
        preset = str(style.get("emotion_preset") or "natural")
        intensity = normalize_emotion_intensity(str(style.get("emotion_intensity") or DEFAULT_EMOTION_INTENSITY), preset)
        vector = style.get("emotion_vector")
        if not isinstance(vector, (list, tuple)):
            vector = emotion_vector_for(preset)
        weight = style.get("emotion_weight")
        if not isinstance(weight, (int, float)):
            weight = emotion_weight_for(intensity, preset)
        self._set_indextts2_advanced_vector(vector)
        self._set_indextts2_advanced_weight(weight)
        self.indextts2_glossary_panel.set_apply_enabled(bool(style.get("glossary_apply_enabled", True)), emit=False)
        self._indextts2_advanced_source_preset = preset
        self._indextts2_advanced_source_intensity = intensity
        self._indextts2_advanced_source_style_name = str(style.get("name") or "通用自然")
        self._indextts2_style_syncing = False
        self._indextts2_style_dirty = False
        self._sync_indextts2_template_chip()
        if persist:
            try:
                style_presets.set_last_selected(str(style.get("id") or style_presets.DEFAULT_STYLE_ID))
            except Exception:
                self.indextts2_status.setText("自定义情感模板暂时不可用，已使用系统模板。")
        self._update_indextts2_template_summary()
        self._render_indextts2_vector_state()

    def _current_indextts2_template(self) -> tuple[str, str]:
        style = self._indextts2_current_style
        preset = str(style.get("emotion_preset") or "natural")
        intensity = normalize_emotion_intensity(str(style.get("emotion_intensity") or DEFAULT_EMOTION_INTENSITY), preset)
        return preset, intensity

    def _update_indextts2_template_summary(self) -> None:
        if not hasattr(self, "indextts2_template_summary"):
            return
        style = self._indextts2_current_style
        glossary_label = "术语开" if self.indextts2_glossary_panel.apply_enabled() else "术语关"
        dirty = " · 未保存" if self._indextts2_style_dirty else ""
        name = str(style.get("name") or "自然")
        self.indextts2_template_name.setText(f"{name}{dirty}")
        self.indextts2_template_name.setToolTip(f"{name}{dirty}")
        self.indextts2_template_summary.setText(
            f"权重 {int(round(self._current_indextts2_advanced_weight() * 100))}% · {glossary_label}"
        )

    # ------------------------------------------------------------------ 8 维向量

    def _current_indextts2_advanced_vector(self) -> list[float]:
        return [round(slider.value() / 100, 2) for slider in self.indextts2_advanced_sliders]

    def _current_indextts2_advanced_weight(self) -> float:
        return round(self.indextts2_weight_slider.value() / 100, 2)

    def _set_indextts2_advanced_vector(self, vector: tuple[float, ...] | list[float]) -> None:
        self._indextts2_advanced_syncing = True
        clean = normalize_emotion_vector(vector)
        for slider, value_label, value in zip(self.indextts2_advanced_sliders, self.indextts2_advanced_value_labels, clean):
            slider.setValue(int(round(value * 100)))
            value_label.setText(f"{value:.2f}")
        self._indextts2_advanced_syncing = False

    def _set_indextts2_advanced_weight(self, weight: float) -> None:
        self._indextts2_advanced_syncing = True
        clean = max(self.indextts2_weight_slider.minimum(), min(self.indextts2_weight_slider.maximum(), int(round(float(weight) * 100))))
        self.indextts2_weight_slider.setValue(clean)
        self.indextts2_weight_value.setText(f"{clean}%")
        self._indextts2_advanced_syncing = False

    def _on_indextts2_vector_changed(self, _value: int = 0) -> None:
        for slider, value_label in zip(self.indextts2_advanced_sliders, self.indextts2_advanced_value_labels):
            value_label.setText(f"{slider.value() / 100:.2f}")
        if self._indextts2_advanced_syncing:
            return
        self._indextts2_style_dirty = self._current_indextts2_style_dirty()
        self._render_indextts2_vector_state()
        self._update_indextts2_template_summary()
        self._update_indextts2_generate_state()

    def _on_indextts2_weight_changed(self, _value: int = 0) -> None:
        self.indextts2_weight_value.setText(f"{self.indextts2_weight_slider.value()}%")
        if self._indextts2_advanced_syncing:
            return
        self._indextts2_style_dirty = self._current_indextts2_style_dirty()
        self._update_indextts2_template_summary()
        self._update_indextts2_generate_state()

    def _clear_indextts2_advanced_vector(self) -> None:
        self._set_indextts2_advanced_vector((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        self._indextts2_style_dirty = self._current_indextts2_style_dirty()
        self._render_indextts2_vector_state()
        self._update_indextts2_generate_state()
        self.main._toast("已清零情感向量")

    def _indextts2_advanced_total(self) -> float:
        try:
            return emotion_vector_total(self._current_indextts2_advanced_vector())
        except ValueError:
            return round(sum(self._current_indextts2_advanced_vector()), 4)

    def _render_indextts2_vector_state(self) -> None:
        if not hasattr(self, "indextts2_advanced_budget"):
            return
        total = self._indextts2_advanced_total()
        if total > MAX_EMOTION_VECTOR_TOTAL:
            over = total - MAX_EMOTION_VECTOR_TOTAL
            self.indextts2_advanced_budget.setText(f"总量 {total:.2f} / 0.80 · 超出 {over:.2f}")
            self.indextts2_advanced_budget.setStyleSheet(
                f"color:{self._consumer.accent};font-size:11px;font-weight:600;"
            )
        else:
            self.indextts2_advanced_budget.setText(f"总量 {total:.2f} / 0.80")
            self.indextts2_advanced_budget.setStyleSheet("")

    def _current_indextts2_style_dirty(self) -> bool:
        glossary_enabled = self.indextts2_glossary_panel.apply_enabled()
        current_vector = self._current_indextts2_advanced_vector()
        style_vector = self._indextts2_current_style.get("emotion_vector")
        if not isinstance(style_vector, (list, tuple)):
            style_vector = emotion_vector_for(str(self._indextts2_current_style.get("emotion_preset") or "natural"))
        try:
            style_weight = float(
                self._indextts2_current_style.get(
                    "emotion_weight",
                    emotion_weight_for(
                        str(self._indextts2_current_style.get("emotion_intensity") or DEFAULT_EMOTION_INTENSITY),
                        str(self._indextts2_current_style.get("emotion_preset") or "natural"),
                    ),
                )
            )
        except (TypeError, ValueError):
            style_weight = emotion_weight_for(
                str(self._indextts2_current_style.get("emotion_intensity") or DEFAULT_EMOTION_INTENSITY),
                str(self._indextts2_current_style.get("emotion_preset") or "natural"),
            )
        return (
            glossary_enabled != bool(self._indextts2_current_style.get("glossary_apply_enabled", True))
            or [round(float(value), 2) for value in current_vector] != [round(float(value), 2) for value in style_vector]
            or self._current_indextts2_advanced_weight() != round(max(self.indextts2_weight_slider.minimum() / 100, min(self.indextts2_weight_slider.maximum() / 100, style_weight)), 2)
        )

    # ------------------------------------------------------------------ 情感参考音频 / 描述文本

    def _pick_indextts2_emotion_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择情感参考音频", self.cfg.last_output_dir,
            "音频文件 (*.wav *.mp3 *.m4a *.aac *.flac *.aiff *.aif);;所有文件 (*)",
        )
        if not path:
            return
        self._load_indextts2_emotion_audio(path)

    def _load_indextts2_emotion_audio(self, path: str) -> None:
        self._indextts2_emotion_audio_path = path
        self._indextts2_emotion_audio_recorded = False
        self._indextts2_emotion_audio_meta = {"source": "选择音频"}
        self._render_indextts2_emoaudio_state()
        self._update_indextts2_generate_state()

    def _record_indextts2_emotion_audio(self) -> None:
        result = self._record_audio_for_voice_use("emotion_reference")
        if not result:
            return
        path, meta = result
        self._indextts2_emotion_audio_path = path
        self._indextts2_emotion_audio_recorded = True
        self._indextts2_emotion_audio_meta = meta
        self._render_indextts2_emoaudio_state()
        self._update_indextts2_generate_state()
        self.main._toast("已作为本次情感参考音频")

    def _clear_indextts2_emotion_audio(self) -> None:
        self._indextts2_emotion_audio_path = ""
        self._indextts2_emotion_audio_recorded = False
        self._indextts2_emotion_audio_meta = {}
        self._render_indextts2_emoaudio_state()
        self._update_indextts2_generate_state()

    def _render_indextts2_emoaudio_state(self) -> None:
        if hasattr(self, "_approved_index"):
            self._approved_index.sync_reference()
        if not hasattr(self, "indextts2_emoaudio_title"):
            return
        if self._indextts2_emotion_audio_path:
            source = str(self._indextts2_emotion_audio_meta.get("source") or ("录制参考" if self._indextts2_emotion_audio_recorded else "选择音频"))
            stats = self._indextts2_emotion_audio_meta.get("stats")
            duration = ""
            if isinstance(stats, dict) and stats.get("duration"):
                duration = f" · {float(stats['duration']):.1f}s"
            filename = Path(self._indextts2_emotion_audio_path).name
            self.indextts2_emoaudio_title.setText("已选择情感参考音频")
            self.indextts2_emoaudio_drop.action.setText("点击更换")
            self.indextts2_emoaudio_drop.set_status(f"{filename} · {source}{duration} · 仅本次使用")
            self.indextts2_emoaudio_drop.setToolTip(self._indextts2_emotion_audio_path)
        else:
            self.indextts2_emoaudio_title.setText("拖入情感参考音频")
            self.indextts2_emoaudio_drop.action.setText("或点击选择")
            self.indextts2_emoaudio_drop.set_status("WAV · MP3 · M4A · AAC · FLAC · AIFF")
            self.indextts2_emoaudio_drop.setToolTip("")

    # ------------------------------------------------------------------ 模板增删存（复用既有 store）

    def _indextts2_template_summary_for_dialog(self, style: dict | None = None) -> str:
        style = dict(style or self._indextts2_current_style)
        source = "系统" if style.get("system", False) else "我的"
        glossary = "术语开" if self.indextts2_glossary_panel.apply_enabled() else "术语关"
        return (
            f"{source} · {style.get('name', '自然')} · 总量 {self._indextts2_advanced_total():.2f} · "
            f"权重 {self._current_indextts2_advanced_weight():.2f} · {glossary}"
        )

    def _indextts2_template_name_dialog(self, *, title: str = "保存为情感模板", initial_name: str = "") -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        adopt_dialog(dlg, accessible_name=title)
        adopt_consumer_theme_scope(dlg, "coreWorkspace")
        dlg.setMinimumWidth(390)
        dlg.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(12)
        head = QLabel(title)
        head.setObjectName("sectionTitle")
        lay.addWidget(head)
        name = QLineEdit()
        name.setObjectName("linkInput")
        name.setPlaceholderText("模板名称")
        name.setText(initial_name)
        lay.addWidget(name)
        summary = QLabel(self._indextts2_template_summary_for_dialog())
        summary.setObjectName("pageMeta")
        summary.setWordWrap(True)
        lay.addWidget(summary)
        note = QLabel("只需命名模板；当前 8 维情绪参数、整体情感权重和术语开关会自动保存。")
        note.setObjectName("pageMeta")
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("act")
        cancel.clicked.connect(dlg.reject)
        row.addWidget(cancel)
        ok = QPushButton("  保存")
        ok.setObjectName("grabBtn")
        ok.clicked.connect(dlg.accept)
        row.addWidget(ok)
        lay.addLayout(row)
        if dlg.exec() != QDialog.Accepted:
            return None
        return name.text()

    def _resolve_indextts2_save_target(self, name: str, *, current_style_id: str | None = None) -> tuple[str, str | None] | None:
        store = style_presets.load_store()
        preset, intensity = self._current_indextts2_template()
        try:
            clean_name, _, _ = style_presets.validate_style(name, preset, intensity)
        except ValueError as exc:
            dialogs.alert(self, "情感模板", str(exc))
            return None
        existing = style_presets.find_style_by_name(clean_name, store)
        if not existing:
            return clean_name, current_style_id
        existing_id = str(existing.get("id") or "")
        if existing.get("system", False):
            dialogs.alert(self, "情感模板", "系统模板不能覆盖，请换一个名称。")
            return None
        if current_style_id and existing_id == current_style_id:
            return clean_name, current_style_id
        if dialogs.confirm(
            self,
            "覆盖同名模板？",
            f"已存在自定义模板「{clean_name}」。要用当前参数覆盖原模板吗？",
            ok="覆盖",
        ):
            return clean_name, existing_id
        self.indextts2_status.setText("已取消覆盖，当前未保存配置仍保留。")
        return None

    def _upsert_indextts2_style_from_current(self, name: str, *, style_id: str | None = None) -> dict:
        preset, intensity = self._current_indextts2_template()
        return style_presets.upsert_custom_style(
            name,
            preset,
            self.indextts2_glossary_panel.apply_enabled(),
            intensity,
            style_id=style_id,
            emotion_vector=self._current_indextts2_advanced_vector(),
            emotion_weight=self._current_indextts2_advanced_weight(),
        )

    def _save_indextts2_style(self) -> None:
        if self._indextts2_advanced_total() > MAX_EMOTION_VECTOR_TOTAL:
            dialogs.alert(self, "情感模板", "当前情感总量超出上限，请降低部分滑杆后再保存。")
            return
        name = self._indextts2_template_name_dialog(title="保存为情感模板")
        if name is None:
            return
        target = self._resolve_indextts2_save_target(name)
        if target is None:
            return
        clean_name, style_id = target
        try:
            store = self._upsert_indextts2_style_from_current(clean_name, style_id=style_id)
        except ValueError as exc:
            dialogs.alert(self, "情感模板", str(exc))
            return
        except Exception:
            dialogs.alert(self, "情感模板", "保存失败，请重试。")
            return
        self._reload_indextts2_styles(select_id=str(store.get("last_selected_style_id") or style_presets.DEFAULT_STYLE_ID))
        self._set_indextts2_emotion_mode("template")
        self.main._toast("情感模板已保存")

    def _current_indextts2_style_meta(self) -> dict:
        style = dict(self._indextts2_current_style)
        meta = style_presets.style_meta(style, dirty=self._indextts2_style_dirty)
        _, intensity = self._current_indextts2_template()
        meta["emotion_intensity"] = intensity
        meta["emotion_vector"] = self._current_indextts2_advanced_vector()
        meta["emotion_weight"] = self._current_indextts2_advanced_weight()
        return meta

    def _on_indextts2_glossary_changed(self) -> None:
        if not self._indextts2_style_syncing:
            self._indextts2_style_dirty = self._current_indextts2_style_dirty()
            self._update_indextts2_template_summary()
        self._update_indextts2_generate_state()

    def _preview_indextts2_glossary(self) -> None:
        text = self.indextts2_text_edit.toPlainText()
        result = self.indextts2_glossary_panel.preview_text(text)
        if result.applied:
            self.indextts2_status.setText(f"术语读音预览：将应用 {result.matched_terms} 条词条，替换 {result.replacement_count} 处。")
        else:
            self.indextts2_status.setText("术语读音预览：当前文本未命中，或总开关已关闭。")

    # ------------------------------------------------------------------ 本地环境状态

    def _refresh_indextts2_status(self, *, force: bool = False) -> None:
        if self._indextts2_status_checking:
            return
        if self._indextts2_status is not None and not force:
            self._render_indextts2_status(self._indextts2_status)
            return
        self._indextts2_status_checking = True
        self._indextts2_status_error = ""
        self._indextts2_status = None if force else self._indextts2_status
        self._set_indextts2_ready_chip("● 检查中", "info")
        self.indextts2_status_title.setText(f"正在检查 {self._index_name} 本地环境")
        self.indextts2_status_detail.setText("正在后台检查 runtime、model 与 config，界面可继续操作。")
        self.indextts2_status_banner.setVisible(False)
        set_surface_status(self.indextts2_status_banner, "info")
        self._render_indextts2_status_rows(checking=True)
        self.btn_indextts2_refresh.setEnabled(False)
        self._update_indextts2_config_action()
        self._update_indextts2_generate_state()
        self._indextts2_status_worker = IndexTTS2StatusWorker(self._index_version)
        self._indextts2_status_worker.finished_ok.connect(self._on_indextts2_status_ready)
        self._indextts2_status_worker.failed.connect(self._on_indextts2_status_failed)
        self._indextts2_status_worker.finished.connect(self._on_indextts2_status_finished)
        self._indextts2_status_worker.start()

    def _set_indextts2_ready_chip(self, text: str, kind: str) -> None:
        if hasattr(self, "indextts2_ready_chip"):
            self.indextts2_ready_chip.setText(text)
            set_status_kind(self.indextts2_ready_chip, kind)

    def _on_indextts2_status_ready(self, status) -> None:
        self._indextts2_status = status
        self._indextts2_status_error = ""
        self._render_indextts2_status(status)
        self._update_indextts2_generate_state()
        self._sync_indextts2_background_state_with_environment()
        self._emit_model_context_changed()

    def _on_indextts2_status_failed(self, msg: str) -> None:
        self._indextts2_status = None
        self._indextts2_status_error = msg
        self._set_indextts2_ready_chip("● 检查失败", "error")
        self.indextts2_status_banner.setVisible(False)
        set_surface_status(self.indextts2_status_banner, "error")
        self.indextts2_status_title.setText(f"无法检查 {self._index_name} 状态")
        self.indextts2_status_detail.setText(msg)
        self._render_indextts2_status_rows(failed=True)
        self._update_indextts2_config_action()
        self._update_indextts2_generate_state()
        self._sync_indextts2_background_state_with_environment()
        self._emit_model_context_changed()

    def _on_indextts2_status_finished(self) -> None:
        self._indextts2_status_checking = False
        self.btn_indextts2_refresh.setEnabled(True)
        self._update_indextts2_config_action()
        self._update_indextts2_generate_state()
        self._sync_indextts2_background_state_with_environment()
        self._emit_model_context_changed()

    def _render_indextts2_status(self, status) -> None:
        self._render_indextts2_status_rows(status=status)
        self._update_indextts2_config_action(status)
        if status.ready and str(getattr(status, "model_version", "2.5")) == self._index_version:
            self._set_indextts2_ready_chip("● AI大模型就绪", "success")
            set_surface_status(self.indextts2_status_banner, "success")
            self.indextts2_status_banner.setVisible(False)
            self.indextts2_status_title.setText(f"{self._index_name} 已就绪 · 本地生成 · 可离线使用")
            self.indextts2_status_detail.setText(
                f"runtime {_gb(status.runtime_size_bytes)} · model {_gb(status.model_size_bytes)}。"
                "本模型用于情感配音，启动和生成可能明显慢于实时。"
            )
            return
        self._set_indextts2_ready_chip("● 未就绪", "warning")
        self.indextts2_status_banner.setVisible(False)
        set_surface_status(self.indextts2_status_banner, "warning")
        missing = ", ".join(status.missing_items) or "未知缺失项"
        title = f"{self._index_name} 本地运行环境未准备"
        if status.runtime_ready and status.model_ready and not status.config_ready:
            title = f"{self._index_name} 受管配置不可用"
        elif status.runtime_ready and not status.model_ready:
            title = f"缺少 {self._index_name} model"
        elif status.model_ready and not status.runtime_ready:
            title = f"缺少 {self._index_name} runtime"
        self.indextts2_status_title.setText(title)
        self.indextts2_status_detail.setText(
            f"请到 AI 大模型配置 → 文案配音 → 本地部署安装 {self._index_name}。"
            f"当前界面不会联网下载，也不会要求 API Key。缺失：{missing}"
        )

    def _render_indextts2_status_rows(self, *, status=None, checking: bool = False, failed: bool = False) -> None:
        if status is None:
            runtime = model = config = _readiness_text(False, checking=checking, failed=failed)
            kinds = ("info", "info", "info") if checking else (("error", "error", "error") if failed else ("neutral", "neutral", "neutral"))
        else:
            runtime = _readiness_text(bool(status.runtime_ready))
            model = _readiness_text(bool(status.model_ready))
            config = _readiness_text(bool(status.config_ready), state=str(getattr(status, "config_state", "")))
            config_state = str(getattr(status, "config_state", ""))
            kinds = (
                "success" if status.runtime_ready else "warning",
                "success" if status.model_ready else "warning",
                "success" if status.config_ready else ("error" if config_state == "error" else "warning"),
            )
        self.indextts2_runtime_status.setText(f"runtime  {runtime}")
        self.indextts2_model_status.setText(f"model    {model}")
        self.indextts2_config_status.setText(f"config   {config}")
        for label, kind in zip(
            (self.indextts2_runtime_status, self.indextts2_model_status, self.indextts2_config_status),
            kinds,
        ):
            set_status_kind(label, kind)

    def _update_indextts2_config_action(self, status=None) -> None:
        if not hasattr(self, "btn_indextts2_sync_config"):
            return
        status = status or self._indextts2_status
        syncing = bool(self._indextts2_config_worker and self._indextts2_config_worker.isRunning())
        can_sync = (
            self._index_version == "2.5"
            and status is not None
            and bool(getattr(status, "runtime_ready", False))
            and bool(getattr(status, "model_ready", False))
            and str(getattr(status, "config_state", "")) == "needs_fix"
        )
        self.btn_indextts2_sync_config.setVisible(can_sync or syncing)
        self.btn_indextts2_sync_config.setEnabled(can_sync and not syncing and not self._indextts2_status_checking)
        self.btn_indextts2_sync_config.setToolTip("受管运行时通常无需同步官方配置" if can_sync else "")

    def _sync_indextts2_config(self) -> None:
        if self._index_version != "2.5":
            return
        if self._indextts2_config_worker and self._indextts2_config_worker.isRunning():
            return
        status = self._indextts2_status
        if (
            status is None
            or not getattr(status, "runtime_ready", False)
            or not getattr(status, "model_ready", False)
            or str(getattr(status, "config_state", "")) != "needs_fix"
        ):
            dialogs.alert(self, "同步配置", "当前无需同步配置，或本地 runtime/model 尚未准备完成。")
            return
        ok = dialogs.confirm(
            self,
            f"同步 {self._index_name} 配置",
            "这会把本机官方 IndexTTS 配置的 model_dir 同步为 Lulu 受管模型目录。"
            "操作只在本机写入配置文件，不会联网下载，不会要求 API Key，也不会上传文本或音频。",
            ok="同步配置",
        )
        if not ok:
            return
        self.indextts2_status_title.setText(f"正在同步 {self._index_name} 配置")
        self.indextts2_status_detail.setText("正在写入本机官方 IndexTTS 配置；完成后会自动重新检查状态。")
        self.btn_indextts2_refresh.setEnabled(False)
        self._indextts2_config_worker = IndexTTS2ConfigSyncWorker()
        self._indextts2_config_worker.finished_ok.connect(self._on_indextts2_config_synced)
        self._indextts2_config_worker.failed.connect(self._on_indextts2_config_sync_failed)
        self._indextts2_config_worker.finished.connect(self._on_indextts2_config_sync_finished)
        self._update_indextts2_config_action()
        self._update_indextts2_generate_state()
        self._indextts2_config_worker.start()

    def _on_indextts2_config_synced(self, status) -> None:
        self._indextts2_status = status
        self._indextts2_status_error = ""
        self._render_indextts2_status(status)
        self.indextts2_status.setText(f"{self._index_name} 配置已同步，可以继续生成。")
        self.main._toast(f"{self._index_name} 配置已同步")
        self._update_indextts2_generate_state()
        self._sync_indextts2_background_state_with_environment()

    def _on_indextts2_config_sync_failed(self, msg: str) -> None:
        self.indextts2_status_banner.setVisible(False)
        set_surface_status(self.indextts2_status_banner, "error")
        self.indextts2_status_title.setText(f"{self._index_name} 配置同步失败")
        self.indextts2_status_detail.setText(msg)
        self.indextts2_status.setText("配置同步失败。")
        self._update_indextts2_config_action()
        self._update_indextts2_generate_state()
        self._sync_indextts2_background_state_with_environment()

    def _on_indextts2_config_sync_finished(self) -> None:
        self.btn_indextts2_refresh.setEnabled(True)
        self._update_indextts2_config_action()
        self._update_indextts2_generate_state()

    def _show_indextts2_help(self) -> None:
        dialogs.alert(
            self,
            f"{self._index_name} 准备说明",
            f"请到 AI 大模型配置 → 文案配音 → 本地部署安装 {self._index_name}。"
            "重新检查只做本地只读检查，不会联网下载，不会要求 API Key。"
            "生成时使用 Lulu 受管 runtime 与模型目录，不依赖本机旧 uv venv 或官方全局配置，也不会上传文本或音频。",
        )

    # ------------------------------------------------------------------ 音色

    def _selected_indextts2_voice(self):
        vid = self.indextts2_voice_combo.currentData()
        return voices.get_voice(vid) if vid else None

    def _selected_indextts2_voice_label(self) -> str:
        voice = self._selected_indextts2_voice()
        if not voice:
            data = self.indextts2_voice_combo.currentData()
            return str(data or "未选择音色")
        name = str(getattr(voice, "name", "") or "").strip()
        return name or str(getattr(voice, "id", "") or "未命名音色")[:12]

    def _indextts2_voice_ready(self) -> bool:
        voice = self._selected_indextts2_voice()
        if voice is None:
            return False
        return Path(voice.reference_audio_path).is_file()

    def _update_indextts2_voice_status(self) -> None:
        if hasattr(self, "_approved_index"):
            self._approved_index.sync_voice()
        voice = self._selected_indextts2_voice()
        meta = "点击卡片打开音色库"
        if voice is None:
            self.indextts2_voice_status.setText("")
        elif not Path(voice.reference_audio_path).exists():
            self.indextts2_voice_status.setText("缺少参考音频")
            meta = "缺少参考音频 · 点击更换"
        else:
            self.indextts2_voice_status.setText("音色可用")
            meta = "本地音色可用 · 点击整卡更换"
        refs = self._voice_sel.get(id(self.indextts2_voice_combo))
        if refs is not None:
            selector = refs.get("selector")
            if selector is not None:
                selector.trigger.setToolTip(meta)
                selector.trigger.setAccessibleDescription(meta)
        self._update_indextts2_generate_state()

    def _new_voice(self, target_combo: QComboBox | None = None) -> None:
        """共用音色入口；IndexTTS 只保存音频，其他引擎按需转写。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择参考音频", self.cfg.last_output_dir,
            "音频文件 (*.wav *.mp3 *.m4a *.aac *.flac *.aiff *.aif);;所有文件 (*)",
        )
        if not path:
            return
        self._new_voice_from_path(path, target_combo=target_combo, cleanup_source=False)

    def _record_voice_sample(self, target_combo: QComboBox | None = None) -> None:
        result = self._record_audio_for_voice_use("voice_sample")
        if not result:
            return
        path, _meta = result
        self._new_voice_from_path(path, target_combo=target_combo, cleanup_source=True)

    def _record_audio_for_voice_use(self, purpose: str) -> tuple[str, dict[str, object]] | None:
        dialog = _VoiceRecordingDialog(self, purpose=purpose)
        if dialog.exec() != QDialog.Accepted or not dialog.result_path:
            return None
        return dialog.result_path, dict(dialog.result_meta)

    def _analyze_voice_recording(self, path: Path) -> dict[str, object]:
        from ..core.recording import analyze_recording_file

        return analyze_recording_file(path)

    def _mix_voice_recording_tracks(self, mic_path: Path, system_path: Path, output_path: Path) -> dict[str, object]:
        from ..core.recording import mix_recording_tracks

        return mix_recording_tracks(mic_path, system_path, output_path)

    def _new_voice_from_path(
        self,
        path: str,
        *,
        target_combo: QComboBox | None = None,
        cleanup_source: bool = False,
    ) -> None:
        audio_only = target_combo is None or target_combo is self.indextts2_voice_combo
        from .voice_library import library_style
        dlg = QDialog(self._voice_library_dialog or self)
        dlg.setWindowTitle("新建音色")
        adopt_dialog(dlg, accessible_name="新建音色")
        adopt_consumer_theme_scope(dlg, "coreWorkspace")
        dlg.setMinimumWidth(440)
        dlg.setStyleSheet(library_style(self._consumer))
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)
        head = QLabel("新建音色")
        head.setObjectName("libraryTitle")
        lay.addWidget(head)
        ref_lab = QLabel(f"参考音频：{Path(path).name}")
        ref_lab.setObjectName("libraryMeta")
        ref_lab.setWordWrap(True)
        lay.addWidget(ref_lab)
        guide = QLabel("建议使用 10–30 秒清晰的单人录音，并确认有权使用。")
        guide.setObjectName("libraryMeta")
        guide.setWordWrap(True)
        lay.addWidget(guide)
        lay.addWidget(QLabel("音色名称"))
        name = QLineEdit()
        name.setAccessibleName("音色名称")
        name.setObjectName("linkInput")
        name.setPlaceholderText("音色名称")
        name.setText(Path(path).stem)
        lay.addWidget(name)
        lay.addWidget(QLabel("参考文字 · 选填" if audio_only else "参考文字"))
        ref_text = QPlainTextEdit()
        ref_text.setAccessibleName("参考文字")
        ref_text.setTabChangesFocus(True)
        ref_text.setObjectName("mdEditor")
        ref_text.setFixedHeight(96)
        ref_text.setPlaceholderText("录音中实际说的话，之后也可以补充" if audio_only else "正在识别录音中的文字，可校对后保存")
        lay.addWidget(ref_text)
        status = QLabel("IndexTTS 可直接使用录音，无需填写参考文字" if audio_only else "正在本地识别参考文字…")
        status.setObjectName("libraryMeta")
        status.setWordWrap(True)
        lay.addWidget(status)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        adopt_button(cancel, role="secondary", accessible_name="取消添加音色")
        cancel.setFixedHeight(34)
        cancel.setAutoDefault(False)
        cancel.clicked.connect(dlg.reject)
        row.addWidget(cancel)
        ok = QPushButton("保存音色")
        adopt_button(ok, role="primary", accessible_name="保存音色")
        ok.setFixedHeight(34)
        ok.setAutoDefault(False)
        saved_voice = None
        save_error = QLabel("")
        save_error.setObjectName("libraryMeta")
        save_error.setAccessibleName("音色保存提示")
        save_error.setWordWrap(True)
        lay.addWidget(save_error)
        def save_voice():
            nonlocal saved_voice
            if not name.text().strip():
                save_error.setText("请填写音色名称。")
                name.setFocus()
                return
            try:
                saved_voice = voices.add_voice(name.text(), path, ref_text.toPlainText())
            except Exception as exc:  # noqa: BLE001
                save_error.setText(user_error_message(exc, "音色保存失败，请重试。"))
                return
            dlg.accept()
        ok.clicked.connect(save_voice)
        row.addWidget(ok)
        lay.addLayout(row)

        if self._new_voice_worker is not None and self._new_voice_worker.isRunning():
            # 上一次转写还没跑完就再次打开：断开旧连接、让它后台自然结束，不在主线程等待
            for sig in (self._new_voice_worker.progress, self._new_voice_worker.finished_ok, self._new_voice_worker.failed):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        if not audio_only:
            self._new_voice_worker = ReferenceTranscribeWorker(path)
            self._new_voice_worker.progress.connect(lambda msg, _frac: status.setText(f"参考音频：{msg}"))
            self._new_voice_worker.finished_ok.connect(lambda text: (ref_text.setPlainText(text), status.setText("参考文本已生成，请校对后保存。")))
            self._new_voice_worker.failed.connect(lambda msg: status.setText(f"自动转写失败，请手动填写参考文本。{msg}"))
            self._new_voice_worker.start()

        accepted = dlg.exec() == QDialog.Accepted
        # 关键：不要在主线程 wait()/terminate() 后台转写线程（SenseVoice 加载很重，会卡死 UI、
        # terminate 还可能崩）。改为断开它与弹窗控件的连接，让它在后台自然跑完；弹窗销毁后也不回调。
        if self._new_voice_worker is not None:
            for sig in (self._new_voice_worker.progress, self._new_voice_worker.finished_ok, self._new_voice_worker.failed):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        if not accepted or saved_voice is None:
            if cleanup_source:
                self._cleanup_recorded_source(path)
            return
        voice = saved_voice
        self.refresh()
        combo = target_combo if target_combo is not None else self.indextts2_voice_combo
        idx = combo.findData(voice.id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        if cleanup_source:
            self._cleanup_recorded_source(path)
        self.main._toast("音色已保存")

    def _cleanup_recorded_source(self, path: str) -> None:
        try:
            p = Path(path)
            if p.exists():
                p.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ 生成门槛 + 生成

    def _update_indextts2_generate_state(self) -> None:
        if not hasattr(self, "btn_indextts2_generate"):
            return
        reason = self._indextts2_gate_reason()
        busy = bool(self._indextts2_worker and self._indextts2_worker.isRunning())
        can_generate = reason == "" and not busy
        self.btn_indextts2_generate.setEnabled(can_generate)
        preview_text = self._indextts2_preview_text()
        self.btn_indextts2_preview.setEnabled(bool(preview_text) and not busy and not self._indextts2_gate_reason(text=preview_text))
        self.btn_indextts2_preview.setVisible(not busy)
        self.btn_indextts2_generate.setToolTip(reason or "开始本地情感配音")
        self.indextts2_gate_hint.setVisible(bool(reason))
        self.indextts2_gate_hint.setText(f"当前还不能生成：{reason}" if reason else "")
        if hasattr(self, "_approved_index"):
            self._approved_index.sync_busy(busy)
        self._refit_panel()

    def _indextts2_gate_reason(self, *, text: str | None = None) -> str:
        if self._indextts2_release_worker and self._indextts2_release_worker.isRunning():
            return "正在释放上一版本的后台，请稍候"
        busy = bool(self._indextts2_worker and self._indextts2_worker.isRunning())
        other_engine = self._local_synthesis_busy_label(exclude="indextts2")
        if other_engine:
            return f"{other_engine} 正在生成，请先等待或取消"
        if self._indextts2_status_checking:
            return "正在检查本地环境..."
        if self._indextts2_status_error:
            return "状态检查失败，请重新检查"
        status = self._indextts2_status
        if status is None:
            return f"请先检查 {self._index_name} 本地环境"
        if str(getattr(status, "model_version", "2.5")) != self._index_version:
            return "所选版本与本地模型不一致，请检查对应版本的环境"
        if any("install_receipt_invalid" in item for item in getattr(status, "missing_items", ())):
            return f"{self._index_name} 安装校验记录失效，需核验并修复后使用"
        if not getattr(status, "runtime_ready", False):
            return f"请先准备 {self._index_name} runtime"
        if not getattr(status, "model_ready", False):
            return f"请先准备 {self._index_name} model"
        if not getattr(status, "config_ready", False):
            return f"请先完成 {self._index_name} 配置"
        if not self._indextts2_voice_ready():
            return f"请选择 {self._index_name} 可用音色"
        if not self._indextts2_text_has_content:
            return "请输入要生成的文字"
        text = self.indextts2_text_edit.toPlainText() if text is None else text
        assessment = assess_text_length(text)
        segment_plan = assess_segment_plan(text)
        if segment_plan.is_blocked and segment_plan.level == "too_long":
            return segment_plan.blocking_reason
        if assessment.level == "blocked" and not segment_plan.is_segmented:
            return assessment.blocking_reason
        mode = self._indextts2_emotion_mode
        if mode == "emoaudio" and not (self._indextts2_emotion_audio_path and Path(self._indextts2_emotion_audio_path).exists()):
            return "请选择情感参考音频"
        if mode == "emotext" and not self.indextts2_emotion_text_edit.toPlainText().strip():
            return "请输入情感描述文本"
        if mode == "vector" and self._indextts2_advanced_total() > MAX_EMOTION_VECTOR_TOTAL:
            return "8 维向量超出上限，暂不能生成"
        if busy:
            return "正在生成当前配音"
        return ""

    def _indextts2_environment_ready(self) -> bool:
        status = self._indextts2_status
        return bool(status is not None and getattr(status, "runtime_ready", False)
                    and str(getattr(status, "model_version", "2.5")) == self._index_version
                    and getattr(status, "model_ready", False) and getattr(status, "config_ready", False))

    def _indextts2_environment_waiting_label(self) -> str:
        if self._indextts2_status_checking:
            return "本地环境检查中"
        if self._indextts2_status_error:
            return "状态检查失败 · 请重新检查"
        status = self._indextts2_status
        if status is None:
            return "请先检查本地环境"
        if not getattr(status, "runtime_ready", False):
            return "本地环境未准备 · 请先安装 runtime"
        if not getattr(status, "model_ready", False):
            return "本地环境未准备 · 请先安装 model"
        if not getattr(status, "config_ready", False):
            return "本地配置未完成 · 请先同步/检查"
        return "本地环境未准备 · 请先完成安装/检查"

    def _indextts2_emotion_engine_options(self) -> dict:
        """按当前情感控制方式拼 engine_options 的情感相关字段。"""
        mode = self._indextts2_emotion_mode
        weight = self._current_indextts2_advanced_weight()
        if mode == "follow":
            return {"emotion_mode": "follow"}
        if mode == "emoaudio":
            return {
                "emotion_mode": "emoaudio",
                "emotion_audio_path": self._indextts2_emotion_audio_path,
                "emotion_weight": weight,
            }
        if mode == "emotext":
            return {
                "emotion_mode": "emotext",
                "emotion_text": self.indextts2_emotion_text_edit.toPlainText().strip(),
                "emotion_weight": weight,
            }
        preset, intensity = self._current_indextts2_template()
        vector = list(normalize_emotion_vector(self._current_indextts2_advanced_vector()))
        summary = (
            self._indextts2_template_summary_for_dialog()
            if mode == "vector"
            else f"情感模板「{self._indextts2_current_style.get('name', '自然')}」"
        )
        return {
            "emotion_mode": mode,  # template 或 vector
            "emotion_preset": preset,
            "emotion_intensity": intensity,
            "emotion_vector": vector,
            "emotion_weight": weight,
            "emotion_summary": summary,
            "emotion_source_preset": preset,
            "emotion_source_intensity": intensity,
        }

    def _indextts2_preview_text(self) -> str:
        selected = self.indextts2_text_edit.textCursor().selectedText().replace("\u2029", "\n").strip()
        if hasattr(self, "_approved_index"):
            return selected
        if selected:
            return selected
        text = self.indextts2_text_edit.toPlainText().strip().split("\n", 1)[0]
        end = 80
        for match in re.finditer(r'<[^|>\n]+\|[^>\n]+>', text):
            if match.start() < end < match.end():
                end = match.end()
                break
        return text[:end]

    def _generate_indextts2(self, _checked=False, *, preview: bool = False) -> None:
        if self._indextts2_worker and self._indextts2_worker.isRunning():
            return
        generation_options = None
        if self._index_version in ('2.0', '2.5'):
            from ..core.synthesis.indextts2_worker import validate_index_generation
            key = 'indextts25_generation_options' if self._index_version == '2.5' else 'indextts2_generation_options'
            try:
                generation_options = validate_index_generation(getattr(self.cfg, key, {}), self._index_version)
            except ValueError:
                dialogs.alert(self, '生成设置', '生成设置无效，请打开生成设置并恢复默认后再生成。')
                return
        if not self._guard_local_synthesis_start("indextts2"):
            return
        text = self._indextts2_preview_text() if preview else self.indextts2_text_edit.toPlainText().strip()
        if preview and len(text) > 200:
            dialogs.alert(self, "选段试听", "请选择 200 字以内的一小段，再点击试听选段。")
            return
        reason = self._indextts2_gate_reason(text=text)
        if reason and reason != "请输入要生成的文字":
            dialogs.alert(self, f"{self._index_name}", reason)
            return
        if not text:
            dialogs.alert(self, f"{self._index_name}", "请输入要生成的文字。")
            return
        length_assessment = assess_text_length(text)
        segment_plan = assess_segment_plan(text)
        if segment_plan.is_blocked and segment_plan.level == "too_long":
            self.indextts2_status.setText(f"文本长度保护：{segment_plan.hint}")
            dialogs.alert(self, f"{self._index_name}", segment_plan.blocking_reason)
            return
        if length_assessment.is_blocked and not segment_plan.is_segmented:
            self.indextts2_status.setText(f"文本长度保护：{length_assessment.hint}")
            dialogs.alert(self, f"{self._index_name}", length_assessment.blocking_reason)
            return
        glossary_result = self.indextts2_glossary_panel.apply_to_text(text)
        text_for_generation = glossary_result.text.strip() or text
        segment_plan = assess_segment_plan(text_for_generation)
        if segment_plan.is_blocked:
            self.indextts2_status.setText(f"文本长度保护：{segment_plan.hint}")
            dialogs.alert(self, f"{self._index_name}", segment_plan.blocking_reason)
            return
        text_segments = list(segment_plan.segments) if segment_plan.is_segmented else []
        self._indextts2_glossary_applied = bool(glossary_result.applied)
        glossary_prefix = "部分术语规则未应用，请检查词典提示。" if glossary_result.warnings else ""

        emotion_opts = self._indextts2_emotion_engine_options()
        voice_id = self.indextts2_voice_combo.currentData()
        preset = str(emotion_opts.get("emotion_preset") or "natural")
        intensity = str(emotion_opts.get("emotion_intensity") or DEFAULT_EMOTION_INTENSITY)
        self._indextts2_style_meta = self._current_indextts2_style_meta()
        self._indextts2_last_length_meta = {
            "level": "segmented" if segment_plan.is_segmented else length_assessment.level,
            "char_count": segment_plan.char_count,
            "label": "分段生成" if segment_plan.is_segmented else length_assessment.label,
            "hint": segment_plan.hint if segment_plan.is_segmented else length_assessment.hint,
            "recommended_limit": length_assessment.recommended_limit,
            "risky_limit": length_assessment.risky_limit,
            "segment_generation": segment_plan.is_segmented,
            "segment_count": len(segment_plan.segments),
            "segment_lengths": [len(seg) for seg in segment_plan.segments],
        }
        out_dir = library.audio_storage_dir(preview=preview)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = ("Lulu选段试听_" if preview else "Lulu配音_") + datetime.now().strftime("%Y%m%d_%H%M%S")
        out = unique_output_path(out_dir / f"{stem}.wav")
        self.indextts2_player.pause()
        self.indextts2_progress.setVisible(True)
        start_hint = (
            f"分段生成：将分为 {len(segment_plan.segments)} 段逐段生成并合并。"
            if segment_plan.is_segmented
            else f"{length_assessment.label}: {length_assessment.hint}"
        )
        bg_status = self._indextts2_background_status_snapshot()
        bg_alive = bool(bg_status.get("alive"))
        running_message = (
            f"{glossary_prefix}{start_hint} 复用已常驻的 {self._index_name} 后台生成中..."
            if bg_alive
            else (
                f"{glossary_prefix}{start_hint} 首次加载模型或首次使用这个音色会慢一些，后续同会话通常会快一些。"
                f"正在启动 {self._index_name} 本地运行环境..."
            )
        )
        self._set_voice_progress_running(
            self.indextts2_progress,
            self.indextts2_status,
            running_message,
            -1,
            prefix=f"{self._index_name}:",
        )
        self.btn_indextts2_cancel.setEnabled(True)
        self.btn_indextts2_cancel.setVisible(True)
        self._indextts2_worker = IndexTTS2SynthesisWorker(
            text_for_generation,
            voice_id,
            str(out),
            preset,
            self._indextts2_style_meta,
            intensity,
            emotion_opts.get("emotion_vector"),
            emotion_opts.get("emotion_weight"),
            str(emotion_opts.get("emotion_summary") or ""),
            str(emotion_opts.get("emotion_source_preset") or ""),
            str(emotion_opts.get("emotion_source_intensity") or ""),
            self._indextts2_last_length_meta,
            text_segments,
            emotion_mode=str(emotion_opts.get("emotion_mode") or ""),
            emotion_audio_path=str(emotion_opts.get("emotion_audio_path") or ""),
            emotion_text=str(emotion_opts.get("emotion_text") or ""),
            language=str(self.indextts2_language_combo.currentData() or "ZH"),
            speed=self.indextts2_speed_slider.value() / 100,
            preview=preview,
            model_version=self._index_version,
            random_emotion=bool(hasattr(self, "_approved_index") and self._approved_index.random_emotion and self._indextts2_emotion_mode in ("vector", "emotext")),
            generation_options=generation_options,
        )
        self._indextts2_worker.progress.connect(self._on_indextts2_progress)
        self._indextts2_worker.lifecycle.connect(self._on_indextts2_lifecycle)
        self._indextts2_worker.finished_ok.connect(self._on_indextts2_done)
        self._indextts2_worker.failed.connect(self._on_indextts2_failed)
        self._indextts2_worker.cancelled.connect(self._on_indextts2_cancelled)
        self._indextts2_worker.finished.connect(self._on_indextts2_thread_finished)
        if bg_alive:
            self._set_indextts2_background_state("正在本地生成 · 可取消", busy=True, loaded=True)
        else:
            self._set_indextts2_background_state("正在加载本地模型", busy=True, loaded=False)
        self._update_indextts2_generate_state()
        self._indextts2_worker.start()

    def _on_indextts2_progress(self, msg: str, frac: float) -> None:
        self._set_voice_progress_running(
            self.indextts2_progress,
            self.indextts2_status,
            msg,
            frac,
            prefix=f"{self._index_name}:",
            finalizing_cap=98,
        )

    def _set_indextts2_background_state(self, text: str, *, busy: bool = False, loaded: bool = False) -> None:
        if not hasattr(self, "indextts2_bg_status"):
            return
        display_text = self._indextts2_background_display_text(text, loaded=loaded)
        self.indextts2_bg_status.setText(display_text)
        self.btn_indextts2_release_bg.setEnabled(bool(loaded) and not busy)

    def _indextts2_background_display_text(self, text: str, *, loaded: bool = False) -> str:
        if self._indextts2_environment_ready():
            return text
        if "释放失败" in text:
            return "后台释放失败 · 本地环境仍未准备"
        if "已释放" in text:
            return "后台已释放 · 本地环境仍未准备"
        if loaded:
            return f"后台在运行 · {self._indextts2_environment_waiting_label()}"
        return self._indextts2_environment_waiting_label()

    def _sync_indextts2_background_state_with_environment(self) -> None:
        if not hasattr(self, "indextts2_bg_status") or self._indextts2_environment_ready():
            return
        current = self.indextts2_bg_status.text()
        if any(token in current for token in ("已就绪", "可本地离线生成", "连续生成更快", "正在本地生成")):
            self.indextts2_bg_status.setText(self._indextts2_environment_waiting_label())

    def _indextts2_background_status_snapshot(self) -> dict:
        try:
            return dict(persistent_worker_status())
        except Exception:
            return {}

    def _on_indextts2_lifecycle(self, status: object) -> None:
        info = dict(status or {}) if isinstance(status, dict) else {}
        if info.get("alive"):
            self._set_indextts2_background_state("后台已就绪 · 连续生成更快", loaded=True)
            if self._indextts2_environment_ready() and "后台已释放" in self.indextts2_status.text():
                self.indextts2_status.setText(f"{self._index_name} 后台已就绪，连续生成会更快。")
            elif not self._indextts2_environment_ready() and any(
                token in self.indextts2_status.text() for token in ("已就绪", "可本地离线生成")
            ):
                self.indextts2_status.setText("等待生成...")
        else:
            self._set_indextts2_background_state("后台未加载", loaded=False)

    def _release_indextts2_background(self) -> None:
        if self._indextts2_worker and self._indextts2_worker.isRunning():
            dialogs.alert(self, f"{self._index_name} 后台", "当前正在生成。请先取消当前任务，或等待生成完成后再释放后台。")
            return
        if self._indextts2_release_worker and self._indextts2_release_worker.isRunning():
            return
        self._set_indextts2_background_state("正在释放后台…", busy=True, loaded=True)
        self._indextts2_release_worker = IndexTTS2BackgroundReleaseWorker()
        self._indextts2_release_worker.finished_ok.connect(self._on_indextts2_background_released)
        self._indextts2_release_worker.failed.connect(self._on_indextts2_background_release_failed)
        self._indextts2_release_worker.start()

    def _on_indextts2_background_released(self, status: object) -> None:
        self._on_indextts2_lifecycle(status)
        self._set_indextts2_background_state("后台已释放 · 下次生成会重新加载", loaded=False)
        self.indextts2_status.setText(f"{self._index_name} 后台已释放。下次生成会重新加载。")

    def _on_indextts2_background_release_failed(self, msg: str) -> None:
        self._set_indextts2_background_state("后台释放失败", loaded=True)
        dialogs.alert(self, f"{self._index_name} 后台", msg)

    def _indextts2_mode_label(self, control_mode: str) -> str:
        return EMOTION_MODE_LABELS.get(control_mode, EMOTION_MODE_LABELS.get(self._indextts2_emotion_mode, "情感模板"))

    def _on_indextts2_done(self, result) -> None:
        self._indextts2_last_output = str(result.output_path)
        self._set_voice_progress_complete(self.indextts2_progress)
        # 以本次生成的 result 为准取音色/参数，避免慢速生成期间用户切音色/动滑杆导致素材库写错
        result_voice_id = str(getattr(result, "voice_id", "") or self.indextts2_voice_combo.currentData() or "")
        result_voice = voices.get_voice(result_voice_id) if result_voice_id else None
        voice_label = (str(getattr(result_voice, "name", "") or "").strip()
                       or self._selected_indextts2_voice_label())
        done_weight = result.meta.get("emotion_weight")
        done_weight = float(done_weight) if done_weight is not None else self._current_indextts2_advanced_weight()
        elapsed = result.meta.get("elapsed_seconds")
        extra = f" · 耗时 {elapsed}s" if elapsed is not None else ""
        glossary = " · 已应用术语读音" if self._indextts2_glossary_applied else ""
        control_mode = str(result.meta.get("emotion_control_mode") or self._indextts2_emotion_mode)
        mode_label = self._indextts2_mode_label(control_mode)
        emotion_key = str(result.meta.get("emotion_preset") or "natural")
        if control_mode == "follow":
            emotion_text = " · 情感方式：跟随音色"
        elif control_mode == "emoaudio":
            emotion_text = f" · 情感方式：{mode_label} · 整体情感权重：{done_weight:.2f}"
        elif control_mode == "emotext":
            desc = str(result.meta.get("emotion_text") or "").strip()
            shown = desc if len(desc) <= 16 else desc[:16] + "…"
            emotion_text = f" · 情感方式：情感描述文本「{shown}」 · 整体情感权重：{done_weight:.2f}"
        elif control_mode == "vector":
            emotion_text = f" · 情感方式：{mode_label} · 整体情感权重：{done_weight:.2f}"
        else:
            emotion_text = f" · 情感模板：{EMOTION_LABELS.get(emotion_key, emotion_key)} · 整体情感权重：{done_weight:.2f}"
        style_meta = dict(result.meta.get("style_preset") or self._indextts2_style_meta or {})
        text_length_meta = dict(result.meta.get("text_length") or self._indextts2_last_length_meta or {})
        length_text = f" · 文本：{text_length_meta.get('label')}" if text_length_meta.get("label") else ""
        segment_count = int(result.meta.get("segment_count") or text_length_meta.get("segment_count") or 0)
        segment_text = f" · 分段生成 · {segment_count} 段完成" if segment_count > 1 else ""
        style_name = style_meta.get("name") or self._indextts2_current_style.get("name") or "自然"
        style_dirty = " · 自定义 / 未保存" if style_meta.get("dirty") else ""
        text_snapshot = str(getattr(result, "text", "") or self.indextts2_text_edit.toPlainText().strip())
        source = self._source_payload_for_text(text_snapshot)
        preview = bool(result.meta.get("preview", False))
        saved, save_message = (False, "") if preview else self._save_voice_safely(
            result.output_path,
            title=f"{mode_label} · {voice_label}",
            text=text_snapshot,
            engine=f"{self._index_name}",
            voice_id=result_voice_id or None,
            voice_name=voice_label,
            duration_seconds=result.duration_seconds,
            config_summary=f"情感方式：{mode_label}{style_dirty}; {emotion_text.strip(' ·')}; 术语读音：{'已应用' if self._indextts2_glossary_applied else '未应用'}",
            source_type=str(source.get("source_type") or "manual"),
            source_id=source.get("source_id"),
            source_title=source.get("source_title"),
            source_text_snapshot=source.get("source_text_snapshot"),
            source_chain=source.get("source_chain") or [],
            meta=dict(result.meta or {}),
        )
        record = build_result_summary(
            output_path=result.output_path,
            voice_label=voice_label,
            style_meta=style_meta,
            result_meta=dict(result.meta or {}),
            duration_seconds=result.duration_seconds,
            glossary_applied=self._indextts2_glossary_applied,
            generated_at=datetime.now(),
        )
        self._add_indextts2_result(record)
        self.btn_indextts2_cancel.setVisible(False)
        self._load_indextts2_player(self._indextts2_last_output, result.duration_seconds)
        if preview:
            self.indextts2_status.setText(f"选段试听已生成 · {result.duration_seconds:.1f}s · 未加入素材库；满意后可生成全文。")
            if self.isVisible() and self.panel_stack.currentIndex() == 0:
                self.indextts2_player.play()
        elif saved:
            self.indextts2_status.setText(
                f"全文已保存到音频库 · {result.duration_seconds:.1f} 秒"
                if hasattr(self, "_approved_index") else
                f"已保存到素材库 → 配音 · {result.duration_seconds:.1f}s{extra}{emotion_text}{glossary}{length_text}{segment_text} · {Path(result.output_path).name}"
            )
        else:
            self.indextts2_status.setText(save_message)
            dialogs.alert(self, "素材库保存失败", save_message)
        if saved and getattr(self.main, "library_page", None) is not None:
            self.main.library_page.refresh()
        self._update_indextts2_generate_state()
        if self.isVisible() and self.panel_stack.currentIndex() == 0:
            QTimer.singleShot(0, lambda: self.page_scroll.ensureWidgetVisible(self.indextts2_player_bar))
        notify = getattr(self.main, "_notify_task_completed", None)
        if notify is not None:
            notify(page_index=4)

    def _on_indextts2_thread_finished(self) -> None:
        self._update_indextts2_generate_state()
        if (not self.isVisible() or self.panel_stack.currentIndex() != 0) and self._indextts2_background_status_snapshot().get('alive'):
            self._release_indextts2_background()

    def _on_indextts2_failed(self, msg: str) -> None:
        self._set_voice_progress_stopped(self.indextts2_progress)
        self.indextts2_status.setText(f"{self._index_name} 生成失败：{msg}")
        self.btn_indextts2_cancel.setVisible(False)
        self.indextts2_result_summary.setText("本次生成失败，未新增成功结果。可继续试听或导出下方已有结果。")
        self.indextts2_result_summary.setVisible(True)
        self._update_indextts2_generate_state()
        dialogs.alert(self, f"{self._index_name} 生成失败", msg)
        notify_failed = getattr(self.main, "_notify_task_failed", None)
        if notify_failed is not None:
            notify_failed()

    def _on_indextts2_cancelled(self) -> None:
        self._set_voice_progress_stopped(self.indextts2_progress)
        self.indextts2_status.setText("已取消。文本、音色和情感已保留，可重新生成。")
        self.btn_indextts2_cancel.setVisible(False)
        self.indextts2_result_summary.setText("本次已取消，未新增成功结果。可继续试听或导出下方已有结果。")
        self.indextts2_result_summary.setVisible(True)
        self._update_indextts2_generate_state()

    def _cancel_indextts2(self) -> None:
        if self._indextts2_worker and self._indextts2_worker.isRunning():
            self._indextts2_worker.request_cancel()
            self.indextts2_status.setText(f"正在取消 {self._index_name} 生成…")
            self.btn_indextts2_cancel.setEnabled(False)

    # ------------------------------------------------------------------ IndexTTS 2.5 结果区 + 播放器

    def _set_indextts2_player_visible(self, visible: bool) -> None:
        self.indextts2_player_bar.setVisible(visible)
        if hasattr(self, "indextts2_result_empty"):
            self.indextts2_result_empty.setVisible(not visible)

    def _reset_indextts2_player(self) -> None:
        self.indextts2_player.stop()
        self.indextts2_player.setSource(QUrl())
        self.indextts2_player_slider.setRange(0, 0)
        self.indextts2_player_slider.setValue(0)
        self.indextts2_time_label.setText("0:00 / 0:00")
        # stop() emits no state signal when already stopped. Reset the visual
        # explicitly through the same icon/text policy as normal playback.
        self._on_indextts2_player_state(QMediaPlayer.StoppedState)
        self.btn_indextts2_cancel.setEnabled(True)
        self._set_indextts2_player_visible(False)
        if not self._indextts2_results:
            self.indextts2_result_summary.setVisible(False)
        self._refit_panel()

    def _load_indextts2_player(self, output_path: str, duration_seconds: float | None = None) -> None:
        if hasattr(self, '_approved_index'):
            self._approved_index.sync_adjustment_entry()
        if hasattr(self.indextts2_player_slider, "load_file"):
            self.indextts2_player_slider.load_file(output_path)
        self.indextts2_player_bar.setToolTip(str(Path(output_path).expanduser().resolve()))
        self.indextts2_player_bar.setAccessibleDescription(self.indextts2_player_bar.toolTip())
        self.indextts2_player.stop()
        self.indextts2_player.setSource(QUrl.fromLocalFile(str(Path(output_path).expanduser().resolve())))
        total_ms = int((duration_seconds or 0) * 1000)
        if total_ms > 0:
            self.indextts2_player_slider.setRange(0, total_ms)
            self.indextts2_time_label.setText(f"0:00 / {_time_label(total_ms)}")
        self._set_indextts2_player_visible(True)
        self._refit_panel()

    def _add_indextts2_result(self, record: dict) -> None:
        self._indextts2_results.insert(0, dict(record))
        self._indextts2_results = self._indextts2_results[:5]
        self._render_indextts2_results()

    def _select_indextts2_result(self, record: dict) -> None:
        output_path = str(record.get("output_path") or "")
        if not output_path:
            return
        self._indextts2_last_output = output_path
        self.indextts2_result_summary.setText(str(record.get("summary") or ""))
        self.indextts2_result_summary.setVisible(True)
        self._load_indextts2_player(output_path)

    def _clear_indextts2_recent_layout(self) -> None:
        while self.indextts2_recent_layout.count():
            item = self.indextts2_recent_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _render_indextts2_results(self) -> None:
        if hasattr(self, "_approved_index"):
            self._approved_index.history_button.setText(f"历史记录 ({len(self._indextts2_results)})")
            if self._indextts2_results:
                summary = str(self._indextts2_results[0].get("summary") or "")
                self.indextts2_result_summary.setText(summary)
                self.indextts2_result_summary.setToolTip(summary)
                self.indextts2_result_summary.show()
            return
        self._clear_indextts2_recent_layout()
        self.indextts2_recent_title.setVisible(bool(self._indextts2_results))
        if not self._indextts2_results:
            return
        current = self._indextts2_results[0]
        self.indextts2_result_summary.setText(str(current.get("summary") or ""))
        self.indextts2_result_summary.setVisible(True)
        for record in self._indextts2_results:
            row = QFrame()
            adopt_surface(row, "well")
            output_tooltip = str(record.get("output_path") or "")
            row.setToolTip(output_tooltip)
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 8, 10, 8)
            h.setSpacing(8)
            title = QLabel(str(record.get("title") or Path(str(record.get("output_path") or "")).name))
            title.setObjectName("pageMeta")
            title.setWordWrap(True)
            h.addWidget(title, 1)
            play = QPushButton("  听")
            play.setObjectName("act")
            play.setIcon(QIcon(icons.pixmap("play", T.INK_SOFT, 13)))
            play.clicked.connect(lambda _checked=False, r=dict(record): self._select_indextts2_result(r))
            adopt_button(play, role="secondary", size="sm", accessible_name="试听此结果")
            h.addWidget(play)
            reveal = QPushButton("  Finder")
            reveal.setObjectName("act")
            reveal.setIcon(QIcon(icons.pixmap("folder", T.INK_SOFT, 13)))
            reveal.clicked.connect(lambda _checked=False, r=dict(record): self._reveal_indextts2_output(r))
            adopt_button(reveal, role="tertiary", size="sm", accessible_name="在Finder中显示此结果")
            h.addWidget(reveal)
            export = QPushButton("  导出")
            export.setObjectName("act")
            export.setIcon(QIcon(icons.pixmap("export", T.INK_SOFT, 13)))
            export.clicked.connect(lambda _checked=False, r=dict(record): self._export_indextts2_wav(r))
            adopt_button(export, role="tertiary", size="sm", accessible_name="导出此结果")
            h.addWidget(export)
            self.indextts2_recent_layout.addWidget(row)

    def _toggle_indextts2_play(self) -> None:
        if not self._indextts2_last_output:
            return
        if self.indextts2_player.playbackState() == QMediaPlayer.PlayingState:
            self.indextts2_player.pause()
        else:
            if self.indextts2_player.source().isEmpty():
                self.indextts2_player.setSource(QUrl.fromLocalFile(str(Path(self._indextts2_last_output).expanduser().resolve())))
            if self.indextts2_player.position() >= self.indextts2_player.duration() > 0:
                self.indextts2_player.setPosition(0)
            self.indextts2_player.play()

    def _on_indextts2_player_duration(self, duration: int) -> None:
        self.indextts2_player_slider.setRange(0, max(0, duration))
        self.indextts2_time_label.setText(f"{_time_label(self.indextts2_player.position())} / {_time_label(duration)}")

    def _on_indextts2_player_position(self, position: int) -> None:
        if not self._indextts2_player_dragging:
            self.indextts2_player_slider.setValue(position)
        self.indextts2_time_label.setText(f"{_time_label(position)} / {_time_label(self.indextts2_player.duration())}")

    def _on_indextts2_player_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        refined = hasattr(self, "_approved_index")
        self.btn_indextts2_play.setText("" if refined else "  暂停" if playing else "  播放")
        self.btn_indextts2_play.setAccessibleName("暂停本次结果" if playing else "播放本次结果")
        ico = "pause" if playing else "play"
        self.btn_indextts2_play.setIcon(QIcon(icons.pixmap(ico, "#ffffff" if refined else self._consumer.accent, 18 if refined else 13)))

    def _on_indextts2_slider_pressed(self) -> None:
        self._indextts2_player_dragging = True

    def _on_indextts2_slider_released(self) -> None:
        self._indextts2_player_dragging = False
        self.indextts2_player.setPosition(self.indextts2_player_slider.value())

    def _current_indextts2_record(self) -> dict | None:
        if not self._indextts2_last_output:
            return None
        for record in self._indextts2_results:
            if str(record.get("output_path") or "") == self._indextts2_last_output:
                return dict(record)
        return {"output_path": self._indextts2_last_output, "default_filename": Path(self._indextts2_last_output).name}

    def _reveal_indextts2_output(self, record: dict | None = None) -> None:
        record = record or self._current_indextts2_record()
        if not record:
            return
        output = Path(str(record.get("output_path") or "")).expanduser()
        if not output.exists():
            dialogs.alert(self, f"{self._index_name}", "生成文件不存在，可能已被移动或删除。")
            return
        try:
            subprocess.run(["open", "-R", str(output)], check=False)
        except OSError as exc:
            dialogs.alert(self, f"{self._index_name}", _finder_reveal_failed_message(exc, output))

    def _export_indextts2_wav(self, record: dict | None = None) -> None:
        record = record or self._current_indextts2_record()
        if not record:
            return
        source = Path(str(record.get("output_path") or "")).expanduser()
        if not source.exists():
            dialogs.alert(self, f"{self._index_name}", "生成文件不存在，可能已被移动或删除。")
            return
        default_name = str(record.get("default_filename") or source.name)
        try:
            target = library.download_audio(source, self.cfg.tts_output_dir, default_name)
        except (OSError, ValueError) as exc:
            dialogs.alert(self, "下载失败", f"库内音频仍保留。请检查下载位置后重试。\n{exc}")
            return
        self.main._toast("音频已下载：" + str(target))

    # ------------------------------------------------------------------ VoxCPM2 生成处理

    def _pick_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择音频下载文件夹", self.cfg.tts_output_dir)
        if d:
            self.cfg.tts_output_dir = d
            self.cfg.save()
            self._update_output_dir_btn()

    def _pick_model_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择 VoxCPM2 模型根目录", str(local_models.model_root()))
        if d:
            self.cfg.tts_model_dir = d
            self.cfg.save()
            self.refresh()
            self.main._toast("模型目录已更新，下次加载 VoxCPM2 时生效")

    def _reset_model_dir(self) -> None:
        self.cfg.tts_model_dir = ""
        self.cfg.save()
        self.refresh()
        self.main._toast("已恢复默认模型目录")

    def _update_output_dir_btn(self) -> None:
        for name in ('out_dir_btn','btn_indextts2_output_dir'):
            button=getattr(self,name,None)
            if button is not None:button.set_path(self.cfg.tts_output_dir)
        if hasattr(self,'_qwen3'):self._qwen3.output_dir_button.set_path(self.cfg.tts_output_dir)

    def _update_indextts2_output_dir_btn(self) -> None:
        if hasattr(self,'btn_indextts2_output_dir'):self.btn_indextts2_output_dir.set_path(self.cfg.tts_output_dir)


    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        if hasattr(self, "_approved_index"):
            self._approved_index.stop_playback()
        if hasattr(self, "_approved_vox"):
            self._approved_vox.voice_player.stop()
            self.player.stop()
        super().hideEvent(event)


    def _generate(self) -> None:
        if hasattr(self, "_approved_vox"):
            self._approved_vox.generate()
            return
        if self._worker and self._worker.isRunning():
            return
        if not self._guard_local_synthesis_start("voxcpm2"):
            return
        voice_id = self.voice_combo.currentData()
        if not voice_id:
            dialogs.alert(self, "生成配音", "请先保存或选择一个音色。")
            return
        text = self.text_edit.toPlainText().strip()
        if not text:
            dialogs.alert(self, "生成配音", "请输入要生成配音的文本。")
            return
        fmt = self._voxcpm2_format()
        out_dir = library.audio_storage_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = "Lulu配音_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        out = unique_output_path(out_dir / f"{stem}.{fmt}")
        self._last_output = None
        self._reset_player()
        self.progress.setVisible(True)
        self._set_voice_progress_running(self.progress, self.status, "准备生成…", -1)
        self.btn_generate.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setVisible(True)
        self._worker = SynthesisWorker(text, voice_id, str(out), fmt, engine="local_voxcpm2")
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _on_progress(self, msg: str, frac: float) -> None:
        self._set_voice_progress_running(self.progress, self.status, msg, frac)

    def _on_done(self, result) -> None:
        self._last_output = str(result.output_path)
        self._set_voice_progress_complete(self.progress)
        voice_id = str(getattr(result, "voice_id", "") or self.voice_combo.currentData() or "")
        voice = voices.get_voice(voice_id) if voice_id else None
        voice_name = getattr(voice, "name", "") or self.voice_combo.currentText() or "未命名音色"
        text_snapshot = str(getattr(result, "text", "") or self.text_edit.toPlainText().strip())
        source = self._source_payload_for_text(text_snapshot)
        saved, save_message = self._save_voice_safely(
            result.output_path,
            title=f"VoxCPM2 · {voice_name}",
            text=text_snapshot,
            engine="VoxCPM2",
            voice_id=voice_id or None,
            voice_name=voice_name,
            duration_seconds=result.duration_seconds,
            config_summary=f"格式：{Path(result.output_path).suffix.lstrip('.') or self._voxcpm2_format()} · 10 steps · reference only",
            source_type=str(source.get("source_type") or "manual"),
            source_id=source.get("source_id"),
            source_title=source.get("source_title"),
            source_text_snapshot=source.get("source_text_snapshot"),
            source_chain=source.get("source_chain") or [],
            meta=dict(getattr(result, "meta", {}) or {}),
        )
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self._load_player(self._last_output, result.duration_seconds)
        if saved:
            self.status.setText(
                f"完成 · 已保存到素材库 → 配音 · {result.duration_seconds:.1f}s · {Path(result.output_path).name}"
            )
        else:
            self.status.setText(save_message)
            dialogs.alert(self, "素材库保存失败", save_message)
        if saved and getattr(self.main, "library_page", None) is not None:
            self.main.library_page.refresh()
        self.refresh()
        notify = getattr(self.main, "_notify_task_completed", None)
        if notify is not None:
            notify(page_index=4)

    def _on_failed(self, msg: str) -> None:
        self._set_voice_progress_stopped(self.progress)
        self.status.setText("生成失败")
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self._reset_player()
        dialogs.alert(self, "生成失败", msg)
        notify_failed = getattr(self.main, "_notify_task_failed", None)
        if notify_failed is not None:
            notify_failed()

    def _on_cancelled(self) -> None:
        self._set_voice_progress_stopped(self.progress)
        self.status.setText("已取消")
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self._reset_player()

    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.request_cancel()
            self.status.setText("正在取消生成…上一份结果保留")
            self.btn_cancel.setEnabled(False)

    def shutdown_workers(self) -> None:
        """Stop background QThreads before the page/window is destroyed."""
        self._model_status_closing = True
        for worker in list(self._model_status_reads.values()):
            worker.request_cancel()
            worker.wait()
        if hasattr(self, "_approved_index"):
            self._approved_index.shutdown()
        if hasattr(self, "_approved_vox"):
            self._approved_vox.shutdown()
        if hasattr(self, "_qwen3"):
            self._qwen3.shutdown()
        self._reset_player()
        self._reset_indextts2_player()
        self._stop_thread(self._worker, request_cancel=True)
        self._stop_thread(self._indextts2_worker, request_cancel=True)
        shutdown_persistent_worker()
        self._stop_thread(self._indextts2_release_worker)
        self._stop_thread(self._indextts2_status_worker)
        self._stop_thread(self._indextts2_config_worker)
        self._stop_thread(self._ref_worker)
        self._stop_thread(self._new_voice_worker)
        self._worker = None
        self._indextts2_worker = None
        self._indextts2_release_worker = None
        self._indextts2_status_worker = None
        self._indextts2_config_worker = None
        self._ref_worker = None
        self._new_voice_worker = None

    def _stop_thread(self, worker, *, request_cancel: bool = False, timeout_ms: int = 3000) -> None:
        if worker is None or not worker.isRunning():
            return
        if request_cancel and hasattr(worker, "request_cancel"):
            worker.request_cancel()
        if not worker.wait(timeout_ms):
            worker.terminate()
            worker.wait(1000)

    def _set_player_visible(self, visible: bool) -> None:
        self.voxcpm2_player_bar.setVisible(visible)
        if hasattr(self, "voxcpm2_result_hint"):
            self.voxcpm2_result_hint.setVisible(not visible)

    def _reset_player(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.player_slider.setRange(0, 0)
        self.player_slider.setValue(0)
        self.time_label.setText("0:00 / 0:00")
        self.btn_play.setText("  播放")
        self.btn_play.setIcon(QIcon(icons.pixmap("play", T.INK_SOFT, 13)))
        self._set_player_visible(False)

    def _load_player(self, output_path: str, duration_seconds: float | None = None) -> None:
        self.voxcpm2_player_bar.setToolTip(str(Path(output_path).expanduser().resolve()))
        self.voxcpm2_player_bar.setAccessibleDescription(self.voxcpm2_player_bar.toolTip())
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(Path(output_path).expanduser().resolve())))
        total_ms = int((duration_seconds or 0) * 1000)
        if total_ms > 0:
            self.player_slider.setRange(0, total_ms)
            self.time_label.setText(f"0:00 / {_time_label(total_ms)}")
        self._set_player_visible(True)

    def _toggle_play(self) -> None:
        if not self._last_output:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.player.source().isEmpty():
                self.player.setSource(QUrl.fromLocalFile(str(Path(self._last_output).expanduser().resolve())))
            if self.player.position() >= self.player.duration() > 0:
                self.player.setPosition(0)
            self.player.play()

    def _on_player_duration(self, duration: int) -> None:
        self.player_slider.setRange(0, max(0, duration))
        self.time_label.setText(f"{_time_label(self.player.position())} / {_time_label(duration)}")

    def _on_player_position(self, position: int) -> None:
        if not self._player_dragging:
            self.player_slider.setValue(position)
        self.time_label.setText(f"{_time_label(position)} / {_time_label(self.player.duration())}")

    def _on_player_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.btn_play.setText("  暂停" if playing else "  播放")
        ico = "pause" if playing else "play"
        self.btn_play.setIcon(QIcon(icons.pixmap(ico, T.INK_SOFT, 13)))

    def _on_slider_pressed(self) -> None:
        self._player_dragging = True

    def _on_slider_released(self) -> None:
        self._player_dragging = False
        self.player.setPosition(self.player_slider.value())
