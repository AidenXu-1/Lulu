"""引擎注册表:本地模型按运行时分派(需先下载);云端按预设 + SecretStore 构建。"""

from __future__ import annotations

from .. import secrets
from ..models_manager import is_ready, runtime_issue
from .base import TranscriptionEngine
from .local_models import LOCAL_MODELS, RETIRED_MODELS
from .online_api import OpenAICompatEngine
from .providers import DEFAULT_ENGINE, PRESETS, Provider

_local_instances: dict[str, TranscriptionEngine] = {}


def available_engines() -> list[tuple[str, str]]:
    return [(p.id, p.display_name) for p in PRESETS.values()]


def _build_local(name: str, runtime: str) -> TranscriptionEngine:
    if runtime == "native":
        from .local_native import NativeASREngine
        return NativeASREngine(name)
    if runtime == "mlx":
        from .local_whisper_mlx import LocalWhisperMLXEngine
        return LocalWhisperMLXEngine(name)
    raise ValueError(f"未知本地运行时:{runtime}")


def get_engine(name: str | None = None) -> TranscriptionEngine:
    name = name or DEFAULT_ENGINE
    if name in RETIRED_MODELS:
        raise ValueError("此模型已退役，历史文稿保留；请选择 Q8、Whisper 或 MOSS 后重新开始。")

    # 本地模型:先确认已下载,再按运行时分派(各模型实例缓存)
    if name in LOCAL_MODELS:
        m = LOCAL_MODELS[name]
        issue = runtime_issue(name)
        if issue:
            raise RuntimeError(issue)
        if not is_ready(name):
            raise RuntimeError(
                f"{m.display_name} 还没下载。请到「AI 大模型配置」→「内容提取」→ 本地部署 下载后再用。"
            )
        if name not in _local_instances:
            _local_instances[name] = _build_local(name, m.runtime)
        return _local_instances[name]

    # 云端
    preset: Provider | None = PRESETS.get(name)
    if preset is None:
        raise ValueError(f"未知引擎:{name}")
    cfg = secrets.get_config(name)
    if preset.kind == "openai_compat":
        return OpenAICompatEngine(
            engine_id=preset.id, display_name=preset.display_name,
            base_url=preset.base_url or cfg.get("base_url", ""),
            model=preset.default_model or cfg.get("model", ""),
            api_key=cfg.get("api_key"),
        )
    if preset.kind == "dashscope":
        from .dashscope_engine import DashScopeEngine
        return DashScopeEngine(
            engine_id=preset.id, display_name=preset.display_name,
            model=preset.default_model, api_key=cfg.get("api_key"),
        )
    raise ValueError(f"未知引擎类型:{preset.kind}")
