"""智能摘要:把转写逐字稿交给云端 LLM(默认 DeepSeek)整理成摘要 / 要点。

- 配置存在系统安全库(SecretStore)的 "summary_llm" 槽:{api_key, base_url, model}。
- 走 OpenAI 兼容的 /chat/completions;DeepSeek 默认 base_url=https://api.deepseek.com、
  model=deepseek-chat(指向其最新版,不写死版本号)。
- ⚠️ 摘要会把逐字稿上传到所选厂家——是用户显式选择的云端能力,非本地默认。
"""

from __future__ import annotations

from . import secrets

CONFIG_KEY = "summary_llm"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"  # 快又省,够用;想更强可在配置卡切 Pro
_MAX_CHARS = 50_000  # 极长稿子兜底截断,避免超出上下文

_SYSTEM = "你是一个中文内容助理,擅长把口播 / 会议 / 访谈的逐字稿整理成清晰好读的摘要。"
_PROMPT = (
    "请阅读下面的逐字稿,整理输出(用 Markdown):\n"
    "1. **一句话主旨**:一句话概括全文在讲什么。\n"
    "2. **核心要点**:3~6 条,每条一行、简洁,抓住关键信息。\n"
    "3. **结论 / 行动项**:如果有明确结论或要做的事就单列,没有可省略。\n"
    "只输出整理后的结果,不要复述原文、不要寒暄。\n\n"
    "逐字稿:\n{text}"
)


class OutputTruncatedError(RuntimeError):
    """兼容文本服务到达单次输出上限；上层可安全拆段重试。"""


def _cfg(config: dict | None = None) -> dict:
    c = secrets.get_config(CONFIG_KEY) if config is None else config
    model = (c.get("model") or DEFAULT_MODEL).strip()
    if model == "deepseek-chat":  # 旧别名 → 自动升级到明确的当前型号
        model = DEFAULT_MODEL
    return {
        "api_key": (c.get("api_key") or "").strip(),
        "base_url": (c.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/"),
        "model": model,
    }


def is_configured() -> bool:
    return bool(_cfg()["api_key"])


def configured_model() -> str:
    """返回当前文案处理模型名，不向 UI 暴露 API Key 或其它安全配置。"""
    return _cfg()["model"]


def _chat(
    messages: list[dict],
    max_tokens: int,
    timeout: int = 120,
    *,
    thinking: bool | None = None,
    config: dict | None = None,
) -> str:
    import requests

    c = _cfg(config)
    if not c["api_key"]:
        raise RuntimeError("还没配置文案处理用的 API Key。请到「AI 大模型配置」→「文稿助手」里填入。")
    try:
        payload = {
            "model": c["model"],
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
        }
        if thinking is not None:
            # DeepSeek V4 默认思考，且推理与最终正文共用 max_tokens。
            # 交付型任务显式关闭，避免额度耗尽却得到 0 字正文。
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        r = requests.post(
            c["base_url"] + "/chat/completions",
            headers={"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"连接摘要服务失败(检查网络 / 代理):{exc}") from exc
    if r.status_code == 401:
        raise RuntimeError("摘要服务:API Key 无效或无权限(401)。请到「AI 大模型配置」→「文稿助手」核对。")
    if r.status_code == 402 or r.status_code == 403:
        raise RuntimeError("摘要服务:余额不足或无权限。请到对应平台充值后再用。")
    if r.status_code >= 400:
        raise RuntimeError(f"摘要服务返回 {r.status_code}:{r.text[:200]}")
    data = r.json()
    try:
        choice = data["choices"][0]
        finish_reason = str(choice.get("finish_reason") or "").strip().lower()
        if finish_reason and finish_reason != "stop":
            error_type = OutputTruncatedError if finish_reason == "length" else RuntimeError
            raise error_type(
                f"文案处理服务输出未完成（{finish_reason}），未保存部分结果，请重试。"
            )
        return (choice["message"]["content"] or "").strip()
    except RuntimeError:
        raise
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("文案处理服务返回格式不完整，请重试。") from exc


def summarize(text: str) -> str:
    """把逐字稿整理成摘要,返回 Markdown 文本。"""
    text = (text or "").strip()
    if not text:
        raise RuntimeError("没有可摘要的内容。")
    if len(text) > _MAX_CHARS:
        raise RuntimeError(f"文案超过 {_MAX_CHARS:,} 字符，尚未调用云端。请拆成较短文稿后重试。")
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _PROMPT.format(text=text)},
    ]
    return _chat(messages, max_tokens=1024, thinking=False)


def chat(
    messages: list[dict],
    max_tokens: int,
    timeout: int = 120,
    *,
    thinking: bool | None = None,
    config: dict | None = None,
) -> str:
    """公开的对话补全入口,供其它云端文本能力(文案拆解 / 改写)复用同一套配置与错误处理。"""
    return _chat(messages, max_tokens, timeout, thinking=thinking, **({"config": config} if config is not None else {}))


def test_connection(config: dict | None = None) -> str:
    """测试摘要服务连通性。"""
    reply = _chat(
        [{"role": "user", "content": "只回复两个字:正常"}],
        max_tokens=64, timeout=30, thinking=False,
        **({"config": config} if config is not None else {}),
    )
    if not reply:
        raise RuntimeError("服务连接成功，但模型没有返回内容，请检查所选模型。")
    return f"连接正常 ✓(模型回复:{reply[:20]})"
