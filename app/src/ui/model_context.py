"""当前页面正在消费哪个模型的只读 UI 快照。

被动显示只使用本次运行已经读取的配置；尚未读取时展示所选服务，
不为状态标签访问钥匙串，也不把未知状态误报为未配置。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core import models_manager, secrets
from ..core.engines import providers


NO_MODEL_CARD_COPY = {
    "models": "挑一位合拍的搭档",
    "collection": "把他看透！",
    "settings": "把 Lulu 调得更顺手",
    "library_transcript": "稿子们都收在这里",
    "library_rewrite": "好作品在这里碰头",
    "library_voice": "声音们正在候场",
}


@dataclass(frozen=True)
class ModelContext:
    usage: str
    deployment: str
    state: str
    status_text: str
    model_name: str
    purpose_index: int | None = None
    sub_index: int | None = None
    status_kind: str = "neutral"

    @property
    def card_status_text(self) -> str:
        """左下卡第二行的统一文案；无模型页面不提供第二行。"""
        if self.deployment == "local":
            return {
                "ready": "本地模型 · 已就绪",
                "missing": "本地模型 · 未就绪",
                "broken": "本地模型 · 需修复",
            }.get(self.state, "本地模型 · 未就绪")
        if self.deployment == "api":
            return {
                "checking": "大模型 API · 检查中",
                "configured": "大模型 API · 已连接",
                "unconfigured": "大模型 API · 未连接",
                "unknown": "大模型 API",
            }.get(self.state, "大模型 API · 未连接")
        return ""


def local_model_context(
    usage: str,
    model_name: str,
    *,
    ready: bool,
    broken: bool = False,
    purpose_index: int,
) -> ModelContext:
    if broken:
        state, text, kind = "broken", "本地模型需修复", "error"
    elif ready:
        state, text, kind = "ready", "本地模型已就绪", "success"
    else:
        state, text, kind = "missing", "本地模型未就绪", "warning"
    return ModelContext(
        usage=usage,
        deployment="local",
        state=state,
        status_text=text,
        model_name=(model_name or "本地模型").strip(),
        purpose_index=purpose_index,
        sub_index=0,
        status_kind=kind,
    )


def api_model_context(
    usage: str,
    model_name: str,
    *,
    configured: bool,
    purpose_index: int,
) -> ModelContext:
    return ModelContext(
        usage=usage,
        deployment="api",
        state="configured" if configured else "unconfigured",
        status_text="大模型 API 已配置" if configured else "大模型 API 未配置",
        model_name=(model_name or "云端模型").strip(),
        purpose_index=purpose_index,
        sub_index=1,
        status_kind="success" if configured else "warning",
    )


def pending_api_model_context(
    usage: str,
    model_name: str,
    *,
    purpose_index: int,
) -> ModelContext:
    """返回不触达安全库的诚实过渡态。"""
    return ModelContext(
        usage=usage,
        deployment="api",
        state="checking",
        status_text="正在检查大模型配置",
        model_name=(model_name or "云端模型").strip(),
        purpose_index=purpose_index,
        sub_index=1,
        status_kind="neutral",
    )


def neutral_model_context(copy_key: str) -> ModelContext:
    """返回不消费模型页面的固定互动短语，不生成状态式可见文案。"""
    try:
        phrase = NO_MODEL_CARD_COPY[copy_key]
    except KeyError as exc:
        raise ValueError(f"unknown no-model page: {copy_key}") from exc
    return ModelContext(
        usage="",
        deployment="none",
        state="neutral",
        status_text=phrase,
        model_name=phrase,
        status_kind="neutral",
    )


def _cloud_model_name(provider) -> str:
    if provider.default_model:
        return provider.default_model
    label = provider.display_name.replace("（云端）", "").replace("(云端)", "").strip()
    return label or provider.id


def _custom_openai_unconfigured_context() -> ModelContext:
    return ModelContext(
        usage="内容提取",
        deployment="api",
        state="unconfigured",
        status_text="大模型 API 未配置",
        model_name="尚未设置模型名称",
        purpose_index=0,
        sub_index=1,
        status_kind="neutral",
    )


def asr_model_context(cfg) -> ModelContext:
    provider = providers.PRESETS.get(getattr(cfg, "engine_name", ""))
    if provider is None:
        return ModelContext('内容提取', 'local', 'broken', '原模型已退役，请重新选择',
                            '请选择本地模型', purpose_index=0, sub_index=0, status_kind='warning')
    if provider.kind == "local":
        try:
            downloaded = bool(models_manager.is_downloaded(provider.id))
            issue = models_manager.runtime_issue(provider.id) if downloaded else None
        except Exception:  # noqa: BLE001 - 状态读取失败时不能误报就绪
            downloaded, issue = False, "status unavailable"
        name = provider.display_name.replace("本地 · ", "").strip()
        return local_model_context(
            "内容提取",
            name,
            ready=downloaded and issue is None,
            broken=downloaded and issue is not None,
            purpose_index=0,
        )
    if not secrets.configuration_loaded(provider.id):
        return _unread_api_context("内容提取", _cloud_model_name(provider), 0)
    try:
        snapshot = providers.configuration_snapshot(provider.id)
        configured = snapshot.configured
        model_name = snapshot.model_name
    except Exception:  # noqa: BLE001 - Keychain 不可用时失败关闭
        configured = False
        model_name = ""
    if provider.id == "custom_openai" and not model_name:
        return _custom_openai_unconfigured_context()
    return api_model_context(
        "内容提取",
        model_name or _cloud_model_name(provider),
        configured=configured,
        purpose_index=0,
    )


def _unread_api_context(usage: str, model_name: str, purpose_index: int) -> ModelContext:
    return ModelContext(usage=usage, deployment="api", state="unknown",
                        status_text="大模型 API", model_name=model_name,
                        purpose_index=purpose_index, sub_index=1)
