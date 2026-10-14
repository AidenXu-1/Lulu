"""本地引擎:阿里 SenseVoice-Small(经 FunASR 调用)。

- Apple Silicon 走 MPS,失败回退 CPU(见 ADR-0002/0003)。
- 首次运行从 ModelScope 下载权重到本地缓存,之后推理全程离线(守住离线红线)。
- 重依赖(torch / funasr)在首次 transcribe 时才 import 和加载,避免拖慢 UI 启动。
"""

from __future__ import annotations

from pathlib import Path

from ..models import Segment, Transcript
from .base import CancelCallback, ProgressCallback, TranscriptionEngine, _noop

# ModelScope 上的模型 id(首次运行用,触发下载)
_MODEL_ID = "iic/SenseVoiceSmall"
_VAD_MODEL = "fsmn-vad"  # 长音频切分,FunASR 内置别名
_LANGUAGE_TOKENS = (
    ("<|zh|>", "zh"),
    ("<|en|>", "en"),
    ("<|yue|>", "yue"),
    ("<|ja|>", "ja"),
    ("<|ko|>", "ko"),
)


def _detect_language_token(raw_text: str) -> str | None:
    detected = (
        (position, language)
        for token, language in _LANGUAGE_TOKENS
        if (position := raw_text.find(token)) >= 0
    )
    first = min(detected, default=None)
    return first[1] if first is not None else None


def _resolve_model_args() -> tuple[str, str]:
    """从受管目录加载(命中则走本地路径,绕开 modelscope 子进程/闪窗);否则回退 id。"""
    from .. import models_manager as mm

    sv, vad = mm.sensevoice_paths()
    if sv.is_dir() and vad.is_dir():
        return str(sv), str(vad)
    return _MODEL_ID, _VAD_MODEL


def detect_device() -> str:
    """选择推理设备:优先 Apple Silicon 的 mps,否则 cuda,再否则 cpu。"""
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


class LocalSenseVoiceEngine(TranscriptionEngine):
    name = "local_sensevoice"
    display_name = "本地 · SenseVoice-Small(离线)"
    requires_network = False  # 模型缓存好后推理不联网

    def __init__(self, device: str | None = None):
        self._device = device or detect_device()
        self._model = None  # 懒加载

    # ------------------------------------------------------------------ #
    def _load_model(self, progress: ProgressCallback):
        if self._model is not None:
            return
        progress(f"加载 SenseVoice 模型(设备:{self._device})…首次运行会下载权重", -1.0)
        from funasr import AutoModel

        model_arg, vad_arg = _resolve_model_args()
        self._model = AutoModel(
            model=model_arg,
            vad_model=vad_arg,
            vad_kwargs={"max_single_segment_time": 30000},
            device=self._device,
            trust_remote_code=True,
            disable_update=True,
            disable_pbar=True,
            disable_log=True,
        )

    def warmup(self, progress: ProgressCallback | None = None) -> None:
        from .sensevoice_lock import sensevoice_resource
        with sensevoice_resource():
            self._load_model(progress or _noop)

    # ------------------------------------------------------------------ #
    def transcribe(
        self,
        wav_path: Path,
        progress: ProgressCallback | None = None,
        is_cancelled: CancelCallback | None = None,
    ) -> Transcript:
        from .sensevoice_lock import sensevoice_resource
        with sensevoice_resource():
            return self._transcribe(wav_path, progress, is_cancelled)

    def _transcribe(self, wav_path, progress, is_cancelled):
        progress = progress or _noop
        is_cancelled = is_cancelled or (lambda: False)
        if is_cancelled():
            raise InterruptedError("转录已取消")
        try:
            self._load_model(progress)
            return self._run(wav_path, progress, is_cancelled)
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001
            # MPS 上个别算子可能不支持 → 回退 CPU 重试一次
            if self._device != "cpu":
                progress(f"{self._device} 推理失败({exc}),回退 CPU 重试…", -1.0)
                self._device = "cpu"
                self._model = None
                self._load_model(progress)
                return self._run(wav_path, progress, is_cancelled)
            raise

    def _run(
        self,
        wav_path: Path,
        progress: ProgressCallback,
        is_cancelled: CancelCallback,
    ) -> Transcript:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        progress("正在转录… 0%", 0.0)
        last_fraction = 0.0

        def native_progress(current, total) -> None:
            nonlocal last_fraction
            if is_cancelled():
                raise InterruptedError("转录已取消")
            try:
                fraction = float(current) / float(total)
            except (TypeError, ValueError, ZeroDivisionError):
                progress("正在转录…", -1.0)
                return
            last_fraction = max(last_fraction, min(1.0, fraction))
            progress(f"正在转录… {int(last_fraction * 100)}%", last_fraction)

        results = self._model.generate(
            input=str(wav_path),
            cache={},
            language="auto",   # 自动判别语种
            use_itn=True,      # 数字/标点正规化
            batch_size_s=60,
            merge_vad=True,
            merge_length_s=15,
            num_workers=0,     # 单进程,避免打包后子进程闪窗
            progress_callback=native_progress,
        )
        if is_cancelled():
            raise InterruptedError("转录已取消")

        segments: list[Segment] = []
        texts: list[str] = []
        detected_lang: str | None = None
        for item in results:
            raw = item.get("text", "")
            if detected_lang is None:
                detected_lang = _detect_language_token(raw) or item.get("lang") or None
            clean = rich_transcription_postprocess(raw)
            if not clean:
                continue
            texts.append(clean)
            start = item.get("start")
            end = item.get("end")
            segments.append(
                Segment(
                    text=clean,
                    start=(start / 1000.0 if isinstance(start, (int, float)) else None),
                    end=(end / 1000.0 if isinstance(end, (int, float)) else None),
                )
            )
        full_text = "\n".join(texts).strip()
        progress("转录完成", 1.0)
        return Transcript(
            text=full_text,
            segments=segments,
            language=detected_lang,
            engine=self.name,
            source=str(wav_path),
        )
