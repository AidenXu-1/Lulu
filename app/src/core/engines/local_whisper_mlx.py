"""本地引擎:Whisper large-v3-turbo(MLX,仅 Apple Silicon)。

mlx_whisper.transcribe(wav, path_or_hf_repo=本地模型目录)直接转写本地 wav,带分段时间戳。
模型由「本地模型」页下载到受管目录。
"""

from __future__ import annotations

from pathlib import Path

from ..models import Segment, Transcript
from .base import CancelCallback, ProgressCallback, TranscriptionEngine, _noop
from .mlx_whisper_progress import transcribe_with_audio_progress


class LocalWhisperMLXEngine(TranscriptionEngine):
    name = "local_whisper_mlx"
    display_name = "本地 · Whisper large-v3-turbo (MLX)"
    requires_network = False

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> Transcript:
        progress = progress or _noop
        is_cancelled = is_cancelled or (lambda: False)
        if is_cancelled():
            raise InterruptedError("转录已取消")
        from .. import models_manager as mm

        import numpy as np
        import soundfile as sf

        progress("正在转录(Whisper MLX)… 0%", 0.0)
        # 上游已转成 16k 单声道 wav。直接读成数组喂给 mlx-whisper,
        # 绕开它内部写死的 `["ffmpeg", ...]`(访达启动的 .app PATH 里没有 ffmpeg)。
        audio, _sr = sf.read(str(wav_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        def report_audio_position(fraction: float) -> None:
            if is_cancelled():
                raise InterruptedError("转录已取消")
            percent = max(0, min(100, int(fraction * 100)))
            progress(f"正在转录(Whisper MLX) · 已处理音频 {percent}%", fraction)

        res = transcribe_with_audio_progress(
            np.ascontiguousarray(audio, dtype=np.float32),
            path_or_hf_repo=str(mm.model_path("local_whisper_mlx")),
            on_audio_position=report_audio_position,
        )
        if is_cancelled():
            raise InterruptedError("转录已取消")
        text = (res.get("text") or "").strip()
        segments: list[Segment] = []
        for s in res.get("segments") or []:
            t = (s.get("text") or "").strip()
            if not t:
                continue
            segments.append(Segment(text=t, start=s.get("start"), end=s.get("end")))
        progress("完成", 1.0)
        return Transcript(text=text, segments=segments, language=res.get("language"), engine=self.name)
