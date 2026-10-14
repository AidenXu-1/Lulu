"""本地模型注册表:每个本地 ASR 模型的描述符(给 UI 渲染卡片 + 配置弹窗)。

新增一个本地模型 = 加一条 LocalModel + 写一个对应的引擎适配器。
"""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalModel:
    id: str                 # 引擎 id(= registry/providers 里的 engine_name)
    display_name: str
    runtime: str            # funasr | transformers | mlx
    repo: str               # ModelScope / HF 仓库 id
    repo_source: str        # modelscope | hf
    approx_size_mb: int     # 权重约体积，仅用于未安装卡片的参考信息
    apple_silicon_only: bool
    languages: str          # 卡片用:语言概览
    speed_note: str         # 卡片用:一句话特点(白话,不放术语)
    best_for: str = ""      # 一句话推荐场景(卡片 + 弹窗都用,帮用户选对)
    # —— 配置弹窗(都用大白话,不放 RTF / MPS / 仓库 id 之类术语)——
    min_spec: str = ""      # 最低电脑配置
    features: str = ""      # 特色功能
    platform_note: str = "" # 适用平台
    source: str = ""        # 模型来源
    timestamps: bool = False # 是否支持句级时间戳(可导 SRT / 带时间点 md)
    ready: bool = True      # 适配器是否已就绪(未就绪只展示、不可选用)
    default: bool = False
    min_spec_label: str = "配置参考"


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


LOCAL_MODELS: dict[str, LocalModel] = {
    "local_sensevoice": LocalModel(
        id="local_sensevoice",
        display_name="SenseVoice-Small",
        runtime="funasr",
        repo="iic/SenseVoiceSmall",
        repo_source="modelscope",
        approx_size_mb=950,
        apple_silicon_only=False,
        languages="中 / 英 / 粤 / 日 / 韩等",
        speed_note="又快又稳,还能听出情绪",
        best_for="日常中文转写，适合不需要时间戳的文稿",
        default=True,
        min_spec="最低内存尚未验证；所需磁盘空间会在下载前检查。",
        features="支持中文、粤语、日语、韩语等语音识别，以及情绪和声音事件识别；不提供时间戳。",
        platform_note="Mac / Windows / Linux 都能跑",
        source="阿里开源 · SenseVoice",
        timestamps=False,
    ),
    "local_qwen3": LocalModel(
        id="local_qwen3",
        display_name="Qwen3-ASR-0.6B",
        runtime="transformers",
        repo="Qwen/Qwen3-ASR-0.6B",
        repo_source="modelscope",
        approx_size_mb=3000,
        apple_silicon_only=False,
        languages="52 种语言 / 方言",
        speed_note="自带时间戳,能导字幕",
        best_for="方言和多语种转写，需要时间戳时可选",
        ready=True,
        min_spec="最低内存尚未验证；需同时安装转写与对齐模型，所需空间会在下载前检查。",
        features="52 种语言方言通吃;自带时间戳,能导字幕和带时间点的笔记。速度中规中矩,不快也不慢。",
        platform_note="Mac / Windows / Linux 都能跑",
        source="阿里 Qwen 开源 · 转写 + 对齐两个模型,点下载会一起拿",
        timestamps=True,
    ),
    "local_whisper_mlx": LocalModel(
        id="local_whisper_mlx",
        display_name="Whisper large-v3-turbo (MLX)",
        runtime="mlx",
        repo="mlx-community/whisper-large-v3-turbo",
        repo_source="hf",
        approx_size_mb=1600,
        apple_silicon_only=True,
        languages="99 种语言",
        speed_note="支持时间戳与字幕导出",
        best_for="苹果芯 Mac 上的多语言转写与字幕制作",
        ready=True,
        min_spec="需要苹果芯 Mac（M 系列）；最低内存尚未验证，所需空间会在下载前检查。",
        features="OpenAI Whisper 的 MLX 社区版本，支持多语言转写、时间戳与字幕导出。",
        platform_note="只支持苹果芯 Mac(Intel、Windows 用不了)",
        source="OpenAI Whisper · MLX 社区优化版",
        timestamps=True,
    ),
}


def available_local_models() -> list[LocalModel]:
    """按当前平台过滤(非 Apple Silicon 隐藏仅 Mac 的模型)。"""
    apple = is_apple_silicon()
    return [m for m in LOCAL_MODELS.values() if apple or not m.apple_silicon_only]
