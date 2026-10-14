"""引擎 / 云厂家预设表(含每家所需的配置字段)。

UI 读它动态渲染设置项;registry 读它构建对应引擎。新增一家 = 加一条预设。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import secrets


@dataclass(frozen=True)
class Field:
    name: str               # 配置键(存入 SecretStore 的 dict)
    label: str
    secret: bool = True     # True → 密码框
    placeholder: str = ""
    required: bool = True


@dataclass(frozen=True)
class Provider:
    id: str
    display_name: str
    kind: str                 # local | openai_compat | dashscope | volcano
    requires_network: bool
    needs_key: bool = False
    base_url: str = ""        # openai_compat 具名家的固定地址;自定义留空,由用户填
    default_model: str = ""
    signup_url: str = ""
    price_hint: str = ""
    experimental: bool = False
    fields: tuple[Field, ...] = ()


@dataclass(frozen=True)
class ProviderConfigSnapshot:
    """可安全交给 UI 的配置真相；有意不包含 API Key、地址或其它原始配置。"""

    configured: bool
    model_name: str


DEFAULT_ENGINE = "local_sensevoice"

_KEY = Field("api_key", "API Key", placeholder="粘贴你的 API Key")

PRESETS: dict[str, Provider] = {
    "dashscope": Provider(
        id="dashscope", display_name="阿里云百炼 · Paraformer(云端)",
        kind="dashscope", requires_network=True, needs_key=True,
        default_model="paraformer-realtime-v2",
        signup_url="https://bailian.console.aliyun.com/",
        price_hint="阿里原厂,中文最强",
        fields=(Field("api_key", "API Key", placeholder="sk-...(阿里云百炼 / DashScope)"),),
    ),
    "openai": Provider(
        id="openai", display_name="OpenAI · gpt-4o-transcribe(云端)",
        kind="openai_compat", requires_network=True, needs_key=True,
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-transcribe",
        signup_url="https://platform.openai.com/api-keys",
        price_hint="质量高,约 $0.2/小时,需国际网络",
        fields=(_KEY,),
    ),
    "siliconflow": Provider(
        id="siliconflow", display_name="硅基流动 · SenseVoice(云端)",
        kind="openai_compat", requires_network=True, needs_key=True,
        base_url="https://api.siliconflow.cn/v1",
        default_model="FunAudioLLM/SenseVoiceSmall",
        signup_url="https://cloud.siliconflow.cn/account/ak",
        price_hint="和本地同款模型,价格极低,中文稳",
        fields=(_KEY,),
    ),
    "groq": Provider(
        id="groq", display_name="Groq · Whisper-v3-turbo(云端)",
        kind="openai_compat", requires_network=True, needs_key=True,
        base_url="https://api.groq.com/openai/v1",
        default_model="whisper-large-v3-turbo",
        signup_url="https://console.groq.com/keys",
        price_hint="超快,约 $0.04/小时,中文尚可",
        fields=(_KEY,),
    ),
    "custom_openai": Provider(
        id="custom_openai", display_name="自定义 · OpenAI 兼容(云端)",
        kind="openai_compat", requires_network=True, needs_key=True,
        signup_url="",
        price_hint="任意 OpenAI 兼容服务,自填地址/模型/Key",
        fields=(
            Field("base_url", "接口地址", secret=False, placeholder="https://xxx/v1"),
            Field("model", "模型名", secret=False, placeholder="如 whisper-1 / 服务商给的模型名"),
            _KEY,
        ),
    ),
}


# 本地模型从 local_models 派生(平台过滤),并排在云端之前
from .local_models import available_local_models as _alm  # noqa: E402

_LOCAL_PRESETS = {
    m.id: Provider(
        id=m.id, display_name=f"本地 · {m.display_name}",
        kind="local", requires_network=False,
        price_hint="完全离线、不上传、免费;在「AI 大模型配置」→「文案提取」下载/管理",
    )
    for m in _alm()
}
PRESETS = {**_LOCAL_PRESETS, **PRESETS}


def all_presets() -> list[Provider]:
    return list(PRESETS.values())


def get_preset(engine_id: str) -> Provider | None:
    return PRESETS.get(engine_id)


def configuration_snapshot(engine_id: str) -> ProviderConfigSnapshot:
    """一次只读安全读取，返回是否配置及实际模型名，不暴露原始 Key。"""
    provider = PRESETS.get(engine_id)
    if provider is None:
        return ProviderConfigSnapshot(configured=False, model_name="")
    if not provider.needs_key:
        model_name = provider.default_model or provider.display_name.replace("本地 · ", "").strip()
        return ProviderConfigSnapshot(configured=True, model_name=model_name)
    raw = secrets.get_config(engine_id)
    configured = all(
        str(raw.get(field.name) or "").strip()
        for field in provider.fields
        if field.required
    )
    model_name = str(raw.get("model") or provider.default_model or "").strip()
    if provider.id == "custom_openai" and not model_name:
        configured = False
    return ProviderConfigSnapshot(configured=configured, model_name=model_name)


def is_configured(engine_id: str) -> bool:
    """该引擎是否已可用(本地恒 True;云端要求其 required 字段都已填)。"""
    return configuration_snapshot(engine_id).configured
