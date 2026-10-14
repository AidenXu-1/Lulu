"""转录引擎的统一抽象。

新增一个模型 = 写一个 TranscriptionEngine 子类,UI / pipeline 一行不改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from ..models import Transcript

# 进度回调:(人类可读的阶段说明, 进度 0.0~1.0;-1 表示不确定)
ProgressCallback = Callable[[str, float], None]
CancelCallback = Callable[[], bool]


def _noop(_msg: str, _frac: float) -> None:
    pass


class TranscriptionEngine(ABC):
    """所有转录引擎的基类。"""

    name: str = "base"            # 唯一标识,registry 用它选择
    display_name: str = "基础引擎"  # UI 下拉里展示的名字
    requires_network: bool = False  # 转录推理是否需要联网(本地引擎为 False)

    @abstractmethod
    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> Transcript:
        """把一个 16k 单声道 wav 转成 Transcript。

        约定:传入的音频已由上游 converter 归一化好,引擎不负责转码。
        """
        raise NotImplementedError

    def warmup(self, progress: ProgressCallback | None = None) -> None:
        """可选:提前加载模型/建立连接。默认什么都不做。"""
        return None
