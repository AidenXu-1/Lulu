"""文稿库:把完成的转写结果持久化到本地,可回看 / 搜索 / 重新导出。

落点:`<app-data>/Lulu/library/<id>.json`(纯本地,不上传,守住离线红线)。
每条 = 一个 JSON,含全文 + 分段时间戳 + 元信息(标题/时间/引擎/语种/来源)。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import fcntl
from contextlib import contextmanager
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import Segment, Transcript, TranscriptCover
from .models_manager import app_data_dir

LIBRARY_DIR = app_data_dir() / "library"
DOCS_DIR_NAME = "docs"
_mutation_lock = threading.RLock()
_lock_state = threading.local()


class RevisionConflict(RuntimeError):
    """A caller's original document no longer matches the saved document."""


class FolderIndexError(RuntimeError):
    pass


@contextmanager
def _transaction():
    """Coordinate cooperating GUI/Core processes; nested operations share one lock."""
    with _mutation_lock:
        if getattr(_lock_state, "depth", 0):
            yield
            return
        root = _dir()
        if root.is_symlink():
            raise ValueError("素材库目录不安全，已保留记录")
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            fd = os.open(".library.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _lock_state.depth = 1
            yield
        finally:
            _lock_state.depth = 0
            os.close(fd)


def _serialized(function):
    @wraps(function)
    def call(*args, **kwargs):
        if getattr(sys, "frozen", False) and Path(sys.executable).name == "Lulu":
            from .library_gateway import dispatch
            return dispatch(function.__name__, args, kwargs)
        with _transaction():
            return function(*args, **kwargs)
    return call

# 标题里常残留来源文件后缀(转写自 xxx.mp3),展示时去掉,免得被误认成文件格式。
_TITLE_STRIP_EXTS = {
    # 音频
    "mp3", "wav", "m4a", "aac", "flac", "ogg", "oga", "opus", "wma", "aiff", "aif", "alac", "amr",
    # 视频
    "mp4", "mov", "mkv", "webm", "avi", "flv", "m4v", "3gp", "ts", "wmv", "mpg", "mpeg",
    # 文本/文档(本地上传来源)
    "txt", "md", "markdown", "text", "rtf", "srt", "vtt", "docx",
}


def display_title(title: str) -> str:
    """展示用标题:去掉名字里残留的媒体/文档后缀(如 .mp3)。只去掉一个、且必须是已知后缀。"""
    t = (title or "").strip()
    dot = t.rfind(".")
    if dot > 0 and t[dot + 1:].lower() in _TITLE_STRIP_EXTS:
        return t[:dot].rstrip()
    return t


def reliable_source_url(entry: dict, meta: dict | None = None) -> str:
    """取素材的可跳转原作品链接，兼容旧索引和批量采集元数据。"""

    facts = meta if isinstance(meta, dict) else (entry.get("meta") or {})
    candidates = (
        facts.get("original_url"),
        facts.get("webpage_url"),
        facts.get("work_url"),
        facts.get("source_url"),
        entry.get("source"),
    )
    for candidate in candidates:
        raw = str(candidate or "").strip()
        try:
            parsed = urlsplit(raw)
        except ValueError:
            continue
        if (
            parsed.scheme.casefold() in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return raw
    return ""


def _dir() -> Path:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    return LIBRARY_DIR


def audio_storage_dir(*, preview: bool = False) -> Path:
    """Managed audio lives beside library records; downloads are explicit copies."""
    path = _dir() / ("previews" if preview else "audio")
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_audio(source: str | Path, directory: str | Path, filename: str) -> Path:
    import shutil
    from .synthesis.indextts2_results import unique_output_path
    source = Path(source).expanduser()
    if not source.is_file():
        raise ValueError("音频文件不存在，请重新生成或选择其他结果。")
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    # A temporary file and exclusive publication preserve existing downloads.
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as f:
        temp = Path(f.name)
    try:
        shutil.copyfile(source, temp)
        with temp.open('rb') as f:
            os.fsync(f.fileno())
        while True:
            target = unique_output_path(folder / Path(filename).name)
            try:
                os.link(temp, target)
                return target
            except FileExistsError:
                continue
    finally:
        temp.unlink(missing_ok=True)


def _entry_path(entry_id: str) -> Path:
    if (not isinstance(entry_id, str) or not entry_id or entry_id in {".", "..", "folders"}
            or any(c in entry_id for c in ("/", "\\", "\x00"))):
        raise ValueError("素材 id 不安全")
    return _dir() / f"{entry_id}.json"


def _write_raw(d: dict) -> None:
    entry_id = str(d.get("id") or "")
    if not entry_id:
        raise ValueError("素材 id 不能为空")
    path = _entry_path(entry_id)
    payload = json.dumps(d, ensure_ascii=False, indent=2)
    _write_atomic_text(path, payload)


def _write_atomic_text(path: Path, payload: str) -> None:
    """正文和索引共用完整写入后替换；失败时保留上一次完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_name = f.name
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_name).replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _markdown_rel(entry_id: str) -> str:
    return f"{DOCS_DIR_NAME}/{entry_id}.md"


def _markdown_abs(rel_or_abs: str | None, entry_id: str) -> Path:
    raw = (rel_or_abs or _markdown_rel(entry_id)).strip()
    p = Path(raw).expanduser()
    return p if p.is_absolute() else _dir() / p


def _sync_markdown_data(d: dict) -> tuple[dict, bool]:
    """确保文本类素材有 Markdown 正文文件;外部改 md 时同步回 JSON 文本字段。"""
    kind = d.get("kind", "transcript")
    if kind not in {"transcript", "rewrite"}:
        return d, False
    entry_id = str(d.get("id") or "")
    if not entry_id:
        return d, False
    changed = False
    rel = str(d.get("markdown_path") or _markdown_rel(entry_id))
    path = _markdown_abs(rel, entry_id)
    text = str(d.get("text") or "")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic_text(path, text)
    try:
        md = path.read_text(encoding="utf-8")
    except OSError:
        md = text
    new_hash = _content_hash(md)
    if d.get("markdown_path") != rel:
        d["markdown_path"] = rel
        changed = True
    if d.get("content_hash") != new_hash:
        if md != text and int(d.get("revision") or 0):
            history = _dir() / ".versions" / entry_id / f'{d["revision"]}-{_content_hash(text)}.md'
            if not history.exists():
                _write_atomic_text(history, text)
        if md != text and d.get("segments"):
            d["meta"] = {**(d.get("meta") or {}), "segments_stale": True}
        d["content_hash"] = new_hash
        d["text"] = md
        d["char_count"] = len(md)
        d["revision"] = int(d.get("revision") or 0) + 1
        d["updated_at"] = datetime.now().isoformat(timespec="seconds")
        changed = True
    elif "revision" not in d:
        d["revision"] = 1
        changed = True
    return d, changed


def _write_text_entry(d: dict, text: str, *, preserve_timestamps: bool = False) -> None:
    entry_id = str(d.get("id") or "")
    if not entry_id:
        raise ValueError("素材 id 不能为空")
    rel = str(d.get("markdown_path") or _markdown_rel(entry_id))
    path = _markdown_abs(rel, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if int(d.get("revision") or 0) and d.get("text", "") != (text or ""):
        history = _dir() / ".versions" / entry_id / f'{d["revision"]}-{_content_hash(d.get("text", ""))}.md'
        if not history.exists():
            _write_atomic_text(history, d.get("text", ""))
    _write_atomic_text(path, text or "")
    if not preserve_timestamps and text != d.get("text", "") and d.get("segments"):
        d["meta"] = {**(d.get("meta") or {}), "segments_stale": True}
    d["markdown_path"] = rel
    d["text"] = text or ""
    d["char_count"] = len(text or "")
    d["content_hash"] = _content_hash(text or "")
    d["revision"] = int(d.get("revision") or 0) + 1
    d["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_raw(d)


@_serialized
def markdown_path(entry_id: str) -> Path | None:
    """返回文本素材对应的 Markdown 文件路径;旧条目会懒生成。"""
    d = _load_raw(entry_id)
    if d is None or d.get("kind", "transcript") not in {"transcript", "rewrite"}:
        return None
    d, changed = _sync_markdown_data(d)
    if changed:
        _write_raw(d)
    return _markdown_abs(str(d.get("markdown_path") or ""), entry_id)


@_serialized
def load_markdown(entry_id: str, *, require_exists: bool = False) -> str:
    """读取文本素材的 Markdown 正文;外部改过文件时同步 JSON。"""
    d = _load_raw(entry_id)
    if d is None:
        if require_exists:
            raise FileNotFoundError("已保存文稿不存在或无法读取")
        return ""
    d, changed = _sync_markdown_data(d)
    if changed:
        _write_raw(d)
    return str(d.get("text") or "")


@_serialized
def save_markdown(entry_id: str, markdown: str, *, preserve_timestamps: bool = False,
                  expected_revision: int | None = None, expected_hash: str | None = None) -> None:
    """保存 Markdown 正文到 .md 文件并同步 JSON 文本字段。"""
    d = _load_raw(entry_id)
    if d is None:
        raise FileNotFoundError(entry_id)
    if d.get("kind", "transcript") not in {"transcript", "rewrite"}:
        raise ValueError("只有文稿 / 输出稿支持 Markdown 正文。")
    d, changed = _sync_markdown_data(d)
    if changed:
        _write_raw(d)
    if ((expected_revision is not None and d.get("revision") != expected_revision)
            or (expected_hash is not None and d.get("content_hash") != expected_hash)):
        raise RevisionConflict("原文已有更新，未覆盖。当前处理结果仍保留，请另存或重新处理。")
    _write_text_entry(d, markdown or "", preserve_timestamps=preserve_timestamps)


@_serialized
def document_snapshot(entry_id: str) -> dict | None:
    """Read body and version together, including edits from an external editor."""
    d = _load_raw(entry_id)
    if d is None:
        return None
    d, changed = _sync_markdown_data(d)
    if changed:
        _write_raw(d)
    return dict(d)


@_serialized
def append_markdown_section(entry_id: str, heading: str, text: str) -> None:
    """往正文末尾追加一个 Markdown 小节。"""
    body = load_markdown(entry_id).rstrip()
    title = (heading or "处理结果").strip()
    addition = (text or "").strip()
    if not addition:
        return
    save_markdown(entry_id, f"{body}\n\n## {title}\n\n{addition}\n" if body else f"## {title}\n\n{addition}\n")


# --------------------------------------------------------------------------- #
# 文件夹:成员关系存在每条目的 folder_id 字段(随条目走、移动不破坏);
# 文件夹元信息(id/name/kind)存索引文件 folders.json,按 id 引用 → 改名不破坏。
# --------------------------------------------------------------------------- #
FOLDERS_FILE = LIBRARY_DIR / "folders.json"


def _load_folders() -> list[dict]:
    try:
        d = json.loads(FOLDERS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, ValueError) as exc:
        raise FolderIndexError("文件夹索引无法读取，原文件已保留。") from exc
    folders = d.get("folders") if isinstance(d, dict) else None
    if (not isinstance(folders, list)
            or any(not isinstance(f, dict) or not isinstance(f.get("id"), str)
                   or not f["id"] or not isinstance(f.get("name"), str)
                   or not isinstance(f.get("kind", ""), str) for f in folders)
            or any(("order" in f and type(f["order"]) not in {int, float})
                   or ("created_at" in f and not isinstance(f["created_at"], str)) for f in folders)
            or len({f["id"] for f in folders}) != len(folders)):
        raise FolderIndexError("文件夹索引结构异常，原文件已保留。")
    return folders


def _save_folders(folders: list[dict]) -> None:
    _dir()
    _write_atomic_text(FOLDERS_FILE, json.dumps({"folders": folders}, ensure_ascii=False, indent=2))


def _new_folder_id() -> str:
    now = datetime.now()
    base = "f_" + now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
    existing = {f.get("id") for f in _load_folders()}
    fid, n = base, 1
    while fid in existing:
        fid, n = f"{base}_{n}", n + 1
    return fid


@_serialized
def create_folder(kind: str, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("文件夹名不能为空")
    folders = _load_folders()
    fid = _new_folder_id()
    folders.append({"id": fid, "name": name, "kind": kind,
                    "created_at": datetime.now().isoformat(timespec="seconds")})
    _save_folders(folders)
    return fid


@_serialized
def ensure_folder(kind: str, name: str) -> str:
    """同 (kind, name) 已存在则复用,否则新建。批量再转录会合并到同一文件夹。"""
    name = (name or "").strip()
    for f in _load_folders():
        if f.get("kind") == kind and (f.get("name") or "").strip() == name:
            return f["id"]
    return create_folder(kind, name)


@_serialized
def rename_folder(folder_id: str, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("文件夹名不能为空")
    folders = _load_folders()
    for f in folders:
        if f.get("id") == folder_id:
            f["name"] = name
            _save_folders(folders)
            return name
    raise FileNotFoundError(folder_id)


@_serialized
def delete_folder(folder_id: str) -> None:
    """删文件夹:从索引移除 + 把成员条目的 folder_id 清空(回根),不删条目本身。"""
    folders = _load_folders()
    if not any(f["id"] == folder_id for f in folders):
        raise FileNotFoundError(folder_id)
    for f in _dir().glob("*.json"):
        if f.name.startswith(".") or f.name == FOLDERS_FILE.name:
            continue
        try:
            d = _load_raw(f.stem)
        except (OSError, json.JSONDecodeError):
            continue
        if d and d.get("folder_id") == folder_id:
            d["folder_id"] = None
            _write_raw(d)
    # Keep the folder visible until every readable member has been moved.
    # A failed write leaves a retryable operation, never a dangling removed folder.
    _save_folders([f for f in folders if f["id"] != folder_id])


@_serialized
def set_entry_folder(entry_id: str, folder_id: str | None) -> None:
    """把一条素材移入文件夹(folder_id=None 为移回根)。对各 kind 通用。"""
    d = _load_raw(entry_id)
    if d is None:
        raise FileNotFoundError(entry_id)
    if folder_id is not None:
        target = next((f for f in _load_folders() if f["id"] == folder_id), None)
        compatible = target is not None and (
            target.get("kind") == d.get("kind", "transcript") or
            {target.get("kind"), d.get("kind", "transcript")} <= {"transcript", "rewrite"})
        if not compatible:
            raise ValueError("目标文件夹不存在或不属于当前素材库")
    d["folder_id"] = folder_id
    _write_raw(d)


@_serialized
def list_folders(kind: str) -> list[dict]:
    """该 kind 的文件夹(带 count 成员数),按创建时间倒序。"""
    folders = [dict(f) for f in _load_folders() if f.get("kind") == kind]
    counts: dict[str, int] = {}
    for f in _dir().glob("*.json"):
        if f.name.startswith(".") or f.name == FOLDERS_FILE.name:
            continue
        d = _load_raw(f.stem)
        if d is None:
            continue
        fid = d.get("folder_id")
        if isinstance(fid, str) and fid:
            counts[fid] = counts.get(fid, 0) + 1
    for f in folders:
        f["count"] = counts.get(f["id"], 0)
    # 默认按创建时间倒序;手动拖拽排过序的(带 order)排到前面、按 order 升序。
    folders.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    folders.sort(key=lambda x: x.get("order", float("inf")))
    return folders


@_serialized
def set_folder_order(ordered_ids: list[str]) -> None:
    """按给定 id 顺序给文件夹写入 order 字段(拖拽重排用)。未列出的不动。"""
    pos = {fid: i for i, fid in enumerate(ordered_ids)}
    folders = _load_folders()
    changed = False
    for f in folders:
        if f.get("id") in pos and f.get("order") != pos[f["id"]]:
            f["order"] = pos[f["id"]]
            changed = True
    if changed:
        _save_folders(folders)


@_serialized
def save(transcript: Transcript, title: str, folder_id: str | None = None, *, entry_id: str | None = None) -> str:
    """存一条文稿,返回其 id。id 用时间戳,保证可排序、基本不撞。"""
    now = datetime.now()
    base = now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
    import hashlib
    import re
    result_digest = hashlib.sha256(json.dumps([transcript.source, transcript.text, transcript.engine], ensure_ascii=False).encode()).hexdigest()
    if entry_id is not None:
        if not isinstance(entry_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,160}", entry_id) is None:
            raise ValueError("无效的文稿保存凭据")
        existing = _load_raw(entry_id)
        if existing is not None:
            if existing.get("result_digest") != result_digest or existing.get("kind") != "transcript":
                raise ValueError("文稿保存凭据与原内容不一致")
            return entry_id
        if (_dir() / f"{entry_id}.json").exists():
            raise ValueError("文稿索引无法读取，原文件已保留")
        orphan = _markdown_abs(None, entry_id)
        if orphan.exists() and orphan.read_text(encoding="utf-8") != transcript.text:
            raise ValueError("未提交文稿已改变，原内容已保留")
    else:
        entry_id, n = base, 1
        while (_dir() / f"{entry_id}.json").exists():
            entry_id, n = f"{base}_{n}", n + 1
    cover_meta = transcript.cover.merge_meta(transcript.meta)
    data = {
        "id": entry_id,
        "kind": "transcript",
        "result_digest": result_digest,
        "folder_id": folder_id,
        "title": title,
        "created_at": now.isoformat(timespec="seconds"),
        "engine": transcript.engine,
        "language": transcript.language,
        "source": transcript.source,
        "char_count": len(transcript.text),
        "meta": cover_meta,
        "text": transcript.text,
        "segments": [
            {"text": s.text, "start": s.start, "end": s.end} for s in transcript.segments
        ],
    }
    _write_text_entry(data, transcript.text)
    return entry_id


@_serialized
def save_rewrite(text: str, title: str, source_title: str | None = None,
                 folder_id: str | None = None, *, source_id: str | None = None,
                 source_revision: int | None = None, source_hash: str | None = None,
                 request_id: str | None = None) -> str:
    """存一条「改写文稿」(纯文本,无音频/分段),返回 id。与转录文稿分类管理。"""
    now = datetime.now()
    base = now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
    entry_id, n = base, 1
    while (_dir() / f"{entry_id}.json").exists():
        entry_id, n = f"{base}_{n}", n + 1
    data = {
        "id": entry_id,
        "kind": "rewrite",
        "folder_id": folder_id,
        "title": title,
        "created_at": now.isoformat(timespec="seconds"),
        "engine": "",
        "language": None,
        "source": f"改写自:{source_title}" if source_title else "改写文稿",
        "char_count": len(text),
        "meta": {"source_id": source_id, "source_revision": source_revision,
                 "source_hash": source_hash, "request_id": request_id},
        "text": text,
        "segments": [],
    }
    _write_text_entry(data, text)
    return entry_id


@_serialized
def save_document(text: str, title: str, source: str = "手动文稿",
                  folder_id: str | None = None, meta: dict | None = None) -> str:
    """把上传/自定义输入沉淀为文稿库记录,不触碰用户原文件。"""
    tr = Transcript(
        text=text or "",
        segments=[],
        language=None,
        engine="",
        source=source,
        title=title,
        meta=meta or {},
    )
    return save(tr, title or "文稿", folder_id=folder_id)


def _new_id() -> tuple[str, datetime]:
    now = datetime.now()
    base = now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"
    entry_id, n = base, 1
    while (_dir() / f"{entry_id}.json").exists():
        entry_id, n = f"{base}_{n}", n + 1
    return entry_id, now


def _text_preview(text: str, limit: int = 120) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


@_serialized
def save_voice(
    *,
    title: str,
    audio_path: str | Path,
    text: str,
    engine: str,
    voice_name: str,
    config_summary: str,
    voice_id: str | None = None,
    duration_seconds: float | None = None,
    source_type: str = "manual",
    source_id: str | None = None,
    source_title: str | None = None,
    source_text_snapshot: str | None = None,
    source_chain: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    folder_id: str | None = None,
) -> str:
    """存一条配音素材索引。音频文件不搬家,只记录本次生成的实际路径。"""
    entry_id, now = _new_id()
    audio = Path(audio_path).expanduser()
    source_text = source_text_snapshot if source_text_snapshot is not None else text
    data = {
        "id": entry_id,
        "kind": "voice",
        "folder_id": folder_id,
        "title": title.strip() or audio.stem or "配音素材",
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "audio_path": str(audio),
        "audio_filename": audio.name,
        "duration_seconds": duration_seconds,
        "text": text,
        "text_preview": _text_preview(text),
        "engine": engine,
        "voice_id": voice_id,
        "voice_name": voice_name.strip() or "未命名音色",
        "config_summary": config_summary.strip() or engine,
        "source_type": source_type or "manual",
        "source_id": source_id,
        "source_title": source_title,
        "source_text_snapshot": source_text,
        "source_chain": source_chain or [],
        "meta": meta or {},
    }
    _write_raw(data)
    return entry_id


@_serialized
def list_entries(*, errors: list[str] | None = None) -> list[dict]:
    """返回所有文稿的元信息(不含全文),按时间倒序。"""
    out: list[dict] = []
    for f in _dir().glob("*.json"):
        if f.name.startswith(".") or f.name == FOLDERS_FILE.name:
            continue
        try:
            d = _load_raw(f.stem)
            if d is None:
                raise ValueError("素材索引结构异常")
            mm = d.get("meta") or {}
            kind = d.get("kind", "transcript")
            if kind not in {"transcript", "rewrite", "voice"}:
                continue  # Retired records remain on disk, outside active libraries.
            if kind == "voice":
                audio_path = Path(str(d.get("audio_path") or "")).expanduser()
                source_id = d.get("source_id")
                source_missing = bool(source_id and _load_raw(str(source_id)) is None)
                audio_missing = bool(d.get("audio_path") and not audio_path.exists())
                source_type = d.get("source_type") or "manual"
                source_title = d.get("source_title") or ""
                if source_type == "manual":
                    source_label = "来源:手动输入"
                elif source_missing:
                    source_label = "来源不可用"
                elif source_title:
                    source_label = f"来源:{source_title}"
                else:
                    source_label = f"来源:{source_type}"
                out.append({
                    "id": d.get("id", f.stem),
                    "kind": "voice",
                    "folder_id": d.get("folder_id"),
                    "title": d.get("title", "(无标题)"),
                    "created_at": d.get("created_at", ""),
                    "updated_at": d.get("updated_at", ""),
                    "audio_path": d.get("audio_path", ""),
                    "audio_filename": d.get("audio_filename", ""),
                    "duration_seconds": d.get("duration_seconds"),
                    "text_preview": d.get("text_preview") or _text_preview(d.get("text", "")),
                    "text": d.get("text", ""),
                    "engine": d.get("engine", ""),
                    "voice_id": d.get("voice_id"),
                    "voice_name": d.get("voice_name", ""),
                    "config_summary": d.get("config_summary", ""),
                    "source_type": source_type,
                    "source_id": source_id,
                    "source_title": source_title,
                    "source_label": source_label,
                    "source_text_snapshot": d.get("source_text_snapshot", ""),
                    "missing_flags": {
                        "audio_missing": audio_missing,
                        "source_missing": source_missing,
                    },
                })
                continue
            d, changed = _sync_markdown_data(d)
            if changed:
                _write_raw(d)
            cover = TranscriptCover.from_meta(mm)
            out.append({
                "id": d.get("id", f.stem),
                "kind": kind,   # 旧条目缺省视为转录文稿(向后兼容)
                "folder_id": d.get("folder_id"),
                "title": d.get("title", "(无标题)"),
                "created_at": d.get("created_at", ""),
                "engine": d.get("engine", ""),
                "source": d.get("source", ""),
                "source_url": reliable_source_url(d, mm),
                "language": d.get("language"),
                "char_count": d.get("char_count", len(d.get("text", ""))),
                "has_summary": bool(d.get("summary")),
                "uploader": mm.get("uploader"),
                "duration": mm.get("duration"),
                "stats": mm.get("stats"),
                "author": mm.get("author"),
                "cover_platform": cover.platform,
                "cover_work_id": cover.work_id,
                "cover_url": cover.url,
                "cover_local_path": cover.local_path,
                "cover_status": cover.status,
                "cover_error_code": cover.error_code,
            })
        except (OSError, UnicodeError, ValueError, TypeError):
            if errors is not None:
                errors.append(f.name)
            continue
    out.sort(key=lambda e: e["id"], reverse=True)
    return out


def list_voice_entries() -> list[dict]:
    return [e for e in list_entries() if e.get("kind") == "voice"]


def query_entries(**options) -> dict:
    if getattr(sys, "frozen", False) and Path(sys.executable).name == "Lulu":
        from .library_gateway import dispatch
        return dispatch("query_entries", (), options)
    from .library_query import query
    return query(**options)


@_serialized
def load_voice(entry_id: str) -> dict | None:
    d = _load_raw(entry_id)
    if not d or d.get("kind") != "voice":
        return None
    audio_path = Path(str(d.get("audio_path") or "")).expanduser()
    source_id = d.get("source_id")
    missing_flags = {
        "audio_missing": not d.get("audio_path") or not audio_path.is_file(),
        "source_missing": bool(source_id and _load_raw(str(source_id)) is None),
    }
    d["missing_flags"] = missing_flags
    return d


def _load_raw(entry_id: str) -> dict | None:
    f = _entry_path(entry_id)
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return None
        for key, expected in (("meta", dict), ("text", str), ("id", str), ("segments", list)):
            if key in d and not (key == "meta" and d[key] is None) and not isinstance(d[key], expected):
                return None
        if any(not isinstance(s, dict) for s in d.get("segments", [])):
            return None
        if d.get("id", f.stem) != f.stem:
            return None
        for key in ("kind", "title", "created_at", "updated_at", "markdown_path", "audio_path",
                    "folder_id", "source_id", "source_type", "source", "voice_name"):
            if d.get(key) is not None and not isinstance(d[key], str):
                return None
        d.setdefault("id", f.stem)
        return d
    except (OSError, UnicodeError, ValueError):
        return None


@_serialized
def save_summary(entry_id: str, summary: str) -> None:
    """把生成的摘要写回该文稿条目,持久化保存。"""
    d = _load_raw(entry_id)
    if d is None:
        return
    d["summary"] = summary
    _write_raw(d)


@_serialized
def get_summary(entry_id: str) -> str | None:
    """读已保存的摘要;没有返回 None。"""
    d = _load_raw(entry_id)
    return (d or {}).get("summary") or None


@_serialized
def rename(entry_id: str, title: str) -> str:
    """重命名素材标题并持久化。空标题不允许保存,返回保存后的标题。"""
    new_title = (title or "").strip()
    if not new_title:
        raise ValueError("标题不能为空")
    d = _load_raw(entry_id)
    if d is None:
        raise FileNotFoundError(entry_id)
    d["title"] = new_title
    d["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_raw(d)
    return new_title


@_serialized
def load(entry_id: str) -> Transcript | None:
    """读出一条完整文稿,重建成 Transcript。"""
    d = _load_raw(entry_id)
    if d is None:
        return None
    try:
        d, changed = _sync_markdown_data(d)
        if changed:
            _write_raw(d)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    segs = [
        Segment(text=s.get("text", ""), start=s.get("start"), end=s.get("end"))
        for s in d.get("segments", [])
    ]
    meta = TranscriptCover.from_meta(d.get("meta") or {}).merge_meta(
        d.get("meta") or {}
    )
    meta["lulu_entry_id"] = entry_id
    return Transcript(
        text=d.get("text", ""),
        segments=segs,
        language=d.get("language"),
        engine=d.get("engine"),
        source=d.get("source"),
        title=d.get("title"),
        meta=meta,
    )


def load_text(entry_id: str) -> str:
    """只读全文(搜索用,省得重建对象)。"""
    return load_markdown(entry_id)


@_serialized
def delete(entry_id: str) -> None:
    """Move verified records to a recoverable state; never remove voice audio."""
    raw_id = str(entry_id or "")
    if (
        not raw_id
        or raw_id in {".", ".."}
        or Path(raw_id).name != raw_id
        or "/" in raw_id
        or "\\" in raw_id
        or "\x00" in raw_id
    ):
        raise ValueError("素材 id 不安全，已保留记录")

    root = LIBRARY_DIR.expanduser()
    try:
        root_lstat = root.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(root_lstat.st_mode) or stat.S_ISLNK(root_lstat.st_mode):
        raise ValueError("素材库目录不安全，已保留记录")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, directory_flags)
    docs_fd: int | None = None
    markdown_lstat = None
    try:
        root_opened = os.fstat(root_fd)
        if (root_opened.st_dev, root_opened.st_ino) != (root_lstat.st_dev, root_lstat.st_ino):
            raise ValueError("素材库目录身份已变化，已保留记录")

        json_name = f"{raw_id}.json"
        try:
            json_lstat = os.stat(json_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(json_lstat.st_mode) or json_lstat.st_nlink != 1:
            raise ValueError("素材记录文件不安全，已保留记录")

        json_fd = os.open(
            json_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            json_opened = os.fstat(json_fd)
            if (
                not stat.S_ISREG(json_opened.st_mode)
                or json_opened.st_nlink != 1
                or (json_opened.st_dev, json_opened.st_ino)
                != (json_lstat.st_dev, json_lstat.st_ino)
            ):
                raise ValueError("素材记录文件身份已变化，已保留记录")
            with os.fdopen(json_fd, "r", encoding="utf-8", closefd=False) as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("素材记录无法安全读取，已保留记录") from exc
        finally:
            os.close(json_fd)

        if not isinstance(data, dict) or str(data.get("id") or "") != raw_id:
            raise ValueError("素材记录身份不一致，已保留记录")
        expected_markdown = _markdown_rel(raw_id)
        markdown_path = str(data.get("markdown_path") or expected_markdown).strip()
        if markdown_path != expected_markdown:
            raise ValueError("素材正文路径不安全，已保留记录")

        try:
            docs_lstat = os.stat(DOCS_DIR_NAME, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            docs_lstat = None
        if docs_lstat is not None:
            if not stat.S_ISDIR(docs_lstat.st_mode) or stat.S_ISLNK(docs_lstat.st_mode):
                raise ValueError("素材正文目录不安全，已保留记录")
            docs_fd = os.open(DOCS_DIR_NAME, directory_flags, dir_fd=root_fd)
            docs_opened = os.fstat(docs_fd)
            if (docs_opened.st_dev, docs_opened.st_ino) != (
                docs_lstat.st_dev,
                docs_lstat.st_ino,
            ):
                raise ValueError("素材正文目录身份已变化，已保留记录")

            markdown_name = f"{raw_id}.md"
            try:
                markdown_lstat = os.stat(
                    markdown_name,
                    dir_fd=docs_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                markdown_lstat = None
            if markdown_lstat is not None:
                if not stat.S_ISREG(markdown_lstat.st_mode) or markdown_lstat.st_nlink != 1:
                    raise ValueError("素材正文文件不安全，已保留记录")
                markdown_fd = os.open(
                    markdown_name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=docs_fd,
                )
                try:
                    markdown_opened = os.fstat(markdown_fd)
                finally:
                    os.close(markdown_fd)
                if (
                    not stat.S_ISREG(markdown_opened.st_mode)
                    or markdown_opened.st_nlink != 1
                    or (markdown_opened.st_dev, markdown_opened.st_ino)
                    != (markdown_lstat.st_dev, markdown_lstat.st_ino)
                ):
                    raise ValueError("素材正文文件身份已变化，已保留记录")

        current_root = root.lstat()
        current_json = os.stat(json_name, dir_fd=root_fd, follow_symlinks=False)
        if (
            (current_root.st_dev, current_root.st_ino)
            != (root_opened.st_dev, root_opened.st_ino)
            or (current_json.st_dev, current_json.st_ino)
            != (json_lstat.st_dev, json_lstat.st_ino)
        ):
            raise ValueError("素材记录身份已变化，已保留记录")
        if docs_fd is not None:
            current_docs = (root / DOCS_DIR_NAME).lstat()
            if (current_docs.st_dev, current_docs.st_ino) != (
                docs_opened.st_dev,
                docs_opened.st_ino,
            ):
                raise ValueError("素材正文目录身份已变化，已保留记录")
            if markdown_lstat is not None:
                current_markdown = os.stat(
                    markdown_name,
                    dir_fd=docs_fd,
                    follow_symlinks=False,
                )
                if (
                    current_markdown.st_dev,
                    current_markdown.st_ino,
                ) != (
                    markdown_lstat.st_dev,
                    markdown_lstat.st_ino,
                ):
                    raise ValueError("素材正文文件身份已变化，已保留记录")

        token = secrets.token_hex(12)
        staged_json = f".delete-{raw_id}-{token}.json"
        staged_markdown = f".delete-{raw_id}-{token}.md"
        trash = root / ".trash"
        trash.mkdir(mode=0o700, exist_ok=True)
        if trash.is_symlink():
            raise ValueError("最近删除目录不安全，已保留记录")
        receipt = trash / f"{token}.json"
        _write_atomic_text(receipt, json.dumps({
            "token": token, "id": raw_id, "title": data.get("title") or raw_id,
            "kind": data.get("kind"),
            "deleted_at": datetime.now().isoformat(timespec="seconds"),
            "json": staged_json, "markdown": staged_markdown if markdown_lstat else None,
            "json_identity": [json_lstat.st_dev, json_lstat.st_ino],
            "markdown_identity": [markdown_lstat.st_dev, markdown_lstat.st_ino] if markdown_lstat else None,
            "volume_uuid": _trash_volume_uuid(root),
            "root_device": root_opened.st_dev,
        }, ensure_ascii=False))
        json_staged = False
        markdown_staged = False
        try:
            os.rename(
                json_name,
                staged_json,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            json_staged = True
            moved_json = os.stat(staged_json, dir_fd=root_fd, follow_symlinks=False)
            if (moved_json.st_dev, moved_json.st_ino) != (
                json_lstat.st_dev,
                json_lstat.st_ino,
            ):
                raise ValueError("素材记录文件身份已变化，已保留记录")

            if docs_fd is not None and markdown_lstat is not None:
                os.rename(
                    markdown_name,
                    staged_markdown,
                    src_dir_fd=docs_fd,
                    dst_dir_fd=docs_fd,
                )
                markdown_staged = True
                moved_markdown = os.stat(
                    staged_markdown,
                    dir_fd=docs_fd,
                    follow_symlinks=False,
                )
                if (moved_markdown.st_dev, moved_markdown.st_ino) != (
                    markdown_lstat.st_dev,
                    markdown_lstat.st_ino,
                ):
                    raise ValueError("素材正文文件身份已变化，已保留记录")

            os.fsync(root_fd)
            if docs_fd is not None:
                os.fsync(docs_fd)
        except Exception:
            rollback_failed = False
            if markdown_staged and docs_fd is not None:
                try:
                    os.rename(
                        staged_markdown,
                        markdown_name,
                        src_dir_fd=docs_fd,
                        dst_dir_fd=docs_fd,
                    )
                except OSError:
                    rollback_failed = True
            if json_staged:
                try:
                    os.rename(
                        staged_json,
                        json_name,
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                    )
                except OSError:
                    rollback_failed = True
            if rollback_failed:
                raise ValueError("素材删除事务回滚失败，已保留现场")
            receipt.unlink(missing_ok=True)
            raise

        # Keep staged files and receipt until restore or explicitly enabled disposal.
        os.fsync(root_fd)
    except FileNotFoundError as exc:
        raise ValueError("素材目录身份已变化，已保留记录") from exc
    finally:
        if docs_fd is not None:
            os.close(docs_fd)
        os.close(root_fd)


@_serialized
def list_deleted() -> list[dict]:
    rows = []
    for path in (_dir() / ".trash").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(record, dict) and record.get("token") == path.stem
                    and record.get("json") == f'.delete-{record.get("id")}-{path.stem}.json'
                    and ((_dir() / record["json"]).is_file() or record.get("purging"))):
                rows.append({k: record[k] for k in ("token", "id", "title", "deleted_at")}
                            | {"kind": record.get("kind")})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(rows, key=lambda r: r["deleted_at"], reverse=True)


def _trash_volume_uuid(root: Path) -> str:
    from .artifact_trust import _volume_uuid
    return _volume_uuid(root)


def _current_trash_identity(root: Path, data: dict) -> dict:
    """Resolve persistent volume/inode identity into this mount's device number.

    Keep operation-local dev/inode checks unchanged. Older receipts have no
    volume UUID: only mutually consistent JSON/body records may cross mounts.
    This helper never changes a receipt or the retained files on disk.
    """
    device = root.lstat().st_dev
    identities = [data.get("json_identity"), data.get("markdown_identity")]
    identities += [item[1] for item in data.get("purge_history", [])]
    identities = [value for value in identities if value is not None]
    if not all(isinstance(value, list) and len(value) == 2
               and all(type(part) is int for part in value) for value in identities):
        raise ValueError("删除凭据身份无效，已保留资料")
    volume = ""
    if data.get("volume_uuid"):
        volume = _trash_volume_uuid(root)
        if not volume or volume != data["volume_uuid"]:
            raise ValueError("待恢复资料所在卷已变化，已保留资料")
        if (type(data.get("root_device")) is not int
                or any(value[0] != data["root_device"] for value in identities)):
            raise ValueError("删除凭据跨卷身份不一致，已保留资料")
    if all(value[0] == device for value in identities):
        return data
    if not data.get("volume_uuid"):
        volume = _trash_volume_uuid(root)
        if len({value[0] for value in identities}) != 1:
            raise ValueError("旧删除凭据跨卷身份不一致，已保留资料")
        import re
        token, eid = data.get("token"), data.get("id")
        _entry_path(eid)
        if (not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{24}", token) is None
                or data.get("purging") or not data.get("markdown")
                or data.get("json") != f".delete-{eid}-{token}.json"
                or data["markdown"] != f".delete-{eid}-{token}.md"):
            raise ValueError("旧删除凭据无法确认原文件，已保留资料")

        def read_retained(path, identity, limit):
            if path.parent.is_symlink():
                raise ValueError("待恢复路径不安全，已保留资料")
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_dev != device or before.st_ino != identity[1]
                    or before.st_size > limit):
                raise ValueError("旧删除资料身份已变化，已保留资料")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("待恢复文件已变化，已保留资料")
                content = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                if (len(content) > limit or after.st_nlink != 1
                        or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                        != (before.st_size, before.st_mtime_ns, before.st_ctime_ns)):
                    raise ValueError("读取期间资料已变化，已保留资料")
                return content.decode("utf-8")

        raw = json.loads(read_retained(root / data["json"], data["json_identity"], 16 * 1024 * 1024))
        body = read_retained(root / DOCS_DIR_NAME / data["markdown"], data["markdown_identity"], 8 * 1024 * 1024)
        if (raw.get("id") != eid or raw.get("markdown_path") != _markdown_rel(eid)
                or raw.get("content_hash") != _content_hash(body)
                or body != (raw.get("markdown") or raw.get("text") or "")):
            raise ValueError("旧删除资料内容无法相互确认，已保留资料")
    result = dict(data)
    for key in ("json_identity", "markdown_identity"):
        if data.get(key) is not None:
            result[key] = [device, data[key][1]]
    if "purge_history" in data:
        result["purge_history"] = [[name, [device, identity[1]]]
                                   for name, identity in data["purge_history"]]
    if volume:
        result["volume_uuid"] = volume
        result["root_device"] = device
    return result


@_serialized
def restore_deleted(token: str) -> str:
    import re
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{24}", token) is None:
        raise ValueError("恢复凭据无效")
    root = _dir()
    receipt = root / ".trash" / f"{token}.json"
    if root.is_symlink() or receipt.is_symlink() or receipt.parent.is_symlink():
        raise ValueError("恢复路径不安全，已保留资料")
    stored_data = json.loads(receipt.read_text(encoding="utf-8"))
    data = _current_trash_identity(root, stored_data)
    if data.get("purging"):
        raise ValueError("这项资料已开始彻底删除，请重试删除剩余文件")
    eid = data["id"]
    destination = _entry_path(eid)
    pairs = [(root / f".delete-{eid}-{token}.json", destination, data["json_identity"])]
    if data.get("markdown"):
        pairs.append((root / DOCS_DIR_NAME / f".delete-{eid}-{token}.md",
                      root / DOCS_DIR_NAME / f"{eid}.md", data["markdown_identity"]))
    if root.is_symlink() or receipt.is_symlink() or receipt.parent.is_symlink():
        raise ValueError("恢复路径不安全，已保留资料")
    pending = []
    for source, target, identity in pairs:
        if source.parent.is_symlink() or target.is_symlink():
            raise RevisionConflict("原位置已有资料或路径已变化，未覆盖。")
        if target.exists():
            saved = target.lstat()
            if not stat.S_ISREG(saved.st_mode) or [saved.st_dev, saved.st_ino] != identity:
                raise RevisionConflict("原位置已有资料，未覆盖。")
            # Resume an interrupted delete/restore whose original file survived.
            if source.exists():
                staged = source.lstat()
                if not stat.S_ISREG(staged.st_mode) or [staged.st_dev, staged.st_ino] != identity:
                    raise ValueError("待恢复资料身份已变化，已保留现场")
            continue
        current = source.lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or [current.st_dev, current.st_ino] != identity):
            raise ValueError("待恢复资料身份已变化，已保留现场")
        pending.append((source, target, identity))
    if data is not stored_data:
        # Persist the verified mount identity before creating resumable links.
        _write_atomic_text(receipt, json.dumps(data, ensure_ascii=False))
    created = []
    try:
        # Exclusive hard links publish without ever overwriting a concurrent writer.
        for source, target, _ in reversed(pending):
            os.link(source, target, follow_symlinks=False)
            created.append((target, _))
    except Exception:
        for target, identity in created:
            current = target.lstat()
            if [current.st_dev, current.st_ino] == identity:
                target.unlink()
        raise
    for source, _, _ in pairs:
        source.unlink(missing_ok=True)
    receipt.unlink()
    for parent in {source.parent for source, _, _ in pairs} | {receipt.parent}:
        fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return eid


@_serialized
def list_versions(entry_id: str) -> list[dict]:
    import re
    _entry_path(entry_id)
    root = _dir() / ".versions" / entry_id
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("修改记录路径不安全")
    rows = []
    for path in root.glob("*.md"):
        match = re.fullmatch(r"([0-9]+)-[a-f0-9]{64}\.md", path.name)
        saved = path.lstat()
        if match and stat.S_ISREG(saved.st_mode):
            rows.append({"name": path.name, "revision": int(match[1]),
                         "saved_at": datetime.fromtimestamp(saved.st_mtime).isoformat(timespec="seconds"),
                         "bytes": saved.st_size})
    return sorted(rows, key=lambda r: (r["saved_at"], r["revision"]), reverse=True)


@_serialized
def read_version(entry_id: str, name: str) -> str:
    import re
    _entry_path(entry_id)
    if not isinstance(name, str) or re.fullmatch(r"[0-9]+-[a-f0-9]{64}\.md", name) is None:
        raise ValueError("版本凭据无效")
    path = _dir() / ".versions" / entry_id / name
    if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise ValueError("修改记录路径不安全")
    saved = path.lstat()
    if not stat.S_ISREG(saved.st_mode) or saved.st_size > 8 * 1024 * 1024:
        raise ValueError("修改记录无法读取，原文件已保留")
    text = path.read_text(encoding="utf-8")
    if _content_hash(text) != name.split("-", 1)[1][:-3]:
        raise ValueError("历史版本内容已经改变，未覆盖当前文稿")
    return text


@_serialized
def restore_version(entry_id: str, name: str, expected_hash: str) -> None:
    save_markdown(entry_id, read_version(entry_id, name), expected_hash=expected_hash)


@_serialized
def deleted_preview(token: str) -> dict:
    import re
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{24}", token) is None:
        raise ValueError("恢复凭据无效")
    root = _dir()
    receipt = root / ".trash" / f"{token}.json"
    if root.is_symlink() or receipt.is_symlink() or receipt.parent.is_symlink():
        raise ValueError("恢复路径不安全")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data = _current_trash_identity(root, data)
    if data.get("purging"):
        raise ValueError("这项资料已开始彻底删除，请重试删除剩余文件")
    eid = data["id"]
    _entry_path(eid)
    staged = root / f".delete-{eid}-{token}.json"
    saved = staged.lstat()
    if (not stat.S_ISREG(saved.st_mode) or [saved.st_dev, saved.st_ino] != data["json_identity"]
            or saved.st_size > 16 * 1024 * 1024):
        raise ValueError("待恢复资料身份已变化，原文件已保留")
    raw = json.loads(staged.read_text(encoding="utf-8"))
    text = raw.get("source_text_snapshot") or raw.get("markdown") or raw.get("text") or ""
    if data.get("markdown"):
        markdown = root / DOCS_DIR_NAME / f".delete-{eid}-{token}.md"
        saved = markdown.lstat()
        if (markdown.parent.is_symlink() or not stat.S_ISREG(saved.st_mode)
                or [saved.st_dev, saved.st_ino] != data["markdown_identity"]
                or saved.st_size > 8 * 1024 * 1024):
            raise ValueError("待恢复正文身份已变化，原文件已保留")
        text = markdown.read_text(encoding="utf-8")
    return dict(title=raw.get("title") or data.get("title"), kind=raw.get("kind"),
                deleted_at=data.get("deleted_at"), text=text)


@_serialized
def relink_voice(entry_id: str, audio_path: str) -> None:
    d = _load_raw(entry_id)
    path = Path(audio_path).expanduser()
    if d is None or d.get("kind") != "voice":
        raise FileNotFoundError(entry_id)
    if not path.is_file() or path.suffix.casefold() not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aiff"}:
        raise ValueError("请选择可读取的音频文件")
    d.update(audio_path=str(path.resolve()), audio_filename=path.name,
             updated_at=datetime.now().isoformat(timespec="seconds"))
    _write_raw(d)
