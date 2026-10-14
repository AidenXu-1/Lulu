"""Retired cloud TTS compatibility types and local historical metadata.

Remote operations fail before credentials, upload or network I/O. Retained so
older integrations receive an explicit local-only error without losing audio.
"""

from __future__ import annotations

import binascii
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .base import SynthesisError
from ..models_manager import app_data_dir
from ..recording.audio_tools import RecordingAudioError, analyze_recording_file

CONFIG_KEY = "minimax_tts"
DEFAULT_BASE_URL = "https://api.minimax.io"
ALT_BASE_URL = "https://api-uw.minimax.io"
DEFAULT_MODEL = "speech-2.8-turbo"
DEFAULT_VOICE_ID = "male-qn-qingse"
DEFAULT_FORMAT = "mp3"
MAX_SYNC_CHARS = 3000
MAX_API_CHARS = 10000
DEFAULT_TIMEOUT = 90
VOICE_CACHE_VERSION = 1
CLONE_MAX_BYTES = 20 * 1024 * 1024
CLONE_MIN_SECONDS = 10.0
CLONE_MAX_SECONDS = 300.0
CLONE_AUDIO_EXTS = {".mp3", ".m4a", ".wav"}
VOICE_ID_RE = re.compile(r"^[A-Za-z](?:[A-Za-z0-9_-]{6,254}[A-Za-z0-9])$")

MODELS = (
    ("speech-2.8-turbo", "快 · 2.8 Turbo"),
    ("speech-2.8-hd", "高质量 · 2.8 HD"),
)
FORMATS = ("mp3", "wav", "flac")

BUILTIN_SYSTEM_VOICES = (
    ("male-qn-qingse", "青涩青年音", "官方音色"),
    ("female-shaonv", "少女音", "官方音色"),
    ("Chinese (Mandarin)_Reliable_Executive", "中文 · 可靠主管", "官方音色"),
    ("Chinese (Mandarin)_News_Anchor", "中文 · 新闻主播", "官方音色"),
    ("English_expressive_narrator", "英文 · 叙事旁白", "官方音色"),
    ("English_radiant_girl", "英文 · 明亮女声", "官方音色"),
)


class MiniMaxTtsError(SynthesisError):
    """用户可见的 MiniMax 云端配音错误。"""

    def __init__(self, user_message: str, *, code: str = "MINIMAX_ERROR"):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


@dataclass
class MiniMaxTtsRequest:
    text: str
    output_path: Path
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    voice_id: str = DEFAULT_VOICE_ID
    output_format: str = DEFAULT_FORMAT
    speed: float = 1.0
    volume: float = 1.0
    pitch: int = 0
    language_boost: str = "auto"
    sample_rate: int = 32000
    bitrate: int = 128000
    timeout_seconds: int = DEFAULT_TIMEOUT
    group_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MiniMaxTtsResult:
    output_path: Path
    duration_seconds: float
    model: str
    voice_id: str
    output_format: str
    usage_characters: int = 0
    trace_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MiniMaxVoice:
    voice_id: str
    name: str
    source: str
    category: str = ""
    created_time: str = ""
    provider: str = "MiniMax"
    cloud: bool = True
    origin: str = "cache"


@dataclass
class MiniMaxVoiceCatalog:
    official: list[MiniMaxVoice] = field(default_factory=list)
    cloned: list[MiniMaxVoice] = field(default_factory=list)
    custom: list[MiniMaxVoice] = field(default_factory=list)
    from_cache: bool = False
    used_fallback: bool = False
    updated_at: str = ""


@dataclass
class MiniMaxCloneRequest:
    sample_path: Path
    display_name: str
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    voice_id: str = ""
    authorization_confirmed: bool = False
    timeout_seconds: int = DEFAULT_TIMEOUT
    is_cancelled: Callable[[], bool] | None = None


@dataclass
class MiniMaxCloneResult:
    voice: MiniMaxVoice
    uploaded_file_id: str = ""
    trace_id: str = ""
    remote_file_cleanup_attempted: bool = False
    remote_file_cleanup_succeeded: bool = False
    remote_cleanup_warning: str = ""


@dataclass
class MiniMaxDeleteResult:
    target_id: str
    target_type: str
    remote_attempted: bool = False
    remote_succeeded: bool = False
    local_removed: bool = False
    user_message: str = ""


HttpPost = Callable[[str, dict[str, str], bytes, int], bytes]
MultipartPost = Callable[[str, dict[str, str], dict[str, str], str, Path, int], bytes]


def _clean_base_url(value: str | None) -> str:
    raw = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    if raw.endswith("/v1/t2a_v2"):
        raw = raw[: -len("/v1/t2a_v2")]
    return raw or DEFAULT_BASE_URL


def _model(value: str | None) -> str:
    value = (value or DEFAULT_MODEL).strip()
    return value if value in {m for m, _label in MODELS} else DEFAULT_MODEL


def _fmt(value: str | None) -> str:
    value = (value or DEFAULT_FORMAT).strip().lower()
    return value if value in FORMATS else DEFAULT_FORMAT


def _cfg() -> dict:
    return {"api_key": "", "group_id": "", "model": DEFAULT_MODEL, "voice_id": DEFAULT_VOICE_ID, "base_url": DEFAULT_BASE_URL}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _minimax_dir(*, create: bool = True) -> Path:
    path = app_data_dir() / "minimax"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _voice_cache_path(*, create_parent: bool = False) -> Path:
    return _minimax_dir(create=create_parent) / "voices.json"


def _voice_to_dict(voice: MiniMaxVoice) -> dict[str, Any]:
    return {
        "voice_id": voice.voice_id,
        "name": voice.name,
        "source": voice.source,
        "category": voice.category,
        "created_time": voice.created_time,
        "provider": voice.provider,
        "cloud": bool(voice.cloud),
        "origin": voice.origin,
    }


def _voice_from_dict(raw: dict[str, Any], *, default_source: str = "system", origin: str = "cache") -> MiniMaxVoice | None:
    voice_id = str(raw.get("voice_id") or raw.get("id") or "").strip()
    if not voice_id:
        return None
    name = str(raw.get("name") or raw.get("voice_name") or raw.get("display_name") or voice_id).strip()
    return MiniMaxVoice(
        voice_id=voice_id,
        name=name or voice_id,
        source=str(raw.get("source") or default_source).strip() or default_source,
        category=str(raw.get("category") or raw.get("voice_type") or "").strip(),
        created_time=str(raw.get("created_time") or raw.get("created_at") or "").strip(),
        provider="MiniMax",
        cloud=True,
        origin=str(raw.get("origin") or origin).strip() or origin,
    )


def _read_voice_cache() -> dict[str, Any]:
    path = _voice_cache_path(create_parent=False)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_voice_cache(data: dict[str, Any]) -> None:
    safe = {
        "version": VOICE_CACHE_VERSION,
        "updated_at": str(data.get("updated_at") or _now_iso()),
        "official": list(data.get("official") or []),
        "cloned": list(data.get("cloned") or []),
        "custom": list(data.get("custom") or []),
    }
    _voice_cache_path(create_parent=True).write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _builtin_official_voices() -> list[MiniMaxVoice]:
    return [
        MiniMaxVoice(voice_id=voice_id, name=name, source="system", category=category, origin="builtin")
        for voice_id, name, category in BUILTIN_SYSTEM_VOICES
    ]


def _dedupe_voices(voices: list[MiniMaxVoice]) -> list[MiniMaxVoice]:
    seen: set[str] = set()
    result: list[MiniMaxVoice] = []
    for voice in voices:
        if voice.voice_id in seen:
            continue
        seen.add(voice.voice_id)
        result.append(voice)
    return result


def voice_catalog() -> MiniMaxVoiceCatalog:
    data = _read_voice_cache()
    official = [
        v
        for item in data.get("official") or []
        if isinstance(item, dict)
        for v in [_voice_from_dict(item, default_source="system")]
        if v is not None
    ]
    cloned = [
        v
        for item in data.get("cloned") or []
        if isinstance(item, dict)
        for v in [_voice_from_dict(item, default_source="voice_cloning")]
        if v is not None
    ]
    custom = [
        v
        for item in data.get("custom") or []
        if isinstance(item, dict)
        for v in [_voice_from_dict(item, default_source="custom")]
        if v is not None
    ]
    used_fallback = False
    if not official:
        official = _builtin_official_voices()
        used_fallback = True
    return MiniMaxVoiceCatalog(
        official=_dedupe_voices(official),
        cloned=_dedupe_voices(cloned),
        custom=_dedupe_voices(custom),
        from_cache=bool(data),
        used_fallback=used_fallback,
        updated_at=str(data.get("updated_at") or ""),
    )


def save_custom_voice(voice_id: str, name: str | None = None) -> MiniMaxVoice:
    voice_id = (voice_id or "").strip()
    if not voice_id:
        raise MiniMaxTtsError("请填写 MiniMax 音色 ID。", code="VOICE_MISSING")
    voice = MiniMaxVoice(voice_id=voice_id, name=(name or voice_id).strip() or voice_id, source="custom", origin="user")
    data = _read_voice_cache()
    custom = [
        v
        for item in data.get("custom") or []
        if isinstance(item, dict)
        for v in [_voice_from_dict(item, default_source="custom")]
        if v is not None and v.voice_id != voice.voice_id
    ]
    custom.insert(0, voice)
    data["custom"] = [_voice_to_dict(v) for v in _dedupe_voices(custom)[:50]]
    data["updated_at"] = _now_iso()
    _write_voice_cache(data)
    return voice


def _save_cloned_voice(voice: MiniMaxVoice) -> None:
    data = _read_voice_cache()
    cloned = [
        v
        for item in data.get("cloned") or []
        if isinstance(item, dict)
        for v in [_voice_from_dict(item, default_source="voice_cloning")]
        if v is not None and v.voice_id != voice.voice_id
    ]
    cloned.insert(0, voice)
    data["cloned"] = [_voice_to_dict(v) for v in _dedupe_voices(cloned)[:100]]
    data["updated_at"] = _now_iso()
    _write_voice_cache(data)


def remove_cloned_voice_record(voice_id: str) -> bool:
    """Remove only Lulu's local cloned-voice reference; does not delete MiniMax remote resources."""

    voice_id = (voice_id or "").strip()
    if not voice_id:
        return False
    data = _read_voice_cache()
    old_items = [item for item in data.get("cloned") or [] if isinstance(item, dict)]
    new_items = [item for item in old_items if str(item.get("voice_id") or "").strip() != voice_id]
    if len(new_items) == len(old_items):
        return False
    data["cloned"] = new_items
    data["updated_at"] = _now_iso()
    _write_voice_cache(data)
    return True


def delete_uploaded_file(
    file_id: str,
    *,
    api_key: str = "",
    base_url: str = DEFAULT_BASE_URL,
    purpose: str = "voice_clone",
    post: HttpPost | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> MiniMaxDeleteResult:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def delete_remote_voice(
    voice_id: str,
    *,
    api_key: str = "",
    base_url: str = DEFAULT_BASE_URL,
    voice_type: str = "voice_cloning",
    post: HttpPost | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> MiniMaxDeleteResult:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def delete_cloned_voice(
    voice_id: str,
    *,
    delete_remote: bool = False,
    api_key: str = "",
    base_url: str = DEFAULT_BASE_URL,
    post: HttpPost | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> MiniMaxDeleteResult:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def get_config() -> dict:
    return _cfg()


def save_config(cfg: dict) -> None:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def is_configured() -> bool:
    """首版国际站口径:只要求 API Key;GroupId 不再默认阻断。"""
    return bool(_cfg()["api_key"])


def request_from_config(
    *,
    text: str,
    output_path: str | Path,
    voice_id: str | None = None,
    model: str | None = None,
    output_format: str | None = None,
    speed: float = 1.0,
    volume: float = 1.0,
    pitch: int = 0,
    language_boost: str = "auto",
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> MiniMaxTtsRequest:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def validate_request(req: MiniMaxTtsRequest) -> None:
    text = (req.text or "").strip()
    if not text:
        raise MiniMaxTtsError("请输入或选择待配音文本。", code="TEXT_EMPTY")
    if len(text) >= MAX_API_CHARS:
        raise MiniMaxTtsError("MiniMax 单次文本需小于 10000 字。请拆成多段后再生成。", code="TEXT_TOO_LONG")
    if len(text) > MAX_SYNC_CHARS:
        raise MiniMaxTtsError("这段文案较长，首版建议拆成 3000 字以内再生成。", code="TEXT_SYNC_TOO_LONG")
    if not (req.api_key or "").strip():
        raise MiniMaxTtsError("请先在 AI 大模型配置里填写 MiniMax API Key。", code="KEY_MISSING")
    if req.output_format not in FORMATS:
        raise MiniMaxTtsError("当前输出格式不可用，请换一种格式后重试。", code="FORMAT_INVALID")
    if not (req.voice_id or "").strip():
        raise MiniMaxTtsError("请填写 MiniMax 音色 ID。", code="VOICE_MISSING")


def _default_post(url: str, headers: dict[str, str], body: bytes, timeout: int) -> bytes:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def _json_post(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    post: HttpPost | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def _default_multipart_post(
    url: str,
    headers: dict[str, str],
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
    timeout: int,
) -> bytes:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def _audio_payload_to_file(audio_hex: str, output_path: Path) -> int:
    try:
        audio = binascii.unhexlify(audio_hex.strip())
    except (binascii.Error, ValueError) as exc:
        raise MiniMaxTtsError("MiniMax 返回了结果，但没有可用音频。请重试或换模型/音色。", code="AUDIO_HEX_INVALID") from exc
    if not audio:
        raise MiniMaxTtsError("MiniMax 返回了结果，但没有可用音频。请重试或换模型/音色。", code="AUDIO_EMPTY")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio)
    return len(audio)


def _request_payload(req: MiniMaxTtsRequest) -> dict[str, Any]:
    return {
        "model": req.model,
        "text": req.text.strip(),
        "stream": False,
        "language_boost": req.language_boost,
        "output_format": "hex",
        "voice_setting": {
            "voice_id": req.voice_id.strip(),
            "speed": max(0.5, min(2.0, float(req.speed))),
            "vol": max(0.5, min(2.0, float(req.volume))),
            "pitch": max(-12, min(12, int(req.pitch))),
        },
        "audio_setting": {
            "sample_rate": int(req.sample_rate or 32000),
            "bitrate": int(req.bitrate or 128000),
            "format": req.output_format,
            "channel": 1,
        },
    }


def _classify_error(status: int | None, code: Any, message: str) -> MiniMaxTtsError:
    text = f"{code or ''} {message or ''}".lower()
    if status in {401, 403} or "unauthorized" in text or "forbidden" in text or "api key" in text:
        return MiniMaxTtsError("MiniMax API Key 无效或无权限，请检查后重试。", code="AUTH_FAILED")
    if "balance" in text or "insufficient" in text or "quota" in text:
        return MiniMaxTtsError("MiniMax 账号余额不足或额度不可用，请到 MiniMax 控制台处理。", code="QUOTA_FAILED")
    if status == 429 or "rate" in text or "limit" in text or "too frequent" in text:
        return MiniMaxTtsError("MiniMax 请求过于频繁或用量已达上限，请稍后再试。", code="RATE_LIMITED")
    if "sensitive" in text or "content" in text or "risk" in text:
        return MiniMaxTtsError("MiniMax 拒绝了这段文本。请调整文案后重试。", code="SENSITIVE_TEXT")
    if "voice" in text:
        return MiniMaxTtsError("当前音色 ID 不可用，请换一个音色后重试。", code="VOICE_INVALID")
    if "model" in text:
        return MiniMaxTtsError("当前 MiniMax 模型不可用，请换一个模型后重试。", code="MODEL_INVALID")
    if "param" in text or "json" in text:
        return MiniMaxTtsError("MiniMax 参数校验失败，请检查模型、音色和格式后重试。", code="PARAM_INVALID")
    if status and status >= 500:
        return MiniMaxTtsError("MiniMax 服务暂时不可用，请稍后重试或改用本地模型。", code="SERVER_ERROR")
    return MiniMaxTtsError("MiniMax 生成失败，未保存音频。请稍后重试或改用本地模型。", code="REMOTE_FAILED")


def _parse_response(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MiniMaxTtsError("MiniMax 返回了无法识别的结果，未保存音频。", code="NON_JSON") from exc
    if not isinstance(data, dict):
        raise MiniMaxTtsError("MiniMax 返回了无法识别的结果，未保存音频。", code="NON_JSON")
    return data


def _response_ok(payload: dict[str, Any]) -> bool:
    base = payload.get("base_resp") or payload.get("base_response") or {}
    if not isinstance(base, dict):
        return True
    code = base.get("status_code")
    return code in (0, "0", None)


def _raise_response_error(payload: dict[str, Any]) -> None:
    base = payload.get("base_resp") or payload.get("base_response") or {}
    if not isinstance(base, dict):
        raise MiniMaxTtsError("MiniMax 返回了无法识别的结果，请稍后重试。", code="NON_JSON")
    raise _classify_error(None, base.get("status_code"), str(base.get("status_msg") or base.get("message") or ""))


def _parse_voice_items(raw_items: Any, *, source: str) -> list[MiniMaxVoice]:
    if not isinstance(raw_items, list):
        return []
    voices: list[MiniMaxVoice] = []
    for item in raw_items:
        if isinstance(item, str):
            voices.append(MiniMaxVoice(voice_id=item, name=item, source=source, category="官方音色", origin="api"))
        elif isinstance(item, dict):
            voice = _voice_from_dict(item, default_source=source, origin="api")
            if voice:
                voices.append(voice)
    return _dedupe_voices(voices)


def refresh_voice_catalog(
    *,
    voice_type: str = "all",
    post: HttpPost | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> MiniMaxVoiceCatalog:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def _generated_voice_id(display_name: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", display_name or "").strip("_")
    if not prefix or not prefix[0].isalpha():
        prefix = "lulu_voice"
    prefix = prefix[:40].strip("_") or "lulu_voice"
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def validate_clone_voice_id(voice_id: str) -> None:
    if not VOICE_ID_RE.match((voice_id or "").strip()):
        raise MiniMaxTtsError("云端克隆音色 ID 需 8-256 位，以英文字母开头，只能包含字母、数字、短横线和下划线，且不能以下划线或短横线结尾。", code="VOICE_ID_INVALID")


def validate_clone_sample(path: str | Path) -> dict[str, Any]:
    audio = Path(path).expanduser()
    if not audio.exists() or not audio.is_file():
        raise MiniMaxTtsError("请选择可读取的本地音频文件。", code="SAMPLE_MISSING")
    suffix = audio.suffix.lower()
    if suffix not in CLONE_AUDIO_EXTS:
        raise MiniMaxTtsError("克隆音色样本仅支持 MP3、M4A 或 WAV。", code="SAMPLE_FORMAT_INVALID")
    size = audio.stat().st_size
    if size <= 0:
        raise MiniMaxTtsError("音频文件为空，请重新选择。", code="SAMPLE_EMPTY")
    if size > CLONE_MAX_BYTES:
        raise MiniMaxTtsError("克隆音色样本需小于 20MB。", code="SAMPLE_TOO_LARGE")
    try:
        stats = analyze_recording_file(audio)
    except RecordingAudioError as exc:
        raise MiniMaxTtsError("音频文件无法读取，请换一个 10 秒到 5 分钟的清晰人声音频。", code="SAMPLE_UNREADABLE") from exc
    duration = float(stats.get("duration") or 0.0)
    if duration < CLONE_MIN_SECONDS:
        raise MiniMaxTtsError("克隆音色样本至少需要 10 秒。", code="SAMPLE_TOO_SHORT")
    if duration > CLONE_MAX_SECONDS:
        raise MiniMaxTtsError("克隆音色样本最长 5 分钟，请先裁剪后再上传。", code="SAMPLE_TOO_LONG")
    return {
        "path": audio,
        "suffix": suffix,
        "size": size,
        "duration_seconds": duration,
        "is_silent": bool(stats.get("is_silent")),
    }


def _extract_file_id(payload: dict[str, Any]) -> str:
    data = payload.get("file") if isinstance(payload.get("file"), dict) else payload.get("data")
    if isinstance(data, dict):
        for key in ("file_id", "id"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    value = str(payload.get("file_id") or "").strip()
    if value:
        return value
    raise MiniMaxTtsError("MiniMax 已响应上传请求，但没有返回可用文件 ID。", code="UPLOAD_FILE_ID_MISSING")


def upload_clone_audio(
    req: MiniMaxCloneRequest,
    *,
    multipart_post: MultipartPost | None = None,
) -> str:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def _clone_payload(file_id: str, voice_id: str) -> dict[str, Any]:
    # 不传 text/model,避免自动试听和额外 T2A 计费。
    return {
        "file_id": file_id,
        "voice_id": voice_id,
        "need_noise_reduction": False,
        "need_volume_normalization": False,
    }


def _cleanup_uploaded_clone_file(
    file_id: str,
    *,
    api_key: str,
    base_url: str,
    post: HttpPost | None,
    timeout_seconds: int,
) -> tuple[bool, bool, str]:
    if not str(file_id or "").strip():
        return False, False, ""
    try:
        delete_uploaded_file(
            file_id,
            api_key=api_key,
            base_url=base_url,
            post=post,
            timeout_seconds=timeout_seconds,
        )
    except MiniMaxTtsError:
        return True, False, "已尝试删除远端样本文件,但 MiniMax 未确认成功；请到 MiniMax 控制台核对。"
    return True, True, "已请求 MiniMax 删除远端样本文件。"


def _cancelled(req: MiniMaxCloneRequest) -> bool:
    return bool(req.is_cancelled and req.is_cancelled())


def clone_voice(
    req: MiniMaxCloneRequest,
    *,
    post: HttpPost | None = None,
    multipart_post: MultipartPost | None = None,
) -> MiniMaxCloneResult:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def synthesize(
    req: MiniMaxTtsRequest,
    *,
    post: HttpPost | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> MiniMaxTtsResult:
    raise MiniMaxTtsError("Lulu 已停止支持云端 API 配音，请选择本地模型。", code="CLOUD_TTS_RETIRED")


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, MiniMaxTtsError):
        return exc.user_message
    return "MiniMax 生成失败，未保存音频。请稍后重试或改用本地模型。"
