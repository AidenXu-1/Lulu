"""语音合成引擎注册表。"""

from __future__ import annotations

from .base import SynthesisEngine

_instances: dict[str, SynthesisEngine] = {}


def get_synthesis_engine(name: str = "local_voxcpm2") -> SynthesisEngine:
    # 本地 TTS 一律走独立运行时包的子进程引擎(主进程从不 import 任何 TTS)。
    # 未安装运行时包时,引擎会给出「请到 AI 大模型配置 → 文案配音 → 本地部署 安装」的清晰提示。
    if name not in _instances:
        if name == "local_voxcpm2":
            from .voxcpm2_subprocess import VoxCPM2SubprocessEngine
            _instances[name] = VoxCPM2SubprocessEngine()
        elif name == "local_indextts2":
            from .indextts2_subprocess import IndexTTS2SubprocessEngine
            _instances[name] = IndexTTS2SubprocessEngine()
        elif name == 'local_qwen3_tts':
            from .qwen3_subprocess import Qwen3Engine
            _instances[name] = Qwen3Engine()
        else:
            raise ValueError(f"未知合成引擎:{name}")
    return _instances[name]
