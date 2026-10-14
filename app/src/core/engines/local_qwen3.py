"""本地引擎:Qwen3-ASR-0.6B(qwen-asr / transformers,本地 wav 直读)。

模型由「本地模型」页下载到受管目录;这里从该目录加载。Apple Silicon 走 MPS,失败回退 CPU。
"""

from __future__ import annotations

from pathlib import Path

from ..models import Segment, Transcript
from .base import CancelCallback, ProgressCallback, TranscriptionEngine, _noop
from .local_sensevoice import detect_device

_SENT_END = set("。!?…\n.!?")          # 句末标点 → 断句
_SOFT_BREAK = set(",;、,;:")            # 次级停顿,够长才断
_MAX_CHARS = 28                          # 一行字幕的目标上限
_GAP_SEC = 0.8                           # 停顿超过这么久也断句
_PROGRESS_CHUNK_SECONDS = 60.0            # 真实结算粒度；兼顾上下文、吞吐与取消响应


def _merge_tokens(toks: list[tuple[str, float, float]]) -> list[Segment]:
    """把字/词级时间戳合并成句子级 Segment(按标点 / 长度 / 停顿断句)。"""
    segs: list[Segment] = []
    buf, start, end = "", None, None
    for i, (txt, st, en) in enumerate(toks):
        if start is None:
            start, buf = st, ""
        buf += txt
        end = en
        last = txt[-1] if txt else ""
        gap = (toks[i + 1][1] - en) if i + 1 < len(toks) else 0.0
        long_enough = len(buf) >= _MAX_CHARS
        if (last in _SENT_END
                or (last in _SOFT_BREAK and len(buf) >= 12)
                or long_enough
                or gap >= _GAP_SEC):
            segs.append(Segment(text=buf.strip(), start=start, end=end))
            buf, start, end = "", None, None
    if buf.strip():
        segs.append(Segment(text=buf.strip(), start=start, end=end))
    return segs


class LocalQwen3Engine(TranscriptionEngine):
    name = "local_qwen3"
    display_name = "本地 · Qwen3-ASR-0.6B"
    requires_network = False

    def __init__(self, device: str | None = None):
        self._device = device or detect_device()
        self._model = None

    def _load(self, progress: ProgressCallback) -> None:
        if self._model is not None:
            return
        from .. import models_manager as mm
        issue = mm.runtime_issue(self.name)
        if issue:
            raise RuntimeError(issue)
        import torch
        from qwen_asr import Qwen3ASRModel

        progress(f"加载 Qwen3-ASR + 对齐模型(设备:{self._device})…", -1.0)
        asr_dir, aligner_dir = mm.qwen3_paths()
        # max_new_tokens 是「每个音频分块」的输出上限。带时间戳时库会把音频切成 ≤180s
        # 的块,256 太小 → 每块都被截断、整体只出一半内容。给足额度(到 EOS 自然停)。
        self._model = Qwen3ASRModel.from_pretrained(
            str(asr_dir), dtype=torch.float16, device_map=self._device, max_new_tokens=4096,
            forced_aligner=str(aligner_dir),                      # 启用时间戳
            forced_aligner_kwargs={"dtype": torch.float16, "device_map": self._device},
        )

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
        try:
            self._load(progress)
            return self._run(wav_path, progress, is_cancelled)
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._device != "cpu":
                progress(f"{self._device} 失败({exc}),回退 CPU 重试…", -1.0)
                self._device = "cpu"
                self._model = None
                self._load(progress)
                return self._run(wav_path, progress, is_cancelled)
            raise

    def _run(
        self,
        wav_path: Path,
        progress: ProgressCallback,
        is_cancelled: CancelCallback,
    ) -> Transcript:
        import numpy as np
        import soundfile as sf

        progress("正在转录(Qwen3,带时间戳)… 0%", 0.0)
        audio, sr = sf.read(str(wav_path), dtype="float32")
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        total_samples = int(audio.shape[0])
        chunk_samples = max(1, int(float(sr) * _PROGRESS_CHUNK_SECONDS))
        text_parts: list[str] = []
        toks: list[tuple[str, float, float]] = []
        lang = None
        context = ""
        for start in range(0, total_samples, chunk_samples):
            if is_cancelled():
                raise InterruptedError("转录已取消")
            end = min(total_samples, start + chunk_samples)
            res = self._model.transcribe(
                audio=(np.ascontiguousarray(audio[start:end]), int(sr)),
                context=context,
                language=None,
                return_time_stamps=True,
            )
            r0 = res[0] if res else None
            chunk_text = (r0.text or "").strip() if r0 else ""
            if chunk_text:
                text_parts.append(chunk_text)
                context = (context + chunk_text)[-240:]
            if lang is None and r0 is not None:
                lang = getattr(r0, "language", None)
            offset = start / float(sr)
            for ts in (getattr(r0, "time_stamps", None) or []) if r0 else []:
                txt = (getattr(ts, "text", None) or "").strip()
                st, en = getattr(ts, "start_time", None), getattr(ts, "end_time", None)
                if txt and isinstance(st, (int, float)) and isinstance(en, (int, float)):
                    toks.append((txt, float(st) + offset, float(en) + offset))
            fraction = end / total_samples if total_samples else 1.0
            progress(f"正在转录(Qwen3)… {int(fraction * 100)}%", fraction)
        if is_cancelled():
            raise InterruptedError("转录已取消")
        text = "".join(text_parts).strip()

        # 对齐结果是字/词级(start_time/end_time 已是秒)。字级做字幕太碎,
        # 这里按标点 / 长度 / 停顿合并成句子级 Segment,适合 SRT / Markdown。
        segments = _merge_tokens(toks)

        progress("完成", 1.0)
        return Transcript(text=text, segments=segments, language=lang, engine=self.name)
