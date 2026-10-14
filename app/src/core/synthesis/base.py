"""语音合成引擎抽象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ProgressCallback = Callable[[str, float], None]


def _noop(_msg: str, _frac: float) -> None:
    pass


@dataclass
class VoiceProfile:
    id: str
    name: str
    reference_audio_path: str
    reference_text: str = ""
    engine: str = "local_voxcpm2"
    model_id: str = "openbmb/VoxCPM2"
    created_at: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class SynthesisRequest:
    text: str
    voice: VoiceProfile | None
    output_path: Path
    output_format: str = "wav"
    inference_timesteps: int = 10
    cfg_value: float = 2.0
    denoise: bool = False
    retry_badcase: bool = True
    engine_options: dict = field(default_factory=dict)


@dataclass
class SynthesisResult:
    output_path: Path
    wav_path: Path
    duration_seconds: float
    engine: str
    voice_id: str
    text: str
    meta: dict = field(default_factory=dict)


class SynthesisError(RuntimeError):
    """语音合成失败。"""


class SynthesisEngine:
    name = "base"
    display_name = "基础合成引擎"
    requires_network = False

    def synthesize(
        self,
        request: SynthesisRequest,
        progress: ProgressCallback | None = None,
        is_cancelled=None,
    ) -> SynthesisResult:
        raise NotImplementedError
