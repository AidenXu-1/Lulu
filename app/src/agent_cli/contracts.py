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
_WORKSPACE_REQUEST_ERRORS = {
    'RSS_FEED_UNAVAILABLE': '未读到可用的播客 RSS，请检查订阅链接或网络；已有节目与任务保留',
    'VOICE_LIBRARY_UNREADABLE': '音色库记录无法完整读取，已暂停修改；原资料保留，请在 Lulu 音色库中核对并恢复备份或切换完整的音色库',
    'VOICE_DELETE_INCOMPLETE': '音色删除未完成，部分参考文件可能已删除；列表记录保留，请检查文件夹权限后重试删除',
    'LULU_WINDOW_REQUIRED': '请先打开 Lulu 主窗口，等待连接后重试；关闭窗口后不后台执行设置或更新',
    'LULU_LIVE_SESSION_REQUIRED': '请先打开 Lulu 的直播录制页，建立正常会话后重试；退出 Lulu 会停止监控',
    'BATCH_NETWORK_CONSENT_REQUIRED': '本批需要联网下载或访问主页，请明确设置 network_consent=true 后再开始',
    'CLOUD_AUDIO_UPLOAD_CONSENT_REQUIRED': '音频会上传至所选云端服务并按账户计费；按服务额度调度，结果未确认不自动重发。确认后设置 cloud_upload_consent=true',
    'MODEL_ACTION_CONFIRMATION_REQUIRED': '请明确确认本次下载、移除或搬移模型的操作 confirmed=true',
    'UPDATE_INSTALL_CONFIRMATION_REQUIRED': '安装会退出并重开 Lulu，请明确设置 confirmed=true',
    'SUBTITLE_TIMESTAMPS_UNAVAILABLE': '当前正文缺少完整且有效的时间信息，请导出 TXT 或 Markdown',
    'WORKSPACE_REQUEST_DUPLICATE_FIELD': '请求文件包含重复字段，请为每个字段保留唯一值',
    'WORKSPACE_REQUEST_TOO_DEEP': '请求文件的 JSON 嵌套过深，请简化请求内容',
    'WORKSPACE_REQUEST_UNSAFE': '请求文件不能是符号链接，请使用普通 JSON 文件',
    'WORKSPACE_REQUEST_INVALID': '工作区请求文件格式或参数无效，请检查请求内容',
}


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
    if reason in _WORKSPACE_REQUEST_ERRORS:
        message = _WORKSPACE_REQUEST_ERRORS[reason]
    from ..core.audio.converter import INPUT_MEDIA_MESSAGES
    local_input_messages = {**INPUT_MEDIA_MESSAGES,
        'SOURCE_NOT_FOUND': '找不到要转录的本地文件，请重新选择；已有结果保留。',
        'SOURCE_NOT_REGULAR': '请选择普通本地音视频文件，文件夹和符号链接不能用于转录。',
        'SOURCE_INVALID': '请提供本地音视频文件的绝对路径。'}
    if reason in local_input_messages:
        message = local_input_messages[reason]
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
                    "检查播客订阅链接和网络后重试；可先用较小的 limit 读取最近几集"
                    if reason == 'RSS_FEED_UNAVAILABLE' else
                    "检查音色文件夹及列表文件的写入权限后重试删除，以完成剩余文件和列表清理"
                    if reason == 'VOICE_DELETE_INCOMPLETE' else
                    "在 Lulu 音色库核对提示，恢复备份或选择完整的音色库；不要重复添加或删除"
                    if reason == 'VOICE_LIBRARY_UNREADABLE' else
                    "选择存在、可正常播放且含声音的普通本地文件后重试"
                    if reason in local_input_messages else
                    "检查绝对路径所指的普通 JSON 请求文件，修正参数后再调用"
                    if reason in _WORKSPACE_REQUEST_ERRORS else
                    "核对链接提取参数、当前安装能力和原任务状态，不要重复下载"
                    if reason in EXTRACTION_MESSAGES else
                    "运行 transcribe status 查看所选模型状态；在 Lulu 中完成安装或修复后再调用"
                    if reason in MODEL_MESSAGES else
                    "允许当前执行环境访问本机 Lulu XPC 服务后，仍使用 ~/.local/bin/lulu"
                    if (details or {}).get("reason_code") in {"CORE_TRANSPORT_UNAVAILABLE", "CORE_TRANSPORT_TIMEOUT"}
                    else "先查询原任务状态与回执，不要重复执行"
                    if (details or {}).get("reason_code") == "CORE_REQUEST_OUTCOME_UNKNOWN"
                    else "查看 help 核对命令与参数；如任务已创建，先查询原任务状态和回执"
                ),
                "details": _closed_error_details(details),
            },
        },
        stdout,
    )
    return exit_code
