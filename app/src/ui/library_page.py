"""「文稿库」页：回看 / 搜索 / 重新导出已保存的转写文稿（本地持久化）。"""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, QRectF, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtMultimedia import QMediaPlayer
from .audio_output import create_audio_output as QAudioOutput
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import library
from ..core.output.writer import write_transcript
from . import dialogs, icons, theme as T
from .control_foundation import (
    PaperTextureFrame,
    adopt_consumer_theme_scope,
    adopt_scroll_area,
    adopt_text_field,
    build_consumer_page_header,
)
from .widgets import ElideLabel, StatRow


_ENTRY_MIME = "application/x-lulu-entry"     # 拖拽卡片时携带的 entry_id
_FOLDER_MIME = "application/x-lulu-folder"   # 拖拽文件夹头时携带的 folder_id（重排顺序）


def _fmt_date(iso: str) -> str:
    # "2026-06-08T01:06:26" → "2026-06-08 01:06"
    return iso.replace("T", " ")[:16] if iso else ""


_KIND_LABELS = {
    "all": "素材库",
    "transcript": "文稿库",
    "rewrite": "输出库",
    "voice": "音频库",
}

_KIND_META = {
    "all": "你转写、处理、输出、配音的成果都收在这里，全部存在本机。",
    "transcript": "Lulu 生成、上传或手动输入的原文稿都在这里，可以查看、搜索、导出，或继续处理。",
    "rewrite": "改写稿等输出内容都在这里，随时查看、搜索、导出或去配音。",
    "voice": "配好的音频都在这里，可试听、导出，或在访达里找到。",
}

_KIND_ICONS = {
    "all": "layers",
    "transcript": "file",
    "rewrite": "pen",
    "voice": "waveform",
}


def _library_workspace_qss(consumer: T.ConsumerTheme) -> str:
    """Page-local theme contract for the four library modes.

    Batch 3 shares the same page class for all four navigation entries, so the
    selectors live here instead of widening the global Batch 2 theme surface.
    """
    canvas = T.ACTIVE.BG
    return f"""
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"],
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#coreCanvas {{
        background: {canvas}; border: none;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#libraryWorkbench {{
        background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
            stop:0 {consumer.surface_focal}, stop:1 {consumer.surface_work});
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_work}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#librarySection {{
        background: {consumer.surface_focal};
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_surface}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#libraryContentWorkspace {{
        background: {consumer.surface_work};
        border: 1px solid {consumer.line}; border-radius: {consumer.radius_surface}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#libraryEmptyPanel {{
        background: transparent; border: none;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#libraryEmptyGlyph {{
        background: {consumer.accent_wash}; border: none;
        border-radius: {consumer.radius_control}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QLabel#libraryStoragePath {{
        color: {consumer.ink_muted}; font-size: {consumer.type_caption}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QLabel#libraryEmptyText {{
        color: {consumer.ink_soft}; font-size: {consumer.type_body}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QWidget#libraryFolderSection {{
        background: transparent; border: none;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#folderHead {{
        background: {consumer.surface_focal};
        border: 1px solid {consumer.line};
        border-radius: {consumer.radius_control}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#folderHead:hover {{
        background: {consumer.secondary_hover};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#folderHead[selected="true"],
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#folderHead[dropActive="true"] {{
        background: {consumer.accent_wash};
        border: 1px solid {consumer.accent_hairline_active};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#folderBody {{
        background: transparent; border: none; border-radius: 0;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#folderGlyph {{
        background: transparent;
        border: 1px solid {consumer.accent_hairline_active};
        border-radius: {consumer.radius_control}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#folderChevron {{
        color: {consumer.ink_muted}; font-size: {consumer.type_body}px;
        font-weight: 600; background: transparent; border: none; padding: 0;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QLabel#folderName {{
        color: {consumer.ink}; font-size: {consumer.type_body}px;
        font-weight: 600; background: transparent;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QLabel#folderCount {{
        color: {consumer.ink_muted}; font-size: {consumer.type_caption}px;
        background: transparent; border: none; padding: 0 4px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QLabel#folderCount[dropHint="true"] {{
        color: {consumer.accent}; font-weight: 600;
        background: {consumer.accent_wash};
        border: 1px solid {consumer.accent_hairline};
        border-radius: {consumer.radius_control}px;
        padding: 3px 8px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#libraryBatchBar {{
        background: {consumer.surface_focal}; border: 1px solid {consumer.line};
        border-radius: {consumer.radius_surface}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QLabel#libraryBatchLabel {{
        color: {consumer.ink}; font-size: {consumer.type_body}px;
        font-weight: 600; background: transparent;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#taskCard {{
        background: {consumer.surface_focal}; border: 1px solid {consumer.line};
        border-radius: {consumer.radius_surface}px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#taskCard[selected="true"] {{
        background: {consumer.surface_focal}; border: 1px solid {consumer.accent_hairline_active};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#taskCard[dragging="true"] {{
        background: {consumer.accent_wash};
        border: 1px dashed {consumer.accent_hairline_active};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#selectionMarker {{
        background: {consumer.surface_focal}; border: 1px solid {consumer.line};
        border-radius: 9px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QFrame#selectionMarker[selected="true"] {{
        background: {consumer.accent}; border-color: {consumer.accent};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QLineEdit#inlineTitleEdit {{
        background: {consumer.surface_focal}; border: 1px solid {consumer.accent_hairline};
        border-radius: {consumer.radius_control}px; padding: 4px 8px;
        color: {consumer.ink}; selection-background-color: {consumer.accent_wash};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#act {{
        background: {consumer.secondary}; border: 1px solid transparent;
        border-radius: {consumer.radius_control}px; color: {consumer.ink_soft};
        font-size: {consumer.type_caption}px; padding: 5px 11px;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#act:hover {{
        background: {consumer.accent_wash}; color: {consumer.accent};
        border-color: {consumer.accent_hairline};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#act[libraryRole="primary"] {{
        background: {consumer.accent}; color: #FFFFFF; border-color: {consumer.accent};
        font-weight: 600;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#act[libraryRole="primary"]:hover {{
        background: {consumer.accent_hover}; color: #FFFFFF; border-color: {consumer.accent_hover};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#act[libraryRole="selectAll"] {{
        background: transparent; color: {consumer.accent};
        border-color: {consumer.accent_hairline};
        font-weight: 600;
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#act[libraryRole="folderSelectAll"][selected="true"] {{
        background: {consumer.accent_wash}; color: {consumer.accent};
        border-color: {consumer.accent_hairline_active};
    }}
    QWidget#root[consumerThemeScope="coreWorkspace"][workspacePage="library"] QPushButton#act:disabled {{
        background: transparent; color: {consumer.ink_muted}; border-color: transparent;
    }}
    """


class LibraryViewer(QDialog):
    """文稿查看器（库内条目专用）：全文 + 复制 + 导出，自带逻辑、不依赖任务队列。"""

    def __init__(self, title: str, transcript, parent=None, entry_id: str | None = None, cfg=None):
        super().__init__(parent)
        self.transcript = transcript
        self._title = title
        self._entry_id = entry_id
        self._cfg = cfg
        self.setWindowTitle(title)
        self.resize(620, 640)
        self.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)

        head = QLabel(library.display_title(title))
        head.setObjectName("pageTitle")
        head.setWordWrap(True)
        lay.addWidget(head)
        tr = transcript
        meta = QLabel(
            f"{len(tr.text)} 字 · 引擎 {tr.engine}"
            + (f" · 语种 {tr.language}" if tr.language else "")
            + (" · 带时间戳" if tr.has_timestamps else "")
        )
        meta.setObjectName("pageMeta")
        lay.addWidget(meta)
        srow = StatRow(color=T.INK_SOFT)
        if srow.set_meta(tr.meta or {}):
            lay.addWidget(srow)

        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText(tr.text)
        box.setStyleSheet(
            f"QPlainTextEdit{{background:{T.CARD};border:1px solid {T.LINE};border-radius:12px;"
            f"padding:14px;font-size:14px;color:{T.INK};line-height:1.7;}}"
        )
        lay.addWidget(box, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        summary = QPushButton("  生成摘要")
        summary.setObjectName("act")
        summary.setCursor(Qt.PointingHandCursor)
        summary.setIcon(QIcon(icons.pixmap("summary", T.INK_SOFT, 13)))
        summary.clicked.connect(self._summarize)
        feishu_btn = QPushButton("  存入飞书")
        feishu_btn.setObjectName("act")
        feishu_btn.setCursor(Qt.PointingHandCursor)
        feishu_btn.setIcon(QIcon(icons.pixmap("feishu", T.INK_SOFT, 13)))
        feishu_btn.clicked.connect(self._feishu)
        obs = QPushButton("  存入 Obsidian")
        obs.setObjectName("act")
        obs.setCursor(Qt.PointingHandCursor)
        obs.setIcon(QIcon(icons.pixmap("obsidian", T.INK_SOFT, 13)))
        obs.clicked.connect(self._obsidian)
        export = QPushButton("  导出")
        export.setObjectName("act")
        export.setCursor(Qt.PointingHandCursor)
        export.setIcon(QIcon(icons.pixmap("export", T.INK_SOFT, 13)))
        export.clicked.connect(self._export)
        copy = QPushButton("  复制全文")
        copy.setObjectName("grabBtn")
        copy.setCursor(Qt.PointingHandCursor)
        copy.setIcon(QIcon(icons.pixmap("copy", "#FFFFFF", 14)))
        copy.clicked.connect(self._copy)
        row.addWidget(summary)
        row.addWidget(feishu_btn)
        row.addWidget(obs)
        row.addWidget(export)
        row.addWidget(copy)
        lay.addLayout(row)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.transcript.text)

    def _summarize(self) -> None:
        from ..core import summarize
        if not summarize.is_configured():
            dialogs.alert(
                self, "智能摘要",
                "还没配置摘要服务。请到「AI 大模型配置」→「文稿助手」填入 API Key（默认 DeepSeek）。",
            )
            return
        from .summary_dialog import SummaryDialog
        SummaryDialog(self._title, self.transcript.text, self, entry_id=self._entry_id).exec()

    def _obsidian(self) -> None:
        cfg = self._cfg
        folder = cfg.obsidian_dir if (cfg and cfg.obsidian_dir and Path(cfg.obsidian_dir).is_dir()) else None
        if not folder:
            d = QFileDialog.getExistingDirectory(self, "选择 Obsidian 仓库里的目标文件夹", str(Path.home()))
            if not d:
                return
            folder = d
            if cfg:
                cfg.obsidian_dir = d
                cfg.save()
        summary = library.get_summary(self._entry_id) if self._entry_id else None
        try:
            from ..core import obsidian
            p = obsidian.save_note(self.transcript, folder, self._title, summary=summary)
            dialogs.alert(self, "存入 Obsidian", f"已存入：{p.name}")
        except Exception as exc:  # noqa: BLE001
            dialogs.alert(self, "存入 Obsidian 失败", str(exc))

    def _feishu(self) -> None:
        from .feishu_dialog import send_to_feishu
        summary = library.get_summary(self._entry_id) if self._entry_id else None
        self._feishu_worker = send_to_feishu(
            self,
            self._cfg,
            self.transcript,
            self._title,
            summary=summary,
        )

    def _export(self) -> None:
        safe = "".join(c for c in self._title[:40] if c not in '/\\:*?"<>|')
        directory = getattr(self._cfg, "last_output_dir", "") or str(Path.home() / "Downloads")
        default = str(Path(directory).expanduser() / f"{safe or '文稿'}.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出文稿", default,
            "文本 (*.txt);;Markdown (*.md);;字幕 (*.srt);;所有文件 (*)",
        )
        if not path:
            return
        try:
            written = write_transcript(self.transcript, Path(path))
            dialogs.alert(self, "导出", f"已导出：{Path(written).name}")
        except Exception as exc:  # noqa: BLE001
            dialogs.alert(self, "导出失败", str(exc))


class EntryCard(PaperTextureFrame):
    viewRequested = Signal(str)
    renameRequested = Signal(str)
    renameCommitted = Signal(str, str)
    summaryRequested = Signal(str)
    exportRequested = Signal(str)
    revealRequested = Signal(str)
    deleteRequested = Signal(str)
    moveRequested = Signal(str, str)   # (entry_id, kind) → 移动到文件夹
    clicked = Signal(str, object)      # (entry_id, keyboard_modifiers) → 多选

    def __init__(self, entry: dict):
        consumer = T.consumer_theme(T.ACTIVE.name)
        super().__init__(
            strength=consumer.paper_texture_opacity,
            corner_radius=consumer.radius_surface,
            layer="libraryCard",
        )
        self._consumer = consumer
        self.entry = entry
        self._drag_start = None    # 左键按下位置；拖过阈值才发起 QDrag
        self._double_clicking = False
        self._selected = False
        self._select_mode = False
        self.setObjectName("taskCard")
        self.setProperty("selected", "false")
        self.setProperty("dragging", "false")
        self.setAccessibleDescription("可拖动到文件夹")
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        lay = QVBoxLayout(self)
        self._card_layout = lay
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)

        self.title_band = QFrame()
        self.title_band.setObjectName("entryTitleBand")
        self.title_band.setFixedHeight(32)
        title_row = QHBoxLayout(self.title_band)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self.selection_marker = QFrame()
        self.selection_marker.setObjectName("selectionMarker")
        self.selection_marker.setProperty("selected", "false")
        self.selection_marker.setFixedSize(20, 20)
        self.selection_marker.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        selection_marker_layout = QHBoxLayout(self.selection_marker)
        selection_marker_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_check = icons.icon_label("check", "#FFFFFF", 11)
        self.selection_check.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.selection_check.hide()
        selection_marker_layout.addWidget(
            self.selection_check, 0, Qt.AlignCenter
        )
        self.selection_marker.hide()
        title_row.addWidget(self.selection_marker, 0, Qt.AlignVCenter)

        _disp = library.display_title(entry["title"])
        self._display_title = _disp
        self.title_label = ElideLabel(_disp)
        self.title_label.setObjectName("taskName")
        self.title_label.setToolTip(_disp)
        # 标题编辑已有清晰的铅笔入口；让标题也属于卡片双击查看热区。
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.title_label.setMinimumWidth(72)
        title_height = max(28, self.title_label.fontMetrics().height() + 8)
        self.title_label.setMinimumHeight(title_height)
        self.title_label.setMaximumHeight(title_height)
        self.title_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        title_row.addWidget(self.title_label, 1)

        self.title_edit = QLineEdit(_disp)
        self.title_edit.setObjectName("inlineTitleEdit")
        self.title_edit.setMinimumHeight(title_height)
        self.title_edit.setMaximumHeight(title_height)
        self.title_edit.hide()
        self.title_edit.returnPressed.connect(self._commit_title_edit)
        self.title_edit.editingFinished.connect(self._commit_title_edit)
        title_row.addWidget(self.title_edit, 1)

        self.rename_btn = QPushButton()
        self.rename_btn.setObjectName("act")
        self.rename_btn.setCursor(Qt.PointingHandCursor)
        self.rename_btn.setToolTip("修改标题")
        self.rename_btn.setFixedSize(34, 30)
        self.rename_btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.rename_btn.setIcon(QIcon(icons.pixmap("pen", T.INK_SOFT, 13)))
        self.rename_btn.clicked.connect(self._on_title_button)
        title_row.addWidget(self.rename_btn, 0, Qt.AlignRight)
        lay.addWidget(self.title_band)
        self._editing_title = False

        if entry.get("kind") == "voice":
            flags = entry.get("missing_flags") or {}
            missing = " · 音频文件不在原位置" if flags.get("audio_missing") else ""
            sub_text = (
                f"配音素材 · {_fmt_date(entry['created_at'])} · {entry.get('engine') or '未知模型'}"
                f" · {entry.get('voice_name') or '未命名音色'}{missing}"
            )
        elif entry.get("kind") == "rewrite":
            sub_text = f"输出稿 · {_fmt_date(entry['created_at'])} · {entry.get('engine') or '输出'}"
        else:
            sub_text = f"文稿 · {_fmt_date(entry['created_at'])} · {entry.get('engine') or '未知模型'}"
        self.meta_band = QFrame()
        self.meta_band.setObjectName("entryMetaBand")
        self.meta_band.setFixedHeight(20)
        meta_layout = QHBoxLayout(self.meta_band)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        self.meta_label = ElideLabel(sub_text)
        self.meta_label.setObjectName("taskSrc")
        self.meta_label.setWordWrap(False)
        self.meta_label.setToolTip(sub_text)
        self.meta_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        meta_layout.addWidget(self.meta_label, 1)
        lay.addWidget(self.meta_band)

        # 互动数据行（来自带数据的视频）：作者/粉丝 + 各指标图标，同任务卡口径
        self.stats_band = QFrame()
        self.stats_band.setObjectName("entryStatsBand")
        self.stats_band.setFixedHeight(22)
        stats_layout = QHBoxLayout(self.stats_band)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stat_row = StatRow(color=T.INK_SOFT)
        self.stat_row.set_meta({"stats": entry.get("stats"), "author": entry.get("author")})
        stats_layout.addWidget(self.stat_row, 1)
        lay.addWidget(self.stats_band)

        self.reason_band = None
        self.action_band = QFrame()
        self.action_band.setObjectName("entryActionBand")
        self.action_layout = QGridLayout(self.action_band)
        self.action_layout.setHorizontalSpacing(10)
        self.action_layout.setVerticalSpacing(8)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        is_voice_card = entry.get("kind") == "voice"
        if is_voice_card:
            actions = (
                ("详情", "list", self.viewRequested),
                ("导出", "export", self.exportRequested),
                ("Finder", "folder", self.revealRequested),
                ("删除", "x", self.deleteRequested),
            )
        else:
            actions = (
                ("查看", "list", self.viewRequested),
                ("导出", "export", self.exportRequested),
                ("Finder", "folder", self.revealRequested),
                ("删除", "x", self.deleteRequested),
            )
        self.action_buttons = [self.rename_btn]
        action_widgets = []
        for text, ico, sig in actions:
            b = QPushButton(text)
            b.setObjectName("act")
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumWidth(72)
            if is_voice_card:
                b.setMaximumWidth(86)
            b.setFixedHeight(32)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setIcon(QIcon(icons.pixmap(ico, T.INK_SOFT, 13)))
            b.clicked.connect(lambda _=False, s=sig: s.emit(self.entry["id"]))
            action_widgets.append(b)
            self.action_buttons.append(b)
        # 原链接优先读采集主档中的稳定作品地址，兼容旧来源字段。
        src = str(entry.get("source_url") or entry.get("source") or "")
        if entry.get("kind") != "voice":
            link_btn = QPushButton("原链接")
            link_btn.setObjectName("act")
            link_btn.setCursor(Qt.PointingHandCursor)
            has_source_link = src.startswith(("http://", "https://"))
            link_btn.setToolTip(src if has_source_link else "该素材没有可用的原作品链接")
            link_btn.setMinimumWidth(72)
            link_btn.setFixedHeight(32)
            link_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            link_btn.setIcon(QIcon(icons.pixmap("link", T.INK_SOFT, 13)))
            link_btn.setEnabled(has_source_link)
            if has_source_link:
                link_btn.clicked.connect(lambda _=False, u=src: self._open_source(u))
            action_widgets.append(link_btn)
            self.action_buttons.append(link_btn)
        move_btn = QPushButton("移动")
        move_btn.setObjectName("act")
        move_btn.setCursor(Qt.PointingHandCursor)
        move_btn.setToolTip("移动到文件夹")
        move_btn.setMinimumWidth(72)
        if is_voice_card:
            move_btn.setMaximumWidth(86)
        move_btn.setFixedHeight(32)
        move_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        move_btn.setIcon(QIcon(icons.pixmap("layers", T.INK_SOFT, 13)))
        move_btn.clicked.connect(
            lambda: self.moveRequested.emit(self.entry["id"], self.entry.get("kind", "transcript")))
        action_widgets.append(move_btn)
        self.action_buttons.append(move_btn)
        self._action_widgets = tuple(action_widgets)
        self._action_columns = 0
        self._layout_actions(0)
        lay.addWidget(self.action_band)
        self._sync_card_height()

    def _layout_actions(self, available_width: int) -> None:
        """宽卡保持一行，只有真正窄到会裁切时才自动变为两行。"""

        left, _top, right, _bottom = self._card_layout.getContentsMargins()
        action_width = max(0, available_width - left - right)
        self.action_band.resize(action_width, self.action_band.height())
        if self.entry.get("kind") == "voice":
            columns = (
                len(self._action_widgets)
                if available_width == 0 or available_width >= 440
                else 3
            )
        else:
            columns = 6 if available_width >= 620 else 3
        if columns == self._action_columns:
            self.action_layout.activate()
            return
        self._action_columns = columns
        for widget in self._action_widgets:
            self.action_layout.removeWidget(widget)
        for index, widget in enumerate(self._action_widgets):
            self.action_layout.addWidget(widget, index // columns, index % columns)
        for column in range(6):
            self.action_layout.setColumnStretch(column, 1 if column < columns else 0)
        rows = max(1, (len(self._action_widgets) + columns - 1) // columns)
        self.action_band.setFixedHeight(rows * 32 + (rows - 1) * 8)
        self.action_layout.invalidate()
        self.action_layout.activate()
        self._sync_card_height()

    def _sync_card_height(self) -> None:
        """卡片只能通过换行增高，绝不允许滚动区把内容向下压叠。"""

        if not hasattr(self, "action_band"):
            return
        bands = [self.title_band, self.meta_band, self.stats_band]
        if self.reason_band is not None:
            bands.append(self.reason_band)
        bands.append(self.action_band)
        left, top, right, bottom = self._card_layout.getContentsMargins()
        content_height = sum(band.height() for band in bands)
        total_height = top + bottom + content_height
        total_height += self._card_layout.spacing() * max(0, len(bands) - 1)
        self.setFixedHeight(total_height)
        self.updateGeometry()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "_action_widgets"):
            self._layout_actions(self.contentsRect().width())

    def _open_source(self, url: str) -> None:
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url))

    def _on_title_button(self) -> None:
        if self._editing_title:
            self._commit_title_edit()
        else:
            self._start_title_edit()

    def _start_title_edit(self) -> None:
        if self._editing_title:
            return
        self._editing_title = True
        title = self.title_label.text()
        self.title_edit.setText(title)
        self.title_label.hide()
        self.title_edit.show()
        self.title_edit.setFocus(Qt.MouseFocusReason)
        self.title_edit.selectAll()
        self.rename_btn.setToolTip("保存标题")
        self.rename_btn.setIcon(QIcon(icons.pixmap("check", self._consumer.accent, 13)))

    def _commit_title_edit(self) -> None:
        if not self._editing_title:
            return
        self._editing_title = False
        new_title = self.title_edit.text().strip()
        old_title = self.title_label.text()
        self.title_edit.hide()
        self.title_label.show()
        self.rename_btn.setToolTip("修改标题")
        self.rename_btn.setIcon(QIcon(icons.pixmap("pen", T.INK_SOFT, 13)))
        if not new_title or new_title == old_title:
            self.title_edit.setText(old_title)
            return
        self.renameCommitted.emit(self.entry["id"], new_title)

    # ---------------- 拖拽源：把卡片拖进文件夹 ---------------- #
    def _build_drag_preview(self) -> tuple[QPixmap, QPoint]:
        """用紧凑语义浮片代替整张卡片截图，减轻拖动时的视觉重量。"""
        preview_width = max(300, min(420, self.width() - 20))
        preview_height = 72
        shadow_margin = 10
        outer_width = preview_width + shadow_margin * 2
        outer_height = preview_height + shadow_margin * 2
        dpr = max(1.0, self.devicePixelRatioF())
        pixmap = QPixmap(
            round(outer_width * dpr),
            round(outer_height * dpr),
        )
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card_rect = QRectF(
            shadow_margin,
            4,
            preview_width,
            preview_height,
        )
        for offset, alpha in ((8, 10), (6, 13), (4, 17)):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(
                card_rect.translated(0, offset),
                12,
                12,
            )

        painter.setPen(QPen(QColor(self._consumer.accent), 1))
        painter.setBrush(QColor(self._consumer.surface_focal))
        painter.drawRoundedRect(card_rect, 12, 12)
        painter.setPen(
            QPen(
                QColor(self._consumer.accent),
                4,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(
            QPoint(shadow_margin + 10, 22),
            QPoint(shadow_margin + 10, 58),
        )

        kind = self.entry.get("kind", "transcript")
        icon_name = {
            "rewrite": "pen",
            "voice": "waveform",
        }.get(kind, "file")
        icon = icons.pixmap(icon_name, self._consumer.accent, 18)
        painter.drawPixmap(shadow_margin + 24, 21, icon)

        text_left = shadow_margin + 54
        text_width = preview_width - 74
        title_font = QFont(self.title_label.font())
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(self._consumer.ink))
        title = painter.fontMetrics().elidedText(
            self._display_title,
            Qt.TextElideMode.ElideRight,
            text_width,
        )
        painter.drawText(
            QRectF(text_left, 12, text_width, 28),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title,
        )

        detail_font = QFont(self.font())
        if detail_font.pointSizeF() > 0:
            detail_font.setPointSizeF(
                max(9.5, detail_font.pointSizeF() - 1.0)
            )
        painter.setFont(detail_font)
        painter.setPen(QColor(self._consumer.ink_muted))
        painter.drawText(
            QRectF(text_left, 40, text_width, 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{_KIND_LABELS.get(kind, '素材')} · 拖动到文件夹",
        )
        painter.end()
        return pixmap, QPoint(shadow_margin + 32, 36)

    def _set_dragging(self, on: bool) -> None:
        self.setProperty("dragging", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._editing_title:
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_start is None
            or self._editing_title
            or not (event.buttons() & Qt.LeftButton)
        ):
            super().mouseMoveEvent(event)
            return
        moved = (event.position().toPoint() - self._drag_start).manhattanLength()
        if moved < QApplication.startDragDistance():
            return
        self._drag_start = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_ENTRY_MIME, self.entry["id"].encode("utf-8"))
        drag.setMimeData(mime)
        pixmap, hotspot = self._build_drag_preview()
        drag.setPixmap(pixmap)
        drag.setHotSpot(hotspot)
        self._set_dragging(True)
        try:
            drag.exec(Qt.MoveAction)
        finally:
            self._set_dragging(False)

    def mouseReleaseEvent(self, event) -> None:
        # 按下后没拖动 = 一次点击 → 上抛给页面处理选择（由修饰键/选择态决定）
        if (
            event.button() == Qt.LeftButton
            and self._drag_start is not None
            and not self._editing_title
            and not self._double_clicking
        ):
            self.clicked.emit(self.entry["id"], event.modifiers())
        self._drag_start = None
        self._double_clicking = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._editing_title:
            self._double_clicking = True
            self._drag_start = None
            if not self._selected:
                self.clicked.emit(self.entry["id"], event.modifiers())
            self.viewRequested.emit(self.entry["id"])
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def set_selected(self, on: bool) -> None:
        changed = on != self._selected
        self._selected = on
        if changed:
            self.setProperty("selected", "true" if on else "false")
            self.style().unpolish(self)
            self.style().polish(self)
            self.selection_marker.setProperty(
                "selected", "true" if on else "false"
            )
            self.selection_marker.style().unpolish(self.selection_marker)
            self.selection_marker.style().polish(self.selection_marker)
        self.selection_check.setVisible(self._select_mode and on)

    def set_select_mode(self, on: bool) -> None:
        self._select_mode = on
        self.selection_marker.setVisible(on)
        self.selection_check.setVisible(on and self._selected)


class FolderSection(QWidget):
    """可折叠文件夹区：头（箭头+名+计数+重命名/删除）+ 成员卡片体。

    支持：点头展开/折叠、⌘/⇧ 点或选择态下点头多选、拖头重排顺序、拖卡片进来。
    """

    expandToggled = Signal(str, bool)
    renameRequested = Signal(str)
    deleteRequested = Signal(str)
    selectAllRequested = Signal(str)
    dropEntry = Signal(str, str)       # (entry_id, folder_id);folder_id="" 表示根目录
    dropFolder = Signal(str, str)      # （拖动的 folder_id, 落点 folder_id）→ 重排
    headClicked = Signal(str, object)  # (folder_id, keyboard_modifiers)→ 选择

    def __init__(self, folder: dict, expanded: bool, *, is_root: bool = False):
        super().__init__()
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.folder = folder
        self._fid = folder["id"]
        self._is_root = is_root
        self._expanded = expanded
        self._selected = False
        self._drop_active = False
        self._select_mode = False
        self._drag_start = None
        self._member_total = max(0, int(folder.get("count", 0) or 0))
        self._member_selected = 0
        self.setObjectName("libraryFolderSection")
        self.setProperty("expanded", "true" if expanded else "false")
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self.setAcceptDrops(True)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        self.head = PaperTextureFrame(
            strength=min(0.22, self._consumer.paper_texture_opacity * 1.25),
            corner_radius=self._consumer.radius_control,
            layer="folderHead",
            base_color=self._consumer.surface_focal,
        )
        head = self.head
        head.setObjectName("folderHead")
        head.setProperty("selected", "false")
        head.setProperty("dropActive", "false")
        head.setMinimumHeight(44)
        head.setCursor(Qt.PointingHandCursor)
        head.mousePressEvent = self._head_press
        head.mouseMoveEvent = self._head_move
        head.mouseReleaseEvent = self._head_release
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 8, 10, 8)
        hl.setSpacing(8)

        self.chevron_label = QPushButton()
        self.chevron_label.setObjectName("folderChevron")
        self.chevron_label.setFixedSize(24, 24)
        self.chevron_label.setFlat(True)
        self.chevron_label.setCursor(Qt.PointingHandCursor)
        self.chevron_label.clicked.connect(self._toggle)
        hl.addWidget(self.chevron_label)

        folder_glyph = PaperTextureFrame(
            strength=0.28,
            corner_radius=self._consumer.radius_control,
            layer="folderGlyph",
            base_color=self._consumer.accent,
        )
        folder_glyph.setObjectName("folderGlyph")
        folder_glyph.setFixedSize(24, 24)
        folder_glyph_lay = QHBoxLayout(folder_glyph)
        folder_glyph_lay.setContentsMargins(0, 0, 0, 0)
        self.folder_icon = icons.icon_label(
            "layers" if is_root else "folder", "#FFFFFF", 16
        )
        self.folder_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        folder_glyph_lay.addWidget(self.folder_icon, 0, Qt.AlignCenter)
        hl.addWidget(folder_glyph)

        self.folder_name_label = QLabel()
        self.folder_name_label.setObjectName("folderName")
        self.folder_name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.name_label = self.folder_name_label
        hl.addWidget(self.folder_name_label, 1)

        self.count_label = QLabel()
        self.count_label.setObjectName("folderCount")
        self.count_label.setProperty("dropHint", "false")
        self.count_label.setAlignment(Qt.AlignCenter)
        self.count_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        hl.addWidget(self.count_label)

        if not is_root:
            self.select_all_btn = QPushButton()
            self.select_all_btn.setObjectName("act")
            self.select_all_btn.setProperty("libraryRole", "folderSelectAll")
            self.select_all_btn.setCursor(Qt.PointingHandCursor)
            self.select_all_btn.setFixedSize(34, 28)
            self.select_all_btn.clicked.connect(
                lambda: self.selectAllRequested.emit(self._fid)
            )
            hl.addWidget(self.select_all_btn)
            self.rename_btn = QPushButton()
            self.rename_btn.setObjectName("act")
            self.rename_btn.setCursor(Qt.PointingHandCursor)
            self.rename_btn.setToolTip("重命名文件夹")
            self.rename_btn.setFixedSize(34, 28)
            self.rename_btn.setIcon(QIcon(icons.pixmap("pen", T.INK_SOFT, 13)))
            self.rename_btn.clicked.connect(lambda: self.renameRequested.emit(self._fid))
            hl.addWidget(self.rename_btn)
            self.delete_btn = QPushButton()
            self.delete_btn.setObjectName("act")
            self.delete_btn.setCursor(Qt.PointingHandCursor)
            self.delete_btn.setToolTip("删除文件夹（里面素材移回根目录）")
            self.delete_btn.setFixedSize(34, 28)
            self.delete_btn.setIcon(QIcon(icons.pixmap("x", T.INK_SOFT, 13)))
            self.delete_btn.clicked.connect(lambda: self.deleteRequested.emit(self._fid))
            hl.addWidget(self.delete_btn)
        v.addWidget(head)

        self.body = QFrame()
        self.body.setObjectName("folderBody")
        self.body.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(30, 4, 4, 4)
        self.body_lay.setSpacing(10)
        self.body_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        v.addWidget(self.body)
        self.body.setVisible(expanded)
        self._sync_text()
        if not is_root:
            self.set_member_selection(self._member_total, 0)

    def _sync_text(self) -> None:
        self.chevron_label.setText("▾" if self._expanded else "▸")
        action = "收起文件夹" if self._expanded else "展开文件夹"
        self.chevron_label.setAccessibleName(action)
        self.chevron_label.setToolTip(action)
        self.folder_name_label.setText(str(self.folder.get("name") or "未命名文件夹"))
        self.count_label.setText(f"{self.folder.get('count', 0)} 条")
        self.setProperty("expanded", "true" if self._expanded else "false")
        state = "已展开" if self._expanded else "已收起"
        self.head.setAccessibleName(f"{self.folder_name_label.text()}，{state}")
        self.head.setToolTip("单击收起" if self._expanded else "单击展开")

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self._sync_text()
        self._refresh_expanded_geometry()
        QTimer.singleShot(0, self._refresh_expanded_geometry)
        self.expandToggled.emit(self._fid, self._expanded)

    def _refresh_expanded_geometry(self) -> None:
        self.body.updateGeometry()
        if self.layout() is not None:
            self.layout().activate()
        self.adjustSize()
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None:
            if parent.layout() is not None:
                parent.layout().activate()
            parent.adjustSize()
            parent.updateGeometry()

    def add_card(self, card: QWidget) -> None:
        self.body_lay.addWidget(card)

    def set_select_mode(self, on: bool) -> None:
        self._select_mode = on

    def set_selected(self, on: bool) -> None:
        self._selected = on
        self._apply_head_style()

    def set_member_selection(self, total: int, selected: int) -> None:
        if self._is_root or not hasattr(self, "select_all_btn"):
            return
        self._member_total = max(0, int(total))
        self._member_selected = max(0, min(int(selected), self._member_total))
        all_selected = bool(
            self._member_total and self._member_selected == self._member_total
        )
        if all_selected:
            self.select_all_btn.setIcon(
                QIcon(icons.pixmap("check", self._consumer.accent, 13))
            )
            tooltip = f"取消选择文件夹内 {self._member_total} 条素材"
        else:
            self.select_all_btn.setIcon(
                QIcon(icons.pixmap("layers", T.INK_SOFT, 13))
            )
            tooltip = (
                f"全选文件夹内 {self._member_total} 条素材"
                if self._member_total
                else "文件夹内暂无素材"
            )
        self.select_all_btn.setProperty(
            "selected", "true" if all_selected else "false"
        )
        self.select_all_btn.setToolTip(tooltip)
        self.select_all_btn.setAccessibleName(tooltip)
        self.select_all_btn.setEnabled(self._member_total > 0)
        self.select_all_btn.style().unpolish(self.select_all_btn)
        self.select_all_btn.style().polish(self.select_all_btn)

    # ---------------- 头部：点击（展开/选择）+ 拖动（重排） ---------------- #
    def _head_press(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start = event.position().toPoint()

    def _head_move(self, event) -> None:
        if (
            self._is_root
            or self._drag_start is None
            or not (event.buttons() & Qt.LeftButton)
            or self._select_mode
            or event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier)
        ):
            return
        if (event.position().toPoint() - self._drag_start).manhattanLength() < QApplication.startDragDistance():
            return
        self._drag_start = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_FOLDER_MIME, self._fid.encode("utf-8"))
        drag.setMimeData(mime)
        drag.setPixmap(self.head.grab())
        drag.setHotSpot(event.position().toPoint())
        drag.exec(Qt.MoveAction)

    def _head_release(self, event) -> None:
        if event.button() != Qt.LeftButton or self._drag_start is None:
            self._drag_start = None
            return
        self._drag_start = None
        mods = event.modifiers()
        selecting = self._select_mode or bool(mods & (Qt.ShiftModifier | Qt.ControlModifier))
        if selecting and not self._is_root:
            self.headClicked.emit(self._fid, mods)
        else:
            self._toggle()

    # ---------------- 拖拽目标：接收卡片 / 接收文件夹（重排） ---------------- #
    def _apply_head_style(self) -> None:
        self.head.setProperty("selected", "true" if self._selected else "false")
        self.head.setProperty(
            "dropActive", "true" if self._drop_active else "false"
        )
        self.head.style().unpolish(self.head)
        self.head.style().polish(self.head)
        self.count_label.style().unpolish(self.count_label)
        self.count_label.style().polish(self.count_label)

    def _set_drop_active(self, on: bool, drop_kind: str = "") -> None:
        self._drop_active = on
        self.count_label.setProperty("dropHint", "true" if on else "false")
        if on:
            if drop_kind == "folder":
                hint = "松开排序"
            elif self._is_root:
                hint = "松开移至根目录"
            else:
                hint = "松开移入"
            self.count_label.setText(hint)
            self.head.setAccessibleName(
                f"{self.folder_name_label.text()}，{hint}"
            )
            self.head.setToolTip(hint)
        else:
            self.count_label.setText(f"{self.folder.get('count', 0)} 条")
            state = "已展开" if self._expanded else "已收起"
            self.head.setAccessibleName(
                f"{self.folder_name_label.text()}，{state}"
            )
            self.head.setToolTip(
                "单击收起" if self._expanded else "单击展开"
            )
        self._apply_head_style()

    def _accepts(self, mime) -> bool:
        if mime.hasFormat(_ENTRY_MIME):
            return True
        # 文件夹重排：根目录区不作为重排落点；不接受拖到自己头上
        if mime.hasFormat(_FOLDER_MIME) and not self._is_root:
            return bytes(mime.data(_FOLDER_MIME)).decode("utf-8") != self._fid
        return False

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if self._accepts(mime):
            event.acceptProposedAction()
            self._set_drop_active(
                True,
                "entry" if mime.hasFormat(_ENTRY_MIME) else "folder",
            )
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_active(False)

    def dropEvent(self, event) -> None:
        self._set_drop_active(False)
        mime = event.mimeData()
        if mime.hasFormat(_ENTRY_MIME):
            entry_id = bytes(mime.data(_ENTRY_MIME)).decode("utf-8")
            if entry_id:
                self.dropEntry.emit(entry_id, self._fid)
            event.acceptProposedAction()
        elif mime.hasFormat(_FOLDER_MIME) and not self._is_root:
            src = bytes(mime.data(_FOLDER_MIME)).decode("utf-8")
            if src and src != self._fid:
                self.dropFolder.emit(src, self._fid)
            event.acceptProposedAction()
        else:
            event.ignore()


class _LibraryActions(QWidget):
    """侧边栏「文稿库」页。每次切到这页都从磁盘刷新。"""

    def __init__(self, cfg=None, on_view=None, on_renamed=None):
        super().__init__()
        self.cfg = cfg
        self._on_view = on_view   # 有则把「查看」交给素材库工作区，否则回退到独立查看窗口
        self._on_renamed = on_renamed
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.setObjectName("root")
        self.setProperty("workspacePage", "library")
        adopt_consumer_theme_scope(self, "coreWorkspace")
        self.setStyleSheet(_library_workspace_qss(self._consumer))
        self._query = ""
        self._search_entries = None
        self._search_index_stamp = None
        self._search_bodies = {}
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(160)
        self._search_timer.timeout.connect(lambda: self._on_search(self.search_field.text()))
        self._kind = "all"        # all / transcript / rewrite / voice
        self._expanded: dict[str, bool] = {}   # 文件夹展开态（跨 refresh 保留）
        # ---- 多选状态 ----
        self._selecting = False
        self._sel_entries: set[str] = set()
        self._sel_folders: set[str] = set()
        self._active_entry_id: str | None = None  # 兼容旧刷新状态；新交互统一写入 _sel_entries
        self._anchor: str | None = None        # ⇧ 连续选的锚点（entry_id）
        self._ordered_ids: list[str] = []       # 当前展示顺序（⇧ 区间选用）
        self._cards: dict[str, EntryCard] = {}
        self._sections: dict[str, FolderSection] = {}
        self._entries_by_id: dict[str, dict] = {}
        self._feishu_queue: list[tuple[str, str]] = []
        self._build()


    # ---------------- 多选工具条 ---------------- #


    def _open_storage(self) -> None:
        d = library.LIBRARY_DIR
        d.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["open", str(d)], check=False)
        except Exception:  # noqa: BLE001
            pass

    def _on_search(self, text: str) -> None:
        self._query = text.strip().lower()
        self.refresh(search_only=True)


    # ---------------- 文件夹操作 ---------------- #
    def _new_folder(self) -> None:
        if self._kind not in ("transcript", "rewrite", "voice"):
            return
        name = dialogs.prompt(self, "新建文件夹", "给文件夹起个名字",
                              placeholder="例如：口播脚本", ok="创建")
        if name:
            try:
                library.create_folder(self._kind, name)
            except (OSError, ValueError, RuntimeError) as exc:
                dialogs.alert(self, "文件夹未创建", str(exc))
            self.refresh()

    def _rename_folder(self, folder_id: str) -> None:
        try:
            cur = next((f["name"] for f in library.list_folders(self._kind) if f["id"] == folder_id), "")
        except (OSError, ValueError, RuntimeError) as exc:
            dialogs.alert(self, "文件夹无法读取", str(exc))
            return
        name = dialogs.prompt(self, "重命名文件夹", "新名称", text=cur, ok="保存")
        if name:
            try:
                library.rename_folder(folder_id, name)
            except (OSError, ValueError, RuntimeError) as exc:
                dialogs.alert(self, "文件夹未重命名", str(exc))
            self.refresh()

    def _delete_folder(self, folder_id: str) -> None:
        if dialogs.confirm(self, "删除文件夹",
                           "删除后，里面的素材会移回根目录（不会被删除）。确定删除这个文件夹吗？"):
            try:
                library.delete_folder(folder_id)
            except (OSError, ValueError, RuntimeError) as exc:
                dialogs.alert(self, "文件夹删除未完成", str(exc))
            self.refresh()

    def _drop_move(self, entry_id: str, folder_id: str) -> None:
        # 拖拽落点：folder_id="" 表示根目录
        try:
            library.set_entry_folder(entry_id, folder_id or None)
        except (OSError, ValueError, RuntimeError) as exc:
            dialogs.alert(self, "移动未完成", str(exc))
        self.refresh()

    def _drop_folder(self, src_id: str, target_id: str) -> None:
        # 把 src 文件夹挪到 target 文件夹之前，持久化顺序
        ids = [f["id"] for f in library.list_folders(self._kind)]
        if src_id not in ids or target_id not in ids:
            return
        ids.remove(src_id)
        ids.insert(ids.index(target_id), src_id)
        library.set_folder_order(ids)
        self.refresh()

    # ---------------- 批量操作 ---------------- #
    def _selected_entries_ordered(self) -> list[str]:
        return [e for e in self._ordered_ids if e in self._sel_entries]

    def _entry_title(self, entry_id: str) -> str:
        return (self._entries_by_id.get(entry_id) or {}).get("title") or "素材"


    def _text_entry_ids(self) -> list[str]:
        return [
            e for e in self._selected_entries_ordered()
            if (self._entries_by_id.get(e) or {}).get("kind", "transcript") != "voice"
        ]

    def _batch_obsidian(self) -> None:
        ids = self._text_entry_ids()
        if not ids:
            return
        cfg = self.cfg
        folder = cfg.obsidian_dir if (cfg and cfg.obsidian_dir and Path(cfg.obsidian_dir).is_dir()) else None
        if not folder:
            d = QFileDialog.getExistingDirectory(self, "选择 Obsidian 仓库里的目标文件夹", str(Path.home()))
            if not d:
                return
            folder = d
            if cfg:
                cfg.obsidian_dir = d
                cfg.save()
        from ..core import obsidian
        done, skipped = 0, []
        succeeded: set[str] = set()
        for eid in ids:
            tr = library.load(eid)
            if tr is None:
                skipped.append(self._entry_title(eid))
                continue
            try:
                obsidian.save_note(tr, folder, self._entry_title(eid),
                                   summary=library.get_summary(eid))
                done += 1
                succeeded.add(eid)
            except Exception:  # noqa: BLE001
                skipped.append(self._entry_title(eid))
        msg = f"已存入 Obsidian {done} 篇。"
        if skipped:
            msg += f"\n跳过 {len(skipped)} 篇（读取或写入失败）。"
        dialogs.alert(self, "批量存入 Obsidian", msg)
        self._sel_entries.difference_update(succeeded)
        for folder_id in tuple(self._sel_folders):
            if not self._folder_member_ids(folder_id).issubset(self._sel_entries):
                self._sel_folders.discard(folder_id)
        self._anchor = next(iter(self._sel_entries), None)
        self._set_selecting(True)

    def _batch_feishu(self) -> None:
        ids = self._text_entry_ids()
        if not ids:
            return
        from datetime import datetime, timezone
        from ..core.feishu_storage import record_from_transcript
        from .feishu_dialog import send_records_to_feishu

        archived_at = int(datetime.now(timezone.utc).timestamp() * 1000)
        records = []
        for eid in ids:
            tr = library.load(eid)
            if tr is None:
                continue
            records.append(
                record_from_transcript(
                    tr,
                    title=self._entry_title(eid),
                    summary=library.get_summary(eid),
                    captured_at_ms=archived_at,
                )
            )
        if not records:
            dialogs.alert(self, "存入飞书作品库", "选中的文稿已经不存在，Lulu 没有发送。")
            return
        self._set_selecting(False)
        self._feishu_worker = send_records_to_feishu(
            self,
            self.cfg,
            records,
            title=f"已选 {len(records)} 篇作品",
            context="selection",
        )

    def _move_entry(self, entry_id: str, kind: str) -> None:
        try:
            folders = library.list_folders(kind)
        except (OSError, ValueError, RuntimeError) as exc:
            dialogs.alert(self, "文件夹无法读取", str(exc))
            return
        names = ["（根目录）"] + [f["name"] for f in folders]
        idx = dialogs.choose(self, "移动到文件夹", "选择目标：", names, current=0)
        if idx is None:
            return
        target = None if idx == 0 else folders[idx - 1]["id"]
        try:
            library.set_entry_folder(entry_id, target)
        except (OSError, ValueError, RuntimeError) as exc:
            dialogs.alert(self, "移动未完成", str(exc))
        self.refresh()

    def _view(self, entry_id: str) -> None:
        voice = library.load_voice(entry_id)
        if voice is not None:
            if self._on_view is not None:
                self._on_view(entry_id)
                return
            self._view_voice(voice)
            return
        if self._on_view is not None:        # 主窗口接管，进入素材库工作区
            self._on_view(entry_id)
            return
        tr = library.load(entry_id)
        if tr is None:
            return
        title = str((library.document_snapshot(entry_id) or {}).get("title") or "文稿")
        LibraryViewer(title, tr, self, entry_id=entry_id, cfg=self.cfg).exec()



    def _rename_inline(self, entry_id: str, title: str) -> None:
        try:
            saved_title = library.rename(entry_id, title)
        except ValueError:
            dialogs.alert(self, "修改素材标题", "标题不能为空。")
            self.refresh()
            return
        except (OSError, RuntimeError) as exc:
            dialogs.alert(self, "修改素材标题失败", str(exc))
            self.refresh()
            return
        self.refresh()
        if self._on_renamed is not None:
            self._on_renamed(entry_id, saved_title)

    def _view_voice(self, item: dict) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(str(item.get("title") or "配音素材"))
        dlg.resize(620, 560)
        dlg.setStyleSheet(f"QDialog{{background:{T.BG};}}" + T.QSS)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(10)
        title = QLabel(str(item.get("title") or "配音素材"))
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        lay.addWidget(title)
        flags = item.get("missing_flags") or {}
        source_type = item.get("source_type") or "manual"
        if source_type == "manual":
            source_label = "来源：手动输入"
        elif flags.get("source_missing"):
            source_label = "来源不可用"
        else:
            source_label = f"来源：{item.get('source_title') or source_type}"
        audio_status = "音频文件不在原位置" if flags.get("audio_missing") else f"音频：{item.get('audio_filename') or '未知文件'}"
        meta = QLabel(
            f"{item.get('engine') or '未知模型'} · {item.get('voice_name') or '未命名音色'} · "
            f"{source_label} · {audio_status}"
        )
        meta.setObjectName("pageMeta")
        meta.setWordWrap(True)
        lay.addWidget(meta)
        cfg = QLabel(str(item.get("config_summary") or ""))
        cfg.setObjectName("pageMeta")
        cfg.setWordWrap(True)
        lay.addWidget(cfg)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(str(item.get("source_text_snapshot") or item.get("text") or ""))
        text.setStyleSheet(
            f"QPlainTextEdit{{background:{T.CARD};border:1px solid {T.LINE};border-radius:12px;"
            f"padding:14px;font-size:14px;color:{T.INK};line-height:1.7;}}"
        )
        lay.addWidget(text, 1)
        dlg.exec()

    def _summarize(self, entry_id: str) -> None:
        voice = library.load_voice(entry_id)
        if voice is not None:
            self._play_voice(voice)
            return
        from ..core import summarize
        tr = library.load(entry_id)
        if tr is None:
            return
        if not summarize.is_configured():
            dialogs.alert(
                self, "智能摘要",
                "还没配置摘要服务。请到「AI 大模型配置」→「文稿助手」填入 API Key（默认 DeepSeek）。",
            )
            return
        from .summary_dialog import SummaryDialog
        title = str((library.document_snapshot(entry_id) or {}).get("title") or "文稿")
        SummaryDialog(title, tr.text, self, entry_id=entry_id).exec()

    def _export(self, entry_id: str) -> None:
        voice = library.load_voice(entry_id)
        if voice is not None:
            self._export_voice(voice)
            return
        tr = library.load(entry_id)
        if tr is None:
            return
        title = str((library.document_snapshot(entry_id) or {}).get("title") or "文稿")
        v = LibraryViewer(title, tr, self)
        v._export()

    def _voice_audio_path_or_alert(self, item: dict) -> Path | None:
        path = Path(str(item.get("audio_path") or "")).expanduser()
        if not path.exists():
            dialogs.alert(self, "配音素材", "音频文件不在原位置，可能已被移动或删除。素材记录仍已保留。")
            return None
        return path

    def _play_voice(self, item: dict) -> None:
        path = self._voice_audio_path_or_alert(item)
        if path is None:
            return
        self._player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self._player.play()

    def _export_voice(self, item: dict) -> None:
        source = self._voice_audio_path_or_alert(item)
        if source is None:
            return
        default = str(Path.home() / "Downloads" / (str(item.get("audio_filename") or source.name)))
        path, _ = QFileDialog.getSaveFileName(self, "导出配音音频", default, "音频文件 (*.*)")
        if not path:
            return
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        dialogs.alert(self, "导出", f"已导出：{target.name}")

    def _reveal(self, entry_id: str) -> None:
        voice = library.load_voice(entry_id)
        if voice is not None:
            path = self._voice_audio_path_or_alert(voice)
            if path is None:
                return
        else:
            path = library.LIBRARY_DIR / f"{entry_id}.json"
            if not path.exists():
                dialogs.alert(self, "素材库", "素材记录不在原位置，可能已被移动或删除。")
                return
        try:
            subprocess.run(["open", "-R", str(path)], check=False)
        except OSError:
            dialogs.alert(self, "素材库", "无法在 Finder 中显示该文件，请确认文件仍在本机且当前用户有权限访问。")

    def _delete(self, entry_id: str) -> None:
        voice = library.load_voice(entry_id)
        if voice is not None:
            title = "删除配音素材"
            msg = "将这条配音记录移入最近删除？可以恢复，原音频文件保留。"
        else:
            title = "删除文稿"
            msg = "将这篇文稿移入最近删除？可以恢复，原音视频保留。"
        if not dialogs.confirm(self, title, msg):
            return
        try:
            library.delete(entry_id)
        except (OSError, ValueError, RuntimeError) as exc:
            dialogs.alert(self, "删除未完成", str(exc))
        self.refresh()


# The list workspace reuses the established export, source and integration actions.
def __getattr__(name):
    if name == "LibraryPage":
        from .library_workspace import LibraryWorkspace
        return LibraryWorkspace
    raise AttributeError(name)
