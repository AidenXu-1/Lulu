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

    def __init__(self, model_id='local_whisper_mlx'):
        from .local_models import LOCAL_MODELS
        self.name = model_id
        self.display_name = LOCAL_MODELS[model_id].display_name

    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> Transcript:
        from .asr_resource import model_use
        with model_use(self.name, retain=True):
            return self._transcribe(wav_path, progress, is_cancelled)

    def _transcribe(self, wav_path, progress=None, is_cancelled=None):
        progress = progress or _noop
        is_cancelled = is_cancelled or (lambda: False)
        if is_cancelled():
            raise InterruptedError("转录已取消")
        from .. import models_manager as mm
        if not mm.is_downloaded(self.name):
            raise RuntimeError("所选本地模型尚未校验就绪，请在模型设置中修复。")

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
            path_or_hf_repo=str(mm.model_path(self.name)),
            on_audio_position=report_audio_position,
            is_cancelled=is_cancelled,
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
        from .local_models import LOCAL_MODELS
        from ..artifact_trust import model_revision
        from importlib.metadata import version
        model = LOCAL_MODELS[self.name]
        return Transcript(text=text, segments=segments, language=res.get("language"), engine=self.name,
            meta={'asr': dict(model_id=self.name, model_revision=model_revision(model.repo, model.repo_source),
                runtime_version=version('mlx-whisper'), timestamp_kind='segment', diarization=False,
                timing_estimated=False)})
