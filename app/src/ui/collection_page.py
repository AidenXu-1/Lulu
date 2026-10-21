"""采集结果列表页（切片 1/2）。

两种采集方式：
- 抓取最新（路 A）：匿名、零风险、最新约 20 条。
- 全量采集（路 B）：登录小号、抓全部作品、有封号风险。
列表：点击表头排序、关键词筛选、已选计数、导出表格、批量转录、删除；表格内部滚动。
后台抓取走页内 QThread（不动 worker.py，减少与并行会话的冲突面）。
"""

from __future__ import annotations

import os
import math
import re
import subprocess
import threading
import time as monotonic_time
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from PySide6.QtCore import (
    QDate, QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, Qt,
    QObject, QThread, QTimer, QUrl, Signal,
)

_FOCUS_ANIM_MS = 240
_EASE_C1 = QPointF(0.32, 0.0)   # 贝塞尔：慢起→快走→缓停，丝滑展开/收起
_EASE_C2 = QPointF(0.2, 1.0)
_MAX_H = 16777215              # Qt 控件最大高度上限（展开后解除折叠 cap）
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QFileDialog, QFrame, QComboBox, QListView, QButtonGroup,
    QSizePolicy, QMenu, QProgressBar, QApplication, QStyledItemDelegate,
    QStyle, QStyleOptionViewItem,
)

from . import theme as T, icons, dialogs
from .collection_history_dialog import CollectionHistoryDialog
from .consumer_date import ConsumerDate
from .consumer_select import ConsumerActionPopup, ConsumerSelect
from .control_foundation import (
    LuluCheckBox,
    PaperTextureFrame,
    adopt_button,
    adopt_consumer_theme_scope,
    adopt_segment,
    adopt_select_field,
    adopt_surface,
    adopt_text_field,
    build_consumer_page_header,
)
from ..core.collect import export
from ..core.collect.contracts import CollectResult
from ..core.collect.history import CollectionHistoryRepository, HistoryError, HistorySnapshot
from ..core.collect.application import (
    CollectionApplicationService,
    CollectionCommand,
    CollectionCoverSummary,
    CollectionExecutionResult,
    DOUYIN_MODE_EFFICIENCY as MODE_EFFICIENCY,
    XHS_RELEASE_DISABLED_MESSAGE,
    XhsBrowserStatus,
)
from ..core.collect.preferences import (
    CollectCoverPreferenceService,
    CollectCoverPreferenceSnapshot,
    MaterialPreferenceService,
    MaterialPreferenceSnapshot,
)
from ..core.collect.repository import (
    CollectionRepository,
    RepositoryError,
    RepositorySnapshot,
)
from ..core.collect.media import (
    CollectionMediaAssociationService,
    ManagedMediaFetchError,
    ManagedMediaStore,
    reliable_media_sources,
)
from ..core.collect.douyin_browser import login, is_logged_in, clear_login

_COLS = [
    "", "作者", "标题", "发布日期", "时长",
    "点赞", "收藏", "评论", "分享", "转录", "音视频", "封面",
]
_TRANSCRIBED_COL = 9
_MEDIA_COL = 10
_COVER_COL = 11
_ROW_SELECTED_ROLE = int(Qt.ItemDataRole.UserRole) + 41
# 可点击排序的列 → CollectedItem 属性；以及该列默认降序（数值/日期默认降序，作者升序）
_SORT_ATTR = {
    1: "author_name", 3: "create_time", 4: "duration_ms",
    5: "like", 6: "collect", 7: "comment", 8: "share",
}
_SORT_DESC_DEFAULT = {
    1: False, 3: True, 4: True, 5: True, 6: True, 7: True, 8: True,
}
# 排序下拉（显示名 → 列号），与点表头排序共用同一状态
_SORT_FIELDS = [
    ("发布日期", 3), ("点赞", 5), ("收藏", 6), ("评论", 7),
    ("分享", 8), ("时长", 4), ("作者", 1),
]
_FILTER_FIELDS = [
    ("不筛选", None, "none"),
    ("点赞", "like", "numeric"),
    ("收藏", "collect", "numeric"),
    ("评论", "comment", "numeric"),
    ("分享", "share", "numeric"),
    ("时长（秒）", "duration_sec", "numeric"),
    ("作品标题", "title", "text"),
    ("作者名称", "author_name", "text"),
    ("作品 ID", "work_id", "text"),
    ("原作品链接", "work_url", "text"),
    ("平台", "platform", "enum"),
    ("内容类型", "content_kind", "enum"),
    ("封面状态", "cover_status", "enum"),
]
_FILTER_OPERATORS = {
    "numeric": [
        ("大于 >", "gt"), ("大于等于 ≥", "ge"), ("等于 =", "eq"),
        ("不等于 ≠", "ne"), ("小于 <", "lt"), ("小于等于 ≤", "le"),
    ],
    "text": [
        ("包含", "contains"), ("不包含", "not_contains"),
        ("等于", "eq"), ("不等于", "ne"),
    ],
    "enum": [("等于", "eq"), ("不等于", "ne")],
}
_ENUM_FILTER_VALUES = {
    "platform": [("抖音", "douyin"), ("小红书", "xiaohongshu"), ("B站", "bilibili")],
    "content_kind": [
        ("视频", "video"), ("图文", "image"), ("混合", "mixed"),
    ],
    "cover_status": [
        ("未请求", "not_requested"), ("待下载", "url_only"),
        ("下载中", "pending"), ("已保存", "downloaded"),
        ("下载失败", "failed"), ("不可用", "unavailable"),
        ("隐私跳过", "skipped_privacy"),
    ],
}
_DATE_FILTERS = (
    ("all", "日期不限"),
    ("7", "近 7 天"),
    ("30", "近 30 天"),
    ("90", "近 90 天"),
    ("custom", "自定义日期"),
)
_DATA_LOCATION_OPTIONS = (
    ("collection", "采集数据"),
    ("covers", "封面"),
    ("library", "文稿"),
    ("media", "音视频"),
)
_FILTER_FIELD_TYPES = {
    str(attr): str(field_type)
    for _label, attr, field_type in _FILTER_FIELDS
    if attr is not None
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_TRUTHFUL_FIELDS = frozenset({
    "create_time", "duration_ms", "like", "collect", "comment", "share",
})
_UNKNOWN_TOOLTIPS = {
    "create_time": "未获取发布时间",
    "duration_ms": "未获取时长",
    "like": "未获取点赞数",
    "collect": "未获取收藏数",
    "comment": "未获取评论数",
    "share": "未获取分享数",
}

_BROWSER_CANDIDATES = [
    ("Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("Brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("Chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
]

# 专用浏览器线程超过页面关闭等待窗时，保留 Python 引用直到真正退出，
# 避免 Qt 因 QThread 仍在运行却被回收而崩溃。不借此把 profile 标成可重用。
_PENDING_BROWSER_WORKERS: set[QThread] = set()
_PENDING_BITABLE_WORKERS: set[QThread] = set()
_XHS_RELEASE_CAPABILITY_PROBE = (
    "https://www.xiaohongshu.com/user/profile/lulu_release_capability"
)


class _CollectionRowDelegate(QStyledItemDelegate):
    """Paint the existing selection truth as one continuous full-row highlight."""

    def __init__(self, consumer: T.ConsumerTheme, parent=None):
        super().__init__(parent)
        self._consumer = consumer

    def selected_background(self) -> QColor:
        """Return an opaque wash so every column has the same visible background."""
        base = QColor(self._consumer.surface_focal)
        accent = QColor(self._consumer.accent)
        ratio = 0.18
        return QColor(
            round(base.red() * (1.0 - ratio) + accent.red() * ratio),
            round(base.green() * (1.0 - ratio) + accent.green() * ratio),
            round(base.blue() * (1.0 - ratio) + accent.blue() * ratio),
        )

    def paint(self, painter: QPainter, option, index) -> None:
        selected = bool(index.data(_ROW_SELECTED_ROLE))
        rendered = QStyleOptionViewItem(option)
        self.initStyleOption(rendered, index)
        if selected:
            rendered.state &= ~(
                QStyle.StateFlag.State_Selected
                | QStyle.StateFlag.State_MouseOver
            )
            painter.fillRect(option.rect, self.selected_background())
            rendered.backgroundBrush = QBrush(Qt.BrushStyle.NoBrush)
        style = rendered.widget.style() if rendered.widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            rendered,
            painter,
            rendered.widget,
        )


def _guard_running_browser_worker(worker: QThread) -> None:
    _PENDING_BROWSER_WORKERS.add(worker)
    worker.finished.connect(lambda w=worker: _PENDING_BROWSER_WORKERS.discard(w))


def _guard_running_bitable_worker(worker: QThread) -> None:
    """关闭页面后仍持有飞书线程，直到当前请求边界自然返回。"""

    _PENDING_BITABLE_WORKERS.add(worker)
    worker.finished.connect(lambda w=worker: _PENDING_BITABLE_WORKERS.discard(w))


def _detect_browsers() -> list[tuple[str, str | None]]:
    found = [(n, p) for n, p in _BROWSER_CANDIDATES if os.path.exists(p)]
    return found or [("默认浏览器", None)]


def _combo_qss() -> str:
    consumer = T.consumer_theme(T.ACTIVE.name)
    return (
        f"QComboBox{{background:{consumer.surface_focal};border:1px solid {consumer.line};border-radius:9px;"
        f"padding:5px 12px;color:{consumer.ink};}}"
        f"QComboBox:hover{{border-color:{consumer.accent_hairline};}}"
        f"QComboBox:focus{{border-color:{consumer.accent};}}"
        f"QComboBox::drop-down{{border:none;width:22px;}}"
        # 弹层容器：圆角、留白、细边
        f"QComboBox QAbstractItemView{{background:{consumer.surface_floating};border:1px solid {consumer.line};"
        f"border-radius:12px;padding:6px;outline:0;}}"
        # 选项：统一行高、圆角、hover/选中陶土底
        f"QComboBox QAbstractItemView::item{{min-height:32px;padding:4px 12px;margin:1px 0;"
        f"border-radius:8px;color:{consumer.ink};background:transparent;}}"
        f"QComboBox QAbstractItemView::item:hover{{background:{consumer.accent_wash};color:{consumer.ink};}}"
        f"QComboBox QAbstractItemView::item:selected{{background:{consumer.accent_wash};color:{consumer.accent};}}"
    )


def _setup_combo(combo: QComboBox) -> None:
    """强制 Qt 自绘弹层（QListView）+ 应用样式，避免 macOS 原生菜单忽略 QSS 显得廉价。

    须在 addItem 之后调用：弹层宽度按最长选项自适应，避免文字被截断。
    """
    combo.setView(QListView())
    combo.setStyleSheet(_combo_qss())
    fm = combo.fontMetrics()
    longest = max((fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())),
                  default=60)
    # 文字宽 + 充足内边距，并设下限（离屏字体测量偏小，真机中文更宽，留足余量防截断）
    combo.view().setMinimumWidth(max(longest + 64, 136))


def _beijing_today() -> date:
    return datetime.now(_SHANGHAI).date()


def _beijing_day_bounds(start: date, end: date) -> tuple[float, float]:
    """Inclusive local dates represented as a half-open timestamp range."""
    lower = datetime.combine(start, time.min, tzinfo=_SHANGHAI)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=_SHANGHAI)
    return lower.timestamp(), upper.timestamp()


class _CollectionWorker(QThread):
    """统一后台采集 worker；只调用 application service，不接触 QWidget。"""

    progress = Signal(str, int)
    done = Signal(object, int)
    failed = Signal(str, int)

    def __init__(self, service, command: CollectionCommand, run_token: int):
        super().__init__()
        self._service = service
        self._command = command
        self._run_token = run_token
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            collect_with_cover = getattr(
                self._service, "collect_with_cover", None
            )
            runner = (
                collect_with_cover
                if callable(collect_with_cover)
                else self._service.collect
            )
            result = runner(
                self._command,
                progress=lambda m, c: self.progress.emit(m, c),
                is_cancelled=lambda: self._cancel,
            )
            self.done.emit(result, self._run_token)
        except Exception:  # noqa: BLE001 - 不把堆栈或敏感路径送进 UI
            self.failed.emit("采集过程发生异常，已安全停止。", self._run_token)


class _CoverRetryWorker(QThread):
    """只重试封面子阶段，不重新采集主页。"""

    done = Signal(object)
    failed = Signal()

    def __init__(self, workflow, items):
        super().__init__()
        self._workflow = workflow
        self._items = tuple(items)
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            result = self._workflow.run(
                CollectResult.complete(self._items),
                snapshot=CollectCoverPreferenceSnapshot(True),
                is_cancelled=lambda: self._cancel,
            )
            self.done.emit(result)
        except Exception:
            self.failed.emit()


def _fetch_managed_media(item, download_mode, destination, is_cancelled):
    """生产网络取材边界；只由用户显式素材动作的 QThread 调用。"""
    from ..core.audio.extractor import (
        download_douyin_video_cdn,
        is_douyin_video_cdn,
        resolve_source,
    )
    from ..core.collect.douyin_fetch import fetch_item

    def progress(_message, _fraction):
        if is_cancelled():
            raise InterruptedError("素材下载已取消")

    # 主页的「音视频文件」始终代表完整视频。保留入参仅为与
    # ManagedMediaFetcher 协议兼容，不让内容提取页的「仅音频」偏好泄漏到主页。
    _ = download_mode
    root = Path(destination)
    sources = reliable_media_sources(item)
    if not sources:
        raise RuntimeError("没有可用的音视频来源")

    video_extensions = frozenset({".m4v", ".mkv", ".mov", ".mp4", ".webm"})
    for index, source in enumerate(sources, start=1):
        if is_cancelled():
            raise InterruptedError("素材下载已取消")
        attempt = root / f"attempt-{index}"
        attempt.mkdir(mode=0o700, parents=True, exist_ok=False)
        try:
            is_douyin_direct = (
                getattr(item, "platform", "") == "douyin"
                and source == str(getattr(item, "play_url", "") or "").strip()
            )
            if is_douyin_direct:
                if not is_douyin_video_cdn(source):
                    continue
                fetched = Path(
                    download_douyin_video_cdn(
                        source,
                        attempt,
                        progress,
                        work_id=str(getattr(item, "work_id", "") or ""),
                    )
                )
            elif (
                getattr(item, "platform", "") == "douyin"
                and source
                == str(getattr(item, "work_url", "") or "").strip()
            ):
                refreshed = fetch_item(str(getattr(item, "work_id", "") or ""))
                refreshed_url = str(refreshed.play_url or "").strip()
                if not is_douyin_video_cdn(refreshed_url):
                    continue
                fetched = Path(download_douyin_video_cdn(
                    refreshed_url,
                    attempt,
                    progress,
                    work_id=str(getattr(item, "work_id", "") or ""),
                ))
            else:
                fetched = Path(resolve_source(
                    source,
                    attempt,
                    True,
                    progress,
                    {},
                    link_media_mode="video",
                ))
            if not fetched.is_absolute() or fetched.is_symlink():
                continue
            try:
                fetched.resolve(strict=True).relative_to(attempt.resolve(strict=True))
            except (FileNotFoundError, OSError, ValueError):
                continue
            try:
                valid = (
                    fetched.is_file()
                    and fetched.stat().st_size > 0
                    and fetched.suffix.casefold() in video_extensions
                )
            except OSError:
                valid = False
            if valid:
                return fetched
        except InterruptedError:
            raise
        except Exception:
            # 播放直链可能过期；下一候选按同一作品 ID 匿名刷新一次。
            continue
    raise ManagedMediaFetchError(
        "media_source_refresh_failed",
        "播放地址已过期或不可用，作品页也未能刷新。请重新采集主页后再试。",
    )


def _cover_cas_values(before, after) -> tuple[dict, dict]:
    expected = {
        "cover_local_path": before.cover_local_path,
        "cover_status": before.cover_status,
    }
    updates = {
        "cover_local_path": after.cover_local_path,
        "cover_status": after.cover_status,
    }
    before_error = str(before.last_error_code or "")
    after_error = str(after.last_error_code or "")
    if all(
        not value or value.startswith(("cover:", "cover_"))
        for value in (before_error, after_error)
    ):
        expected["last_error_code"] = before.last_error_code
        updates["last_error_code"] = after.last_error_code
    return expected, updates


def _commit_cover_state(repository, before, after):
    """提交封面 owned fields；返回（最新条目，是否已证明提交）。"""

    expected, updates = _cover_cas_values(before, after)
    try:
        repository.compare_and_swap_fields(
            before.stable_key,
            expected=expected,
            updates=updates,
        )
        return after, True
    except RepositoryError:
        try:
            latest = repository.load()
        except RepositoryError:
            return before, False
        current = next(
            (
                item for item in latest.items
                if item.stable_key == before.stable_key
            ),
            None,
        )
        if current is None:
            return before, False
        if all(getattr(current, name) == value for name, value in updates.items()):
            return current, True
        return current, False


class _TranscribedRead(QObject):
    """Disposable, read-only daemon; slow disk must not hold GUI shutdown open.

    Unlike upload QThreads it has no writes to settle and no native QThread
    destructor that requires a blocked read to finish before process exit.
    """
    finished = Signal()
    def __init__(self, read):
        super().__init__()
        self.read = read
        self.result = None
        self.error = False
        self._stopped = threading.Event()
        self._interrupted = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True, name='lulu-library-status')
        self._thread.start()

    def isRunning(self):
        return self._thread is not None and not self._stopped.is_set()

    def requestInterruption(self):
        self._interrupted.set()

    def wait(self, timeout_ms):
        return self._stopped.wait(timeout_ms / 1000)

    def run(self):
        try:
            if not self._interrupted.is_set():
                self.result = self.read()
        except Exception:
            self.error = True
        finally:
            self._stopped.set()
            try:
                self.finished.emit()
            except RuntimeError:
                pass  # The QApplication may already have been destroyed.


class _MaterialDownloadWorker(QThread):
    """历史素材下载：受管媒体与封面分别结算、分别 CAS。"""

    done = Signal(object)
    progress = Signal(object)
    failed = Signal()

    def __init__(
        self, repository, cover_workflow, items, snapshot, *, origin
    ):
        super().__init__()
        self._repository = repository
        self._cover_workflow = cover_workflow
        self._items = tuple(items)
        self._snapshot = snapshot
        self._origin = origin
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from ..core.task import TaskExecutionPolicy

        try:
            current = self._items
            media_result = None
            if self._snapshot.save_media:
                policy = TaskExecutionPolicy(
                    origin=self._origin,
                    mode="materialize",
                    stable_key=current[0].stable_key,
                    material_snapshot=self._snapshot,
                    media_destination="managed_collection",
                )
                def fetch_with_progress(item, mode, destination, is_cancelled):
                    self.progress.emit(
                        (item.stable_key, "media", "downloading")
                    )
                    return _fetch_managed_media(
                        item, mode, destination, is_cancelled
                    )

                service = CollectionMediaAssociationService(
                    self._repository,
                    ManagedMediaStore(self._repository.path.parent / "media"),
                    fetch_with_progress,
                )
                media_result = service.run(
                    current, policy=policy, is_cancelled=lambda: self._cancel
                )
                current = media_result.items

            cover_result = None
            if self._snapshot.save_cover and self._cover_workflow is not None:
                for item in current:
                    if str(getattr(item, "cover_url", "") or ""):
                        self.progress.emit(
                            (item.stable_key, "cover", "downloading")
                        )
                cover_result = self._cover_workflow.run(
                    CollectResult.complete(current),
                    snapshot=CollectCoverPreferenceSnapshot(True),
                    is_cancelled=lambda: self._cancel,
                )
                settled = []
                outcomes = list(cover_result.outcomes)
                for index, (before, after) in enumerate(zip(
                    current, cover_result.collect_result.items
                )):
                    if (
                        before.cover_local_path == after.cover_local_path
                        and before.cover_status == after.cover_status
                        and before.last_error_code == after.last_error_code
                    ):
                        settled.append(before)
                        continue
                    committed, proved = _commit_cover_state(
                        self._repository, before, after
                    )
                    settled.append(committed)
                    if not proved:
                        outcomes[index] = replace(
                            outcomes[index],
                            status="failed",
                            reason_code="cover_state_commit_failed",
                            relative_path="",
                        )
                current = tuple(settled)
                cover_result = replace(
                    cover_result,
                    collect_result=replace(
                        cover_result.collect_result,
                        items=current,
                    ),
                    outcomes=tuple(outcomes),
                )
            self.done.emit((current, media_result, cover_result, self._cancel))
        except Exception:
            self.failed.emit()


class _LoginWorker(QThread):
    """后台打开浏览器让用户登录（小号），登录态持久复用。"""

    progress = Signal(str, int)
    done = Signal(bool)

    def __init__(self, browser_path: str | None = None):
        super().__init__()
        self._browser_path = browser_path
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            ok = login(self._browser_path,
                       progress=lambda m, c: self.progress.emit(m, c),
                       is_cancelled=lambda: self._cancel)
            self.done.emit(bool(ok))
        except Exception as exc:  # noqa: BLE001
            self.progress.emit(f"登录失败：{exc}", 0)
            self.done.emit(False)


class _XhsLoginWorker(QThread):
    """打开或接管小红书专用常驻浏览器。"""

    progress = Signal(str, int)
    done = Signal(str)

    def __init__(self, service, browser_path: str | None = None):
        super().__init__()
        self._service = service
        self._browser_path = browser_path
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        result = self._service.run_profile_login(
            "xiaohongshu",
            browser_path=self._browser_path,
            progress=lambda m, c: self.progress.emit(m, c),
            is_cancelled=lambda: self._cancel,
        )
        self.done.emit(result)


class _XhsCloseWorker(QThread):
    """只响应用户显式关闭，调用 P0 的正常关闭出口。"""

    done = Signal(object)

    def __init__(self, service):
        super().__init__()
        self._service = service

    def run(self) -> None:
        self.done.emit(self._service.close_xhs_browser())


class _BitableWorker(QThread):
    """后台：新建多维表格，或验证后追加到已有表格。"""

    done = Signal(bool, object)  # ok, BitableExportReceipt 或脱敏错误

    def __init__(
        self,
        space_id,
        parent_node,
        title,
        table_name,
        fields,
        preflight,
        *,
        existing_url: str = "",
        archive_records=(),
        selection: dict | None = None,
        source_items=(),
        transcripts: dict | None = None,
    ):
        super().__init__()
        self._args = (space_id, parent_node, title, table_name, fields)
        self._preflight = preflight
        self.existing_url = str(existing_url or "").strip()
        self.archive_records = tuple(archive_records)
        self.selection = dict(selection or {})
        self.source_items = tuple(source_items)
        self.transcripts = dict(transcripts or {})
        self.resolved_target = None
        self.schema_version = 1
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def run(self) -> None:
        from ..core import feishu
        from ..core import feishu_storage
        try:
            args = list(self._args)
            preflight = self._preflight
            allowed_fields = feishu_storage.STANDARD_LIBRARY_TEMPLATE_V1.field_names
            deduped = (
                feishu_storage.dedupe_archive_records(self.archive_records)
                if self.archive_records
                else None
            )
            if self.existing_url:
                try:
                    self.resolved_target = feishu.resolve_existing_bitable(
                        self.existing_url,
                        args[4],
                    )
                except feishu.BitableTargetError:
                    # 兼容旧 Lulu 的 13 字段表；不自动给用户表增删字段。
                    if (
                        not self.archive_records
                        or self.selection.get("schema_version") != 0
                    ):
                        raise
                    args[4] = list(export.BITABLE_FIELDS)
                    self.schema_version = 0
                    allowed_fields = export.BITABLE_HEADERS
                    self.resolved_target = feishu.resolve_existing_bitable(
                        self.existing_url,
                        args[4],
                    )

            def rebuild_preflight(current_deduped):
                if current_deduped is None:
                    if self.source_items:
                        prepared = export.prepare_bitable_export(
                            self.source_items,
                            self.transcripts,
                        )
                        return replace(prepared, records=preflight.records)
                    return preflight
                if self.source_items and len(self.source_items) != len(
                    self.archive_records
                ):
                    raise ValueError("飞书导出冻结数据不一致")
                index_map = {
                    source_index: target_index
                    for target_index, source_index in enumerate(
                        current_deduped.source_indexes
                    )
                }

                def remap(values):
                    return tuple(
                        replace(
                            item,
                            record_index=index_map[item.record_index],
                        )
                        for item in values
                        if item.record_index in index_map
                    )

                job_id = str(uuid4())
                payloads = tuple(
                    feishu_storage.record_to_bitable_record(
                        record,
                        allowed_fields=allowed_fields,
                        write_id=f"{job_id}:{index}",
                    )
                    for index, record in enumerate(current_deduped.records)
                )
                # 查重先做，随后只读取真正需要新增的作品附件。整批已存在时
                # 不会白白读取封面或计算大文件哈希。
                if self.source_items:
                    prepared = export.prepare_bitable_export(
                        tuple(
                            self.source_items[source_index]
                            for source_index in current_deduped.source_indexes
                        ),
                        self.transcripts,
                    )
                    return replace(prepared, records=payloads)
                return replace(
                    preflight,
                    records=payloads,
                    cover_candidates=remap(preflight.cover_candidates),
                    cover_skipped=remap(preflight.cover_skipped),
                    media_candidates=remap(preflight.media_candidates),
                    media_skipped=remap(preflight.media_skipped),
                )

            def perform_write(current_deduped):
                duplicate_count = (
                    current_deduped.duplicate_count
                    if current_deduped is not None
                    else 0
                )
                if (
                    current_deduped is not None
                    and not current_deduped.records
                    and self.resolved_target is not None
                ):
                    return feishu.BitableExportReceipt(
                        url=self.resolved_target.url,
                        row_total=0,
                        row_success=0,
                        row_failed=0,
                        row_unconfirmed=0,
                        row_cancelled=0,
                        cover_candidate=0,
                        cover_success=0,
                        cover_skipped=0,
                        cover_failed=0,
                        cover_uncertain=0,
                        cancelled=False,
                        row_duplicate_skipped=duplicate_count,
                        destination_app_token=self.resolved_target.app_token,
                        destination_table_id=self.resolved_target.table_id,
                        destination_table_name=self.resolved_target.table_name,
                    )
                current_preflight = rebuild_preflight(current_deduped)
                receipt = feishu.export_collection_to_new_bitable(
                    *args,
                    current_preflight.records,
                    current_preflight.cover_candidates,
                    current_preflight.cover_skipped,
                    current_preflight.media_candidates,
                    current_preflight.media_skipped,
                    destination_kind=str(
                        self.selection.get("destination_kind") or "wiki"
                    ),
                    existing_target=self.resolved_target,
                    is_cancelled=lambda: self._cancel or self.isInterruptionRequested(),
                )
                return replace(
                    receipt,
                    row_duplicate_skipped=duplicate_count,
                )

            if self.resolved_target is not None:
                with feishu._bitable_table_write_lock(
                    self.resolved_target.app_token,
                    self.resolved_target.table_id,
                ):
                    if self.schema_version == 1 and deduped is not None:
                        existing_urls = feishu.find_existing_bitable_text_values(
                            self.resolved_target.app_token,
                            self.resolved_target.table_id,
                            "原作品链接",
                            (record.work_url for record in deduped.records),
                        )
                        deduped = feishu_storage.dedupe_archive_records(
                            self.archive_records,
                            existing_work_urls=existing_urls,
                        )
                    receipt = perform_write(deduped)
            else:
                receipt = perform_write(deduped)
            self.done.emit(True, receipt)
        except Exception as exc:  # noqa: BLE001
            safe_types = (
                feishu.FeishuApiError,
                feishu.FeishuTransportError,
                feishu.BitablePartialWriteError,
                feishu.BitableTargetError,
            )
            message = (
                str(exc)
                if isinstance(exc, safe_types)
                else "飞书导出未完成，请稍后重试。"
            )
            self.done.emit(False, message)


def _bitable_size_text(size: int) -> str:
    value = max(0, int(size or 0))
    if value < 1024:
        return f"{value} B"
    if value < 1024 ** 2:
        return f"{value / 1024:.1f} KB"
    if value < 1024 ** 3:
        return f"{value / 1024 ** 2:.1f} MB"
    return f"{value / 1024 ** 3:.2f} GB"


class _AutoHideLabel(QLabel):
    """Transient overlay message that never participates in page layout."""

    on_text = None   # 页面注入：文字变化后回调，用来联动活动区显隐

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._persistent = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(6000)
        self._hide_timer.timeout.connect(self._expire)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setVisible(bool(text))
        self._hide_timer.stop()
        if text and not self._persistent:
            self._hide_timer.start()
        if self.on_text is not None:
            self.on_text()

    def set_persistent(self, persistent: bool) -> None:
        self._persistent = bool(persistent)
        self._hide_timer.stop()
        if self.text() and not self._persistent:
            self._hide_timer.start()

    def _expire(self) -> None:
        super().setText("")
        self.hide()
        if self.on_text is not None:
            self.on_text()


class CollectionPage(QWidget):
    def __init__(
        self,
        cfg=None,
        on_transcribe=None,
        collection_service=None,
        history_repository=None,
        records_repository=None,
        cover_preference_service=None,
        cover_workflow=None,
        collection_task_states=None,
        douyin_login_open=None, douyin_login_state=None, douyin_login_busy=None,
    ):
        super().__init__()
        self.cfg = cfg
        self._on_transcribe = on_transcribe
        self._douyin_login_open = douyin_login_open
        self._douyin_login_state = douyin_login_state
        self._douyin_login_busy = douyin_login_busy or (lambda: False)
        self._service = collection_service or CollectionApplicationService()
        self._items: list = []
        self._view: list = []
        self._selected_keys: set[str] = set()
        self._selection_anchor_key = ""
        self._syncing_table_checks = False
        self._date_mode = "all"
        self._custom_date_range: tuple[date, date] | None = None
        self._active_field_filter: tuple[str, str, str, object] | None = None
        self._transcribed_read = None
        self._transcribed_read_pending = False
        self._transcribed: set = set()   # stable_key + 旧 aweme_id（用于「已转录」列 ✓/✗）
        self._sort_col = 3            # 默认按发布日期
        self._sort_desc = True        # 新→旧
        configured_mode = str(getattr(cfg, "collection_mode", "partial") or "")
        self._selected_mode = (
            configured_mode if configured_mode in {"partial", "full"} else "partial"
        )
        self._active_collection_mode = ""
        self._stopping_collection = False
        self._worker: _CollectionWorker | None = None
        self._full_worker: _CollectionWorker | None = None
        self._login_worker: _LoginWorker | None = None
        self._xhs_login_worker: _XhsLoginWorker | None = None
        self._xhs_close_worker: _XhsCloseWorker | None = None
        self._bitable_worker: _BitableWorker | None = None
        self._run_token = 0
        self._history_context: dict[int, tuple[CollectionCommand, object]] = {}
        self._material_snapshots: dict[int, MaterialPreferenceSnapshot] = {}
        self._history_repo: CollectionHistoryRepository | None = history_repository
        self._history_snapshot: HistorySnapshot | None = None
        self._history_error_code = ""
        self._records_repo: CollectionRepository | None = records_repository
        self._records_snapshot: RepositorySnapshot | None = None
        self._records_error_code = ""
        self._material_preferences = cover_preference_service
        if self._material_preferences is None and cfg is not None:
            try:
                self._material_preferences = MaterialPreferenceService(cfg)
            except (AttributeError, TypeError):
                self._material_preferences = None
        self._cover_preferences = self._material_preferences
        self._cover_workflow = cover_workflow
        self._collection_task_states = collection_task_states or (lambda: {})
        self._cover_retry_worker: _CoverRetryWorker | None = None
        self._material_worker: _MaterialDownloadWorker | None = None
        self._pending_material_jobs: list[tuple[tuple, MaterialPreferenceSnapshot, str]] = []
        self._material_runtime: dict[str, dict[str, str]] = {}
        self._active_material_entries: set[tuple[str, str]] = set()
        self._shutting_down = False
        self._cover_retry_is_recovery = False
        self._cover_unsubscribe = None
        self._history_dialog: CollectionHistoryDialog | None = None
        self._entry_state = self._service.inspect("")
        self._xhs_homepage_release_disabled = (
            self._service.inspect(
                _XHS_RELEASE_CAPABILITY_PROBE
            ).capability.reason
            == "release_disabled"
        )
        self._table_focus = False     # 放大查看：折叠上方采集控制区，表格填满
        self._form_anim = None
        self._form_natural_h = 0
        self._consumer = T.consumer_theme(T.ACTIVE.name)
        self.setObjectName("root")
        self.setProperty("workspacePage", "collection")
        adopt_consumer_theme_scope(self, "coreWorkspace")
        self._build()
        if self._material_preferences is not None:
            self._cover_unsubscribe = self._material_preferences.subscribe(
                self._sync_material_preferences
            )
            self._sync_material_preferences(self._material_snapshot())
        self._init_history(
            explicit_history=history_repository is not None,
            explicit_records=records_repository is not None,
        )

    def shutdown_workers(self, timeout_ms: int = 3000) -> None:
        """Cancel and join page-owned workers before the main window closes."""
        self._shutting_down = True
        reader = getattr(self, "_transcribed_read", None)
        self._transcribed_read_pending = False
        if reader is not None and reader.isRunning():
            reader.requestInterruption()
            if not reader.wait(100):
                _guard_running_browser_worker(reader)
        self._transcribed_read = None
        pending_material_jobs = getattr(self, "_pending_material_jobs", None)
        if pending_material_jobs is not None:
            pending_material_jobs.clear()
        history_dialog = getattr(self, "_history_dialog", None)
        if history_dialog is not None:
            history_dialog.reject()
        session = getattr(self, '_feishu_session', None)
        if session is not None:
            session.request_cancel()
            save_worker = session.dialog._worker
            if save_worker is not None and save_worker.isRunning():
                save_worker.request_cancel()
                if not save_worker.wait(timeout_ms):
                    _guard_running_bitable_worker(save_worker)
        worker_names = (
            "_worker", "_full_worker", "_login_worker", "_xhs_login_worker",
            "_xhs_close_worker", "_bitable_worker", "_cover_retry_worker",
            "_material_worker", "_feishu_prepare_worker",
        )
        for name in worker_names:
            worker = getattr(self, name, None)
            if worker is None or not worker.isRunning():
                continue
            if hasattr(worker, "request_cancel"):
                worker.request_cancel()
            worker.requestInterruption()
            if not worker.wait(timeout_ms):
                if isinstance(worker, _BitableWorker) or name == '_feishu_prepare_worker':
                    _guard_running_bitable_worker(worker)
                elif isinstance(
                    worker,
                    (_CollectionWorker, _XhsLoginWorker, _XhsCloseWorker),
                ):
                    _guard_running_browser_worker(worker)
                else:
                    worker.terminate()
                    worker.wait(1000)
        for name in worker_names:
            setattr(self, name, None)
        cover_unsubscribe = getattr(self, "_cover_unsubscribe", None)
        if cover_unsubscribe is not None:
            cover_unsubscribe()
            self._cover_unsubscribe = None

    # ---------------- UI ----------------
    def _build(self) -> None:
        host = QVBoxLayout(self)
        host.setContentsMargins(0, 0, 0, 0)
        host.setSpacing(0)
        canvas = PaperTextureFrame(
            strength=self._consumer.paper_canvas_opacity,
            layer="canvas",
        )
        canvas.setObjectName("coreCanvas")
        host.addWidget(canvas)
        root = QVBoxLayout(canvas)
        root.setContentsMargins(18, 0, 18, 28)
        root.setSpacing(0)

        # 表格以上的内容（整页全宽，与下方表格同边距）
        col_wrap = QWidget()
        col = QVBoxLayout(col_wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        page_meta = (
            "贴一个抖音公开主页链接，Lulu 会识别并采集。采集数据只留在你电脑上。"
            if self._xhs_homepage_release_disabled
            else
            "贴一个抖音或小红书公开主页链接，Lulu 会识别平台。采集数据只留在你电脑上。"
        )
        header, self.page_title, self.page_meta = build_consumer_page_header(
            "主页采集",
            page_meta,
        )
        col.addWidget(header)
        col.addSpacing(24)

        # URL 输入 → #intake 卡（页面主入口）
        intake = QFrame()
        intake.setObjectName("intake")
        adopt_surface(intake, "card", variant="linkCard")
        intake.setObjectName("intake")
        il = QHBoxLayout(intake)
        il.setContentsMargins(10, 6, 10, 6)
        il.setSpacing(0)
        self.input = QLineEdit()
        self.input.setObjectName("linkInput")
        adopt_text_field(self.input, accessible_name="主页链接")
        self.input.setClearButtonEnabled(True)
        self.input.setPlaceholderText(
            "粘贴抖音公开主页链接"
            if self._xhs_homepage_release_disabled
            else "粘贴抖音或小红书公开主页链接"
        )
        self.input.setStyleSheet("QLineEdit#linkInput{background:transparent;border:none;}")
        self.input.returnPressed.connect(self._on_primary_action)
        self.input.textChanged.connect(self._on_input_changed)
        il.addWidget(self.input, 1)
        self.platform_status = QLabel("")
        self.platform_status.setObjectName("pageMeta")
        self.platform_status.setMinimumWidth(80)
        self.platform_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        il.addWidget(self.platform_status)
        col.addWidget(intake)
        col.addSpacing(12)

        col.addWidget(self._build_collect_card())

        # 活动区采用页面浮层，不进入布局；提示和进度出现时不会推挤结果卡。
        self.activity = QFrame(self)
        self.activity.setObjectName("collectionActivityOverlay")
        self.activity.setAttribute(Qt.WA_StyledBackground, True)
        av = QVBoxLayout(self.activity)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(6)
        self.status = _AutoHideLabel("", self.activity)
        self.status.setObjectName("collectionToast")
        self.status.setAccessibleName("主页采集操作提示")
        self.status.setWordWrap(True)
        self.status.setOpenExternalLinks(True)   # 成功时状态行可带可点的飞书链接
        self.status.setStyleSheet(
            f"QLabel#collectionToast{{background:{self._consumer.surface_floating};"
            f"color:{self._consumer.ink};border:1px solid {self._consumer.line};"
            "border-radius:10px;padding:10px 12px;font-size:12px;}"
        )
        self.status.hide()
        self.status.on_text = self._sync_activity   # 状态变化时联动活动区显隐
        av.addWidget(self.status)
        # 进度条同样留在浮层中，不占页面高度。
        self.progress_bar = QProgressBar(self.activity)
        self.progress_bar.setObjectName("collectProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setStyleSheet(
            f"QProgressBar#collectProgress{{background:{self._consumer.surface_work};border:none;border-radius:3px;}}"
            f"QProgressBar#collectProgress::chunk{{background:{self._consumer.accent};border-radius:3px;}}")
        self.progress_bar.hide()
        av.addWidget(self.progress_bar)
        self.activity.hide()

        self.form_box = col_wrap      # 放大查看时折叠的「采集控制区」整块
        root.addWidget(col_wrap)
        root.addSpacing(18)

        self.results_shell = PaperTextureFrame(
            strength=self._consumer.paper_queue_opacity,
            corner_radius=self._consumer.radius_work,
            layer="queue",
        )
        adopt_surface(self.results_shell, "card", variant="tableWorkspace")
        results = QVBoxLayout(self.results_shell)
        results.setContentsMargins(14, 14, 14, 14)
        results.setSpacing(0)

        # 动作行
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.select_all_btn = self._tool_btn("全选", self._toggle_select_all, "check")
        self.transcribe_btn = self._tool_btn("批量转录", self._on_transcribe_selected, "list")
        self.material_download_btn = self._tool_btn(
            "下载所选素材", self._download_selected_materials, "download"
        )
        self.export_btn = self._tool_btn("导出表格", self._on_export, "export")
        self.cover_retry_btn = self._tool_btn("重试封面", self._retry_selected_covers)
        self.cover_finder_btn = self._tool_btn("显示封面", self._reveal_selected_cover, "folder")
        # 封面动作属于每一行；保留旧对象只用于兼容既有内部调用，不再占用全局工具栏。
        self.cover_retry_btn.hide()
        self.cover_finder_btn.hide()
        self.bitable_btn = self._tool_btn("存入飞书", self._on_export_bitable, "feishu")
        self.bitable_btn.setMinimumWidth(92)
        self.history_manage_button = self._tool_btn("管理任务", self._open_history_manager)
        self.data_location_select = ConsumerActionPopup(
            _DATA_LOCATION_OPTIONS,
            accessible_name="数据位置",
        )
        self.data_location_select.row_activated.connect(self._open_data_location)
        self.data_location_select.button_activated.connect(self._choose_data_location)
        self._refresh_data_location_contexts()
        for b in (self.select_all_btn, self.material_download_btn, self.transcribe_btn,
                  self.export_btn, self.bitable_btn, self.history_manage_button):
            bar.addWidget(b)
        bar.addWidget(self.data_location_select)
        bar.addStretch(1)
        self.count_label = QLabel("共 0 条")
        self.count_label.setObjectName("pageMeta")
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar.addWidget(self.count_label)
        # 放大查看与结果计数同驻动作行，避免为两个短控件单独占用整行。
        self.focus_btn = self._tool_btn("放大查看", self._toggle_table_focus, "list")
        bar.addWidget(self.focus_btn)
        results.addLayout(bar)
        results.addSpacing(8)

        history_row = QHBoxLayout()
        history_row.setSpacing(8)
        history_row.addWidget(self._dim_label("采集任务"))
        self.history_select = ConsumerSelect(
            (),
            value="",
            accessible_name="采集任务",
            max_visible_rows=8,
            allow_empty=True,
            empty_text="暂无采集任务",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        self.history_select.setMinimumWidth(260)
        self.history_select.value_changed.connect(self._on_history_selected)
        history_row.addWidget(self.history_select, 1)
        results.addLayout(history_row)
        results.addSpacing(8)

        # 排序 / 筛选行（可见下拉；点表头排序也可，二者同步）
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)
        ctrl.addWidget(self._dim_label("排序"))
        self.sort_combo = ConsumerSelect(
            tuple((str(col_id), name) for name, col_id in _SORT_FIELDS),
            value=str(_SORT_FIELDS[0][1]),
            accessible_name="采集结果排序",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        self.sort_combo.setFixedWidth(104)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_combo)
        ctrl.addWidget(self.sort_combo)
        self.dir_btn = QPushButton("↓ 降序")
        self.dir_btn.setObjectName("act")
        self.dir_btn.setCursor(Qt.PointingHandCursor)
        self.dir_btn.setFixedWidth(78)
        self.dir_btn.clicked.connect(self._toggle_dir)
        ctrl.addWidget(self.dir_btn)
        ctrl.addSpacing(16)
        ctrl.addWidget(self._dim_label("筛选"))
        self.metric_combo = ConsumerSelect(
            tuple(("__none__" if attr is None else str(attr), name) for name, attr, _type in _FILTER_FIELDS),
            value="__none__",
            accessible_name="采集结果筛选指标",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        self.metric_combo.setFixedWidth(104)
        self.metric_combo.setToolTip("未获取值不参与筛选")
        self.metric_combo.currentIndexChanged.connect(self._on_filter_field_changed)
        ctrl.addWidget(self.metric_combo)
        self.operator_combo = ConsumerSelect(
            (),
            value="",
            accessible_name="采集结果筛选运算符",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
            allow_empty=True,
            empty_text="—",
        )
        self.operator_combo.setFixedWidth(112)
        self.operator_combo.currentIndexChanged.connect(
            lambda *_: self._apply_filter_editor()
        )
        ctrl.addWidget(self.operator_combo)
        self.metric_value = QLineEdit()
        self.metric_value.setObjectName("linkInput")
        adopt_text_field(self.metric_value, accessible_name="筛选值")
        self.metric_value.setPlaceholderText("输入筛选值")
        self.metric_value.setFixedWidth(112)
        self.metric_value.setToolTip("未获取值不参与筛选")
        self.metric_value.setStyleSheet(
            f"QLineEdit[state=\"error\"]{{border:1px solid {self._consumer.danger};}}"
        )
        self.metric_value.textChanged.connect(lambda *_: self._apply_filter_editor())
        ctrl.addWidget(self.metric_value)
        self.enum_value_combo = ConsumerSelect(
            (),
            value="",
            accessible_name="筛选枚举值",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
            allow_empty=True,
            empty_text="—",
        )
        self.enum_value_combo.setFixedWidth(112)
        self.enum_value_combo.currentIndexChanged.connect(
            lambda *_: self._apply_filter_editor()
        )
        ctrl.addWidget(self.enum_value_combo)
        ctrl.addStretch(1)
        results.addLayout(ctrl)
        results.addSpacing(8)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self._dim_label("日期"))
        self.date_select = ConsumerSelect(
            _DATE_FILTERS,
            value="all",
            accessible_name="发布日期范围",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        self.date_select.setFixedWidth(118)
        self.date_select.value_changed.connect(self._on_date_mode_changed)
        search_row.addWidget(self.date_select)
        today = _beijing_today()
        initial_date = QDate(today.year, today.month, today.day)
        self.date_start = ConsumerDate(initial_date, accessible_name="开始日期")
        self.date_end = ConsumerDate(initial_date, accessible_name="结束日期")
        self._syncing_custom_dates = False
        for editor, name in (
            (self.date_start, "开始日期"),
            (self.date_end, "结束日期"),
        ):
            editor.setToolTip(f"{name}，按北京时间自然日计算")
            editor.ensurePolished()
            editor.setFixedWidth(138)
            editor.hide()
            search_row.addWidget(editor)
        self.date_connector = QLabel("至")
        self.date_connector.setObjectName("pageMeta")
        self.date_connector.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.date_connector.setFixedWidth(18)
        self.date_connector.hide()
        search_row.insertWidget(search_row.indexOf(self.date_end), self.date_connector)
        self.date_start.dateChanged.connect(self._on_custom_start_changed)
        self.date_end.dateChanged.connect(self._on_custom_end_changed)
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("linkInput")
        adopt_text_field(self.filter_edit, accessible_name="搜索采集结果")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setPlaceholderText("搜索标题 / 作者")
        self.filter_edit.setMinimumWidth(180)
        self.filter_edit.textChanged.connect(lambda *_: self._render())
        search_row.addWidget(self.filter_edit, 1)
        results.addLayout(search_row)
        results.addSpacing(8)

        # 表格（表头常显）+ 空状态浮层（覆盖在表格空白区、居中，不挡表头）
        self._build_table()
        results.addWidget(self.table, 1)
        root.addWidget(self.results_shell, 1)
        self.empty_state = self._build_empty_state()
        self.empty_state.setParent(self.table.viewport())
        self.table.viewport().installEventFilter(self)

        self._refresh_login_status()
        self._set_method_buttons(self._selected_mode)
        self._on_filter_field_changed()
        self._on_input_changed(self.input.text())
        self._update_tools_enabled()
        self._update_headers()            # 默认显示「发布日期 ↓」，提示表头可点排序
        self._update_empty_state()

    def _tool_btn(self, text: str, slot, icon: str | None = None) -> QPushButton:
        b = QPushButton(("  " + text) if icon else text)
        adopt_button(b, role="secondary", size="sm", accessible_name=text)
        if icon:
            b.setIcon(QIcon(icons.pixmap(icon, self._consumer.ink_soft, 13)))
        b.clicked.connect(slot)
        return b

    def _dim_label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("pageMeta")
        return lab

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("taskCard")
        T.shadow(card, blur=16, dy=2, alpha=12)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(5)
        return card, lay

    # ---------------- 通用组件（复用 App 既有设计语言） ----------------
    def _section_panel(self):
        """标准卡片 #sectionPanel(CARD/LINE/radius14)+ 轻阴影。返回 (panel, vbox)。"""
        panel = QFrame()
        adopt_surface(panel, "section", variant="coreSection")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(20, 16, 20, 18)
        pv.setSpacing(0)
        return panel, pv

    def _hr(self) -> QFrame:
        """发丝分隔线（LINE_SOFT,1px）。"""
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{self._consumer.line};border:none;")
        return line

    def _vline(self) -> QFrame:
        """竖向发丝分隔线（两栏之间）。"""
        line = QFrame()
        line.setFixedWidth(1)
        line.setStyleSheet(f"background:{self._consumer.line};border:none;")
        return line

    def _std_segment(self, name: str, labels: list):
        """标准分段控件 #segBox/#seg（选中=白底墨字）。保留 checkable 组，返回 (frame, [buttons])。"""
        seg = QFrame()
        seg.setObjectName("segBox")
        h = QHBoxLayout(seg)
        h.setContentsMargins(3, 3, 3, 3)
        h.setSpacing(2)
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        btns = []
        for t in labels:
            b = QPushButton(t)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            grp.addButton(b)
            h.addWidget(b)
            btns.append(b)
        btns[0].setChecked(True)
        adopt_segment(seg, btns, accessible_name=name)
        setattr(self, f"_{name}_group", grp)   # 持有引用，防被回收
        return seg, btns

    def _row_label(self, text: str) -> QLabel:
        """分组列表左侧固定宽标签。"""
        lab = QLabel(text)
        lab.setFixedWidth(76)
        lab.setStyleSheet(f"color:{self._consumer.ink_soft};font-size:13px;background:transparent;")
        return lab

    def _row(self, label_text: str, *controls, left=None) -> QWidget:
        """macOS 分组设置行：左标签 +（可选）值 + 伸缩 + 右侧控件，行高 44。"""
        row = QWidget()
        row.setFixedHeight(44)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(self._row_label(label_text))
        for wd in (left or []):
            h.addWidget(wd, 0, Qt.AlignVCenter)
        h.addStretch(1)
        for wd in controls:
            h.addWidget(wd, 0, Qt.AlignVCenter)
        return row

    def _primary(self, text: str, slot) -> QPushButton:
        """主操作按钮：复用标准 #grabBtn（实心 TERRA），自然尺寸。"""
        b = QPushButton(text)
        adopt_button(b, role="primary", size="lg", accessible_name=text)
        b.clicked.connect(slot)
        return b

    def _build_empty_state(self) -> QWidget:
        """结果为空时的居中提示（浮在表格空白区，表头仍可见）。"""
        card = QFrame()
        card.setObjectName("emptyCard")
        card.setStyleSheet("QFrame#emptyCard{background:transparent;border:none;}")
        v = QVBoxLayout(card)
        v.addStretch(1)
        t = QLabel("还没有采集结果")
        t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet(f"color:{self._consumer.ink_soft};font-size:15px;font-weight:600;background:transparent;")
        v.addWidget(t)
        v.addSpacing(6)
        s = QLabel("粘贴作者主页链接，选好采集方式就开始。")
        s.setAlignment(Qt.AlignCenter)
        s.setStyleSheet(f"color:{self._consumer.ink_muted};font-size:12px;background:transparent;")
        v.addWidget(s)
        v.addStretch(1)
        return card

    def _update_empty_state(self) -> None:
        empty = not self._view
        self.empty_state.setGeometry(self.table.viewport().rect())
        self.empty_state.setVisible(empty)
        if empty:
            self.empty_state.raise_()

    def eventFilter(self, obj, ev):
        # 表格视口尺寸变化时，让空状态浮层跟随铺满
        if obj is self.table.viewport() and ev.type() == QEvent.Resize:
            self.empty_state.setGeometry(self.table.viewport().rect())
            self._fit_collection_columns()
        return super().eventFilter(obj, ev)

    def _build_collect_card(self) -> QFrame:
        """采集方式卡（#sectionPanel）：顶部分段切「抓取最新 / 全量采集」，下方只显示选中方式内容。"""
        panel, pv = self._section_panel()

        head = QHBoxLayout()
        head.setSpacing(14)
        self.method_title = QLabel("采集方式")
        self.method_title.setObjectName("sectionTitle")
        head.addWidget(self.method_title)
        seg, (self.m_quick_btn, self.m_full_btn) = self._std_segment("method", ["部分采集", "全量采集"])
        self.method_segment = seg
        self.m_quick_btn.toggled.connect(self._on_method_changed)
        head.addWidget(seg)
        head.addStretch(1)

        self.material_preferences_wrap = QFrame()
        self.material_preferences_wrap.setObjectName("collectionMaterialPreferences")
        self.material_preferences_wrap.setStyleSheet(
            "QFrame#collectionMaterialPreferences{background:transparent;border:none;}"
        )
        self.material_preferences_layout = QGridLayout(
            self.material_preferences_wrap
        )
        self.material_preferences_layout.setContentsMargins(0, 0, 0, 0)
        self.material_preferences_layout.setHorizontalSpacing(14)
        self.material_preferences_layout.setVerticalSpacing(0)
        self.save_media_cb = LuluCheckBox(
            "音视频文件", accessible_name="主页采集音视频文件"
        )
        self.save_media_cb.set_consumer_accent(self._consumer.accent)
        self.save_media_cb.setToolTip("为随后发起的新采集任务保存完整视频")
        self.save_media_cb.toggled.connect(self._on_save_media_toggled)
        self.cover_cb = LuluCheckBox(
            "封面文件",
            accessible_name="主页采集封面文件",
        )
        self.cover_cb.set_consumer_accent(self._consumer.accent)
        self.cover_cb.setToolTip("只影响随后启动的新采集任务")
        self.cover_cb.toggled.connect(self._on_cover_preference_toggled)
        self._layout_material_preferences(720)
        head.addWidget(self.material_preferences_wrap, 0, Qt.AlignRight | Qt.AlignVCenter)
        pv.addLayout(head)

        pv.addSpacing(14)
        pv.addWidget(self._hr())
        pv.addSpacing(16)

        self.quick_panel = self._build_quick_panel()
        self.full_panel = self._build_full_panel()
        pv.addWidget(self.quick_panel)
        pv.addWidget(self.full_panel)
        self.full_panel.hide()       # 默认「抓取最新」
        return panel

    def _pick_browser(self, name: str, path) -> None:
        self._browser_name = name
        self._browser_path = path
        self.browser_label.setText(name)

    def _layout_material_preferences(self, available_width: int) -> None:
        """两个素材开关始终同驻采集卡右上角。"""
        layout = self.material_preferences_layout
        widgets = (self.save_media_cb, self.cover_cb)
        for widget in widgets:
            layout.removeWidget(widget)
        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 0)
        layout.addWidget(self.save_media_cb, 0, 0)
        layout.addWidget(self.cover_cb, 0, 1)

    def _current_platform(self) -> str:
        platform = getattr(getattr(self, "_entry_state", None), "identity", None)
        return getattr(platform, "platform", "unknown")

    def _material_snapshot(self) -> MaterialPreferenceSnapshot:
        try:
            snapshot = self._material_preferences.snapshot()
        except Exception:
            snapshot = None
        if isinstance(snapshot, MaterialPreferenceSnapshot):
            return snapshot
        return MaterialPreferenceSnapshot(
            download_mode=(
                "video"
                if getattr(self.cfg, "link_media_mode", "audio") == "video"
                else "audio"
            ),
            save_media=bool(getattr(self.cfg, "keep_media", False)),
            save_cover=bool(getattr(self.cfg, "collect_cover", False)),
        )

    def _material_download_snapshot(self) -> MaterialPreferenceSnapshot:
        """主页只冻结两个开关，音视频素材始终是完整视频。"""
        snapshot = self._material_snapshot()
        return MaterialPreferenceSnapshot(
            download_mode="video",
            save_media=snapshot.save_media,
            save_cover=snapshot.save_cover,
        )

    def _sync_material_preferences(self, snapshot) -> None:
        if not isinstance(snapshot, MaterialPreferenceSnapshot):
            snapshot = self._material_snapshot()
        if not hasattr(self, "cover_cb"):
            return
        for control in (self.save_media_cb, self.cover_cb):
            control.blockSignals(True)
        self.save_media_cb.setChecked(snapshot.save_media)
        self.cover_cb.setChecked(snapshot.save_cover)
        for control in (self.save_media_cb, self.cover_cb):
            control.blockSignals(False)

    def _sync_cover_preference(self, enabled: bool) -> None:
        snapshot = self._material_snapshot()
        self._sync_material_preferences(MaterialPreferenceSnapshot(
            snapshot.download_mode, snapshot.save_media, bool(enabled)
        ))

    def _settle_material_preference(self, receipt) -> bool:
        if receipt.success:
            self._sync_material_preferences(receipt.current_snapshot)
            self.status.setText("已同步到内容提取和主页采集")
            return True
        self._sync_material_preferences(receipt.previous_snapshot)
        dialogs.alert(
            self,
            "设置未保存",
            "设置没有保存，两处仍按原设置执行。",
        )
        return False

    def _on_save_media_toggled(self, enabled: bool) -> None:
        if self._material_preferences is None:
            return
        self._settle_material_preference(
            self._material_preferences.set(save_media=bool(enabled))
        )

    def _on_cover_preference_toggled(self, enabled: bool) -> None:
        if self._material_preferences is None:
            self._sync_cover_preference(False)
            dialogs.alert(
                self,
                "设置未保存",
                "封面下载尚未准备好，本次仍按关闭状态执行。",
            )
            return
        try:
            receipt = self._material_preferences.set(save_cover=bool(enabled))
        except TypeError:  # 0.2.5 fixture 注入面
            from ..core.collect.preferences import MaterialPreferenceReceipt
            previous = self._material_snapshot()
            legacy = self._material_preferences.set(bool(enabled))
            current = self._material_snapshot()
            receipt = MaterialPreferenceReceipt(
                legacy.success,
                legacy.code,
                previous,
                current if legacy.success else previous,
                legacy.changed,
                legacy.notification_failures,
            )
        self._settle_material_preference(receipt)

    def _collect_running(self) -> bool:
        return any(
            worker is not None and worker.isRunning()
            for worker in (self._worker, self._full_worker)
        )

    def _xhs_release_disabled(self) -> bool:
        entry = getattr(self, "_entry_state", None)
        return bool(
            entry is not None
            and entry.identity.platform == "xiaohongshu"
            and entry.capability.reason == "release_disabled"
        )

    def _sync_xhs_browser_status(
        self,
        status: XhsBrowserStatus | None = None,
    ) -> XhsBrowserStatus:
        if status is None:
            getter = getattr(self._service, "xhs_browser_status", None)
            status = (
                getter()
                if callable(getter)
                else XhsBrowserStatus()
            )
        if hasattr(self, "xhs_browser_status"):
            self.xhs_browser_status.setText(status.message)
        busy = self._collect_running() or status.busy
        if hasattr(self, "xhs_close_btn"):
            self.xhs_close_btn.setEnabled(status.running and not busy)
        if hasattr(self, "xhs_login_btn"):
            self.xhs_login_btn.setEnabled(not busy)
        return status

    def _on_input_changed(self, text: str) -> None:
        """纯本地识别平台；不得在粘贴或页面构造时启动 adapter/browser。"""
        pending=getattr(self,'_workspace_request',None)
        if pending and pending[0]!=text.strip():
            self._workspace_request=None
        self._entry_state = self._service.inspect(text)
        platform = self._current_platform()
        busy = self._collect_running()
        self.grab_btn.show()

        if not str(text or "").strip():
            self.platform_status.setText("")
            platform = "douyin"  # 空输入保持既有抖音默认界面，不代表已识别

        if platform == "douyin":
            self.platform_status.setText("抖音" if str(text or "").strip() else "")
            self.method_title.setText(
                "抖音主页采集"
                if self._xhs_homepage_release_disabled
                else "采集方式"
            )
            self.method_segment.show()
            self.quick_summary.setText("免登录 · 零风险 · 抓该作者最新一页作品（约 15–20 条）")
            self.quick_tip.setText(
                XHS_RELEASE_DISABLED_MESSAGE
                if self._xhs_homepage_release_disabled
                else "适合快速看一眼作者最近发了什么。"
            )
            self.grab_btn.setText(
                "开始抖音部分采集"
                if self._xhs_homepage_release_disabled
                else "开始部分采集"
            )
            self.grab_btn.setEnabled(not busy)
            self.xhs_login_btn.hide()
            self.xhs_close_btn.hide()
            self.xhs_browser_status.hide()
            self._on_method_changed()
            return

        self.method_segment.hide()
        self.quick_panel.show()
        self.full_panel.hide()
        if platform == "xiaohongshu":
            if self._xhs_release_disabled():
                self.platform_status.setText("小红书 · 本版暂缓")
                self.method_title.setText("小红书主页采集")
                self.quick_summary.setText(XHS_RELEASE_DISABLED_MESSAGE)
                self.quick_tip.setText("已有采集任务、本地筛选和导出仍可继续使用。")
                self.grab_btn.setText("本版暂缓")
                self.grab_btn.setEnabled(False)
                self.grab_btn.hide()
                self.xhs_login_btn.hide()
                self.xhs_close_btn.hide()
                self.xhs_browser_status.hide()
                return
            exact_homepage = self._entry_state.identity.kind == "homepage"
            self.platform_status.setText("小红书")
            self.method_title.setText("小红书公开主页")
            self.quick_summary.setText("使用 Lulu 专用小红书浏览器 · 最多两轮 / 20 条")
            self.quick_tip.setText("只读取公开主页；登录、验证码和频控都会明确停下并保留结果。")
            self.grab_btn.setText("开始小红书采集")
            self.grab_btn.setEnabled(exact_homepage and not busy)
            self.xhs_login_btn.setVisible(True)
            self.xhs_login_btn.setEnabled(not busy)
            self.xhs_close_btn.setVisible(True)
            self.xhs_browser_status.setVisible(True)
            self._sync_xhs_browser_status()
            return

        if platform == "bilibili":
            self.platform_status.setText("B站 · 暂不可用")
            self.method_title.setText("B站主页采集")
            self.quick_summary.setText("B站主页采集暂不可用")
            self.quick_tip.setText("现有单链接转录能力不受影响。")
        else:
            self.platform_status.setText("未识别")
            self.method_title.setText("识别链接")
            self.quick_summary.setText("请粘贴一个受支持的公开主页链接")
            self.quick_tip.setText("一次只放一个链接；不会联网猜测平台。")
        self.grab_btn.setText("暂不可用")
        self.grab_btn.setEnabled(False)
        self.xhs_login_btn.hide()
        self.xhs_close_btn.hide()
        self.xhs_browser_status.hide()

    def _on_method_changed(self, _checked=False) -> None:
        # 空输入沿用页面既有的抖音默认入口；此时也必须允许用户先选择并保存模式。
        # 已识别为其他平台时，平台自己的能力面板继续保持冻结。
        if self.input.text().strip() and self._current_platform() != "douyin":
            self.quick_panel.show()
            self.full_panel.hide()
            return
        requested = "partial" if self.m_quick_btn.isChecked() else "full"
        # An explicit user mode change ends the one-off scope from the document.
        if requested=='full':
            self._workspace_request=None
        if requested == self._selected_mode:
            self._render_collection_mode_state()
            return
        previous = self._selected_mode
        cfg = self.cfg
        if cfg is not None:
            setattr(cfg, "collection_mode", requested)
            saver = getattr(cfg, "save", None)
            if callable(saver) and not saver():
                setattr(cfg, "collection_mode", previous)
                self._set_method_buttons(previous)
                self._render_collection_mode_state()
                self.status.setText(
                    "采集方式未保存，仍使用"
                    + ("部分采集。" if previous == "partial" else "全量采集。")
                )
                return
        self._selected_mode = requested
        self._render_collection_mode_state()

    def _set_method_buttons(self, mode: str) -> None:
        partial = mode != "full"
        self.m_quick_btn.blockSignals(True)
        self.m_full_btn.blockSignals(True)
        self.m_quick_btn.setChecked(partial)
        self.m_full_btn.setChecked(not partial)
        self.m_quick_btn.blockSignals(False)
        self.m_full_btn.blockSignals(False)

    def _render_collection_mode_state(self) -> None:
        """从单一 selected_mode 同步说明、配置与唯一主动作。"""
        if self.input.text().strip() and self._current_platform() != "douyin":
            self.quick_panel.show()
            self.full_panel.hide()
            return
        partial = self._selected_mode == "partial"
        self._set_method_buttons(self._selected_mode)
        self.quick_panel.setVisible(partial)
        self.full_panel.setVisible(not partial)
        running = bool(self._active_collection_mode)
        for control in (self.m_quick_btn, self.m_full_btn):
            control.setEnabled(not running)
        if partial:
            self.quick_summary.setText(
                "无需登录，采集当前可获得的最新公开作品；速度较快、风险较低。"
            )
            self.quick_tip.setText(
                XHS_RELEASE_DISABLED_MESSAGE
                if self._xhs_homepage_release_disabled
                else "公开方式 · 无需打开专用浏览器"
            )
            text = (
                "正在停止…"
                if self._stopping_collection
                else "停止部分采集"
                if self._active_collection_mode == "partial"
                else "开始抖音部分采集"
                if self._xhs_homepage_release_disabled
                else "开始部分采集"
            )
            self.grab_btn.setText(text)
            self.grab_btn.setAccessibleName(text)
            self.grab_btn.setEnabled(not self._stopping_collection)
        else:
            shared = self._douyin_login_state
            logged = shared()[0] in {'已登录', '已保存浏览器登录'} if shared else is_logged_in()
            text = (
                "正在停止…"
                if self._stopping_collection
                else "停止全量采集"
                if self._active_collection_mode == "full"
                else "开始全量采集"
                if logged
                else "先登录专用浏览器"
            )
            self.full_btn.setText(text)
            self.full_btn.setAccessibleName(text)
            self.full_btn.setEnabled(not self._stopping_collection)

    def _build_quick_panel(self) -> QWidget:
        # 两栏：左说明 / 右开始按钮
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(28)

        left = QVBoxLayout()
        left.setSpacing(6)
        left.addStretch(1)
        self.quick_summary = QLabel("免登录 · 零风险 · 抓该作者最新一页作品（约 15–20 条）")
        self.quick_summary.setObjectName("taskSrc")
        self.quick_summary.setWordWrap(True)
        self.quick_summary.setAlignment(Qt.AlignCenter)
        left.addWidget(self.quick_summary)
        self.quick_tip = QLabel("适合快速看一眼作者最近发了什么。")
        self.quick_tip.setObjectName("taskSrc")
        self.quick_tip.setWordWrap(True)
        self.quick_tip.setAlignment(Qt.AlignCenter)
        left.addWidget(self.quick_tip)
        left.addStretch(1)
        h.addLayout(left, 1)

        h.addWidget(self._vline())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.addStretch(1)
        self.grab_btn = self._primary("开始部分采集", self._on_grab)
        self.grab_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.grab_btn.setMinimumHeight(46)
        right.addWidget(self.grab_btn)
        self.xhs_login_btn = QPushButton("打开专用浏览器登录")
        adopt_button(
            self.xhs_login_btn,
            role="secondary",
            size="sm",
            accessible_name="打开小红书专用浏览器登录",
        )
        self.xhs_login_btn.clicked.connect(self._on_xhs_login)
        self.xhs_login_btn.hide()
        right.addSpacing(8)
        right.addWidget(self.xhs_login_btn)
        self.xhs_close_btn = QPushButton("关闭小红书专用浏览器")
        adopt_button(
            self.xhs_close_btn,
            role="secondary",
            size="sm",
            accessible_name="关闭小红书专用浏览器",
        )
        self.xhs_close_btn.clicked.connect(self._on_xhs_close)
        self.xhs_close_btn.hide()
        right.addSpacing(6)
        right.addWidget(self.xhs_close_btn)
        self.xhs_browser_status = QLabel()
        self.xhs_browser_status.setObjectName("taskSrc")
        self.xhs_browser_status.setWordWrap(True)
        self.xhs_browser_status.hide()
        right.addSpacing(6)
        right.addWidget(self.xhs_browser_status)
        right.addStretch(1)
        h.addLayout(right, 1)
        return w

    def _build_full_panel(self) -> QWidget:
        # 两栏：左 = 配置分组列表；右 = 风险提示 + 开始按钮
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(28)

        # —— 左栏：分组设置列表 ——
        left = QVBoxLayout()
        left.setSpacing(0)

        self.login_dot = QLabel()
        self.login_dot.setFixedSize(9, 9)
        self.login_status = QLabel()
        self.login_status.setStyleSheet(f"color:{self._consumer.ink_soft};font-size:13px;background:transparent;")
        self.login_btn = QPushButton()
        self.login_btn.setObjectName("act")
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.login_btn.clicked.connect(self._on_login)
        self.clear_login_btn = QPushButton("清除登录")
        self.clear_login_btn.setObjectName("act")
        self.clear_login_btn.setCursor(Qt.PointingHandCursor)
        self.clear_login_btn.setToolTip("删掉本机记住的抖音登录，回到未登录状态")
        self.clear_login_btn.clicked.connect(self._on_clear_login)
        left.addWidget(self._row("登录状态", self.login_btn, self.clear_login_btn,
                                 left=[self.login_dot, self.login_status]))
        left.addWidget(self._hr())

        # 浏览器选择：自绘字段（QFrame + QLabel），文字用布局垂直居中——
        # 避开 macOS 原生 QComboBox/QPushButton 框内文字顶对齐、无视 QSS 垂直居中的顽疾。点击弹菜单。
        browsers = _detect_browsers()
        self._browser_name, self._browser_path = browsers[0]
        field = QFrame()
        field.setObjectName("browserField")
        field.setToolTip("全量采集用的浏览器（单独一份数据，不动你平时用的浏览器）")
        field.setCursor(Qt.PointingHandCursor)
        field.setFixedSize(150, 34)
        field.setStyleSheet(
            f"QFrame#browserField{{background:{self._consumer.surface_focal};border:1px solid {self._consumer.line};border-radius:9px;}}"
            f"QFrame#browserField:hover{{border:1px solid {self._consumer.accent_hairline};}}"
            f"QFrame#browserField QLabel{{background:transparent;border:none;}}"
        )
        fl = QHBoxLayout(field)
        fl.setContentsMargins(10, 0, 8, 0)
        fl.setSpacing(6)
        self.browser_label = QLabel(self._browser_name)
        self.browser_label.setStyleSheet(f"color:{self._consumer.ink};")
        fl.addWidget(self.browser_label, 1)
        arrow = QLabel("▾")
        arrow.setStyleSheet(f"color:{self._consumer.ink_muted};font-size:11px;")
        fl.addWidget(arrow, 0)
        bmenu = QMenu(field)
        adopt_consumer_theme_scope(bmenu, "coreWorkspace")
        bmenu.setStyleSheet(
            f"QMenu{{background:{self._consumer.surface_floating};border:1px solid {self._consumer.line};border-radius:10px;padding:6px;}}"
            f"QMenu::item{{padding:7px 16px;border-radius:7px;color:{self._consumer.ink};}}"
            f"QMenu::item:selected{{background:{self._consumer.accent_wash};color:{self._consumer.accent};}}"
        )
        for name, path in browsers:
            act = bmenu.addAction(name)
            act.triggered.connect(lambda _c=False, n=name, p=path: self._pick_browser(n, p))
        self._browser_menu = bmenu
        field.mousePressEvent = lambda _e, f=field, m=bmenu: m.exec(f.mapToGlobal(f.rect().bottomLeft()))
        self.browser_field = field
        left.addWidget(self._row("浏览器", field))
        h.addLayout(left, 1)

        h.addWidget(self._vline())

        # —— 右栏：风险提示（上） + 开始按钮（右下）——
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addStretch(1)
        risk = QLabel("全量采集有一定的平台风控风险，建议登录小号。")
        risk.setWordWrap(True)
        risk.setStyleSheet(f"color:{self._consumer.ink_soft};font-size:12px;background:transparent;")
        right.addWidget(risk)
        right.addSpacing(8)
        self.collection_hint = QLabel(
            "默认快速全量采集；只有出现真实可见验证时，"
            "才会停止并保留已采结果。"
        )
        self.collection_hint.setWordWrap(True)
        self.collection_hint.setStyleSheet(
            f"color:{T.AMBER};background:transparent;padding:0;font-size:12px;"
        )
        right.addWidget(self.collection_hint)
        right.addSpacing(14)
        self.full_btn = self._primary("开始全量采集", self._on_full_primary)
        self.full_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.full_btn.setMinimumHeight(46)
        right.addWidget(self.full_btn)
        right.addStretch(1)
        h.addLayout(right, 1)
        return w

    def _on_full_primary(self) -> None:
        if self._active_collection_mode == "full":
            self._on_full_grab()
            return
        shared = self._douyin_login_state
        logged = shared()[0] in {'已登录', '已保存浏览器登录'} if shared else is_logged_in()
        if not logged:
            self._on_login()
            return
        self._on_full_grab()

    def _build_table(self) -> None:
        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(150)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setItemDelegate(_CollectionRowDelegate(self._consumer, self.table))
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellClicked.connect(self._on_table_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        self.table.verticalHeader().setDefaultSectionSize(58)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setHighlightSections(False)
        hh.setSectionsClickable(True)
        hh.setSectionsMovable(False)
        hh.setCascadingSectionResizes(False)
        hh.setMinimumSectionSize(34)
        hh.sectionClicked.connect(self._on_sort_clicked)
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 34)
        for ci, w in {
            1: 150,
            2: 320,
            3: 120,
            4: 72,
            5: 72,
            6: 72,
            7: 72,
            8: 72,
            9: 80,
            10: 80,
            11: 80,
        }.items():
            hh.setSectionResizeMode(ci, QHeaderView.Interactive)
            self.table.setColumnWidth(ci, w)
        hh.sectionResized.connect(lambda *_: self._fit_collection_columns())
        for ci in (3, 4, 5, 6, 7, 8, 9, 10, 11):
            self.table.horizontalHeaderItem(ci).setTextAlignment(Qt.AlignCenter)
        self.table.setStyleSheet(
            f"QTableWidget{{background:{self._consumer.surface_focal};border:1px solid {self._consumer.line};"
            f"border-radius:14px;gridline-color:{self._consumer.line};}}"
            f"QHeaderView::section{{background:{self._consumer.surface_work};color:{self._consumer.ink_soft};"
            f"border:none;border-right:1px solid {self._consumer.line};"
            f"border-bottom:1px solid {self._consumer.line};padding:10px 10px;font-weight:600;}}"
            f"QHeaderView::section:hover{{color:{self._consumer.accent};"
            f"border-right-color:{self._consumer.accent_hairline};}}"
            f"QTableWidget::item{{padding:8px 10px;color:{self._consumer.ink};"
            f"border-bottom:1px solid {self._consumer.line};}}"
        )

    def _fit_collection_columns(self) -> None:
        if getattr(self, '_fitting_columns', False):
            return
        self._fitting_columns = True
        try:
            table = self.table
            other = sum(table.columnWidth(i) for i in range(table.columnCount()) if i != 2)
            # Title takes spare space. Keep a readable minimum on narrow windows;
            # the existing horizontal scrollbar handles the remainder.
            width = max(240, table.viewport().width() - other)
            if table.columnWidth(2) != width:
                table.setColumnWidth(2, width)
        finally:
            self._fitting_columns = False

    # ---------------- 排序（点击表头） ----------------
    def _on_sort_clicked(self, col: int) -> None:
        if col not in _SORT_ATTR:
            return
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = _SORT_DESC_DEFAULT.get(col, True)
        self._render()

    def _on_sort_combo(self) -> None:
        col = self.sort_combo.currentData()
        if col is None:
            return
        try:
            col = int(col)
        except (TypeError, ValueError):
            return
        if col != self._sort_col:
            self._sort_col = col
            self._sort_desc = _SORT_DESC_DEFAULT.get(col, True)
        self._render()

    def _toggle_dir(self) -> None:
        self._sort_desc = not self._sort_desc
        self._render()

    def _sync_sort_combo(self) -> None:
        self.sort_combo.blockSignals(True)
        for i in range(self.sort_combo.count()):
            if self.sort_combo.itemData(i) == str(self._sort_col):
                self.sort_combo.setCurrentIndex(i)
                break
        self.sort_combo.blockSignals(False)
        self.dir_btn.setText("↓ 降序" if self._sort_desc else "↑ 升序")

    def _update_headers(self) -> None:
        for c in range(len(_COLS)):
            label = _COLS[c]
            if c == self._sort_col and c in _SORT_ATTR:
                label = f"{label} {'↓' if self._sort_desc else '↑'}"
            self.table.horizontalHeaderItem(c).setText(label)

    def _filter_field_data(self) -> tuple[str | None, str]:
        data = str(self.metric_combo.currentData() or "")
        if data == "__none__":
            return None, "none"
        field_type = _FILTER_FIELD_TYPES.get(data)
        if field_type is None:
            return None, "none"
        return data, field_type

    def _set_filter_error(self, message: str = "") -> None:
        failed = bool(message)
        self.metric_value.setProperty("state", "error" if failed else "default")
        self.metric_value.setToolTip(message or "未获取值不参与筛选")
        self.metric_value.setAccessibleDescription(message)
        self.metric_value.style().unpolish(self.metric_value)
        self.metric_value.style().polish(self.metric_value)
        self.metric_value.update()

    def _on_filter_field_changed(self, *_args) -> None:
        self._set_filter_error()
        field_name, field_type = self._filter_field_data()
        self.operator_combo.blockSignals(True)
        self.operator_combo.clear()
        for label, value in _FILTER_OPERATORS.get(field_type, ()):
            self.operator_combo.addItem(label, value)
        if self.operator_combo.count():
            default_index = 0
        else:
            default_index = -1
        if field_type == "numeric":
            # 产品合同规定数值字段默认「大于等于」，同时仍完整保留六种运算符。
            numeric_default = self.operator_combo.findData("ge")
            if numeric_default >= 0:
                default_index = numeric_default
        if default_index >= 0:
            self.operator_combo.setCurrentIndex(default_index)
        self.operator_combo.blockSignals(False)
        self.metric_value.blockSignals(True)
        self.metric_value.clear()
        self.metric_value.blockSignals(False)
        self.enum_value_combo.blockSignals(True)
        self.enum_value_combo.clear()
        for label, value in _ENUM_FILTER_VALUES.get(str(field_name), ()):
            self.enum_value_combo.addItem(label, value)
        if self.enum_value_combo.count():
            self.enum_value_combo.setCurrentIndex(0)
        self.enum_value_combo.blockSignals(False)
        enabled = field_type != "none"
        self.operator_combo.setVisible(enabled)
        self.metric_value.setVisible(field_type in {"numeric", "text"})
        self.enum_value_combo.setVisible(field_type == "enum")
        if field_type == "numeric":
            self.metric_value.setPlaceholderText(
                "输入秒数" if field_name == "duration_sec" else "输入整数"
            )
        elif field_type == "text":
            self.metric_value.setPlaceholderText("输入文字")
        if not enabled:
            self._active_field_filter = None
            self._render()
            return
        if field_type == "enum" and self.enum_value_combo.count():
            self._apply_filter_editor()
            return
        # 字段切换时不把旧值解释成新类型；在新值合法前保留上一次有效结果。
        self._render()

    def _apply_filter_editor(self) -> None:
        field_name, field_type = self._filter_field_data()
        operator = self.operator_combo.currentData()
        if not field_name or field_type == "none" or not operator:
            return
        if field_type == "enum":
            value = self.enum_value_combo.currentData()
            if value is None:
                return
        else:
            raw = self.metric_value.text().strip()
            if not raw:
                self._set_filter_error()
                self._active_field_filter = None
                self._render()
                return
            if field_type == "numeric":
                try:
                    value = float(raw)
                except ValueError:
                    self._set_filter_error("请输入有效的非负数字")
                    return
                if not math.isfinite(value) or value < 0 or (
                    field_name != "duration_sec" and not raw.isdecimal()
                ):
                    self._set_filter_error("请输入有效的非负数字")
                    return
            else:
                value = raw.casefold()
        self._set_filter_error()
        self._active_field_filter = (
            str(field_name), str(field_type), str(operator), value
        )
        self._render()

    @staticmethod
    def _matches_field_filter(item, condition) -> bool:
        field_name, field_type, operator, expected = condition
        if field_type == "numeric":
            if field_name == "duration_sec":
                actual = item.known_numeric("duration_ms")
                actual = None if actual is None else actual / 1000
            else:
                actual = item.known_numeric(field_name)
            if actual is None:
                return False
            return {
                "gt": actual > expected,
                "ge": actual >= expected,
                "eq": actual == expected,
                "ne": actual != expected,
                "lt": actual < expected,
                "le": actual <= expected,
            }[operator]
        raw = getattr(item, field_name, None)
        if raw is None or raw == "" or (
            field_name == "content_kind" and raw == "unknown"
        ):
            return False
        actual = str(raw).casefold()
        if field_type == "text":
            return {
                "contains": expected in actual,
                "not_contains": expected not in actual,
                "eq": actual == expected,
                "ne": actual != expected,
            }[operator]
        return actual == expected if operator == "eq" else actual != expected

    # ---------------- 渲染（筛选+排序） ----------------
    def _render(self) -> None:
        items = self._items
        # 关键词筛选（标题/作者）
        kw = self.filter_edit.text().strip().lower()
        if kw:
            items = [it for it in items
                     if kw in (it.title or "").lower() or kw in (it.author_name or "").lower()]
        bounds = self._active_date_bounds()
        if bounds is not None:
            lower, upper = bounds
            items = [
                it
                for it in items
                if it.field_state("create_time") == "known"
                and it.known_numeric("create_time") is not None
                and lower <= float(it.known_numeric("create_time")) < upper
            ]
        if self._active_field_filter is not None:
            items = [
                item
                for item in items
                if self._matches_field_filter(item, self._active_field_filter)
            ]
        # 排序
        attr = _SORT_ATTR.get(self._sort_col, "create_time")
        if attr in _TRUTHFUL_FIELDS:
            known = [it for it in items if it.known_numeric(attr) is not None]
            unknown = [it for it in items if it.known_numeric(attr) is None]
            self._view = (
                sorted(known, key=lambda it: getattr(it, attr), reverse=self._sort_desc)
                + unknown
            )
        else:
            self._view = sorted(
                items, key=lambda it: getattr(it, attr), reverse=self._sort_desc
            )
        self._fill_table()
        self._update_headers()
        self._sync_sort_combo()
        self._update_count()
        self._update_tools_enabled()
        self._update_empty_state()

    def _fill_table(self, *, status_only=False) -> None:
        self.table.blockSignals(True)
        if not status_only:
            self.table.setRowCount(len(self._view))
        try:
            runtime_states = dict(self._collection_task_states() or {})
        except Exception:  # noqa: BLE001 - 运行态只读出口失败时回落持久真值
            runtime_states = {}
        for r, it in enumerate(self._view):
            if not status_only:
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setData(Qt.UserRole, it.stable_key)
                chk.setCheckState(
                    Qt.Checked if it.stable_key in self._selected_keys else Qt.Unchecked
                )
                self.table.setItem(r, 0, chk)
                cells = [
                    (it.author_name, ""),
                    (it.title, ""),
                    self._truthful_cell(it, "create_time"),
                    self._truthful_cell(it, "duration_ms"),
                    self._truthful_cell(it, "like"),
                    self._truthful_cell(it, "collect"),
                    self._truthful_cell(it, "comment"),
                    self._truthful_cell(it, "share"),
                ]
                columns = (1, 2, 3, 4, 5, 6, 7, 8)
                for c, (text, tooltip) in zip(columns, cells):
                    item = QTableWidgetItem(text)
                    if c >= 3:
                        item.setTextAlignment(Qt.AlignCenter)
                    if c in (1, 2):
                        item.setToolTip(text)
                    elif tooltip:
                        item.setToolTip(tooltip)
                    self.table.setItem(r, c, item)
            runtime = runtime_states.get(it.stable_key)
            material_runtime = self._material_runtime.get(it.stable_key, {})
            transcript_runtime = str(getattr(runtime, "transcript", "") or "")
            media_runtime = str(
                material_runtime.get("media")
                or getattr(runtime, "media", "")
                or ""
            )
            done = (it.stable_key in self._transcribed
                    or bool(it.aweme_id and it.aweme_id in self._transcribed))
            transcript_text = (
                "✓"
                if done
                else {
                    "queued": "排队中",
                    "transcribing": "转录中",
                    "failed": "已失败",
                }.get(transcript_runtime, "未转录")
            )
            self._set_status_cell(
                r,
                _TRANSCRIBED_COL,
                transcript_text,
                (
                    '已存在本地文稿'
                    if transcript_text == '✓' else
                    '转录已失败，可重试'
                    if transcript_text == '已失败' else
                    '尚未开始转录'
                    if transcript_text == '未转录'
                    else ''
                ),
            )

            if it.is_image:
                media_text = "图文"
            else:
                media_text = {
                    "queued": "排队中",
                    "downloading": "下载中",
                    "failed": "已失败",
                }.get(media_runtime, "")
                if not media_text:
                    if self._existing_media_file(it) is not None:
                        media_text = "✓"
                    elif it.media_status == "downloaded":
                        media_text = "需补下"
                    else:
                        media_text = "未下载"
            self._set_status_cell(
                r,
                _MEDIA_COL,
                media_text,
                (
                    '本地受管音视频文件存在；双击此格可在 Finder 中显示。'
                    if media_text == '✓' else
                    '这份音视频已不在 Lulu 管理的位置，可以重新下载所选素材。'
                    if media_text == '需补下' else
                    '尚未下载这份音视频'
                    if media_text == '未下载' else
                    '图文作品没有可保留的音视频'
                    if media_text == '图文'
                    else ''
                ),
            )

            cover_runtime = str(material_runtime.get("cover") or "")
            cover_text = {
                "queued": "排队中",
                "downloading": "下载中",
                "failed": "已失败",
            }.get(cover_runtime, "")
            if not cover_text:
                if it.cover_status == "pending":
                    cover_text = "下载中"
                elif (
                    it.cover_status == "failed"
                    or str(it.last_error_code or "").endswith(
                        "cover_commit_uncertain"
                    )
                ):
                    cover_text = "已失败"
                elif self._managed_cover_file(it) is not None:
                    cover_text = "✓"
                elif it.cover_status == "downloaded":
                    cover_text = "需补下"
                else:
                    cover_text = "未下载"
            self._set_status_cell(
                r,
                _COVER_COL,
                cover_text,
                (
                    '本地封面文件存在；双击此格可在 Finder 中显示。'
                    if cover_text == '✓' else
                    '这份封面已不在 Lulu 管理的位置，可以重新下载所选素材。'
                    if cover_text == '需补下' else
                    '尚未下载这份封面'
                    if cover_text == '未下载'
                    else ''
                ),
            )
        if not status_only:
            self._sync_row_selection_visuals()
        self.table.blockSignals(False)
        visible_keys = {item.stable_key for item in self._view}
        self.select_all_btn.setText(
            "取消可见全选"
            if visible_keys and visible_keys <= self._selected_keys
            else "全选"
        )

    def _set_status_cell(self, row, column, text, tooltip=""):
        previous = self.table.item(row, column)
        if previous is not None and previous.text() == text and previous.toolTip() == tooltip:
            return
        item = self._short_status_item(text, tooltip)
        check = self.table.item(row, 0)
        key = str(check.data(Qt.UserRole) or "") if check is not None else ""
        item.setData(_ROW_SELECTED_ROLE, key in self._selected_keys)
        self.table.setItem(row, column, item)

    def _short_status_item(self, text: str, tooltip: str = "") -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        if text == "✓":
            item.setForeground(QColor(T.GREEN))
        elif text in {"需补下", "已失败"}:
            item.setForeground(QColor(self._consumer.danger))
        elif text in {"排队中", "转录中", "下载中"}:
            item.setForeground(QColor(self._consumer.accent))
        else:
            item.setForeground(QColor(self._consumer.ink_muted))
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def _active_date_bounds(self) -> tuple[float, float] | None:
        if self._date_mode == "all":
            return None
        today = _beijing_today()
        if self._date_mode in {"7", "30", "90"}:
            days = int(self._date_mode)
            return _beijing_day_bounds(today - timedelta(days=days - 1), today)
        if self._custom_date_range is None:
            return None
        return _beijing_day_bounds(*self._custom_date_range)

    def _on_date_mode_changed(self, value: str) -> None:
        self._date_mode = str(value or "all")
        custom = self._date_mode == "custom"
        self.date_start.setVisible(custom)
        self.date_connector.setVisible(custom)
        self.date_end.setVisible(custom)
        if custom and self._custom_date_range is None:
            self._commit_custom_date_range()
        self._render()

    def _commit_custom_date_range(self) -> None:
        start = self.date_start.date().toPython()
        end = self.date_end.date().toPython()
        self._custom_date_range = (start, end)
        if self._date_mode == "custom":
            self._render()

    def _on_custom_start_changed(self, *_args) -> None:
        if self._syncing_custom_dates:
            return
        if self.date_start.date() > self.date_end.date():
            self._syncing_custom_dates = True
            try:
                self.date_end.setDate(self.date_start.date())
            finally:
                self._syncing_custom_dates = False
        self._commit_custom_date_range()

    def _on_custom_end_changed(self, *_args) -> None:
        if self._syncing_custom_dates:
            return
        if self.date_end.date() < self.date_start.date():
            self._syncing_custom_dates = True
            try:
                self.date_start.setDate(self.date_end.date())
            finally:
                self._syncing_custom_dates = False
        self._commit_custom_date_range()

    def _on_custom_date_changed(self, *_args) -> None:
        """Compatibility shim for older focused tests and internal callers."""
        self._on_custom_start_changed()

    @staticmethod
    def _truthful_cell(it, name: str) -> tuple[str, str]:
        state = it.field_state(name)
        if state == "unknown":
            return "—", _UNKNOWN_TOOLTIPS[name]
        if state == "not_applicable":
            return ("图文", "") if name == "duration_ms" else ("—", _UNKNOWN_TOOLTIPS[name])
        value = it.known_numeric(name)
        if name == "create_time":
            if value == 0 and it.has_field_state(name):
                return "0", ""
            return it.publish_date, ""
        if name == "duration_ms":
            if value == 0 and it.has_field_state(name):
                return "0", ""
            return it.duration_text, ""
        return str(value), ""

    @staticmethod
    def _has_unknown_fields(items) -> bool:
        return any(
            it.field_state(name) == "unknown"
            for it in items
            for name in _TRUTHFUL_FIELDS
        )

    def _data_location_path(self, key: str) -> Path:
        repository = self._history_repo or CollectionHistoryRepository()
        if not hasattr(repository, "path"):
            repository = CollectionHistoryRepository()
        app_data_root = repository.path.parent.parent
        fallback = str(Path.home())
        return {
            "collection": app_data_root / "collection",
            "covers": app_data_root / "collection" / "covers",
            "library": Path(
                str(getattr(self.cfg, "last_output_dir", fallback))
            ).expanduser(),
            "media": Path(
                app_data_root / "collection" / "media"
            ),
        }[key]

    def _refresh_data_location_contexts(self) -> None:
        for key, _label in _DATA_LOCATION_OPTIONS:
            target = self._data_location_path(key)
            self.data_location_select.set_action_context(
                key,
                "Lulu 管理" if key == "media" else str(target),
                configurable=key == "library",
            )

    def _set_data_location_feedback(self, message: str) -> None:
        self.data_location_select.trigger.setToolTip(message)
        self.data_location_select.trigger.setAccessibleDescription(message)

    def _open_data_location(self, key: str) -> None:
        try:
            target = self._data_location_path(key)
            if key in {"collection", "covers"}:
                target.mkdir(parents=True, exist_ok=True)
            elif not target.is_dir():
                raise OSError("configured data location is unavailable")
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        except (KeyError, OSError, ValueError):
            opened = False
        self._set_data_location_feedback(
            "已打开数据位置" if opened else "数据位置打开失败"
        )

    def _choose_data_location(self, key: str) -> None:
        if key in {"collection", "covers", "media"}:
            self._open_data_location(key)
            return
        field = {
            "library": "last_output_dir",
        }.get(key)
        if field is None:
            self._set_data_location_feedback("数据位置设置失败")
            return
        current = self._data_location_path(key)
        selected = QFileDialog.getExistingDirectory(
            self,
            "设置今后保存位置",
            str(current),
        )
        # 取消必须保持配置、文件系统和可见状态完全不变。
        if not selected:
            return
        normalized = str(Path(selected).expanduser())
        previous = str(getattr(self.cfg, field, current))
        if normalized == previous:
            return
        save = getattr(self.cfg, "save", None)
        if not callable(save):
            self._set_data_location_feedback("数据位置设置失败")
            return
        setattr(self.cfg, field, normalized)
        try:
            saved = bool(save())
        except Exception:
            saved = False
        if not saved:
            setattr(self.cfg, field, previous)
            self._set_data_location_feedback("数据位置设置失败")
            return
        self._refresh_data_location_contexts()
        self._set_data_location_feedback("已更新今后保存位置")

    # ---------------- 跨重启采集任务 ----------------
    def _init_history(
        self,
        *,
        explicit_history: bool,
        explicit_records: bool,
    ) -> None:
        enabled = explicit_history or explicit_records or (
            QApplication.platformName() != "offscreen"
            and os.environ.get("LULU_COLLECTION_HISTORY_ENABLED", "1") != "0"
        )
        if not enabled:
            self.history_select.setEnabled(False)
            self.history_manage_button.setEnabled(False)
            self.history_manage_button.setToolTip("测试或兼容模式下未启用采集任务")
            return
        if self._history_repo is None and not explicit_records:
            self._history_repo = CollectionHistoryRepository()
        if self._records_repo is None and not explicit_history:
            self._records_repo = CollectionRepository()
        if self._records_repo is not None:
            try:
                self._records_snapshot = self._records_repo.load()
            except RepositoryError as exc:
                self._records_error_code = exc.code
        if self._history_repo is None:
            self.history_select.setEnabled(False)
            self.history_manage_button.setEnabled(False)
            return
        try:
            snapshot = self._history_repo.load()
        except HistoryError as exc:
            self._history_error_code = exc.code
            self.history_select.setEnabled(False)
            self.history_manage_button.setEnabled(False)
            self.history_manage_button.setToolTip(self._history_error_text(exc.code))
            return
        self._apply_history_snapshot(snapshot, restore_selected=True)

    def _history_options(self, snapshot: HistorySnapshot) -> tuple[tuple[str, str], ...]:
        options = []
        for record in snapshot.active_records:
            markers = []
            if record.completion in {"partial", "cancelled"}:
                markers.append("部分结果")
            if self._has_unknown_fields(record.items):
                markers.append("含未获取字段")
            label = record.name + (f" · {' · '.join(markers)}" if markers else "")
            options.append((record.record_id, label))
        return tuple(options)

    def _apply_history_snapshot(
        self, snapshot: HistorySnapshot, *, restore_selected: bool = False
    ) -> HistorySnapshot:
        snapshot = self._reconcile_history_snapshot(snapshot)
        self._history_snapshot = snapshot
        self.history_select.set_options(
            self._history_options(snapshot),
            value=snapshot.selected_record_id,
        )
        self.history_select.setToolTip(
            "任务文件只读，可查看但不能修改。" if not snapshot.writable else ""
        )
        self.history_manage_button.setEnabled(bool(snapshot.active_records or snapshot.trashed_records))
        if restore_selected and snapshot.selected_record is not None:
            self._set_items(list(snapshot.selected_record.items))
        return snapshot

    def _reconcile_history_snapshot(self, snapshot: HistorySnapshot) -> HistorySnapshot:
        """历史 JSON 保留快照；纯本地恢复只用封面字段 CAS 写主档。"""
        reconcile = getattr(self._cover_workflow, "reconcile_persisted", None)
        # Transcription/material workers can update the master since the last
        # page visit. Read it before projecting this history's display facts.
        if self._records_repo is not None:
            try:
                self._records_snapshot = self._records_repo.load()
            except RepositoryError:
                pass
        records = getattr(self._records_snapshot, "items", ())
        current_cover = {
            item.stable_key: item
            for item in records
            if getattr(item, "stable_key", "")
        }

        def overlay_current_cover(item):
            current = current_cover.get(item.stable_key)
            if current is None:
                return item
            return replace(
                item,
                cover_url=current.cover_url,
                cover_local_path=current.cover_local_path,
                cover_status=current.cover_status,
                media_local_path=current.media_local_path,
                media_status=current.media_status,
                transcript_status=current.transcript_status,
                last_error_code=current.last_error_code,
            )

        def reconcile_record(record):
            active = tuple(overlay_current_cover(item) for item in record.items)
            trashed = tuple(
                overlay_current_cover(item) for item in record.item_trash
            )
            if not callable(reconcile):
                return replace(record, items=active, item_trash=trashed)
            try:
                recovery = reconcile(active)
                recovered_items = list(recovery.items)
                for index, (before, after, outcome) in enumerate(zip(
                    active, recovery.items, recovery.outcomes
                )):
                    if outcome.status != "recovered_previous":
                        continue
                    if self._records_repo is None:
                        recovered_items[index] = before
                        continue
                    expected = {
                        "cover_local_path": before.cover_local_path,
                        "cover_status": before.cover_status,
                    }
                    updates = {
                        "cover_local_path": after.cover_local_path,
                        "cover_status": after.cover_status,
                    }
                    if str(before.last_error_code or "").startswith(
                        ("cover:", "cover_")
                    ):
                        expected["last_error_code"] = before.last_error_code
                        updates["last_error_code"] = after.last_error_code
                    try:
                        self._records_repo.compare_and_swap_fields(
                            before.stable_key,
                            expected=expected,
                            updates=updates,
                        )
                    except RepositoryError:
                        latest = self._records_repo.load()
                        recovered_items[index] = next(
                            (
                                item
                                for item in latest.items
                                if item.stable_key == before.stable_key
                            ),
                            before,
                        )
                active = tuple(recovered_items)
            except Exception:
                pass
            return replace(record, items=active, item_trash=trashed)

        reconciled = replace(
            snapshot,
            active_records=tuple(
                reconcile_record(record) for record in snapshot.active_records
            ),
            trashed_records=tuple(
                reconcile_record(record) for record in snapshot.trashed_records
            ),
        )
        if self._records_repo is not None:
            try:
                self._records_snapshot = self._records_repo.load()
            except RepositoryError:
                pass
        return reconciled

    def _on_history_selected(self, record_id: str) -> None:
        snapshot = self._history_snapshot
        repository = self._history_repo
        if snapshot is None or repository is None:
            return
        record = next(
            (row for row in snapshot.active_records if row.record_id == record_id), None
        )
        if record is None:
            return
        if snapshot.writable:
            try:
                snapshot = repository.select(
                    record_id, expected_source_hash=snapshot.source_hash
                )
            except HistoryError as exc:
                self.status.setText(self._history_error_text(exc.code))
                return
        snapshot = self._apply_history_snapshot(snapshot)
        record = next(
            (row for row in snapshot.active_records if row.record_id == record_id),
            None,
        )
        if record is None:
            return
        self._set_items(list(record.items))

    def _open_history_manager(self) -> None:
        if self._history_repo is None or self._history_snapshot is None:
            return
        if self._history_dialog is not None and self._history_dialog.isVisible():
            self._history_dialog.raise_()
            self._history_dialog.search_edit.setFocus(
                Qt.FocusReason.PopupFocusReason
            )
            return
        parent = self.window()
        dialog = CollectionHistoryDialog(
            self._history_repo,
            self._history_snapshot,
            on_changed=self._on_history_changed,
            parent=parent,
        )
        self._history_dialog = dialog
        dialog.finished.connect(lambda *_: setattr(self, "_history_dialog", None))
        dialog.open()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._history_dialog is not None:
            self._history_dialog.reject()
        super().hideEvent(event)

    def _on_history_changed(self, snapshot: HistorySnapshot) -> None:
        snapshot = self._apply_history_snapshot(snapshot)
        selected = snapshot.selected_record
        self._set_items(list(selected.items) if selected is not None else [])

    def _persist_history_result(self, result, token: int, *, retry_pending=True) -> tuple[str, bool]:
        items = tuple(result.items)
        should_save = (
            result.completion == "complete"
            or (result.completion in {"partial", "cancelled"} and bool(items))
        )
        if not should_save:
            self._history_context.pop(token, None)
            return "", True
        pending = getattr(self, "_pending_history_results", None)
        if pending is None:
            pending = self._pending_history_results = {}
        pending[token] = result
        if retry_pending:
            for previous_token, previous_result in tuple(pending.items()):
                if previous_token == token:
                    continue
                _, saved = self._persist_history_result(previous_result, previous_token, retry_pending=False)
                if not saved:
                    break
        context = self._history_context.get(token)
        snapshot = self._history_snapshot
        repository = self._history_repo
        if context is None or snapshot is None or repository is None:
            return "", False
        command, identity = context
        try:
            snapshot = repository.load()
            if not snapshot.writable:
                raise HistoryError(snapshot.code)
            updated = repository.add_record(
                items,
                platform=identity.platform,
                homepage_url=identity.canonical_url,
                source_method=command.mode,
                completion=result.completion,
                reason_code=result.reason_code,
                confirmed_empty=bool(result.confirmed_empty),
                expected_source_hash=snapshot.source_hash,
            )
        except HistoryError as exc:
            self._history_error_code = exc.code
            return self._history_error_text(exc.code), False
        self._history_context.pop(token, None)
        pending.pop(token, None)
        self._history_error_code = ""
        self._apply_history_snapshot(updated)
        return "", True

    def _persist_records_result(self, result) -> tuple[str, bool]:
        items = tuple(result.items)
        repository = self._records_repo
        if not items:
            return "", True
        if repository is None:
            return "", False
        try:
            # 每次都重新读取，瞬时并发/磁盘错误不能永久锁死后续采集。
            self._records_snapshot = repository.load()
            self._records_error_code = ""
            write = getattr(repository, "upsert_collected", None)
            if not callable(write):
                write = repository.upsert
            write(items)
            self._records_snapshot = repository.load()
            self._records_error_code = ""
        except RepositoryError as exc:
            self._records_error_code = exc.code
            return self._records_error_text(exc.code), False
        return "", True

    @staticmethod
    def _history_error_text(code: str) -> str:
        return {
            "future_schema_read_only": "任务文件来自更高版本，本次结果仍在表格中，但没有写入历史。",
            "corrupt_repository": "任务文件需要修复，本次结果仍在表格中，但没有写入历史。",
            "concurrent_change": "任务已被其他窗口更新，本次结果仍在表格中，但没有覆盖历史。",
            "disk_full": "磁盘空间不足，本次结果仍在表格中，但没有写入历史。",
            "permission_denied": "任务目录不可写，本次结果仍在表格中，但没有写入历史。",
            "unsafe_path": "任务路径不安全，已停止写入；当前结果仍保留在表格中。",
        }.get(code, "任务保存失败，当前结果仍保留在表格中。")

    @staticmethod
    def _records_error_text(code: str) -> str:
        return {
            "future_schema_read_only": "作品主档来自更高版本，本次结果仍在表格中，但没有更新主档。",
            "corrupt_repository": "作品主档需要修复，本次结果仍在表格中，但没有更新主档。",
            "concurrent_change": "作品主档已被其他窗口更新，本次结果仍在表格中，没有覆盖主档。",
            "disk_full": "磁盘空间不足，本次结果仍在表格中，但没有更新作品主档。",
            "permission_denied": "作品主档目录不可写，本次结果仍保留在表格中。",
            "unsafe_path": "作品主档路径不安全，已停止写入；当前结果仍保留在表格中。",
        }.get(code, "作品主档保存失败，当前结果仍保留在表格中。")

    def _cover_snapshot_enabled(self) -> bool:
        preferences = self._cover_preferences
        if preferences is None:
            return False
        try:
            return bool(preferences.snapshot().include_cover)
        except Exception:  # noqa: BLE001 - 配置异常时保持真实下载关闭
            return False

    def _on_primary_action(self) -> None:
        if self._workspace_request_limit() is not None:
            self._on_grab()
        elif self._current_platform() == "douyin" and self._selected_mode == "full":
            self._on_full_primary()
        else:
            self._on_grab()

    def accept_workspace_request(self, url: str, limit: int) -> None:
        """Stage only. The existing start button still owns browser/network work."""
        if self._collect_running():
            raise RuntimeError('主页采集已有任务在运行，请等待或停止后再带入')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('采集数量无效')
        self.input.setText(url)
        self._workspace_request=(url,limit)
        self._selected_mode='partial'
        self._render_collection_mode_state()
        self.quick_summary.setText(f'本次文稿任务：最多采集 {limit} 条作品')
        self.grab_btn.setText(f'采集 {limit} 条')
        self.status.setText('已带入文稿会话的采集范围，确认后点击开始。')

    def _workspace_request_limit(self):
        pending=getattr(self,'_workspace_request',None)
        if pending and pending[0]==self.input.text().strip():
            return pending[1]
        self._workspace_request=None
        return None

    # ---------------- 抓取最新（路 A） ----------------
    def _on_grab(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.request_cancel()
            self._stopping_collection = True
            self._render_collection_mode_state()
            if self._current_platform() == "xiaohongshu":
                self.status.setText(
                    "正在停止本次采集…专用浏览器会保持打开。"
                )
            else:
                self.status.setText("正在停止…（已采集的会保留）")
            return
        if self._douyin_login_busy():
            self.status.setText('请先完成或取消抖音登录，再开始采集。')
            return
        url = self.input.text().strip()
        if not url:
            self.status.setText("请先粘贴作者主页链接。")
            return
        if self._collect_running():
            return
        self._on_input_changed(url)
        platform = self._current_platform()
        if platform == "xiaohongshu" and self._xhs_release_disabled():
            self.status.setText(XHS_RELEASE_DISABLED_MESSAGE)
            return
        if platform == "bilibili":
            self.status.setText("B站主页采集暂不可用，现有单链接能力不受影响。")
            return
        if platform not in {"douyin", "xiaohongshu"}:
            self.status.setText("没有识别到受支持的公开主页链接。")
            return
        if platform == "xiaohongshu":
            if self._entry_state.identity.kind != "homepage":
                self.status.setText("小红书当前只支持公开主页采集。")
                return
            if not dialogs.confirm(
                self,
                "小红书公开主页采集",
                "将使用 Lulu 专用的小红书浏览器档案读取该公开主页，最多两轮或 20 条。\n\n"
                "不会读取你日常浏览器的登录信息；遇到登录、验证码或频控会停下并保留已有结果。",
                ok="开始采集",
                cancel="取消",
            ):
                return
            command = CollectionCommand(
                url,
                "profile_public",
                limit=self._workspace_request_limit() or 20,
                include_cover=self._cover_snapshot_enabled(),
                profile_id="default",
                browser_path=self._browser_path,
            )
            self.status.setText(f"正在启动小红书专用浏览器（{self._browser_name}）…")
        else:
            command = CollectionCommand(
                url,
                "anonymous_latest",
                limit=self._workspace_request_limit() or 20,
                include_cover=self._cover_snapshot_enabled(),
            )
            self.status.setText("正在抓取…（匿名、不登录）")
        self._start_collect(command, full=False)

    # ---------------- 全量采集（路 B） ----------------
    def _on_full_grab(self) -> None:
        if self._full_worker and self._full_worker.isRunning():
            self._full_worker.request_cancel()
            self._stopping_collection = True
            self._render_collection_mode_state()
            self.status.setText("正在停止全量采集…（已采集的会保留）")
            return
        if self._douyin_login_busy():
            self.status.setText('请先完成或取消抖音登录，再开始采集。')
            return
        if self._collect_running():
            self.status.setText("已有采集任务在运行，请先停止。")
            return
        url = self.input.text().strip()
        if not url:
            self.status.setText("请先粘贴作者主页链接。")
            return
        shared = self._douyin_login_state
        logged = shared()[0] in {'已登录', '已保存浏览器登录'} if shared else is_logged_in()
        lead = ("将在浏览器里用你的登录态，采集该作者的全部作品。建议用小号登录。"
                if logged else
                "会先弹出浏览器让你登录抖音（建议用小号），再采集该作者的全部作品。")
        mode_line = (
            "将使用快速全量采集；如出现真实可见验证，"
            "会停止并保留已采结果。"
        )
        if not dialogs.confirm(
            self, "全量采集",
            lead + "\n\n" + mode_line + "\n\n"
            "登录由你本人在浏览器完成，App 不经手账号密码；采集数据只留本地、不上传。",
            ok="继续采集", cancel="取消",
        ):
            return
        self.status.setText(f"正在启动浏览器（{self._browser_name}）…")
        command = CollectionCommand(
            url,
            "profile_all",
            include_cover=self._cover_snapshot_enabled(),
            profile_id="default",
            browser_path=self._browser_path,
            browser_mode=MODE_EFFICIENCY,
        )
        self._start_collect(command, full=True)

    def _start_collect(self, command: CollectionCommand, *, full: bool) -> None:
        material_snapshot = self._material_snapshot()
        # 主采集只结算基础信息；封面和音视频都是后续独立素材任务。
        command = replace(command, include_cover=False)
        self._run_token += 1
        token = self._run_token
        self._active_collection_mode = "full" if full else "partial"
        self._collection_started_monotonic = monotonic_time.monotonic()
        self._stopping_collection = False
        self._history_context[token] = (command, self._service.inspect(command.raw_input).identity)
        self._material_snapshots[token] = material_snapshot
        worker = _CollectionWorker(self._service, command, token)
        worker.progress.connect(
            lambda _message, count, t=token: self._on_collect_progress(t, count)
        )
        worker.done.connect(self._on_collect_done)
        worker.failed.connect(self._on_collect_failed)
        worker.finished.connect(
            lambda w=worker, is_full=full: self._on_collect_worker_finished(w, is_full)
        )
        if full:
            self._full_worker = worker
        else:
            self._worker = worker
        self.input.setEnabled(False)
        self.xhs_login_btn.setEnabled(False)
        self.xhs_close_btn.setEnabled(False)
        self._set_busy(True)
        if self._current_platform() == "douyin":
            self._render_collection_mode_state()
        else:
            self.grab_btn.setText("停止采集")
            self.grab_btn.setAccessibleName("停止当前采集")
        worker.start()

    def _on_collect_worker_finished(self, worker, full: bool) -> None:
        name = "_full_worker" if full else "_worker"
        if getattr(self, name, None) is worker:
            setattr(self, name, None)
        self._on_input_changed(self.input.text())
        if not self._xhs_release_disabled():
            self._sync_xhs_browser_status()

    def _on_collect_progress(self, token: int, count: int) -> None:
        if token != self._run_token:
            return
        suffix = f"，已读取 {count} 条" if count > 0 else ""
        started = getattr(self, "_collection_started_monotonic", 0.0)
        elapsed = max(0, int(monotonic_time.monotonic() - started)) if started else 0
        self.status.setText(
            f"正在采集{suffix} · 已用时 {elapsed // 60:02d}:{elapsed % 60:02d}…"
        )

    def _finish_collect(self) -> None:
        self._set_busy(False)
        self._active_collection_mode = ""
        self._stopping_collection = False
        self.input.setEnabled(True)
        self._reset_full_btn()
        self._on_input_changed(self.input.text())

    def _on_collect_done(self, result, token: int) -> None:
        if token != self._run_token:
            return
        self._finish_collect()
        if isinstance(result, CollectionExecutionResult):
            execution = result
            result = execution.collect_result
            cover_summary = execution.cover_summary
        else:
            cover_summary = CollectionCoverSummary()
        items = list(result.items)
        records_warning, records_saved = self._persist_records_result(result)
        history_warning, history_saved = self._persist_history_result(
            result, token
        )
        persistence_complete = records_saved and history_saved
        material_snapshot = self._material_snapshots.pop(
            token, MaterialPreferenceSnapshot()
        )
        material_queue_note = ""
        if (
            (
                material_snapshot.save_media
                or (
                    material_snapshot.save_cover
                    and self._cover_workflow is not None
                )
            )
            and records_saved
            and self._records_repo is not None
            and items
        ):
            frozen_materials = MaterialPreferenceSnapshot(
                "video",
                save_media=material_snapshot.save_media,
                save_cover=material_snapshot.save_cover,
            )
            material_was_busy = self._material_worker is not None
            self._enqueue_material_job(
                items,
                frozen_materials,
                origin="homepage_collect",
            )
            if material_was_busy:
                material_queue_note = "素材下载已排队，稍后自动处理。"
        cover_status = self._cover_status_text(
            cover_summary,
            persistence_complete=persistence_complete,
        )
        suffix = self._result_suffix(
            cover_status, records_warning, history_warning, material_queue_note
        )
        if result.completion == "complete":
            self._set_items(items)
            if result.confirmed_empty:
                self.status.setText("已确认该公开主页暂无作品。" + suffix)
                return
            author = items[0].author_name if items else ""
            missing = "，部分字段未获取" if self._has_unknown_fields(items) else ""
            self.status.setText(
                f"采集完成，共 {len(items)} 条"
                + (f"（作者：{author}）" if author else "")
                + missing + "。" + suffix
            )
            return
        if items:
            self._set_items(items)
        if result.completion == "cancelled":
            if items:
                missing = " 部分字段未获取。" if self._has_unknown_fields(items) else ""
                browser_note = (
                    " 专用浏览器保持打开。"
                    if self._current_platform() == "xiaohongshu"
                    else ""
                )
                self.status.setText(
                    f"已停止，已采集的 {len(items)} 条内容已保留。"
                    + browser_note + missing + suffix
                )
            else:
                if self._current_platform() == "xiaohongshu":
                    self.status.setText(
                        "已停止本次采集，专用浏览器保持打开；原有结果未清除。"
                        + suffix
                    )
                else:
                    self.status.setText("已停止采集，原有结果未清除。" + suffix)
            return

        if self._current_platform() == "douyin":
            platform_messages = {
                "login_required": "请先在 Lulu 专用浏览器登录抖音后再试。",
                "login_expired": "抖音专用登录已失效，请重新登录后再试。",
                "captcha": "抖音出现人工验证，已停止；请完成验证后重新开始。",
                "rate_limited": "抖音访问受限，已停止，请稍后重试。",
            }
        else:
            platform_messages = {
                "login_required": "请先打开 Lulu 专用小红书浏览器，用测试小号登录后再试。",
                "login_expired": "小红书专用登录已过期，请重新登录后再试。",
                "captcha": "小红书要求人工验证，请在专用浏览器完成后重试。",
                "rate_limited": "小红书访问频繁，已停止，请稍后重试。",
            }
        messages = {
            **platform_messages,
            "platform_changed": "平台页面结构已变化，请更新 Lulu 后重试。",
            "network_error": "页面读取失败，已安全停止，请稍后重试。",
            "no_items_unverified": "没有读取到作品，目前无法确认主页真实为空。",
            "permission_denied": "平台拒绝了这次访问，请检查公开范围后重试。",
            "unsupported_link": "当前链接或采集方式暂不支持。",
            "invalid_input": "请检查主页链接后重试。",
            "ambiguous_link": "一次只粘贴一个主页链接。",
        }
        message = messages.get(result.reason_code, "采集未完成，请稍后重试。")
        if result.completion == "partial":
            message += f" 已保留 {len(items)} 条结果。"
        if items and self._has_unknown_fields(items):
            message += " 部分字段未获取。"
        self.status.setText(message + suffix)

    @staticmethod
    def _cover_status_text(
        summary: CollectionCoverSummary,
        *,
        persistence_complete: bool,
    ) -> str:
        if summary.code != "settled" or summary.eligible_count <= 0:
            return ""
        if summary.downloaded_count and not persistence_complete:
            text = (
                "封面文件已保存，但任务更新失败"
                f"（已落盘 {summary.downloaded_count}/{summary.eligible_count}"
                f"，封面失败 {summary.failed_count}"
            )
            if summary.cancelled_count:
                text += f"，取消 {summary.cancelled_count}"
            return text + "）。"
        text = (
            f"封面已保存 {summary.downloaded_count}/{summary.eligible_count}"
            f"，失败 {summary.failed_count}"
        )
        if summary.cancelled_count:
            text += f"，取消 {summary.cancelled_count}"
        if summary.recovered_count:
            text += f"，其中恢复 {summary.recovered_count}"
        return text + "。"

    @staticmethod
    def _result_suffix(*messages: str) -> str:
        clean = [message.strip() for message in messages if message.strip()]
        return (" " + " ".join(clean)) if clean else ""

    def _on_collect_failed(self, message: str, token: int) -> None:
        if token != self._run_token:
            return
        self._history_context.pop(token, None)
        self._material_snapshots.pop(token, None)
        self._finish_collect()
        self.status.setText(message)

    def _set_busy(self, on: bool) -> None:
        """采集进行时显示不参与布局的浮动进度反馈。"""
        self.status.set_persistent(on)
        # 开始前不知道作品总数；用条数和用时反馈，不播放无事实分母的循环条。
        self.progress_bar.setVisible(False)
        self._sync_activity()

    def _sync_activity(self) -> None:
        """Place transient feedback over the results card without reflow."""
        visible = (
            (not self.status.isHidden())
            or (not self.progress_bar.isHidden())
        )
        if not visible:
            self.activity.hide()
            return

        target = getattr(self, "results_shell", self)
        target_width = max(160, target.width() - 32)
        plain_text = re.sub(r"<[^>]+>", "", self.status.text())
        text_width = (
            self.status.fontMetrics().horizontalAdvance(plain_text) + 40
            if plain_text
            else 260
        )
        overlay_width = min(target_width, max(240, min(420, text_width)))
        self.activity.setFixedWidth(overlay_width)
        self.activity.layout().activate()
        self.activity.adjustSize()

        top_left = (
            target.mapTo(self, QPoint(0, 0))
            if target is not self
            else QPoint(0, 0)
        )
        x = top_left.x() + target.width() - self.activity.width() - 18
        y = top_left.y() + target.height() - self.activity.height() - 18
        self.activity.move(max(top_left.x() + 18, x), max(top_left.y() + 18, y))
        self.activity.show()
        self.activity.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "material_preferences_layout"):
            self._layout_material_preferences(self.width())
        if hasattr(self, "export_btn"):
            self._apply_toolbar_density()
        if hasattr(self, "activity"):
            self._sync_activity()

    def _apply_toolbar_density(self) -> None:
        """两侧栏同时展开时使用窄屏文案，不改变功能和点击热区。"""

        compact = self.width() < 1120
        if getattr(self, "_toolbar_compact", None) == compact:
            return
        self._toolbar_compact = compact
        self.export_btn.setText("  导出" if compact else "  导出表格")
        self.export_btn.setAccessibleName("导出表格")
        self._sync_table_checks()
        self._update_count()

    # ---------------- 放大查看（折叠采集区，表格填满） ---------------- #
    def _toggle_table_focus(self) -> None:
        self._set_table_focus(not self._table_focus)

    def _set_table_focus(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._table_focus:
            return
        self._table_focus = enabled
        curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
        curve.addCubicBezierSegment(_EASE_C1, _EASE_C2, QPointF(1.0, 1.0))
        self._animate_form(collapse=enabled, curve=curve)
        self.focus_btn.setText("  退出放大" if enabled else "  放大查看")

    def _animate_form(self, *, collapse: bool, curve=None) -> None:
        """折叠/展开上方采集控制区（form_box 的 maximumHeight 动画），表格丝滑填满/让出。
        离屏 / 拿不到有效高度时直接落终态。"""
        box = self.form_box
        if self._form_anim is not None:
            self._form_anim.stop()
            self._form_anim = None
        if collapse and box.isVisible() and box.height() > 0:
            self._form_natural_h = box.height()
        natural = self._form_natural_h or box.sizeHint().height()
        target = 0 if collapse else natural
        start = box.maximumHeight()
        if start > natural:        # 展开时解除过 cap(_MAX_H)，折叠起点用真实高度
            start = box.height() if box.height() > 0 else natural
        if curve is None or natural <= 0:
            box.setVisible(not collapse)
            box.setMaximumHeight(0 if collapse else _MAX_H)
            return
        box.setVisible(True)
        anim = QPropertyAnimation(box, b"maximumHeight", self)
        anim.setDuration(_FOCUS_ANIM_MS)
        anim.setStartValue(max(0, int(start)))
        anim.setEndValue(int(max(0, target)))
        anim.setEasingCurve(curve)

        def _finish() -> None:
            box.setVisible(not collapse)
            box.setMaximumHeight(0 if collapse else _MAX_H)
        anim.finished.connect(_finish)
        self._form_anim = anim
        anim.start()

    def _reset_full_btn(self) -> None:
        self._render_collection_mode_state()

    # ---------------- 登录 ----------------
    def _refresh_login_status(self) -> None:
        label, saved = self._douyin_login_state() if self._douyin_login_state else ('已登录' if is_logged_in() else '未登录', is_logged_in())
        logged = label in {'已登录', '已保存浏览器登录'}
        # 状态小圆点：已登录绿 / 未登录灰
        self.login_dot.setStyleSheet(
            f"background:{T.GREEN if logged else self._consumer.ink_muted};border-radius:4px;")
        self.login_status.setText(label)
        self.login_btn.setText("重新登录" if label != "未登录" else "登录账号")
        # 只在有登录档案时才让「清除登录」可点
        self.clear_login_btn.setEnabled(saved and not self._douyin_login_busy())
        if hasattr(self, "full_btn"):
            self._render_collection_mode_state()

    def _on_clear_login(self) -> None:
        if self._douyin_login_open:
            self._douyin_login_open('clear')
            return
        if self._collect_running():
            self.status.setText("正在采集中，请先停止采集再清除登录。")
            return
        if ((self._login_worker and self._login_worker.isRunning())
                or (self._xhs_login_worker and self._xhs_login_worker.isRunning())):
            self.status.setText("正在登录中，请先完成或取消登录再清除。")
            return
        if not dialogs.confirm(
            self, "清除抖音登录",
            "删掉本机记住的抖音登录。\n\n"
            "· 下次做全量采集得重新登一次小号。\n"
            "· 已采集的数据、你平时用的浏览器都不受影响。",
            ok="清除登录", cancel="取消",
        ):
            return
        removed = clear_login()
        self._refresh_login_status()
        self.status.setText("已清除抖音登录档案，现在是未登录状态。"
                            if removed else "没有可清除的登录档案（当前未登录）。")

    def _on_login(self) -> None:
        if self._douyin_login_open:
            self._douyin_login_open('login')
            return
        if self._collect_running() or (self._xhs_login_worker and self._xhs_login_worker.isRunning()):
            self.status.setText("已有采集或登录任务在运行，请先完成或停止。")
            return
        if self._login_worker and self._login_worker.isRunning():
            return
        self.login_btn.setEnabled(False)
        self.status.setText("正在打开浏览器，请用小号扫码登录…")
        self._login_worker = _LoginWorker(self._browser_path)
        self._login_worker.progress.connect(lambda m, c: self.status.setText(m))
        self._login_worker.done.connect(self._on_login_done)
        self._login_worker.start()

    def _on_login_done(self, ok: bool) -> None:
        self.login_btn.setEnabled(True)
        self._refresh_login_status()
        self.status.setText("已登录！现在粘贴链接，点「开始全量采集」就行，不用再登。"
                            if ok else "未检测到登录（超时或已取消），可重试。")

    def _on_xhs_login(self) -> None:
        self._on_input_changed(self.input.text())
        if self._xhs_release_disabled():
            self.status.setText(XHS_RELEASE_DISABLED_MESSAGE)
            return
        worker = self._xhs_login_worker
        if worker and worker.isRunning():
            self.status.setText("专用浏览器正在打开，请稍候。")
            return
        if self._collect_running() or (
            self._xhs_close_worker is not None
            and self._xhs_close_worker.isRunning()
        ):
            self.status.setText("正在采集中，请先停止采集。")
            return
        if not dialogs.confirm(
            self,
            "打开小红书专用登录",
            "将打开 Lulu 专用的小红书浏览器档案。\n\n"
            "请使用测试小号登录；Lulu 不读取密码、Cookie 或 Token 内容，"
            "也不会访问你日常浏览器的登录信息。\n\n"
            "若 Chrome 出现钥匙串提示，请由你本人处理。Lulu 会在限定时间内"
            "等待同一个专用浏览器可信就绪，不会重复启动。",
            ok="打开专用窗口",
            cancel="取消",
        ):
            return
        self.status.setText("正在等待同一个小红书专用浏览器可信就绪…")
        self.xhs_login_btn.setText("正在打开…")
        self.grab_btn.setEnabled(False)
        self.xhs_close_btn.setEnabled(False)
        self.input.setEnabled(False)
        worker = _XhsLoginWorker(self._service, self._browser_path)
        self._xhs_login_worker = worker
        worker.progress.connect(lambda message, _count: self.status.setText(message))
        worker.done.connect(self._on_xhs_login_done)
        worker.finished.connect(lambda w=worker: self._on_xhs_login_worker_finished(w))
        worker.start()

    def _on_xhs_login_done(self, code: str) -> None:
        messages = {
            "ready": (
                "专用浏览器已打开。请在窗口中完成登录；"
                "完成后可直接采集，由平台结果确认登录状态。"
            ),
            "cancelled": "已取消打开小红书专用登录窗口。",
            "unsupported": "当前平台不支持这个登录入口。",
            "chrome_missing": "未发现 Google Chrome，请先自行安装后重试。",
            "chrome_version_unsupported": "请将 Google Chrome 更新到 136 或更高版本。",
            "profile_identity_blocked": "专用浏览器档案身份无法安全验证，已停止。",
            "profile_in_use_unknown": "专用浏览器档案正被未知进程占用，请先处理。",
            "controller_busy": "另一个 Lulu 正在使用小红书专用浏览器。",
            "cdp_discovery_invalid": "专用浏览器连接尚未可信就绪，请重试。",
            "cdp_exposure_blocked": "专用浏览器连接身份不安全，已停止。",
            "browser_lost": "专用浏览器未能打开，请重试。",
        }
        self.status.setText(
            messages.get(code, "未能打开小红书专用浏览器，本次已安全停止。")
        )
        self._sync_xhs_browser_status()

    def _on_xhs_login_worker_finished(self, worker) -> None:
        if self._xhs_login_worker is worker:
            self._xhs_login_worker = None
        self.input.setEnabled(True)
        self.xhs_login_btn.setText("打开专用浏览器登录")
        self._on_input_changed(self.input.text())

    def _on_xhs_close(self) -> None:
        self._on_input_changed(self.input.text())
        if self._xhs_release_disabled():
            self.status.setText(XHS_RELEASE_DISABLED_MESSAGE)
            return
        if self._collect_running():
            self.status.setText("正在采集中，请先停止本次采集再关闭浏览器。")
            return
        if (
            self._xhs_login_worker is not None
            and self._xhs_login_worker.isRunning()
        ):
            self.status.setText("专用浏览器正在打开，请稍候。")
            return
        if (
            self._xhs_close_worker is not None
            and self._xhs_close_worker.isRunning()
        ):
            return
        status = self._sync_xhs_browser_status()
        if not status.running:
            self.status.setText("小红书专用浏览器当前没有运行。")
            return
        if not dialogs.confirm(
            self,
            "关闭小红书专用浏览器",
            "关闭后不会删除登录状态，下次采集会重新打开。",
            ok="关闭专用浏览器",
            cancel="取消",
        ):
            return
        self.status.setText("正在正常关闭小红书专用浏览器…")
        self.xhs_login_btn.setEnabled(False)
        self.xhs_close_btn.setEnabled(False)
        worker = _XhsCloseWorker(self._service)
        self._xhs_close_worker = worker
        worker.done.connect(self._on_xhs_close_done)
        worker.finished.connect(
            lambda w=worker: self._on_xhs_close_worker_finished(w)
        )
        worker.start()

    def _on_xhs_close_done(self, status: XhsBrowserStatus) -> None:
        self._sync_xhs_browser_status(status)
        if status.code == "absent":
            self.status.setText(
                "小红书专用浏览器已关闭；登录状态仍保留在专用档案中。"
            )
        elif status.code == "collection_busy":
            self.status.setText("正在采集中，请先停止本次采集。")
        elif status.code == "close_failed_browser_alive":
            self.status.setText(
                "专用浏览器仍在运行。请在浏览器窗口中正常退出，或稍后重试。"
                "Lulu 未强制结束它。"
            )
        else:
            self.status.setText(status.message)

    def _on_xhs_close_worker_finished(self, worker) -> None:
        if self._xhs_close_worker is worker:
            self._xhs_close_worker = None
        self._on_input_changed(self.input.text())

    # ---------------- 选择 / 工具 ----------------
    @staticmethod
    def _read_transcribed(inspect_source):
        from ..core import library
        ids = set()
        for entry in library.list_entries():
            if entry.get("kind") != "transcript":
                continue
            source = entry.get("source") or ""
            match = re.search(r"/video/(\d+)", source)
            if match:
                ids.update((match.group(1), f"douyin:{match.group(1)}"))
            identity = inspect_source(source).identity
            if identity.platform != "unknown" and identity.extracted_id:
                ids.add(f"{identity.platform}:{identity.extracted_id}")
        return ids

    def _reload_transcribed(self) -> None:
        # Explicit result replacement retains its existing synchronous contract.
        try:
            self._transcribed = self._read_transcribed(self._service.inspect)
        except Exception:
            pass  # Keep the last verified display instead of clearing its marks.

    def _request_transcribed_read(self):
        if self._shutting_down:
            return
        if self._transcribed_read is not None:
            self._transcribed_read_pending = True
            return
        inspect_source = self._service.inspect
        reader = _TranscribedRead(lambda: CollectionPage._read_transcribed(inspect_source))
        self._transcribed_read = reader
        reader.finished.connect(self._finish_transcribed_read)
        reader.finished.connect(reader.deleteLater)
        reader.start()

    def _finish_transcribed_read(self):
        reader = self._transcribed_read
        if reader is None or self._shutting_down:
            return
        self._transcribed_read = None
        if reader.error:
            if not self._collect_running():
                self.status.setText("文稿状态刷新失败，已保留上次显示；重新进入本页可重试。")
        elif reader.result != self._transcribed:
            self._transcribed = reader.result
            if self._items:
                self._fill_table(status_only=True)
        if self._transcribed_read_pending:
            self._transcribed_read_pending = False
            self._request_transcribed_read()

    def _set_items(self, items: list) -> None:
        self._items = items
        live_keys = {str(item.stable_key or "") for item in items}
        self._material_runtime = {
            key: states
            for key, states in self._material_runtime.items()
            if key in live_keys or any(entry[0] == key for entry in self._active_material_entries)
        }
        self._selected_keys.clear()
        self._selection_anchor_key = ""
        self._reload_transcribed()
        self.filter_edit.blockSignals(True)
        self.filter_edit.clear()
        self.filter_edit.blockSignals(False)
        self._render()

    def _on_item_changed(self, changed) -> None:
        if (
            self._syncing_table_checks
            or changed is None
            or changed.column() != 0
        ):
            self._update_count()
            return
        key = str(changed.data(Qt.UserRole) or "")
        if not key:
            return
        self._reduce_selection(
            key,
            modifiers=QApplication.keyboardModifiers(),
            explicit_checked=changed.checkState() == Qt.Checked,
        )

    def _on_table_cell_clicked(self, row: int, column: int) -> None:
        if column == 0 or not (0 <= row < len(self._view)):
            return
        self._reduce_selection(
            self._view[row].stable_key,
            modifiers=QApplication.keyboardModifiers(),
        )

    def _on_table_cell_double_clicked(self, row: int, column: int) -> None:
        if column not in {_MEDIA_COL, _COVER_COL} or not (0 <= row < len(self._view)):
            return
        item = self._view[row]
        # Qt 的双击可能伴随两次单击；定位动作结束后让作品保持明确选中。
        self._selected_keys.add(item.stable_key)
        self._selection_anchor_key = item.stable_key
        self._sync_table_checks()
        self._update_count()
        if column == _MEDIA_COL:
            path = self._managed_media_file(item)
            label = "音视频"
            missing = "这份音视频未下载或已不在 Lulu 管理的位置，请重新下载所选素材。"
        else:
            path = self._managed_cover_file(item)
            label = "封面"
            missing = "这份封面未下载或已不在 Lulu 管理的位置，请重新下载所选素材。"
        if path is None:
            self.status.setText(missing)
            return
        try:
            opened = subprocess.run(
                ["open", "-R", str(path)], check=False
            ).returncode == 0
        except OSError:
            opened = False
        self.status.setText(
            f"已在 Finder 中定位{label}文件。"
            if opened
            else f"{label}文件存在，但 Finder 打开失败。"
        )

    def _reduce_selection(
        self,
        stable_key: str,
        *,
        modifiers=Qt.KeyboardModifier.NoModifier,
        explicit_checked: bool | None = None,
    ) -> None:
        """Single reducer for checkbox, whole-row, Shift and Cmd/Ctrl gestures."""
        key = str(stable_key or "")
        if not key:
            return
        visible = [item.stable_key for item in self._view]
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        additive = bool(
            modifiers
            & (
                Qt.KeyboardModifier.MetaModifier
                | Qt.KeyboardModifier.ControlModifier
            )
        )
        if shift and self._selection_anchor_key in visible and key in visible:
            left = visible.index(self._selection_anchor_key)
            right = visible.index(key)
            first, last = sorted((left, right))
            self._selected_keys.update(visible[first:last + 1])
        else:
            checked = (
                explicit_checked
                if explicit_checked is not None
                else key not in self._selected_keys
            )
            if checked:
                self._selected_keys.add(key)
            else:
                self._selected_keys.discard(key)
            if not shift or self._selection_anchor_key not in visible:
                self._selection_anchor_key = key
            elif additive:
                self._selection_anchor_key = key
        self._sync_table_checks()
        self._update_count()

    def _sync_table_checks(self) -> None:
        was_syncing = self._syncing_table_checks
        self._syncing_table_checks = True
        was_blocked = self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                check = self.table.item(row, 0)
                if check is None:
                    continue
                key = str(check.data(Qt.UserRole) or "")
                state = Qt.Checked if key in self._selected_keys else Qt.Unchecked
                if check.checkState() != state:
                    check.setCheckState(state)
        finally:
            self.table.blockSignals(was_blocked)
            self._syncing_table_checks = was_syncing
        self._sync_row_selection_visuals()
        visible_keys = {item.stable_key for item in self._view}
        self.select_all_btn.setText(
            ("取消全选" if getattr(self, "_toolbar_compact", False) else "取消可见全选")
            if visible_keys and visible_keys <= self._selected_keys
            else "全选"
        )

    def _sync_row_selection_visuals(self) -> None:
        # Paint-only roles also emit itemChanged; they must not re-enter selection.
        was_blocked = self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                check = self.table.item(row, 0)
                key = str(check.data(Qt.UserRole) or "") if check is not None else ""
                selected = bool(key and key in self._selected_keys)
                for column in range(self.table.columnCount()):
                    cell = self.table.item(row, column)
                    if cell is not None and cell.data(_ROW_SELECTED_ROLE) != selected:
                        cell.setData(_ROW_SELECTED_ROLE, selected)
        finally:
            self.table.blockSignals(was_blocked)
        self.table.viewport().update()

    def _update_count(self) -> None:
        visible_keys = {item.stable_key for item in self._view}
        selected = len(self._selected_keys)
        hidden = len(self._selected_keys - visible_keys)
        total = self.table.rowCount()
        compact = bool(getattr(self, "_toolbar_compact", False))
        if selected:
            hidden_text = f"，其中 {hidden} 条隐藏" if hidden else ""
            full_text = f"已选 {selected} 条{hidden_text} / 共 {total} 条"
            self.count_label.setText(
                f"{selected}/{total} 条" if compact else full_text
            )
            self.count_label.setToolTip(full_text if compact else "")
        else:
            self.count_label.setText(f"共 {total} 条")
            self.count_label.setToolTip("")
        self._update_tools_enabled()

    def _checked_rows(self) -> list[int]:
        return [
            index
            for index, item in enumerate(self._view)
            if item.stable_key in self._selected_keys
        ]

    def _toggle_select_all(self) -> None:
        visible = {item.stable_key for item in self._view}
        if visible and visible <= self._selected_keys:
            self._selected_keys.difference_update(visible)
        else:
            self._selected_keys.update(visible)
        self._sync_table_checks()
        self._update_count()

    def _checked_items(self) -> list:
        return [
            item for item in self._items if item.stable_key in self._selected_keys
        ]

    def _selected_or_all(self) -> list:
        return self._checked_items() or list(self._view)

    def _set_material_runtime(
        self, stable_key: str, kind: str, state: str
    ) -> None:
        if not stable_key or kind not in {"media", "cover"}:
            return
        states = self._material_runtime.setdefault(stable_key, {})
        if state:
            states[kind] = state
        else:
            states.pop(kind, None)
            if not states:
                self._material_runtime.pop(stable_key, None)

    def _queue_material_runtime(
        self, items, snapshot: MaterialPreferenceSnapshot
    ) -> None:
        for item in items:
            stable_key = str(getattr(item, "stable_key", "") or "")
            if not stable_key:
                continue
            if snapshot.save_media and not getattr(item, "is_image", False):
                if self._managed_media_file(item) is None:
                    entry = (stable_key, "media")
                    self._active_material_entries.add(entry)
                    self._set_material_runtime(*entry, "queued")
            if (
                snapshot.save_cover
                and self._cover_workflow is not None
                and str(getattr(item, "cover_url", "") or "")
                and self._managed_cover_file(item) is None
            ):
                entry = (stable_key, "cover")
                self._active_material_entries.add(entry)
                self._set_material_runtime(*entry, "queued")
        self._render()

    def _on_material_progress(self, payload) -> None:
        try:
            stable_key, kind, state = payload
        except (TypeError, ValueError):
            return
        entry = (str(stable_key or ""), str(kind or ""))
        if entry not in self._active_material_entries:
            return
        self._set_material_runtime(entry[0], entry[1], str(state or ""))
        if any(item.stable_key == entry[0] for item in self._items):
            self._render()

    def _settle_material_runtime(self, media_result, cover_result) -> None:
        failed: set[tuple[str, str]] = set()
        for kind, result in (
            ("media", media_result),
            ("cover", cover_result),
        ):
            for outcome in getattr(result, "outcomes", ()):
                entry = (str(outcome.stable_key or ""), kind)
                if outcome.status == "failed":
                    failed.add(entry)
        for stable_key, kind in tuple(self._active_material_entries):
            self._set_material_runtime(stable_key, kind, "")
        for stable_key, kind in failed:
            self._set_material_runtime(stable_key, kind, "failed")
        self._active_material_entries.clear()

    def _fresh_material_items(self, items) -> tuple:
        if self._records_repo is None:
            return tuple(items)
        try:
            current = {
                item.stable_key: item
                for item in self._records_repo.load().items
            }
        except RepositoryError:
            return tuple(items)
        return tuple(current.get(item.stable_key, item) for item in items)

    def _start_material_job(
        self,
        items,
        snapshot: MaterialPreferenceSnapshot,
        *,
        origin: str,
    ) -> None:
        if self._records_repo is None or self._material_worker is not None:
            return
        fresh = self._fresh_material_items(items)
        if not fresh:
            return
        self._queue_material_runtime(fresh, snapshot)
        worker = _MaterialDownloadWorker(
            self._records_repo,
            self._cover_workflow,
            fresh,
            snapshot,
            origin=origin,
        )
        self._material_worker = worker
        worker.progress.connect(self._on_material_progress)
        worker.done.connect(self._on_material_download_done)
        worker.failed.connect(self._on_material_download_failed)
        worker.finished.connect(
            lambda current=worker: self._on_material_download_finished(current)
        )
        self.material_download_btn.setEnabled(False)
        self.material_download_btn.setText("正在创建素材任务…")
        self.status.setText(f"正在处理 {len(fresh)} 项素材…")
        worker.start()

    def _enqueue_material_job(
        self,
        items,
        snapshot: MaterialPreferenceSnapshot,
        *,
        origin: str,
    ) -> None:
        frozen = tuple(items)
        if not frozen:
            return
        if self._material_worker is None:
            self._start_material_job(frozen, snapshot, origin=origin)
            return

        for index, (waiting, waiting_snapshot, waiting_origin) in enumerate(
            self._pending_material_jobs
        ):
            if waiting_snapshot != snapshot or waiting_origin != origin:
                continue
            merged = {item.stable_key: item for item in waiting}
            order = [item.stable_key for item in waiting]
            for item in frozen:
                if item.stable_key not in merged:
                    order.append(item.stable_key)
                merged[item.stable_key] = item
            self._pending_material_jobs[index] = (
                tuple(merged[key] for key in order),
                snapshot,
                origin,
            )
            queued_count = len(self._pending_material_jobs[index][0])
            self.status.setText(
                f"当前素材任务进行中，已排队 {queued_count} 项，稍后自动处理。"
            )
            return

        self._pending_material_jobs.append((frozen, snapshot, origin))
        queued_count = sum(len(job[0]) for job in self._pending_material_jobs)
        self.status.setText(
            f"当前素材任务进行中，已排队 {queued_count} 项，稍后自动处理。"
        )

    def _download_selected_materials(self) -> None:
        selected = self._checked_items()
        if not selected:
            self.status.setText("请先选择作品")
            return
        snapshot = self._material_download_snapshot()
        if not snapshot.save_media and not snapshot.save_cover:
            self.status.setText("请先选择要保存的素材")
            if hasattr(self, "material_preferences_wrap"):
                self.material_preferences_wrap.setFocus(Qt.OtherFocusReason)
            return
        if self._material_worker is not None:
            return
        if self._records_repo is None:
            self.status.setText("作品主档未启用，本次没有创建素材任务。")
            return
        eligible = [
            item
            for item in selected
            if getattr(item, "schema_version", 0) == 2
            and bool(getattr(item, "stable_key", ""))
        ]
        if not eligible:
            self.status.setText("所选作品缺少可靠身份，本次没有创建素材任务。")
            return
        self._enqueue_material_job(
            eligible,
            snapshot,
            origin="homepage_history_material",
        )

    def _existing_media_file(self, item):
        if getattr(item, "media_status", "") != "downloaded":
            return None
        try:
            return ManagedMediaStore(self._data_location_path("media")).existing_file(
                str(getattr(item, "media_local_path", "") or ""), expected_stable_key=item.stable_key)
        except (OSError, RuntimeError, ValueError):
            return None

    def _managed_media_file(self, item):
        if getattr(item, "media_status", "") != "downloaded":
            return None
        try:
            root = self._data_location_path("media")
            validation = ManagedMediaStore(root).validate(
                str(getattr(item, "media_local_path", "") or ""),
                expected_stable_key=item.stable_key,
            )
        except Exception:
            return None
        return validation.path if validation.valid else None

    def _reveal_managed_media(self, item) -> None:
        path = self._managed_media_file(item)
        if path is None:
            self.status.setText(
                "这份音视频已不在 Lulu 管理的位置，可以重新下载所选素材。"
            )
            return
        subprocess.run(["open", "-R", str(path)], check=False)

    def _on_material_download_done(self, payload) -> None:
        items, media_result, cover_result, cancelled = payload
        updated = {item.stable_key: item for item in items}
        self._items = [updated.get(item.stable_key, item) for item in self._items]
        self._settle_material_runtime(media_result, cover_result)
        try:
            if self._records_repo is not None:
                self._records_snapshot = self._records_repo.load()
        except RepositoryError:
            pass
        self._render()
        if cancelled:
            self.status.setText("已停止后续下载，已经完成的素材会保留")
            return
        media_failed = sum(
            outcome.status == "failed"
            for outcome in getattr(media_result, "outcomes", ())
        )
        cover_failed = sum(
            outcome.status == "failed"
            for outcome in getattr(cover_result, "outcomes", ())
        )
        failed = media_failed + cover_failed
        if failed:
            media_refresh_failed = any(
                outcome.status == "failed"
                and outcome.reason_code == "media_source_refresh_failed"
                for outcome in getattr(media_result, "outcomes", ())
            )
            suffix = (
                "；音视频地址无法刷新，可重新采集主页后再试"
                if media_refresh_failed
                else ""
            )
            self.status.setText(
                f"已完成 {len(items)} 项，{failed} 项素材未完成{suffix}"
            )
        else:
            self.status.setText("已完成所选素材下载")

    def _on_material_download_failed(self) -> None:
        for stable_key, kind in tuple(self._active_material_entries):
            self._set_material_runtime(stable_key, kind, "failed")
        self._active_material_entries.clear()
        self._render()
        self.status.setText("素材下载未完成，作品主档与已有文件保持不变。")

    def _on_material_download_finished(self, worker) -> None:
        if self._material_worker is worker:
            self._material_worker = None
        worker.deleteLater()
        if not self._shutting_down and self._pending_material_jobs:
            items, snapshot, origin = self._pending_material_jobs.pop(0)
            self._start_material_job(items, snapshot, origin=origin)
            return
        self.material_download_btn.setText("下载所选素材")
        self._update_tools_enabled()

    def _cover_cell_widget(self, item) -> QWidget:
        cell = QWidget()
        cell.setProperty("coverCell", True)
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(3, 2, 3, 2)
        layout.setSpacing(2)
        code = str(getattr(item, "last_error_code", "") or "")
        status = str(getattr(item, "cover_status", "not_requested") or "")
        url = str(getattr(item, "cover_url", "") or "")

        def label(text: str, tooltip: str = "") -> QLabel:
            widget = QLabel(text)
            widget.setAlignment(Qt.AlignCenter)
            widget.setStyleSheet(
                f"color:{self._consumer.ink_soft};font-size:11px;background:transparent;"
            )
            if tooltip:
                widget.setToolTip(tooltip)
            layout.addWidget(widget)
            return widget

        def action(text: str, callback, tooltip: str = "") -> QPushButton:
            button = QPushButton(text)
            adopt_button(
                button,
                role="secondary",
                size="sm",
                accessible_name=f"{item.title or item.stable_key}：{text}",
            )
            button.setFixedHeight(24)
            button.setEnabled(self._cover_workflow is not None)
            button.setToolTip(
                tooltip
                or (
                    "封面下载能力尚未启用"
                    if self._cover_workflow is None
                    else ""
                )
            )
            button.clicked.connect(callback)
            layout.addWidget(button)
            return button

        if code.endswith("cover_commit_uncertain"):
            label("待恢复", "上一份封面尚未完成安全恢复")
            action(
                "重试恢复",
                lambda _checked=False, row=item: self._retry_cover_item(row),
                "只恢复上一份封面，不启动普通下载",
            )
            return cell
        if code.endswith("recovered_previous"):
            label("已恢复上一份", "上一份封面已安全恢复")
        if status == "downloaded":
            path = self._managed_cover_file(item)
            if path is None:
                label("文件已丢失", "状态显示已保存，但本地受管文件不存在")
                if url:
                    action(
                        "重新下载",
                        lambda _checked=False, row=item: self._retry_cover_item(row),
                    )
                return cell
            preview = QPushButton("已保存")
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                preview.setIcon(
                    QIcon(
                        pixmap.scaled(
                            36,
                            36,
                            Qt.KeepAspectRatio,
                            Qt.SmoothTransformation,
                        )
                    )
                )
                preview.setText("")
                preview.setFixedSize(44, 44)
            else:
                preview.setFixedHeight(30)
            preview.setToolTip("已保存；点击预览，右键可在 Finder 中显示")
            preview.setAccessibleName(
                f"{item.title or item.stable_key}：已保存封面，点击预览"
            )
            preview.clicked.connect(
                lambda _checked=False, row=item: self._preview_cover(row)
            )
            preview.setContextMenuPolicy(Qt.CustomContextMenu)
            preview.customContextMenuRequested.connect(
                lambda point, button=preview, row=item: self._show_cover_menu(
                    button, point, row
                )
            )
            layout.addWidget(preview, 0, Qt.AlignCenter)
            return cell
        if not url:
            label("无封面")
            return cell
        if status == "pending":
            label("下载中")
            return cell
        if status == "url_only":
            label("待下载")
            action(
                "重新下载",
                lambda _checked=False, row=item: self._retry_cover_item(row),
            )
            return cell
        if status == "failed":
            label("下载失败", "封面下载失败，作品与原链接仍保留")
            action(
                "重新下载",
                lambda _checked=False, row=item: self._retry_cover_item(row),
            )
            return cell
        if status == "not_requested":
            label("未下载")
            action(
                "下载封面",
                lambda _checked=False, row=item: self._retry_cover_item(row),
            )
            return cell
        label(
            {
                "unavailable": "不可用",
                "skipped_privacy": "已跳过",
            }.get(status, "状态未知")
        )
        return cell

    def _retry_cover_item(self, item) -> None:
        if not self._cover_retry_eligible(item):
            self.status.setText("这条作品当前没有可安全重试的封面来源。")
            return
        self._start_cover_retry([item])

    def _preview_cover(self, item) -> None:
        path = self._managed_cover_file(item)
        if path is None:
            self.status.setText("本地封面已丢失，无法预览。")
            self._render()
            return
        subprocess.run(["open", str(path)], check=False)

    def _show_cover_menu(self, button, point, item) -> None:
        path = self._managed_cover_file(item)
        if path is None:
            return
        menu = QMenu(button)
        reveal = menu.addAction("在 Finder 中显示")
        chosen = menu.exec(button.mapToGlobal(point))
        if chosen is reveal:
            subprocess.run(["open", "-R", str(path)], check=False)

    @staticmethod
    def _cover_retry_eligible(item) -> bool:
        parsed = urlsplit(str(getattr(item, "cover_url", "") or ""))
        status = getattr(item, "cover_status", "")
        return bool(
            (
                status in {"not_requested", "url_only", "failed"}
                or (
                    status == "downloaded"
                    and CollectionPage._managed_cover_file(item) is None
                )
            )
            and getattr(item, "platform", "")
            and getattr(item, "work_id", "")
            and parsed.scheme == "https"
            and parsed.hostname
        )

    @staticmethod
    def _managed_cover_file(item):
        if getattr(item, "cover_status", "") != "downloaded":
            return None
        relative = str(getattr(item, "cover_local_path", "") or "")
        if not relative:
            return None
        return export.resolve_managed_cover_file(relative)

    def _update_cover_actions(self) -> None:
        if not hasattr(self, "cover_retry_btn"):
            return
        selected = self._checked_items()
        retry_running = self._cover_retry_worker is not None
        recovery = any(
            str(getattr(item, "last_error_code", "") or "").endswith(
                "cover_commit_uncertain"
            )
            for item in selected
        )
        retryable = bool(
            self._cover_workflow is not None
            and selected
            and any(self._cover_retry_eligible(item) for item in selected)
        )
        self.cover_retry_btn.setEnabled(retryable and not retry_running)
        recovery_action = (
            self._cover_retry_is_recovery if retry_running else recovery
        )
        self.cover_retry_btn.setText(
            "正在恢复…"
            if retry_running and recovery_action
            else "正在重试…"
            if retry_running
            else "重试恢复"
            if recovery_action
            else "重试封面"
        )
        self.cover_finder_btn.setEnabled(
            len(selected) == 1 and self._managed_cover_file(selected[0]) is not None
        )

    def _retry_selected_covers(self) -> None:
        if self._cover_workflow is None:
            self.status.setText("封面重试服务尚未准备好。")
            return
        if self._cover_retry_worker is not None:
            return
        items = [
            item for item in self._checked_items()
            if self._cover_retry_eligible(item)
        ]
        if not items:
            self.status.setText("请勾选仍有可靠封面链接的失败或未保存作品。")
            return
        self._start_cover_retry(items)

    def _start_cover_retry(self, items: list) -> None:
        if self._cover_workflow is None or self._cover_retry_worker is not None:
            return
        self._cover_retry_is_recovery = any(
            str(getattr(item, "last_error_code", "") or "").endswith(
                "cover_commit_uncertain"
            )
            for item in items
        )
        worker = _CoverRetryWorker(self._cover_workflow, items)
        self._cover_retry_worker = worker
        worker.done.connect(self._on_cover_retry_done)
        worker.failed.connect(self._on_cover_retry_failed)
        worker.finished.connect(lambda w=worker: self._on_cover_retry_finished(w))
        self.status.setText(f"正在只重试 {len(items)} 条封面，不会重新采集或转录…")
        worker.start()
        self._update_cover_actions()

    def _on_cover_retry_done(self, result) -> None:
        previous = {item.stable_key: item for item in self._items}
        updated = {
            item.stable_key: item
            for item in result.collect_result.items
        }
        persistence_note = ""
        if self._records_repo is not None and updated:
            try:
                persisted_keys = {
                    item.stable_key for item in self._records_repo.load().items
                }
                for stable_key, after in updated.items():
                    before = previous.get(stable_key)
                    if before is None:
                        continue
                    # A legacy local entry can surface a schema-v2 item before
                    # the master repository has its first row. Seed only that
                    # absent identity; the cover mutation itself still uses the
                    # field-owned CAS below.
                    if stable_key not in persisted_keys:
                        self._records_repo.upsert([before])
                        persisted_keys.add(stable_key)
                    committed, proved = _commit_cover_state(
                        self._records_repo, before, after
                    )
                    updated[stable_key] = committed
                    if not proved:
                        persistence_note = "；封面文件已处理，但作品主档更新失败"
                self._records_snapshot = self._records_repo.load()
            except RepositoryError:
                persistence_note = "；封面文件已处理，但作品主档更新失败"
        self._items = [updated.get(item.stable_key, item) for item in self._items]
        downloaded = sum(
            item.cover_status == "downloaded" for item in updated.values()
        )
        failed = sum(item.cover_status == "failed" for item in updated.values())
        recovered = sum(
            outcome.status == "recovered_previous"
            for outcome in getattr(result, "outcomes", ())
        )
        self._render()
        recovered_note = f"，已恢复上一次封面 {recovered} 条" if recovered else ""
        self.status.setText(
            f"封面重试完成：已保存 {downloaded} 条，仍未保存 {failed} 条"
            f"{recovered_note}"
            f"{persistence_note}。"
        )

    def _on_cover_retry_failed(self) -> None:
        self.status.setText("封面重试未完成；作品、转录和原封面链接均已保留。")

    def _on_cover_retry_finished(self, worker) -> None:
        if self._cover_retry_worker is worker:
            self._cover_retry_worker = None
        self._cover_retry_is_recovery = False
        worker.deleteLater()
        self._update_cover_actions()

    def _reveal_selected_cover(self) -> None:
        selected = self._checked_items()
        path = self._managed_cover_file(selected[0]) if len(selected) == 1 else None
        if path is None:
            self.status.setText("只有已安全保存且文件仍存在的封面才能在 Finder 中显示。")
            self._update_cover_actions()
            return
        subprocess.run(["open", "-R", str(path)], check=False)

    def _transcript_text_map(self) -> dict:
        """{稳定键: 转录文稿}，同时保留旧抖音 ID 映射。"""
        m: dict = {}
        try:
            from ..core import library
            for e in library.list_entries():
                if e.get("kind") != "transcript":
                    continue
                source = e.get("source") or ""
                transcript = library.load(e["id"])
                if transcript is not None and transcript.has_timestamps:
                    txt = export.format_transcript_for_export(transcript)
                else:
                    txt = library.load_text(e["id"]) or ""
                if not txt:
                    continue
                mt = re.search(r"/video/(\d+)", source)
                if mt:
                    m.setdefault(mt.group(1), txt)
                    m.setdefault(f"douyin:{mt.group(1)}", txt)
                identity = self._service.inspect(source).identity
                if identity.platform != "unknown" and identity.extracted_id:
                    m.setdefault(f"{identity.platform}:{identity.extracted_id}", txt)
        except Exception:  # noqa: BLE001
            pass
        return m

    def _on_export(self) -> None:
        if not self._view:
            self.status.setText("没有可导出的数据，请先采集。")
            return
        items = self._selected_or_all()
        path, selected = QFileDialog.getSaveFileName(
            self, "导出", "主页采集.xlsx",
            "Excel 工作簿 (*.xlsx);;CSV 文件 (*.csv)")
        if not path:
            return
        # 按所选格式规整扩展名（去掉用户/系统可能带错的后缀，补正确的）
        want_csv = "csv" in (selected or "").lower()
        for ext in (".csv", ".xlsx"):
            if path.lower().endswith(ext):
                path = path[:-len(ext)]
        path += ".csv" if want_csv else ".xlsx"
        transcripts = self._transcript_text_map()
        try:
            if want_csv:
                export.write_csv(items, path, transcripts)
            else:
                export.write_xlsx(items, path, transcripts)
        except Exception as exc:  # noqa: BLE001
            dialogs.alert(self, "导出失败", str(exc))
            return
        self.status.setText(f"已导出 {len(items)} 条到：{path}")

    def _on_export_bitable(self) -> None:
        preparation = getattr(self, '_feishu_prepare_worker', None)
        if preparation is not None and preparation.isRunning():
            self.status.setText('正在准备所选文稿，请稍候。')
            return
        active = getattr(self, "_feishu_session", None)
        if active is not None and active.isRunning():
            active.dialog.raise_()
            return
        items = tuple(self._checked_items())
        if not items:
            self.status.setText("请先在表格中勾选要存入飞书的作品。")
            return
        from .feishu_dialog import send_records_to_feishu
        from .feishu_save_dialog import _CallWorker
        from ..core.feishu_storage import record_from_collected_item
        captured = int(datetime.now().timestamp() * 1000)
        authors = {item.author_name for item in items if item.author_name}
        name = next(iter(authors)) if len(authors) == 1 else "已选作品"
        def prepare():
            transcripts = self._transcript_text_map()
            return tuple(record_from_collected_item(item, transcripts,
                source_entry='collection_selection', captured_at_ms=captured) for item in items)
        results = []
        worker = _CallWorker(prepare)
        self._feishu_prepare_worker = worker
        worker.result.connect(lambda ok,payload: results.append((ok,payload)))
        def finished():
            self._feishu_prepare_worker = None
            if not self._shutting_down and results:
                ok,payload = results[0]
                if ok:
                    self._feishu_session = send_records_to_feishu(self,self.cfg,payload,
                        title=f'{name} · {datetime.now():%Y-%m-%d}',context='homepage',toast=self.status.setText)
                else:
                    self.status.setText(str(payload))
            worker.deleteLater()
        worker.finished.connect(finished)
        self.status.setText('正在准备所选文稿…')
        worker.start()

    def _remember_bitable_destination(
        self,
        mode: str,
        *,
        source_url: str = "",
        table_name: str = "",
    ) -> bool:
        if self.cfg is None:
            return False
        previous = dict(getattr(self.cfg, "feishu_target", {}) or {})
        updated = dict(previous)
        updated["bitable_mode"] = "existing" if mode == "existing" else "new"
        if mode == "existing":
            updated["bitable_url"] = str(source_url or "").strip()
            updated["bitable_name"] = str(table_name or "").strip()
        self.cfg.feishu_target = updated
        saver = getattr(self.cfg, "save", None)
        try:
            saved = not callable(saver) or bool(saver())
        except Exception:  # noqa: BLE001 - 云端结果不能被本地偏好错误覆盖
            saved = False
        if not saved:
            self.cfg.feishu_target = previous
        return saved

    def _on_bitable_done(
        self,
        worker,
        ok: bool,
        payload: object,
        *,
        destination_name: str = "",
        result_title: str = "",
    ) -> None:
        if self._bitable_worker is not worker:
            return
        if bool(getattr(self.window(), "_close_settling", False)):
            return
        self._set_busy(False)
        if ok:
            receipt = payload
            selection = dict(getattr(worker, "selection", {}) or {})
            existing_url = str(getattr(worker, "existing_url", "") or "")
            resolved_target = getattr(worker, "resolved_target", None)
            is_existing = bool(existing_url and resolved_target is not None)
            if is_existing:
                destination_name = "指定的已有多维表格"
                result_title = str(
                    getattr(resolved_target, "table_name", "") or "已有多维表格"
                )
            if selection.get("register"):
                from .feishu_dialog import remember_feishu_destination

                preference_saved = remember_feishu_destination(
                    self.cfg,
                    selection,
                    worker,
                    receipt,
                )
            else:
                # 自定义只作用本次，不自动挤进长期作品库列表或默认项。
                preference_saved = True
            url = str(getattr(receipt, "url", "") or "")
            modal_title = "飞书导出结果"
            duplicate_count = int(
                getattr(receipt, "row_duplicate_skipped", 0) or 0
            )
            summary_lines = [
                (
                    f"位置：{destination_name}"
                    if is_existing
                    else (
                        f"位置：{destination_name or '云盘（我的文稿库）'}"
                        if selection.get("destination_kind") == "drive"
                        else f"位置：知识库「{destination_name or '所选知识库'}」"
                    )
                ),
                f"表格：{result_title or '本次新建的多维表格'}",
                f"数据行：{getattr(receipt, 'row_success', 0)}/{getattr(receipt, 'row_total', 0)}",
                f"封面文件上传：{getattr(receipt, 'cover_success', 0)}/{getattr(receipt, 'cover_candidate', 0)}",
                f"音视频附件：{getattr(receipt, 'media_success', 0)}/{getattr(receipt, 'media_candidate', 0)}",
            ]
            if duplicate_count:
                summary_lines.append(
                    f"已存在或本批重复：跳过 {duplicate_count} 条"
                )
            if (
                bool(getattr(receipt, "outcome_uncertain", False))
                or getattr(receipt, "row_unconfirmed", 0)
                or getattr(receipt, "cover_uncertain", 0)
                or getattr(receipt, "media_uncertain", 0)
            ):
                summary = getattr(receipt, "human_summary", None)
                if callable(summary):
                    modal_title, fact, next_step = summary()
                else:
                    from ..core.feishu import BitableExportReceipt
                    modal_title, fact, next_step = BitableExportReceipt.human_summary(
                        receipt
                    )
                status_text = f"{modal_title}：{fact}。{next_step}。"
                summary_lines.extend(("", fact + "。", next_step + "。"))
            elif bool(getattr(receipt, "complete", False)):
                if duplicate_count and receipt.row_total == 0:
                    modal_title = "作品已在飞书多维表格中"
                    status_text = (
                        f"目标表中已有这些作品，已跳过 {duplicate_count} 条，"
                        "没有重复写入。"
                    )
                else:
                    modal_title = (
                        "已追加到飞书多维表格"
                        if is_existing
                        else "已存入飞书多维表格"
                    )
                    status_text = (
                        ("已追加到飞书多维表格：" if is_existing else "已存入飞书多维表格：")
                        + f"{receipt.row_success}/{receipt.row_total} 条，"
                        f"封面文件 {receipt.cover_success}/{receipt.cover_candidate} 张，"
                        f"音视频附件 {receipt.media_success}/{receipt.media_candidate} 个。"
                    )
                    if duplicate_count:
                        status_text += f" 已跳过重复作品 {duplicate_count} 条。"
            else:
                missing = (
                    receipt.cover_skipped
                    + receipt.cover_failed
                    + receipt.cover_uncertain
                )
                modal_title = "飞书追加部分完成" if is_existing else "飞书导出部分完成"
                status_text = (
                    f"数据写入飞书：{receipt.row_success}/{receipt.row_total} 条。"
                    f"封面文件已上传 {receipt.cover_success} 张，{missing} 张未上传，"
                    f"音视频成功 {getattr(receipt, 'media_success', 0)} 个，"
                    f"{getattr(receipt, 'media_skipped', 0) + getattr(receipt, 'media_failed', 0) + getattr(receipt, 'media_uncertain', 0)} 个未完成。"
                    "未完成附件已留空。"
                )
                summary_lines.extend(("", "未完成附件已留空。"))
                if duplicate_count:
                    status_text += f" 已跳过重复作品 {duplicate_count} 条。"
            details = getattr(receipt, "safe_details", lambda: "")()
            if details:
                summary_lines.extend(("", "需要确认的项：", details))
            if not preference_saved:
                summary_lines.extend(("", "最近使用的飞书目标没有保存，下次需要重新选择。"))
                status_text += " 最近目标未保存。"
            self.status.setText(status_text)
            message = "\n".join(summary_lines)
            if url.startswith("https://"):
                if dialogs.confirm_open(self, modal_title, message):
                    QDesktopServices.openUrl(QUrl(url))
            else:
                dialogs.alert(self, modal_title, message, ok="确定")
            return
        message = str(payload or "").strip() or "飞书导出未完成，请稍后重试。"
        self.status.setText("飞书导出未完成。")
        dialogs.alert(
            self,
            "存入飞书多维表格失败",
            message,
        )

    def _finish_bitable_worker(self, worker) -> None:
        if self._bitable_worker is worker:
            self._bitable_worker = None
        worker.deleteLater()
        self._set_busy(False)
        self._update_tools_enabled()

    def _on_transcribe_selected(self) -> None:
        items = self._checked_items()
        if not items:
            self.status.setText("请先勾选要转录的作品。")
            return
        from ..core.task import TaskGroupCandidate

        sources = []
        candidates = []
        for index, item in enumerate(items):
            source = ""
            skipped_reason = ""
            if item.is_image:
                skipped_reason = "图文作品无音频"
            elif item.platform == "douyin" and item.aweme_id:
                # 保留旧抖音分享页转录链。
                source = f"https://www.iesdouyin.com/share/video/{item.aweme_id}/"
            elif item.platform == "xiaohongshu" and item.work_url:
                source = item.work_url
            elif item.platform not in {"douyin", "xiaohongshu"}:
                skipped_reason = "当前平台尚未接入转录"
            else:
                skipped_reason = "缺少可靠作品链接"
            if source:
                sources.append(source)
            candidates.append(
                TaskGroupCandidate(
                    stable_key=item.stable_key or f"unidentified:{index}",
                    title=(item.title or item.author_name or "未命名作品").strip(),
                    source=source,
                    skipped_reason=skipped_reason,
                )
            )
        skipped = len(items) - len(sources)
        if not sources:
            if all(item.is_image for item in items):
                self.status.setText("选中的都是图文帖（无音频），无法转录。")
            else:
                self.status.setText("选中的作品缺少可靠链接或尚不支持转录。")
            return
        if not self._on_transcribe:
            dialogs.alert(self, "批量转录", "转录入口未接入。")
            return
        # 多条 → 这批自动归入「YYYYMMDD 作者」文件夹；单条留在根目录
        folder_name = None
        if len(sources) > 1:
            author = (items[0].author_name or "未知作者").strip()
            folder_name = f"{datetime.now():%Y%m%d} {author}"
        owner = getattr(self._on_transcribe, "__self__", None)
        group_handler = getattr(owner, "_collect_transcribe_group", None)
        if callable(group_handler):
            group_handler(candidates, folder_name)
        else:
            # 保留独立页面测试和第三方调用者的旧双参数回调合同。
            self._on_transcribe(sources, folder_name)
        msg = f"已把 {len(sources)} 条加入转录队列（在工作台查看进度）。"
        if skipped:
            msg += f"（{skipped} 条无法转录的作品已跳过）"
        self.status.setText(msg)

    def _update_tools_enabled(self) -> None:
        has = bool(self._view)
        selected = bool(self._selected_keys)
        self.select_all_btn.setEnabled(has)
        self.material_download_btn.setEnabled(self._material_worker is None)
        self.export_btn.setEnabled(has or selected)
        self.transcribe_btn.setEnabled(selected)
        self.bitable_btn.setEnabled(selected and self._bitable_worker is None)
        self.bitable_btn.setToolTip(
            "" if selected else "请先勾选至少一条作品"
        )
        self._update_cover_actions()

    def refresh(self) -> None:
        self._refresh_login_status()
        self._on_input_changed(self.input.text())
        if not self._xhs_release_disabled():
            self._sync_xhs_browser_status()
        # 回到本页时刷新「已转录」状态（可能刚在工作台转录了）
        self._request_transcribed_read()
        if self._items:
            self._fill_table(status_only=True)
