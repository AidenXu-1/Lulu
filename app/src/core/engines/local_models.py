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
    runtime: str            # native | mlx
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
    diarization: bool = False


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


RETIRED_MODELS = {'sensevoice': 'SenseVoice-Small', 'local_sensevoice': 'SenseVoice-Small',
                  'local_qwen3': 'Qwen3-ASR-0.6B', 'qwen3': 'Qwen3-ASR-0.6B'}

LOCAL_MODELS: dict[str, LocalModel] = {
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

# Each file belongs to one immutable repository revision (artifact_trust).
# This list also constrains both download planning and the actual transfer.
LOCAL_MODEL_FILES = {
    'local_sensevoice_q8': {
        'FunAudioLLM/SenseVoiceSmall-GGUF': {
            'sensevoice-small-q8.gguf': (254208320, '4ae45c94422de949b387e2e0fb10d7e14e4c42c69db30c3444ecc7d4b844b7c5'),
        },
        'FunAudioLLM/fsmn-vad-GGUF': {
            'fsmn-vad.gguf': (1720512, '1270f2559c495f4e7b6e739541151027d360761a3fda43fc147034f5719f5479'),
        },
    },
    'local_whisper_mlx_4bit': {
        'mlx-community/whisper-large-v3-turbo-4bit': {
            'config.json': (341, '538e24557b8f9bc504700add5e7bbe32087c2353001ff563e64772ad4398671a'),
            'model.safetensors': (463462982, 'e2d6146b49644c13ed7467060d3c43c402bbdfca1f01f1c21f6fbeda23b6567e'),
            'multilingual.tiktoken': (816730, 'b34b360dbb493e781e479794586d661700670d65564001f23024971d1f2fa126'),
        },
    },
    'local_moss_q5': {
        'handy-computer/moss-transcribe-diarize-gguf': {
            'MOSS-Transcribe-Diarize-Q5_K_M.gguf': (700314304, 'cc36d8ad46c66a55125cd9339dc063e62c657d63de60d571442d19aea87e7349'),
        },
    },
}

LOCAL_MODELS.update({
    'local_sensevoice_q8': LocalModel(
        id='local_sensevoice_q8', display_name='SenseVoice Q8', runtime='native',
        repo='FunAudioLLM/SenseVoiceSmall-GGUF', repo_source='hf', approx_size_mb=256,
        apple_silicon_only=True, languages='中 / 英 / 粤 / 日 / 韩',
        speed_note='轻量快速，适合日常中文', best_for='日常中文文稿，无需字幕时间点', default=True,
        min_spec='苹果芯 Mac，macOS 15 或更新版本；最低内存尚未验证。',
        features='本地离线转录；保留基础分段，不提供时间点或发言人区分。',
        platform_note='苹果芯 Mac', source='SenseVoice · Q8 量化版'),
    'local_whisper_mlx_4bit': LocalModel(
        id='local_whisper_mlx_4bit', display_name='Whisper Turbo 4bit', runtime='mlx',
        repo='mlx-community/whisper-large-v3-turbo-4bit', repo_source='hf', approx_size_mb=464,
        apple_silicon_only=True, languages='99 种语言',
        speed_note='体积更小，支持多语言与字幕', best_for='多语言转录、分句时间点及字幕导出',
        min_spec='苹果芯 Mac，macOS 15 或更新版本；最低内存尚未验证。',
        features='与原版共用识别设置，提供分句时间点，不区分发言人。',
        platform_note='苹果芯 Mac', source='OpenAI Whisper · MLX 社区 4bit 版', timestamps=True),
    'local_moss_q5': LocalModel(
        id='local_moss_q5', display_name='MOSS Q5', runtime='native',
        repo='handy-computer/moss-transcribe-diarize-gguf', repo_source='hf', approx_size_mb=700,
        apple_silicon_only=True, languages='中 / 英等',
        speed_note='带匿名发言人与句段时间', best_for='访谈、会议等多人录音',
        min_spec='苹果芯 Mac，macOS 15 或更新版本；长录音内存需求尚待验证。',
        features='本地转录并标记发言人1、2等；时间与分人可能有误差，无法识别真实身份。',
        platform_note='苹果芯 Mac', source='MOSS · handy-computer GGUF Q5 版',
        timestamps=True, diarization=True),
})


def available_local_models() -> list[LocalModel]:
    """按当前平台过滤(非 Apple Silicon 隐藏仅 Mac 的模型)。"""
    apple = is_apple_silicon()
    return sorted((m for m in LOCAL_MODELS.values() if apple or not m.apple_silicon_only), key=lambda m: not m.default)
