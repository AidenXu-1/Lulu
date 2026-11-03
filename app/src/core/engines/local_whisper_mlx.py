"""本地引擎:Whisper large-v3-turbo(MLX,仅 Apple Silicon)。

mlx_whisper.transcribe(wav, path_or_hf_repo=本地模型目录)直接转写本地 wav,带分段时间戳。
模型由「本地模型」页下载到受管目录。
"""

from __future__ import annotations

from bisect import bisect_right
import math
from pathlib import Path
import re

from ..models import Segment, Transcript
from .base import CancelCallback, ProgressCallback, TranscriptionEngine, _noop
from .mlx_whisper_progress import transcribe_with_audio_progress
from .local_native import IncompleteTranscription


def _has_impossible_repetition(result: dict) -> bool:
    """Flag extreme repetition in its actual time span, without deleting text.

    Normal stutters, chants, and repeated phrases over real speaking time stay
    intact. The live failure emitted 442 identical characters in 0.21 seconds.
    Adjacent segments are included so a decoder cannot hide a loop by splitting it.
    """
    segments, ends, parts = [], [], []
    length = 0
    for segment in result.get("segments") or []:
        text = re.sub(r"\s+", "", segment.get("text") or "")
        if text:
            segments.append(segment)
            parts.append(text)
            length += len(text)
            ends.append(length)
    if length < 64:
        return False
    for match in re.finditer(r"(.{1,32})\1{15,}", "".join(parts)):
        if match.end() - match.start() < 64:
            continue
        first = bisect_right(ends, match.start())
        last = bisect_right(ends, match.end() - 1)
        start, end = segments[first].get("start"), segments[last].get("end")
        if (isinstance(start, (int, float)) and isinstance(end, (int, float))
                and math.isfinite(start) and math.isfinite(end)
                and (match.end() - match.start()) / max(.02, end - start) > 80):
            return True
    return False


def _abnormal_output(result, audio_duration):
    if _has_impossible_repetition(result):
        return "abnormal_repetition"
    # Timestamp quantization is 20ms. Allow a full second for normal boundary
    # imprecision, but never accept speech placed beyond the actual recording.
    for segment in result.get("segments") or []:
        if not (segment.get("text") or "").strip():
            continue
        start, end = segment.get("start"), segment.get("end")
        if (isinstance(start, (int, float)) and isinstance(end, (int, float))
                and (not math.isfinite(start) or not math.isfinite(end)
                     or start < -1 or end < start or end > audio_duration + 1)):
            return "invalid_audio_timing"
    return None


def _preserve_abnormal(model_id, attempts, reason, path=None):
    """Keep abnormal output privately before retry/cancel; never save it as done."""
    from uuid import uuid4
    from ..models_manager import app_data_dir
    from ..updater.secure_io import atomic_json
    if path is None:
        path = app_data_dir() / "incomplete-transcriptions" / (uuid4().hex + ".json")
    records = [{"text": value.get("text") or "", "segments": [
        {key: segment.get(key) for key in ("text", "start", "end")}
        for segment in value.get("segments") or []]} for value in attempts]
    try:
        atomic_json(path, {"schema": 1, "model_id": model_id,
                           "reason": reason, "attempts": records})
    except Exception as exc:
        raise IncompleteTranscription("转录结果异常，恢复记录未能保存；请重试。",
                                      records[0]["text"]) from exc
    return path


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

        prepared_audio = np.ascontiguousarray(audio, dtype=np.float32)
        audio_duration = len(prepared_audio) / _sr
        res = transcribe_with_audio_progress(
            prepared_audio,
            path_or_hf_repo=str(mm.model_path(self.name)),
            on_audio_position=report_audio_position,
            is_cancelled=is_cancelled,
        )
        if is_cancelled():
            raise InterruptedError("转录已取消")
        reason = _abnormal_output(res, audio_duration)
        if reason:
            first = res
            evidence = _preserve_abnormal(self.name, [first], reason)
            progress("检测到识别结果异常，正在用同一模型重新识别一次…", -1.)
            if is_cancelled():
                raise InterruptedError("转录已取消")
            # Both options are supported by the installed Whisper MLX runtime.
            # Only abnormal output takes this path; normal decoding stays unchanged.
            recovery_options = {"word_timestamps": True} if reason == "invalid_audio_timing" else {}
            res = transcribe_with_audio_progress(
                prepared_audio, path_or_hf_repo=str(mm.model_path(self.name)),
                on_audio_position=report_audio_position, is_cancelled=is_cancelled,
                condition_on_previous_text=False, temperature=0.,
                **recovery_options,
            )
            _preserve_abnormal(self.name, [first, res], reason, evidence)
            if is_cancelled():
                raise InterruptedError("转录已取消")
            if _abnormal_output(res, audio_duration):
                raise IncompleteTranscription(
                    f"Whisper 识别结果异常，重试后仍未完成；原始结果已保存在：{evidence}",
                    first.get("text") or "",
                )
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
