"""VoxCPM2 本地克隆音 adapter。"""

from __future__ import annotations

import tempfile
import time
import contextlib
import io
import os
import sys
from pathlib import Path

from ..engines.local_sensevoice import detect_device
from . import local_models
from .base import SynthesisEngine, SynthesisError, SynthesisRequest, SynthesisResult, _noop
from .errors import scrub_detail
from .splitter import split_text
# 注:soundfile / .stitcher(连带 numpy)只在 synthesize 内用,改为函数内懒导入——
# 否则 ui.worker 顶层 import 本模块取 was_cancelled 时会把 numpy+soundfile 拖进启动期(~27ms)。


class _ScrubbedStderr:
    def __init__(self, target):
        self._buf = ""
        self._target = target

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = scrub_detail(line)
            if line:
                print(f"[voxcpm] {line}", file=self._target)
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            print(f"[voxcpm] {scrub_detail(self._buf)}", file=self._target)
        self._buf = ""


@contextlib.contextmanager
def _voxcpm_output_boundary():
    """默认收起 VoxCPM 上游输出;调试时只输出脱敏后的短行。"""
    if os.environ.get("LULU_DEBUG_TTS") == "1":
        sink = _ScrubbedStderr(sys.stderr)
    else:
        sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        yield
    if hasattr(sink, "flush"):
        sink.flush()


class VoxCPM2Engine(SynthesisEngine):
    name = "local_voxcpm2"
    display_name = "本地 · VoxCPM2 克隆音"
    requires_network = False

    def __init__(self, device: str | None = None):
        self._device = device or detect_device()
        self._model = None

    def _load_model(self, progress):
        if self._model is not None:
            return self._model
        progress("加载 VoxCPM2 本地模型…首次使用可能需要约 2 分钟", -1.0)
        try:
            from voxcpm import VoxCPM
        except Exception as exc:  # noqa: BLE001
            raise SynthesisError("未安装 voxcpm。请确认 requirements 已安装 voxcpm==2.0.3。") from exc

        start = time.perf_counter()
        cache_dir = local_models.hf_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        with _voxcpm_output_boundary():
            self._model = VoxCPM.from_pretrained(
                "openbmb/VoxCPM2",
                cache_dir=str(cache_dir),
                load_denoiser=False,
                device=self._device,
                optimize=False,
            )
        local_models.mark_downloaded()
        progress(f"VoxCPM2 已加载({time.perf_counter() - start:.1f}s)", 0.12)
        return self._model

    def synthesize(self, request: SynthesisRequest, progress=None, is_cancelled=None) -> SynthesisResult:
        import soundfile as sf
        from .stitcher import stitch_wavs

        progress = progress or _noop
        is_cancelled = is_cancelled or (lambda: False)
        text = (request.text or "").strip()
        if not text:
            raise SynthesisError("请输入要合成的文本。")
        ref = Path(request.voice.reference_audio_path)
        if not ref.exists():
            raise SynthesisError("参考音频不存在,请重新创建音色。")

        def check_cancelled():
            if is_cancelled():
                raise _Cancelled()

        check_cancelled()
        model = self._load_model(progress)
        segments = split_text(text)
        if not segments:
            raise SynthesisError("请输入要合成的文本。")

        with tempfile.TemporaryDirectory(prefix="lulu_tts_") as tmp:
            tmp_dir = Path(tmp)
            parts: list[Path] = []
            sample_rate = int(model.tts_model.sample_rate)
            for idx, seg in enumerate(segments, start=1):
                check_cancelled()
                frac = 0.12 + (idx - 1) / max(len(segments), 1) * 0.74
                progress(f"生成第 {idx}/{len(segments)} 段…", frac)
                with _voxcpm_output_boundary():
                    audio = model.generate(
                        text=seg,
                        reference_wav_path=str(ref),
                        inference_timesteps=request.inference_timesteps,
                        cfg_value=request.cfg_value,
                        denoise=request.denoise,
                        retry_badcase=request.retry_badcase,
                    )
                part = tmp_dir / f"part_{idx:03d}.wav"
                sf.write(str(part), audio, sample_rate)
                parts.append(part)
                check_cancelled()
            progress("拼接音频…", 0.9)
            wav_path = request.output_path
            if wav_path.suffix.lower() != ".wav":
                wav_path = wav_path.with_suffix(".wav")
            wav_path, duration = stitch_wavs(parts, wav_path)

        output_path = wav_path
        if request.output_format == "mp3":
            from ..audio.converter import wav_to_mp3
            progress("导出 MP3…", 0.96)
            output_path = request.output_path.with_suffix(".mp3")
            wav_to_mp3(wav_path, output_path)
        progress("合成完成", 1.0)
        return SynthesisResult(
            output_path=output_path,
            wav_path=wav_path,
            duration_seconds=duration,
            engine=self.name,
            voice_id=request.voice.id,
            text=text,
            meta={
                "segments": len(segments),
                "device": self._device,
                "inference_timesteps": request.inference_timesteps,
                "retry_badcase": request.retry_badcase,
                "denoise": request.denoise,
                "reference_mode": "reference_wav_path",
            },
        )


class _Cancelled(Exception):
    pass


def was_cancelled(exc: BaseException) -> bool:
    return isinstance(exc, _Cancelled)
