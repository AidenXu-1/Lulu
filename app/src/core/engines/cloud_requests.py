"""Owned cloud requests, durable observers and refusal-only retries."""
from dataclasses import asdict
import logging
import multiprocessing
from pathlib import Path
import time
from uuid import uuid4
import wave

from ..models import Transcript, Segment
from .base import _noop
from .cloud_http import CloudFailure
from .cloud_limits import RESOURCES, policy_for, shared_gate


def cloud_ids():
    # Retired IDs remain cloud IDs only for reading old billing/result records.
    from .providers import PRESETS
    return {p.id for p in PRESETS.values() if p.requires_network}


def build_cloud_engine(provider, config):
    """Only the caller reads secure storage; children receive an in-memory snapshot."""
    from .providers import PRESETS
    preset = PRESETS[provider]
    if preset.retired:
        raise ValueError('此云端转录服务已移除，历史结果保留。')
    if not preset.requires_network:
        raise ValueError('请选择云端转录服务')
    key = config.get('api_key')
    if type(key) is not str or not key.strip():
        raise ValueError('请先在安全设置配置此云端转录服务')
    if preset.kind == 'dashscope':
        from .dashscope_engine import DashScopeEngine
        return DashScopeEngine(engine_id=provider, display_name=preset.display_name,
            model=preset.default_model, api_key=key, attempt_limit=1)
    if preset.kind == 'volcano':
        from .volcano_engine import VolcanoEngine
        return VolcanoEngine(api_key=key)
    from .online_api import OpenAICompatEngine
    base = preset.base_url or config.get('base_url','')
    model = preset.default_model or config.get('model','')
    from urllib.parse import urlsplit
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or type(model) is not str or not model.strip() or len(model)>200):
        raise ValueError('自定义转录连接需要有效的服务地址和模型名，请在安全设置核对')
    return OpenAICompatEngine(engine_id=provider, display_name=preset.display_name,
        base_url=base, model=model, api_key=key)


def request_process(connection):
    logging.disable(logging.CRITICAL)
    try:
        args = connection.recv()
        engine = build_cloud_engine(args['provider'], args['config'])
        connection.send({'event':'ready'})
        if connection.recv() != 'send':
            return
        try:
            result = engine.transcribe(Path(args['path']))
            connection.send({'ok':True, 'transcript':asdict(result)})
        except CloudFailure as exc:
            connection.send({'ok':False, 'error':exc.public()})
        except BaseException:
            connection.send({'ok':False, 'error':CloudFailure('unknown').public()})
    except BaseException:
        pass
    finally:
        connection.close()


class CloudBatchEngine:
    requires_network = True

    def __init__(self, provider, config, limits=None, *, worker=None):
        from .providers import PRESETS
        self.name = provider
        self.display_name = PRESETS[provider].display_name
        self.config = dict(config)
        build_cloud_engine(provider,self.config)  # Validate only; no network or secret read.
        self.model = PRESETS[provider].default_model or config.get('model', '')
        self.policy = policy_for(provider, self.model, limits)
        self.gate = shared_gate(provider, self.model, config.get('base_url', ''))
        self.token = self.gate.attach(self.policy)
        self.worker = worker or request_process

    def close(self):
        self.gate.detach(self.token)
        self.config.clear()

    @staticmethod
    def preparation(cancelled):
        return RESOURCES.preparation(cancelled)

    @staticmethod
    def check_disk(folder=None):
        RESOURCES.check_disk(folder)

    def transcribe_bounded(self, wav_path, progress=None, is_cancelled=None,
                           cloud_observer=None, cloud_force_cancel=None):
        progress = progress or _noop
        cancelled = is_cancelled or (lambda:False)
        forced = cloud_force_cancel or (lambda:False)
        observer = cloud_observer or (lambda *_:None)
        path = Path(wav_path)
        size = path.stat().st_size
        with wave.open(str(path), 'rb') as audio:
            seconds = audio.getnframes()/audio.getframerate()
        if not size or size > self.policy.max_bytes or (self.policy.max_seconds and seconds > self.policy.max_seconds):
            raise ValueError(f'准备后的音频超过此服务的文件或时长限制（最多 {self.policy.max_bytes//1024**2} MB）；尚未上传')
        started = time.monotonic()
        for number in range(4):
            if cancelled() or forced():
                raise InterruptedError('已取消，尚未上传')
            observer('waiting', {})
            try:
                with self.gate.audio_budget(seconds,cancelled,progress) as audio_record:
                    return self._once(path, size, seconds, progress, cancelled, forced, observer,audio_record)
            except CloudFailure as exc:
                if exc.kind != 'rate_limited':
                    raise
                delay = max(exc.retry_after, min(30, 2**(number+1)))
                self.gate.refused(delay)
                observer('rejected', {**exc.public(), 'not_before':time.time()+delay})
                if number == 3 or delay > 300 or time.monotonic()-started+delay > 300:
                    raise
                progress('服务暂未受理，按额度稍后继续；已接收任务不会重发…', -1)
        raise CloudFailure('rate_limited')

    def _once(self, path, size, seconds, progress, cancelled, forced, observer,audio_record):
        with RESOURCES.payload(size, self.name, cancelled, progress):
            context = multiprocessing.get_context('spawn')
            parent, child = context.Pipe()
            process = context.Process(target=self.worker, args=(child,), daemon=True)
            acquired = sent = success = False
            try:
                process.start()
                child.close()
                parent.send(dict(provider=self.name, config=self.config, path=str(path)))
                ready_deadline = time.monotonic()+45
                while not parent.poll(.1):
                    if cancelled() or forced():
                        raise InterruptedError('已取消，尚未上传')
                    if time.monotonic() > ready_deadline or not process.is_alive():
                        raise ValueError('云端工作进程未能准备，尚未发送音频')
                if parent.recv() != {'event':'ready'}:
                    raise ValueError('云端工作进程回复无效，尚未发送')
                self.gate.acquire(cancelled, progress)
                acquired = True
                if cancelled() or forced():
                    raise InterruptedError('已取消，尚未上传')
                # Persist intent before the child can start a billable call.
                attempt_id = uuid4().hex
                observer('sending', {'attempt_id':attempt_id, 'provider':self.name, 'model':self.model,
                                     'audio_seconds':seconds,'sent_at':time.time()})
                sent = True
                self.gate.commit_audio(audio_record,attempt_id)
                parent.send('send')
                progress('已开始上传并识别，等待云端结果…', -1)
                deadline = time.monotonic()+max(180, seconds*2+60)
                settling = False
                while not parent.poll(.1):
                    if forced() or time.monotonic() > deadline or not process.is_alive():
                        # A result may arrive between the timed poll and the
                        # process-state check. Keep it before declaring unknown.
                        if parent.poll():
                            break
                        raise CloudFailure('unknown')
                    if cancelled() and not settling:
                        observer('settling', {})
                        progress('已停止后续发送，正在保全已发送任务的结果…', -1)
                        settling = True
                reply = parent.recv()
                if type(reply) is not dict:
                    raise CloudFailure('unknown')
                if reply.get('ok') is not True:
                    error = reply.get('error') or {}
                    raise CloudFailure(error.get('kind'), status=error.get('http_status',0),
                        code=error.get('service_code',''), request_id=error.get('request_id',''),
                        retry_after=error.get('retry_after',0))
                raw = reply['transcript']
                result = Transcript(**{**raw, 'segments':[Segment(**s) for s in raw['segments']]})
                if result.engine != self.name or not result.text.strip():
                    raise CloudFailure('unknown')
                success = True
                observer('received', (result.meta or {}).get('cloud_request', {}))
                return result
            except CloudFailure as exc:
                if exc.kind != 'unknown':
                    audio_record['rejected'] = True
                if exc.kind == 'account':
                    # Close the shared send gate before releasing this slot, so
                    # another batch cannot send in the handoff to its caller.
                    self.gate.refuse_account()
                    observer('rejected', exc.public())
                raise
            except (EOFError, BrokenPipeError, OSError):
                if sent:
                    raise CloudFailure('unknown') from None
                raise ValueError('云端工作进程未能启动，尚未上传') from None
            finally:
                child.close()
                parent.close()
                if process.pid is not None:
                    process.join(.2)
                    if process.is_alive():
                        process.terminate()
                        process.join(3)
                    if process.is_alive():
                        process.kill()
                        process.join(3)
                # An active slot is held until its owned process actually exits.
                if acquired:
                    self.gate.release(success=success)

    transcribe = transcribe_bounded
