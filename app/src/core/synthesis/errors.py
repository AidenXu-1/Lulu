"""合成链路的用户可见错误脱敏。"""

from __future__ import annotations

import re


_PATH_PATTERNS = (
    re.compile(r"(/Users/[^\n'\"，。；;]+)"),
    re.compile(r"(/[A-Za-z0-9._~+/%-]+(?:/[A-Za-z0-9._~+/%-]+)+)"),
    re.compile(r"([A-Za-z]:\\[^\s'\"，。；;]+)"),   # Windows 盘符路径
)
_SECRET_PATTERNS = (
    re.compile(r"Authorization\s*[:=]\s*\S+(?:\s+\S+)?", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key|token)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\b(api[_-]?key|token)\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9._-]+", re.IGNORECASE),
)
_USER_TEXT_PATTERNS = (
    re.compile(r"\b(input\s*text)\s*[:=]\s*.*", re.IGNORECASE),
    re.compile(r"\b(prompt)\s*[:=]\s*.*", re.IGNORECASE),
)
_SEGMENT_FAILURE_RE = re.compile(r"(第\s*\d+\s*/\s*\d+\s*段生成失败[:：]?.*)")
_WARNING_LINE_PATTERNS = (
    re.compile(r"GPT2InferenceModel has generative capabilities", re.IGNORECASE),
    re.compile(r"past_key_values is deprecated", re.IGNORECASE),
    re.compile(r"FutureWarning", re.IGNORECASE),
    re.compile(r"UserWarning", re.IGNORECASE),
)
_INDEXTTS2_READINESS_SIGNALS = (
    "indextts2 本地环境尚未准备",
    "indextts2 运行环境尚未安装",
    "indextts2 模型尚未安装",
    "indextts2 runtime 尚未准备",
    "indextts2 model 尚未准备",
    "indextts2 配置尚未完成",
)


def _trim_preserving_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head_len = min(180, max(limit // 3, 80))
    tail_len = max(limit - head_len - 12, 80)
    return f"{text[:head_len]} ... {text[-tail_len:]}"


def _drop_noise_warning_lines(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) <= 1:
        return str(text or "")
    kept = [
        line for line in lines
        if not any(pattern.search(line) for pattern in _WARNING_LINE_PATTERNS)
    ]
    return "\n".join(kept or lines[-3:])


def scrub_detail(text: str, limit: int = 500) -> str:
    """保留给内部排查的脱敏摘要;不包含绝对路径或密钥形态。"""
    cleaned = _drop_noise_warning_lines(str(text or ""))
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub("[已隐藏凭据]", cleaned)
    for pat in _USER_TEXT_PATTERNS:
        cleaned = pat.sub(lambda match: f"{match.group(1)}: [已隐藏正文]", cleaned)
    for pat in _PATH_PATTERNS:
        cleaned = pat.sub("[本地路径]", cleaned)
    cleaned = " ".join(cleaned.split())
    return _trim_preserving_tail(cleaned, limit)


def user_error_message(exc: BaseException | str, default: str = "配音失败,请检查输入后重试。") -> str:
    """把任意上游异常映射成 UI 可显示的短文案。"""
    if getattr(exc, "reason_code", None) == "TTS_ENGINE_BUSY":
        return "本地配音引擎正在生成，请等当前任务结束后再试。"
    raw = str(exc or "")
    lowered = raw.lower()
    segment_failure = _SEGMENT_FAILURE_RE.search(raw)
    if segment_failure:
        return scrub_detail(segment_failure.group(1))
    if "安全内存" in raw:
        return "IndexTTS2 已触及安全内存上限，本次生成已停止。文案和音色已保留，请缩短文本后再试。"
    if "indextts2 worker forced_exit" in lowered:
        return "系统资源不足或模型进程被强制结束，IndexTTS2 本次生成已安全停止。文案和音色已保留，请释放内存后重试。"
    if "文本过长" in raw or "too long" in lowered or "max_text" in lowered or "timeout" in lowered or "超时" in raw:
        return "文本偏长或生成超时,请缩短文本后重试。"
    if any(signal in lowered for signal in _INDEXTTS2_READINESS_SIGNALS):
        return "IndexTTS2 本地环境尚未准备完成,请先检查 runtime、model 与配置。"
    if "参考音频" in raw or "reference" in lowered:
        return "参考音频不可用,请重新选择。"
    code = getattr(exc, "reason_code", "")
    index_messages = {
        "INDEXTTS_MEMORY_LIMIT": "本次生成达到安全内存额度，后台已释放。文案和上次音频保留，请缩短文本或使用跟随音色后重试。",
        "INDEXTTS_RUNTIME_UNAVAILABLE": "模型加载失败，请到 AI 大模型检查 2.5 环境和权重；旧音色无需删除。",
        "INDEXTTS_GENERATION_FAILED": "本次生成失败，文案和上次音频已保留。请先试听一小段，或释放后台后重试。",
        "INDEXTTS_STITCH_FAILED": "分段音频合并失败，文案已保留，请检查保存位置后重试。",
        "INDEXTTS_INCOMPLETE_AUDIO": "模型未完整读完，已停止保存。请缩短当前句子后重试，避免得到缺字的音频。",
        "INDEXTTS_INVALID_REQUEST": "生成参数不完整，请检查文案、音色和情感设置。",
    }
    if code in index_messages:
        return f"{index_messages[code]}\n错误编号：{code.removeprefix('INDEXTTS_')}"
    if "voxcpm" in lowered or "from_pretrained" in lowered or "snapshot" in lowered:
        return "VoxCPM2 模型加载失败,请检查模型文件、网络或模型目录后重试。"
    if "ffmpeg" in lowered or "mp3" in lowered:
        return "音频导出失败,请换一个导出位置或格式后重试。"
    if "permission" in lowered or "denied" in lowered:
        return "没有权限访问该文件或目录,请重新选择位置。"
    if "no space" in lowered or "disk" in lowered:
        return "磁盘空间不足,请清理空间或更换模型/导出目录。"
    return default
