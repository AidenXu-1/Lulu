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
                 model: str, api_key: str | None, attempt_limit: int = 3):
        self.name = engine_id
        self.display_name = display_name
        self._model = model or "paraformer-realtime-v2"
        self._api_key = (api_key or "").strip()
        if type(attempt_limit) is not int or not 1 <= attempt_limit <= 3:
            raise ValueError('识别尝试次数无效')
        self._attempt_limit = attempt_limit

    def transcribe_bounded(self, wav_path, progress=None, is_cancelled=None):
        """One billable attempt, in an owned process that can be stopped.

        The key travels over an anonymous pipe, never argv, logs or a file.
        A timeout/cancel never triggers another billable request.
        """
        import multiprocessing
        import time
        import wave
        progress = progress or _noop
        cancelled = is_cancelled or (lambda: False)
        with wave.open(str(wav_path)) as audio:
            seconds = audio.getnframes() / audio.getframerate()
        if not 0 < seconds <= 600:
            raise ValueError('百炼双任务每条限 10 分钟以内；长音频请使用原有逐条转录')
        if cancelled():
            raise InterruptedError('已取消，未上传音频')
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_isolated_recognition, args=(child,), daemon=True)
        try:
            process.start()
            child.close()
            parent.send(dict(engine_id=self.name, display_name=self.display_name, model=self._model,
                             api_key=self._api_key, wav_path=str(wav_path)))
            progress('上传到百炼，最多两条同时处理；取消后不会自动重发…', -1)
            deadline = time.monotonic() + max(120, seconds * 2 + 60)
            while not parent.poll(.1):
                if cancelled():
                    raise InterruptedError('已请求取消；服务端可能已计费，未自动重发')
                if time.monotonic() >= deadline or not process.is_alive():
                    raise RuntimeError('百炼未确认结果；可能已计费，请核对后显式重试')
            reply = parent.recv()
            if not reply.get('ok'):
                raise RuntimeError('百炼未完成识别；请核对账户或网络后显式重试，未自动重发')
            data = reply['transcript']
            return Transcript(**{**data, 'segments': [Segment(**row) for row in data['segments']]})
        finally:
            child.close()
            parent.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(3)
                if process.is_alive():
                    process.kill()
                    process.join(3)


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
        from dashscope.audio.asr import Recognition

        res = None
        for attempt in range(self._attempt_limit):  # 保持默认重试；受限实验可明确关闭自动重发。
            try:
                rec = Recognition(
                    model=self._model,
                    format="wav",
                    sample_rate=16000,
                    language_hints=["zh", "en"],
                    callback=None,
                    api_key=self._api_key,
                )
                res = rec.call(str(wav_path))
                break
            except Exception:  # noqa: BLE001
                pass
        if res is None:
            from .cloud_http import CloudFailure
            raise CloudFailure('unknown', code='SDK_EXCEPTION') from None
        if res.status_code != 200:
            from .cloud_http import failure
            raise failure(res.status_code, {'error':{'code':getattr(res,'code',''),
                'message':getattr(res,'message','')}})

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
        request_id = res.get_request_id() if callable(getattr(res, 'get_request_id', None)) else ''
        if not full:
            from .cloud_http import CloudFailure
            raise CloudFailure('unknown', status=200, code='EMPTY_TRANSCRIPT', request_id=request_id)
        return Transcript(text=full, segments=segments, language="zh", engine=self.name,
            meta={'cloud_request': {'provider': self.name, 'model': self._model,
                'request_id': str(request_id), 'attempts': attempt + 1}})

def _isolated_recognition(connection):
    import logging
    from dataclasses import asdict
    logging.disable(logging.CRITICAL)
    try:
        args = connection.recv()
        wav_path = args.pop('wav_path')
        engine = DashScopeEngine(**args, attempt_limit=1)
        result = engine.transcribe(Path(wav_path))
        connection.send(dict(ok=True, transcript=asdict(result)))
    except Exception:
        connection.send(dict(ok=False))
    finally:
        connection.close()
