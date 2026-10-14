"""Versioned document commits with durable, replayable receipts across MD/JSON."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import library
from .store import encode, identity, now, workspace_root


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _folder(name):
    path = workspace_root() / name
    if path.is_symlink():
        raise ValueError("恢复目录不安全")
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _read(path):
    if path.is_symlink():
        raise ValueError("恢复记录路径不安全")
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError('保存恢复记录格式异常，原资料已保留')
    return value


def _validate_receipt(receipt, document_id, request_id):
    required = {'document_id', 'request_id', 'state', 'before', 'after', 'before_hash', 'after_hash', 'result', 'request_hash'}
    if (type(receipt) is not dict or not required <= receipt.keys()
            or receipt['document_id'] != document_id or receipt['request_id'] != request_id
            or type(receipt['state']) is not str
            or receipt['state'] not in {'prepared', 'committed'}):
        raise ValueError('保存恢复记录不完整，原资料已保留')
    for key in ('before', 'after'):
        record = receipt[key]
        if (type(record) is not dict or record.get('id') != document_id
                or type(record.get('revision')) is not int
                or any(type(record.get(name)) is not str for name in ('text', 'title', 'markdown_path'))):
            raise ValueError('保存恢复正文记录异常，原资料已保留')
    if (type(receipt['result']) is not dict
            or receipt['result'].get('document_id') != document_id
            or receipt['result'].get('request_id') != request_id
            or any(type(receipt[k]) is not str or len(receipt[k]) != 64
                   for k in ('before_hash', 'after_hash', 'request_hash'))):
        raise ValueError('保存恢复结果记录异常，原资料已保留')


def _finish(receipt, path):
    eid = receipt["document_id"]
    md = library._markdown_abs(receipt["after"]["markdown_path"], eid)
    current = md.read_text(encoding="utf-8") if md.exists() else ""
    raw = library._load_raw(eid)
    if raw is None or digest(current) not in {receipt["before_hash"], receipt["after_hash"]}:
        raise library.RevisionConflict("上次保存待恢复，磁盘又有新内容；双方资料保留，请先处理冲突")
    if raw not in (receipt["before"], receipt["after"]):
        raise library.RevisionConflict("上次保存的元信息已有变化，未覆盖新记录")
    if receipt["before"].get("text") != receipt["after"]["text"]:
        history = library.LIBRARY_DIR / ".versions" / eid / f'{receipt["before"]["revision"]}-{receipt["before_hash"]}.md'
        if not history.exists():
            library._write_atomic_text(history, receipt["before"]["text"])
    if digest(current) != receipt["after_hash"]:
        library._write_atomic_text(md, receipt["after"]["text"])
    if raw != receipt["after"]:
        library._write_raw(receipt["after"])
    receipt["state"] = "committed"
    library._write_atomic_text(path, encode(receipt))
    pending = _folder("pending") / (eid + ".json")
    pending.unlink(missing_ok=True)
    return receipt["result"]


def recover(document_id: str):
    identity(document_id)
    with library._transaction():
        marker = _folder("pending") / (document_id + ".json")
        if marker.exists():
            pending = _read(marker)
            rid = identity(pending.get("request_id"))
            path = _folder("operations") / (rid + ".json")
            receipt = _read(path) if path.exists() else pending.get("receipt")
            _validate_receipt(receipt, document_id, rid)
            if receipt["document_id"] != document_id:
                raise ValueError("恢复记录不匹配")
            if receipt["state"] == "committed":
                marker.unlink()
            else:
                _finish(receipt, path)


def snapshot(document_id: str) -> dict:
    with library._transaction():
        recover(document_id)
        doc = library.document_snapshot(document_id)
        if doc is None or doc.get("kind", "transcript") not in {"transcript", "rewrite"}:
            raise FileNotFoundError("文稿不存在或已移入最近删除")
        return doc


def save(document_id: str, markdown: str, title: str, expected_revision: int,
         expected_hash: str, request_id: str) -> dict:
    identity(document_id); identity(request_id)
    if type(expected_revision) is not int or not isinstance(markdown, str) or not isinstance(title, str) or not title.strip():
        raise ValueError("保存内容或版本无效")
    if len(markdown.encode()) > 8 * 1024 * 1024 or len(title) > 500:
        raise ValueError("文稿超出单次保存范围，未截断")
    request_hash = digest(encode([document_id, markdown, title, expected_revision, expected_hash]))
    with library._transaction():
        path = _folder("operations") / (request_id + ".json")
        if path.exists():
            old = _read(path)
            _validate_receipt(old, document_id, request_id)
            if old["request_hash"] != request_hash:
                raise library.RevisionConflict("同一保存编号对应不同内容，未覆盖")
            return old["result"] if old["state"] == "committed" else _finish(old, path)
        before = snapshot(document_id)
        if before["revision"] != expected_revision or before["content_hash"] != expected_hash:
            raise library.RevisionConflict("文稿已有新修改，当前编辑保留，请查看最新版本")
        after = dict(before, text=markdown, title=title.strip(), content_hash=digest(markdown), char_count=len(markdown))
        changed = markdown != before["text"] or title.strip() != before["title"]
        if changed:
            after.update(revision=before["revision"] + 1, updated_at=now())
        if markdown != before["text"] and before.get("segments"):
            after["meta"] = dict(before.get("meta") or {}, segments_stale=True)
        result = {"document_id": document_id, "revision": after["revision"], "content_hash": after["content_hash"],
                  "request_id": request_id, "updated_at": after["updated_at"], "changed": changed}
        receipt = dict(document_id=document_id, request_id=request_id, request_hash=request_hash, state="prepared",
                       before=before, after=after, before_hash=before["content_hash"], after_hash=after["content_hash"], result=result)
        # The first durable record contains the whole intent. Even a crash before
        # the operation receipt is written can be recovered by the next reader.
        library._write_atomic_text(_folder("pending") / (document_id + ".json"),
                                   encode({"request_id": request_id, "receipt": receipt}))
        library._write_atomic_text(path, encode(receipt))
        return _finish(receipt, path)


def receipt(request_id):
    value = _read(_folder("operations") / (identity(request_id) + ".json"))
    _validate_receipt(value, identity(value.get('document_id')), request_id)
    return {"state": value["state"], **value["result"]}


def apply_operations(body: str, operations: list[dict]) -> str:
    """All ranges are UTF-8 byte positions in exactly this frozen Markdown."""
    raw = body.encode("utf-8")
    spans, appends = [], []
    for op in operations:
        if op.get("action") not in {"replace", "append"} or not isinstance(op.get("markdown"), str):
            raise ValueError("修改动作格式无效")
        start, end = (len(raw), len(raw)) if op["action"] == "append" else (op.get("start"), op.get("end"))
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(raw):
            raise ValueError("引用位置已失效")
        try:
            original = raw[start:end].decode("utf-8")
            raw[:start].decode("utf-8"); raw[end:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("引用边界无效，未修改正文") from exc
        if op.get("before_hash") != digest(original):
            raise library.RevisionConflict("引用原文已有变化")
        replacement = op["markdown"]
        if op["action"] == "append":
            appends.append("\n\n" + replacement)
            continue
        spans.append((start, end, replacement.encode("utf-8")))
    spans.sort(key=lambda x: (x[0], x[1]))
    for a, b in zip(spans, spans[1:]):
        if a[1] > b[0] or (a[0] == a[1] == b[0] == b[1]):
            raise ValueError("修改范围重叠，请分别审阅")
    for start, end, replacement in reversed(spans):
        raw = raw[:start] + replacement + raw[end:]
    return raw.decode("utf-8") + "".join(appends)
