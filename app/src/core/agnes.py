"""Agnes 多模态云端 API 客户端。

Agnes 是用户显式配置/触发的云端能力:文本、图片、视频请求都会上传用户输入。
API Key 存在系统安全库,本模块不打印、不记录、不返回原始 Key。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import secrets

CONFIG_KEY = "agnes_multimodal"
DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_TEXT_MODEL = "agnes-2.0-flash"
DEFAULT_IMAGE_MODEL = "agnes-image-2.1-flash"
DEFAULT_VIDEO_MODEL = "agnes-video-v2.0"


class AgnesError(RuntimeError):
    """Agnes API 调用失败。"""


class AgnesOutputTruncatedError(AgnesError):
    """Agnes 到达单次输出上限；调用方可在不采用半截结果的前提下拆小重试。"""


@dataclass(frozen=True)
class AgnesConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    text_model: str = DEFAULT_TEXT_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    video_model: str = DEFAULT_VIDEO_MODEL


def _clean_base_url(value: str | None) -> str:
    return (value or DEFAULT_BASE_URL).strip().rstrip("/")


def get_config(config: dict | None = None) -> AgnesConfig:
    raw = secrets.get_config(CONFIG_KEY) if config is None else config
    return AgnesConfig(
        api_key=(raw.get("api_key") or "").strip(),
        base_url=_clean_base_url(raw.get("base_url")),
        text_model=(raw.get("text_model") or raw.get("model") or DEFAULT_TEXT_MODEL).strip(),
        image_model=(raw.get("image_model") or DEFAULT_IMAGE_MODEL).strip(),
        video_model=(raw.get("video_model") or DEFAULT_VIDEO_MODEL).strip(),
    )


def save_config(config: dict[str, str]) -> None:
    clean = {
        "api_key": (config.get("api_key") or "").strip(),
        "base_url": _clean_base_url(config.get("base_url")),
        "text_model": (config.get("text_model") or config.get("model") or DEFAULT_TEXT_MODEL).strip(),
        "image_model": (config.get("image_model") or DEFAULT_IMAGE_MODEL).strip(),
        "video_model": (config.get("video_model") or DEFAULT_VIDEO_MODEL).strip(),
    }
    secrets.set_config(CONFIG_KEY, clean)


def is_configured() -> bool:
    return bool(get_config().api_key)


def _headers(cfg: AgnesConfig) -> dict[str, str]:
    if not cfg.api_key:
        raise AgnesError("还没配置 Agnes API Key。请到「AI 大模型配置」→「文稿助手」里填入。")
    return {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}


def _raise_for_status(resp) -> None:
    code = getattr(resp, "status_code", 0)
    if code < 400:
        return
    if code == 401:
        raise AgnesError("Agnes:API Key 无效或无权限(401)。请到「AI 大模型配置」→「文稿助手」核对。")
    if code in (402, 403):
        raise AgnesError("Agnes:余额不足或无权限。请到 Agnes 平台核对额度和权限。")
    if code == 429:
        raise AgnesError("Agnes:请求过于频繁或额度受限,请稍后再试。")
    text = getattr(resp, "text", "") or ""
    raise AgnesError(f"Agnes 服务返回 {code}:{text[:200]}")


def _post(path: str, payload: dict[str, Any], *, timeout: int, config: dict | None = None):
    import requests

    cfg = get_config(config)
    try:
        resp = requests.post(cfg.base_url + path, headers=_headers(cfg), json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise AgnesError(f"连接 Agnes 失败(检查网络 / 代理):{exc}") from exc
    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError as exc:
        raise AgnesError("Agnes 返回了无法解析的 JSON。") from exc


def _get(url_or_path: str, *, timeout: int):
    import requests

    cfg = get_config()
    url = url_or_path if url_or_path.startswith("http") else cfg.base_url + url_or_path
    try:
        resp = requests.get(url, headers=_headers(cfg), timeout=timeout)
    except requests.RequestException as exc:
        raise AgnesError(f"连接 Agnes 失败(检查网络 / 代理):{exc}") from exc
    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError as exc:
        raise AgnesError("Agnes 返回了无法解析的 JSON。") from exc


def chat(
    messages: list[dict],
    max_tokens: int = 1500,
    timeout: int = 120,
    *,
    thinking: bool | None = None,
    config: dict | None = None,
) -> str:
    cfg = get_config(config)
    payload: dict[str, Any] = {
        "model": cfg.text_model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
    }
    if thinking is not None:
        # Agnes 的 OpenAI 兼容网关使用模板参数控制 reasoning_content。
        # 交付型任务关闭思考，把输出额度留给用户需要保存的正文。
        payload["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    data = _post(
        "/chat/completions",
        payload,
        timeout=timeout,
        **({"config": config} if config is not None else {}),
    )
    try:
        choice = data["choices"][0]
        finish_reason = str(choice.get("finish_reason") or "").strip().lower()
        if finish_reason and finish_reason != "stop":
            error_type = AgnesOutputTruncatedError if finish_reason == "length" else AgnesError
            raise error_type(
                f"Agnes 文本输出未完成（{finish_reason}），未保存部分结果，请重试。"
            )
        return (choice["message"]["content"] or "").strip()
    except AgnesError:
        raise
    except (KeyError, IndexError, TypeError) as exc:
        raise AgnesError("Agnes 文本结果缺少 choices[0].message.content。") from exc


def generate_image(
    prompt: str,
    *,
    size: str = "1024x768",
    image: list[str] | None = None,
    response_format: str = "url",
    timeout: int = 360,
) -> dict:
    cfg = get_config()
    payload: dict[str, Any] = {"model": cfg.image_model, "prompt": prompt, "size": size}
    extra_body: dict[str, Any] = {"response_format": response_format}
    if image:
        extra_body["image"] = image
    payload["extra_body"] = extra_body
    data = _post("/images/generations", payload, timeout=timeout)
    try:
        first = data["data"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise AgnesError("Agnes 图片结果缺少 data[0]。") from exc
    return first


def create_video_task(
    prompt: str,
    *,
    image: str | None = None,
    width: int = 1152,
    height: int = 768,
    num_frames: int = 121,
    frame_rate: int = 24,
    timeout: int = 120,
) -> dict:
    cfg = get_config()
    payload: dict[str, Any] = {
        "model": cfg.video_model,
        "prompt": prompt,
        "width": width,
        "height": height,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
    }
    if image:
        payload["image"] = image
    data = _post("/videos", payload, timeout=timeout)
    if not (data.get("task_id") or data.get("id")) or not data.get("video_id"):
        raise AgnesError("Agnes 视频任务结果缺少 task_id 或 video_id。")
    return data


def get_video_result(video_id: str, *, timeout: int = 120) -> dict:
    if not (video_id or "").strip():
        raise AgnesError("缺少 Agnes video_id。")
    return _get(f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}", timeout=timeout)


def test_connection(config: dict | None = None) -> str:
    reply = chat(
        [{"role": "user", "content": "只回复两个字:正常"}],
        max_tokens=64,
        timeout=30,
        thinking=False,
        **({"config": config} if config is not None else {}),
    )
    if not reply:
        raise AgnesError("服务连接成功，但模型没有返回内容，请检查所选模型。")
    return f"连接正常 ✓(模型回复:{reply[:20]})"
