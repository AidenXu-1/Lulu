"""Closed Phase 3 CLI result primitives."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import re
from collections.abc import Callable
from typing import TextIO, TypeVar


SCHEMA_ID = "lulu.agent/v1"
SCHEMA_REVISION = 1
CLI_PROTOCOL_VERSION = "1.0.0"

_T = TypeVar("_T")

_ERROR_CONTRACTS = {
    "UNKNOWN_COMMAND": (2, "命令不在闭合 grammar 中"),
    "UNKNOWN_FIELD": (2, "参数字段不在闭合 grammar 中"),
    "INVALID_ARGUMENT": (2, "参数不符合闭合 grammar"),
    "AUTH_REQUIRED": (10, "当前操作未通过访问检查，请重新打开 Lulu"),
    "SECURE_STORE_UNAVAILABLE": (10, "系统安全存储不可用"),
    "CONFIRMATION_REQUIRED": (11, "需要可信界面中的本次确认"),
    "HUMAN_ACTION_REQUIRED": (11, "需要在 Lulu 窗口中点击确认"),
    "APPROVAL_INVALID": (11, "本次确认无效"),
    "APPROVAL_EXPIRED": (11, "本次确认已过期"),
    "APPROVAL_CONSUMED": (11, "本次确认已经使用"),
    "NOT_FOUND": (20, "本地资源不存在"),
    "CONFLICT": (21, "权威状态发生冲突"),
    "RESOURCE_BUSY": (22, "共享资源正忙"),
    "CAPABILITY_UNAVAILABLE": (30, "正式 CLI 能力尚未实现"),
    "MODEL_NOT_READY": (30, "本地模型尚未就绪"),
    "RUNTIME_NOT_READY": (30, "本地运行时尚未就绪"),
    "NETWORK_UNAVAILABLE": (40, "网络当前不可用"),
    "NETWORK_TIMEOUT": (40, "网络请求超时"),
    "EXTERNAL_SERVICE_ERROR": (41, "外部服务失败"),
    "EXTERNAL_RESPONSE_INVALID": (41, "外部服务结果无效"),
    "CANCELLED": (50, "当前任务已取消"),
    "INTEGRITY_ERROR": (60, "可信边界完整性校验失败"),
    "UNSAFE_PATH": (60, "本地路径不安全"),
    "PARTIAL_SUCCESS": (70, "任务部分成功"),
    "INCOMPLETE_RESULT": (71, "转录结果不完整，未保存为正常文稿；请查看 Lulu 中的恢复提示"),
    "INTERNAL_ERROR": (1, "内部错误"),
}
_DETAIL_FIELDS = frozenset(
    {
        "reason_code",
        "capability_id",
        "field",
        "expected_state",
        "actual_state",
        "phase",
        "native_code",
        "effect_count",
    }
)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


def write_json_object(payload: dict[str, object], stream: TextIO) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")


def write_jsonl_event(payload: dict[str, object], stream: TextIO) -> None:
    """Write one closed JSONL event without logs, progress text, or traceback."""
    if frozenset(payload) != {"event_schema_id", "sequence", "event", "payload"}:
        raise ValueError("JSONL event fields are closed")
    if payload.get("event_schema_id") != "lulu.agent-event/v1":
        raise ValueError("JSONL event schema is invalid")
    if type(payload.get("sequence")) is not int or payload["sequence"] < 1:
        raise ValueError("JSONL event sequence is invalid")
    if type(payload.get("event")) is not str or not _SAFE_VALUE.fullmatch(payload["event"]):
        raise ValueError("JSONL event kind is invalid")
    if type(payload.get("payload")) is not dict:
        raise ValueError("JSONL event payload is invalid")
    write_json_object(payload, stream)


def capture_third_party_output(
    callback: Callable[[], _T],
) -> tuple[_T, dict[str, str]]:
    """Keep dependency stdout/stderr outside the public protocol streams."""
    captured_stdout = StringIO()
    captured_stderr = StringIO()
    with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
        result = callback()
    return result, {
        "stdout": captured_stdout.getvalue(),
        "stderr": captured_stderr.getvalue(),
    }


def _closed_error_details(details: dict[str, object] | None) -> dict[str, object]:
    closed: dict[str, object] = {}
    for key, value in dict(details or {}).items():
        if key not in _DETAIL_FIELDS:
            continue
        if type(value) in {bool, int}:
            closed[key] = value
            continue
        if type(value) is str and _SAFE_VALUE.fullmatch(value):
            closed[key] = value
    return closed


def write_cli_error(
    *,
    code: str,
    command: str,
    details: dict[str, object] | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Emit one non-reflective error object and return its contract exit code."""
    del stderr
    exit_code, message = _ERROR_CONTRACTS.get(code, _ERROR_CONTRACTS["INTERNAL_ERROR"])
    safe_code = code if code in _ERROR_CONTRACTS else "INTERNAL_ERROR"
    safe_command = command if _SAFE_VALUE.fullmatch(command) else "invalid"
    from ..core.local_transcription_models import MODEL_MESSAGES
    raw_reason = (details or {}).get('reason_code')
    reason = raw_reason if type(raw_reason) is str else None
    if safe_code in {'MODEL_NOT_READY', 'RUNTIME_NOT_READY'} and reason in MODEL_MESSAGES:
        message = MODEL_MESSAGES[reason]
    from ..core.link_assets import ERROR_MESSAGES as EXTRACTION_MESSAGES
    if reason in EXTRACTION_MESSAGES:
        message = EXTRACTION_MESSAGES[reason]
    write_json_object(
        {
            "schema_id": SCHEMA_ID,
            "schema_revision": SCHEMA_REVISION,
            "ok": False,
            "command": safe_command,
            "error": {
                "code": safe_code,
                "message": message,
                "retryable": False,
                "human_action": (
                    "核对链接提取参数、当前安装能力和原任务状态，不要重复下载"
                    if reason in EXTRACTION_MESSAGES else
                    "运行 transcribe status 查看所选模型状态；在 Lulu 中完成安装或修复后再调用"
                    if reason in MODEL_MESSAGES else
                    "允许当前执行环境访问本机 Lulu XPC 服务后，仍使用 ~/.local/bin/lulu"
                    if (details or {}).get("reason_code") in {"CORE_TRANSPORT_UNAVAILABLE", "CORE_TRANSPORT_TIMEOUT"}
                    else "先查询原任务状态与回执，不要重复执行"
                    if (details or {}).get("reason_code") == "CORE_REQUEST_OUTCOME_UNKNOWN"
                    else "使用 help 或等待后续正式 CLI 阶段"
                ),
                "details": _closed_error_details(details),
            },
        },
        stdout,
    )
    return exit_code
