"""Doubao Voice recording/flash, not an Ark chat-compatible endpoint."""
from pathlib import Path
from uuid import uuid4
import wave

from ..models import Transcript, Segment
from .base import TranscriptionEngine, _noop
from .cloud_http import CloudFailure, failure, post_audio, volcano_body, safe_id

RESOURCE = 'volc.bigasr.auc_turbo'
ENDPOINT = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'


class VolcanoEngine(TranscriptionEngine):
    name = 'volcengine'
    display_name = '火山引擎 · 豆包录音识别极速版'
    requires_network = True

    def __init__(self, *, api_key):
        self._api_key = api_key or ''

    def transcribe(self, wav_path:Path, progress=None, is_cancelled=None):
        cancelled = is_cancelled or (lambda:False)
        if cancelled():
            raise InterruptedError('已取消，尚未上传')
        if not self._api_key:
            raise ValueError('请在安全设置配置豆包语音的 API Key，并开通录音文件识别极速版')
        with wave.open(str(wav_path), 'rb') as audio:
            seconds = audio.getnframes()/audio.getframerate()
        if Path(wav_path).stat().st_size > 100*1024**2 or seconds > 7200:
            raise ValueError('火山极速版的上传音频需在100MB、2小时以内；尚未上传')
        request_id = uuid4().hex
        data, headers = post_audio(ENDPOINT,
            {'X-Api-Key':self._api_key, 'X-Api-Resource-Id':RESOURCE,
             'X-Api-Request-Id':request_id, 'X-Api-Sequence':'-1', 'Content-Type':'application/json'},
            volcano_body(wav_path), timeout=max(120, min(1800, seconds*2)))
        code = headers.get('X-Api-Status-Code', '')
        if str(code) != '20000000':
            raise failure(200, data, headers)
        result = data.get('result') or {}
        text = result.get('text') if type(result) is dict else ''
        if type(text) is not str or not text.strip():
            raise CloudFailure('unknown', code=code, request_id=request_id)
        segments = []
        for row in result.get('utterances') or []:
            if type(row) is not dict or type(row.get('text')) is not str:
                continue
            start, end = row.get('start_time'), row.get('end_time')
            if type(start) in {int,float} and type(end) in {int,float} and 0 <= start <= end <= (seconds+5)*1000:
                segments.append(Segment(row['text'], start/1000, end/1000))
        return Transcript(text.strip(), segments, 'zh', self.name,
            meta={'cloud_request':{'provider':self.name, 'model':RESOURCE,
                'request_id':safe_id(headers.get('X-Tt-Logid') or request_id), 'attempts':1}})
