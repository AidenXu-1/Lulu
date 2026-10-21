"""阿里云百炼 · Paraformer 实时识别引擎(dashscope SDK,本地 wav 直读)。

阿里云的「录音文件识别」要公网 URL(不收本地文件),故走**实时识别** Recognition.call(本地wav)——
官方 SDK 支持直接传本地文件、同步返回,无需把音频传到公网。
"""

from __future__ import annotations

from pathlib import Path

from ..models import Segment, Transcript
from .base import CancelCallback, ProgressCallback, TranscriptionEngine, _noop


class DashScopeEngine(TranscriptionEngine):
    requires_network = True

    def __init__(self, *, engine_id: str, display_name: str,
                 model: str, api_key: str | None):
        self.name = engine_id
        self.display_name = display_name
        self._model = model or "paraformer-realtime-v2"
        self._api_key = (api_key or "").strip()

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
        if not self._api_key:
            raise RuntimeError(
                f"{self.display_name} 还没配置 API Key。请在「AI 大模型配置」→「内容提取」→ 云端 API 里填入,或切回本地引擎。"
            )
        progress(f"上传音频到 {self.display_name}…", -1.0)
        import dashscope
        from dashscope.audio.asr import Recognition

        dashscope.api_key = self._api_key
        res = None
        last_exc: Exception | None = None
        for attempt in range(3):  # dashscope websocket 偶发连接竞态(fd -1),重试兜住
            try:
                rec = Recognition(
                    model=self._model,
                    format="wav",
                    sample_rate=16000,
                    language_hints=["zh", "en"],
                    callback=None,
                )
                res = rec.call(str(wav_path))
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if res is None:
            raise RuntimeError(f"连接 {self.display_name} 失败:{last_exc}") from last_exc
        if is_cancelled():
            raise InterruptedError("转录已取消")

        if res.status_code != 200:
            msg = getattr(res, "message", "") or ""
            if res.status_code in (401, 403):
                raise RuntimeError(f"{self.display_name}:API Key 无效或无权限({res.status_code})。{msg}")
            raise RuntimeError(f"{self.display_name} 返回错误 {res.status_code}:{msg}")

        progress("解析云端结果…", -1.0)
        sentences = res.get_sentence() or []
        segments: list[Segment] = []
        for s in sentences:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            bt, et = s.get("begin_time"), s.get("end_time")
            segments.append(
                Segment(
                    text=text,
                    start=bt / 1000.0 if isinstance(bt, (int, float)) else None,
                    end=et / 1000.0 if isinstance(et, (int, float)) else None,
                )
            )
        full = "".join(s.text for s in segments).strip()
        return Transcript(text=full, segments=segments, language="zh", engine=self.name)
