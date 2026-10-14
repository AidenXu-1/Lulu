"""Closed GUI adapter to the existing measured Core; no second storage owner."""
from __future__ import annotations

import dataclasses
import inspect
import json
import hashlib
import re
import threading
import base64
import zlib
from pathlib import Path
from uuid import uuid4

OPERATIONS = frozenset({
    "markdown_path", "load_markdown", "save_markdown", "document_snapshot",
    "append_markdown_section", "create_folder", "ensure_folder", "rename_folder",
    "delete_folder", "set_entry_folder", "list_folders", "set_folder_order",
    "save", "save_rewrite", "save_document", "save_voice", "list_entries",
    "load_voice", "save_summary", "get_summary", "rename", "load", "delete", "query_entries",
    "list_deleted", "restore_deleted", "relink_voice",
    "list_versions", "restore_version",
})
MUTATIONS = frozenset({"save_markdown", "append_markdown_section", "create_folder", "ensure_folder",
    "rename_folder", "delete_folder", "set_entry_folder", "set_folder_order", "save", "save_rewrite",
    "save_document", "save_voice", "save_summary", "rename", "delete", "restore_deleted", "relink_voice",
    "restore_version"})
_pending = {}
_pending_lock = threading.RLock()
_WIRE_LIMIT = 64 * 1024 * 1024


def _pack(value):
    """Keep long transcripts within the existing native message boundary."""
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(body) > _WIRE_LIMIT:
        raise ValueError("资料超过当前单次传输上限，请分批处理；原文件未截断")
    if len(body) <= 512 * 1024:
        return value
    compressed = base64.b64encode(zlib.compress(body)).decode("ascii")
    if len(compressed) > 3 * 1024 * 1024:
        raise ValueError("资料过大，无法通过当前传输通道；原文件未截断")
    return {"wire": "lulu-library-zlib-v1", "payload": compressed}


def _unpack(value):
    if type(value) is not dict or "wire" not in value:
        return value
    if (set(value) != {"wire", "payload"} or value["wire"] != "lulu-library-zlib-v1"
            or type(value["payload"]) is not str or len(value["payload"]) > 3 * 1024 * 1024):
        raise ValueError("LIBRARY_WIRE_INVALID")
    try:
        decoder = zlib.decompressobj()
        body = decoder.decompress(base64.b64decode(value["payload"], validate=True), _WIRE_LIMIT + 1)
        if len(body) > _WIRE_LIMIT or not decoder.eof or decoder.unused_data:
            raise ValueError("LIBRARY_WIRE_INVALID")
        return json.loads(body)
    except (ValueError, zlib.error) as exc:
        raise ValueError("LIBRARY_WIRE_INVALID") from exc


def validate_request(arguments):
    arguments = _unpack(arguments)
    if (type(arguments) is not dict or set(arguments) not in ({"operation", "args", "kwargs"}, {"operation", "args", "kwargs", "request_id"})
            or arguments.get("operation") not in OPERATIONS
            or type(arguments.get("args")) is not list or type(arguments.get("kwargs")) is not dict
            or len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > _WIRE_LIMIT):
        raise ValueError("LIBRARY_REQUEST_INVALID")
    if "request_id" in arguments and re.fullmatch(r"[a-f0-9]{32}", str(arguments["request_id"])) is None:
        raise ValueError("LIBRARY_REQUEST_ID_INVALID")


def encode(value):
    from .models import Transcript
    if isinstance(value, Transcript):
        return {"$transcript": dataclasses.asdict(value)}
    if isinstance(value, Path):
        return {"$path": str(value)}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


def decode(value):
    from .models import Transcript, Segment
    if isinstance(value, dict):
        if set(value) == {"$path"}:
            return Path(value["$path"])
        if set(value) == {"$transcript"}:
            fields = dict(value["$transcript"])
            fields["segments"] = [Segment(**s) for s in fields.get("segments", [])]
            return Transcript(**fields)
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def dispatch(operation: str, args: tuple, kwargs: dict):
    from . import library
    from .agent_entry.entrypoints import build_default_gui_core_dispatcher
    # list_entries reports damaged filenames through an output argument locally.
    # Transport that output explicitly rather than mutating a serialized copy.
    errors = kwargs.get("errors")
    request = {
        "operation": operation, "args": encode(args),
        "kwargs": encode({k: v for k, v in kwargs.items() if k != "errors"}),
    }
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if operation in MUTATIONS:
        with _pending_lock:
            request["request_id"] = _pending.setdefault(digest, uuid4().hex)
    try:
        response = _unpack(build_default_gui_core_dispatcher()("gui.library", _pack(request)))
    except Exception as exc:
        if operation in MUTATIONS:
            raise RuntimeError(f'保存结果尚无法确认，资料操作凭据 {request["request_id"]} 已保留。请勿另建重复任务。') from exc
        raise
    with _pending_lock:
        if response.get("error") != "OutcomeUnknown":
            _pending.pop(digest, None)
    if not response.get("ok"):
        error_type = {
            "RevisionConflict": library.RevisionConflict,
            "FolderIndexError": library.FolderIndexError,
            "FileNotFoundError": FileNotFoundError,
            "ValueError": ValueError,
        }.get(response.get("error"), RuntimeError)
        raise error_type(response.get("message") or "资料操作失败，原资料已保留。")
    if errors is not None:
        errors.extend(response.get("read_errors", []))
    return decode(response.get("value"))


def handle(arguments: dict) -> dict:
    return _pack(_handle(_unpack(arguments)))


def _handle(arguments: dict) -> dict:
    from . import library
    validate_request(arguments)
    request_id = arguments.get("request_id")
    if request_id and arguments["operation"] in MUTATIONS:
        digest = hashlib.sha256(json.dumps({k: v for k, v in arguments.items() if k != "request_id"}, sort_keys=True).encode()).hexdigest()
        with library._transaction():
            path = library.LIBRARY_DIR / ".operations" / f"{request_id}.json"
            if path.exists():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous.get("digest") != digest:
                    return {"ok": False, "error": "RevisionConflict", "message": "同一操作凭据对应不同内容，未执行。"}
                return previous.get("response") or {"ok": False, "error": "OutcomeUnknown",
                    "message": f"上次操作尚待核对，未重复执行。凭据：{request_id}"}
            library._write_atomic_text(path, json.dumps({"digest": digest, "response": None}))
            response = _invoke(arguments)
            if not response.get("ok") and response.get("error") not in {
                    "RevisionConflict", "FolderIndexError", "ValueError", "FileNotFoundError"}:
                # An I/O failure may follow a successful Markdown write. Keep the
                # pending receipt instead of allowing an append to execute twice.
                return {"ok": False, "error": "OutcomeUnknown",
                        "message": f"操作中断，结果需要核对，未自动重试。凭据：{request_id}。{response.get('message', '')}"}
            library._write_atomic_text(path, json.dumps({"digest": digest, "response": response}, ensure_ascii=False))
            return response
    return _invoke(arguments)


def _invoke(arguments):
    from . import library
    function = getattr(library, arguments["operation"])
    args, kwargs = decode(arguments["args"]), decode(arguments["kwargs"])
    inspect.signature(function).bind(*args, **kwargs)
    errors = []
    if arguments["operation"] == "list_entries":
        kwargs["errors"] = errors
    try:
        value = function(*args, **kwargs)
        return {"ok": True, "value": encode(value), "read_errors": errors}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
