"""采集结果导出:CSV(零依赖、带 BOM)+ Excel(.xlsx,openpyxl)。

列 = 作者/标题/发布日期/时长/点赞/收藏/评论/分享/原作品链接/音视频直链/转录文稿。
「转录文稿」来自文稿库(按 aweme_id 匹配),未转录留空 —— 由调用方传入 transcripts。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field as dataclass_field
import hashlib
import io
import math
import os
from pathlib import Path
import stat
from typing import Iterable

from .bitable_media import (
    BitableMediaCandidate,
    BitableMediaIssue,
    prepare_bitable_media,
)
from .models import CollectedItem

HEADERS = [
    "作者昵称", "标题", "发布日期", "时长",
    "点赞", "收藏", "评论", "分享",
    "原作品链接", "音视频直链", "转录文稿", "封面状态", "封面本地文件",
]

_UNKNOWN_EXPORT = "未获取"
_BITABLE_COVER_MAX_BYTES = 20 * 1024 * 1024
_BITABLE_COVER_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_COVER_STATUS_LABELS = {
    "not_requested": "未下载封面",
    "url_only": "已有封面链接，尚未保存",
    "failed": "封面保存失败",
}


def _timecode(seconds: object) -> str:
    if isinstance(seconds, bool):
        return ""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value < 0:
        return ""
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_transcript_for_export(transcript: object) -> str:
    """为表格出口补上已有分段时间；无可用时间戳时保留原文。"""

    from ..output.segments import speaker_text
    plain = str(getattr(transcript, "text", "") or "")
    if (getattr(transcript, "meta", {}) or {}).get("segments_stale", False):
        return plain
    segments = getattr(transcript, "segments", ())
    if not isinstance(segments, (list, tuple)):
        return plain
    lines: list[str] = []
    has_timestamp = False
    for segment in segments:
        text = str(getattr(segment, "text", "") or "").strip()
        if not text:
            continue
        text = speaker_text(segment)
        stamp = _timecode(getattr(segment, "start", None))
        if stamp:
            has_timestamp = True
            lines.append(f"[{stamp}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines) if has_timestamp and lines else plain


def _transcript_value(
    item: CollectedItem,
    transcripts: dict | None,
) -> str:
    values = transcripts or {}
    for key in (item.stable_key, item.aweme_id):
        if not key:
            continue
        value = values.get(key)
        if value:
            return str(value)
    return ""


def _managed_cover_file(
    relative_path: str,
    *,
    read_limit: int | None = None,
) -> tuple[Path, bytes | None] | None:
    """以 openat 链验证受管封面，并可在同一稳定身份内有界读取。

    ``read_limit`` 为空时只验证路径；提供时返回文件字节。任何身份、大小或
    修改时间漂移都会拒绝，且不会把候选路径暴露给上层错误信息。
    """

    relative = Path(str(relative_path or ""))
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    descriptors: list[int] = []
    try:
        from ..models_manager import app_data_dir

        data_root = Path(os.path.abspath(app_data_dir()))
        collection_root = data_root / "collection"
        raw_configured_root = collection_root / "covers"
        raw_platform_root = raw_configured_root / relative.parts[0]
        managed_directories = (
            data_root,
            collection_root,
            raw_configured_root,
            raw_platform_root,
        )
        managed_identities: list[tuple[Path, int, int]] = []
        for directory in managed_directories:
            directory_info = os.stat(directory, follow_symlinks=False)
            if not stat.S_ISDIR(directory_info.st_mode):
                return None
            managed_identities.append(
                (directory, directory_info.st_dev, directory_info.st_ino)
            )

        configured_root = raw_configured_root.resolve(strict=True)
        if configured_root == Path(configured_root.anchor):
            return None
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        current_fd = os.open(configured_root.anchor, directory_flags)
        descriptors.append(current_fd)
        directory_entries: list[tuple[int, str, int, int, int]] = []

        for component in configured_root.parts[1:] + (relative.parts[0],):
            child_fd = os.open(component, directory_flags, dir_fd=current_fd)
            descriptors.append(child_fd)
            child_info = os.fstat(child_fd)
            entry_info = os.stat(
                component, dir_fd=current_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(child_info.st_mode)
                or not stat.S_ISDIR(entry_info.st_mode)
                or (entry_info.st_dev, entry_info.st_ino)
                != (child_info.st_dev, child_info.st_ino)
            ):
                return None
            directory_entries.append(
                (
                    current_fd,
                    component,
                    child_fd,
                    child_info.st_dev,
                    child_info.st_ino,
                )
            )
            current_fd = child_fd

        configured_root_info = os.fstat(directory_entries[-2][2])
        platform_root_info = os.fstat(current_fd)
        if (
            (configured_root_info.st_dev, configured_root_info.st_ino)
            != (managed_identities[2][1], managed_identities[2][2])
            or (platform_root_info.st_dev, platform_root_info.st_ino)
            != (managed_identities[3][1], managed_identities[3][2])
        ):
            return None

        filename = relative.parts[1]
        before = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(filename, flags, dir_fd=current_fd)
        descriptors.append(file_fd)
        opened = os.fstat(file_fd)
        after = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_nlink != 1
        ):
            return None
        for parent_fd, component, child_fd, expected_dev, expected_ino in (
            directory_entries
        ):
            current = os.stat(
                component, dir_fd=parent_fd, follow_symlinks=False
            )
            opened_directory = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(current.st_mode)
                or not stat.S_ISDIR(opened_directory.st_mode)
                or (current.st_dev, current.st_ino)
                != (expected_dev, expected_ino)
                or (opened_directory.st_dev, opened_directory.st_ino)
                != (expected_dev, expected_ino)
            ):
                return None
        for directory, expected_dev, expected_ino in managed_identities:
            current = os.stat(directory, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (expected_dev, expected_ino)
            ):
                return None
        payload: bytes | None = None
        if read_limit is not None:
            limit = int(read_limit)
            if limit <= 0 or opened.st_size < 1 or opened.st_size > limit:
                return None
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if not payload or len(payload) > limit:
                return None
        final_file = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(final_file.st_mode)
            or final_file.st_nlink != 1
            or (final_file.st_dev, final_file.st_ino)
            != (opened.st_dev, opened.st_ino)
            or final_file.st_size != opened.st_size
            or final_file.st_mtime_ns != opened.st_mtime_ns
        ):
            return None
        return configured_root / relative, payload
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def resolve_managed_cover_file(relative_path: str) -> Path | None:
    """返回受管根内、身份稳定、无符号链接且单链接的普通文件。"""

    resolved = _managed_cover_file(relative_path)
    return resolved[0] if resolved is not None else None


def _cover_export_values(it: CollectedItem) -> tuple[str, str]:
    if it.cover_status != "downloaded":
        label = _COVER_STATUS_LABELS.get(it.cover_status, "封面状态未知")
        if str(it.last_error_code or "").endswith("cover_commit_uncertain"):
            label = "上一份封面待恢复"
        return label, ""
    if not it.cover_local_path:
        return "本地封面文件已丢失", ""
    candidate = resolve_managed_cover_file(it.cover_local_path)
    if candidate is None:
        return "本地封面文件已丢失", ""
    return "封面已保存", str(candidate)


def _export_field(it: CollectedItem, name: str):
    state = it.field_state(name)
    if state == "unknown":
        return _UNKNOWN_EXPORT
    if state == "not_applicable":
        return "图文" if name == "duration_ms" else _UNKNOWN_EXPORT
    value = it.known_numeric(name)
    if name == "create_time":
        if value == 0 and it.has_field_state(name):
            return 0
        return it.publish_date
    if name == "duration_ms":
        if value == 0 and it.has_field_state(name):
            return 0
        return it.duration_text
    return value


def _row(it: CollectedItem, transcripts: dict | None = None) -> list:
    text = _transcript_value(it, transcripts)
    cover_status, cover_path = _cover_export_values(it)
    return [
        it.author_name, it.title,
        _export_field(it, "create_time"), _export_field(it, "duration_ms"),
        _export_field(it, "like"), _export_field(it, "collect"),
        _export_field(it, "comment"), _export_field(it, "share"),
        it.work_url, it.play_url, text, cover_status, cover_path,
    ]


def to_csv_string(items: Iterable[CollectedItem], transcripts: dict | None = None) -> str:
    """采集结果 → CSV 文本(含表头)。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    for it in items:
        w.writerow(_row(it, transcripts))
    return buf.getvalue()


def write_csv(items: Iterable[CollectedItem], dst: str | Path,
              transcripts: dict | None = None) -> Path:
    """导出 CSV 到 dst(UTF-8 BOM,Excel 友好)。返回路径。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(to_csv_string(items, transcripts), encoding="utf-8-sig", newline="")
    return dst


# 飞书字段定义；三元组第三项是原生字段 property，None 表示显式 null。
BITABLE_FIELDS = [
    ("作者昵称", 1), ("标题", 1), ("发布日期", 5), ("时长", 1),
    ("点赞", 2, {"formatter": "0"}),
    ("收藏", 2, {"formatter": "0"}),
    ("评论", 2, {"formatter": "0"}),
    ("分享", 2, {"formatter": "0"}),
    ("原作品链接", 15), ("音视频直链", 15), ("转录文稿", 1),
    ("封面", 17, None),
    ("音视频附件", 17, None),
]
BITABLE_HEADERS = [field[0] for field in BITABLE_FIELDS]


@dataclass(frozen=True)
class BitableCoverCandidate:
    record_index: int
    stable_key: str
    title: str
    file_name: str
    mime_type: str
    data: bytes = dataclass_field(repr=False)


@dataclass(frozen=True)
class BitableCoverIssue:
    record_index: int
    stable_key: str
    title: str
    reason_code: str
    retry_hint: str


@dataclass(frozen=True)
class BitableExportPreflight:
    records: tuple[dict, ...]
    cover_candidates: tuple[BitableCoverCandidate, ...]
    cover_skipped: tuple[BitableCoverIssue, ...]
    media_candidates: tuple[BitableMediaCandidate, ...] = ()
    media_skipped: tuple[BitableMediaIssue, ...] = ()

    @property
    def row_count(self) -> int:
        return len(self.records)

    @property
    def media_total_bytes(self) -> int:
        return sum(candidate.size for candidate in self.media_candidates)


def to_bitable_records(items: Iterable[CollectedItem], transcripts: dict | None = None) -> list:
    """采集结果 → 飞书 bitable 批量写入用的 records=[{"fields": {...}}, ...]。

    空值/无效日期/空链接一律省略该字段(不写空对象,避免飞书报字段类型错误)。
    """
    records = []
    for it in items:
        f: dict = {}
        if it.author_name:
            f["作者昵称"] = it.author_name
        if it.title:
            f["标题"] = it.title
        create_time = it.known_numeric("create_time")
        if create_time is not None and (create_time or it.has_field_state("create_time")):
            f["发布日期"] = create_time * 1000   # 飞书日期字段:epoch 毫秒
        duration_ms = it.known_numeric("duration_ms")
        if duration_ms is not None:
            if duration_ms == 0 and it.has_field_state("duration_ms"):
                f["时长"] = "0"
            elif it.duration_text:
                f["时长"] = it.duration_text
        for col, name in (("点赞", "like"), ("收藏", "collect"),
                          ("评论", "comment"), ("分享", "share")):
            val = it.known_numeric(name)
            if type(val) is int and val >= 0:
                f[col] = val
        if it.work_url:
            f["原作品链接"] = {"link": it.work_url, "text": it.work_url}
        if it.play_url:
            f["音视频直链"] = {"link": it.play_url, "text": it.play_url}
        text = _transcript_value(it, transcripts)
        if text:
            f["转录文稿"] = text
        records.append({"fields": f})
    return records


def _cover_payload_mime(path: Path, data: bytes) -> str:
    suffix = path.suffix.casefold()
    expected = _BITABLE_COVER_TYPES.get(suffix, "")
    if expected == "image/jpeg" and data.startswith(b"\xff\xd8\xff"):
        return expected
    if expected == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return expected
    if (
        expected == "image/webp"
        and len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP"
    ):
        return expected
    return ""


def _cover_issue(
    item: CollectedItem,
    record_index: int,
    reason_code: str,
) -> BitableCoverIssue:
    hints = {
        "cover_not_downloaded": "回到采集结果保存或重试封面后再试",
        "cover_path_missing": "回到采集结果重新保存封面后再试",
        "cover_file_rejected": "检查本地封面仍存在且未被替换后再试",
        "cover_type_mismatch": "重新保存为 JPG、PNG 或 WebP 后再试",
    }
    return BitableCoverIssue(
        record_index=record_index,
        stable_key=item.stable_key or f"row-{record_index + 1}",
        title=str(item.title or "")[:120],
        reason_code=reason_code,
        retry_hint=hints[reason_code],
    )


def prepare_bitable_export(
    items: Iterable[CollectedItem],
    transcripts: dict | None = None,
    *,
    media_root: Path | str | None = None,
) -> BitableExportPreflight:
    """冻结记录、受管封面和音视频；不读取远端 URL 或用户素材目录。"""

    frozen_items = tuple(items)
    records = tuple(to_bitable_records(frozen_items, transcripts))
    candidates: list[BitableCoverCandidate] = []
    skipped: list[BitableCoverIssue] = []
    for record_index, item in enumerate(frozen_items):
        if item.cover_status != "downloaded":
            if item.cover_url or item.cover_status in {"url_only", "failed"}:
                skipped.append(
                    _cover_issue(item, record_index, "cover_not_downloaded")
                )
            continue
        if not item.cover_local_path:
            skipped.append(_cover_issue(item, record_index, "cover_path_missing"))
            continue
        managed = _managed_cover_file(
            item.cover_local_path,
            read_limit=_BITABLE_COVER_MAX_BYTES,
        )
        if managed is None or managed[1] is None:
            skipped.append(_cover_issue(item, record_index, "cover_file_rejected"))
            continue
        path, data = managed
        mime_type = _cover_payload_mime(path, data)
        if not mime_type:
            skipped.append(_cover_issue(item, record_index, "cover_type_mismatch"))
            continue
        identity = item.stable_key or f"row-{record_index + 1}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        candidates.append(
            BitableCoverCandidate(
                record_index=record_index,
                stable_key=identity,
                title=str(item.title or "")[:120],
                file_name=f"cover-{digest}{path.suffix.casefold()}",
                mime_type=mime_type,
                data=data,
            )
        )
    media_candidates, media_skipped = prepare_bitable_media(
        frozen_items,
        root=media_root,
    )
    return BitableExportPreflight(
        records=records,
        cover_candidates=tuple(candidates),
        cover_skipped=tuple(skipped),
        media_candidates=media_candidates,
        media_skipped=media_skipped,
    )


def write_xlsx(items: Iterable[CollectedItem], dst: str | Path,
               transcripts: dict | None = None) -> Path:
    """导出真·Excel(.xlsx):表头加粗、列宽合理、转录文稿列自动换行。返回路径。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "采集结果"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for it in items:
        ws.append(_row(it, transcripts))
    widths = [14, 40, 12, 8, 8, 8, 8, 8, 36, 36, 60, 24, 48]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows(min_row=2, min_col=11, max_col=11):  # 转录文稿列
        row[0].alignment = wrap
    ws.freeze_panes = "A2"  # 冻结表头
    wb.save(str(dst))
    return dst
