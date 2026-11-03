"""Account-scoped pacing and local resource admission, separate from model locks."""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import shutil
import tempfile
import threading
import time

MiB = 1024 * 1024


@dataclass(frozen=True)
class CloudPolicy:
    rps: float = 0
    rpm: float = 0
    requests_per_day: float = 0
    max_inflight: int = 0
    max_bytes: int = 25 * MiB
    max_seconds: int = 0
    response_format: str = 'json'
    audio_seconds_per_hour: float = 0
    audio_seconds_per_day: float = 0


def policy_for(provider, model='', overrides=None):
    # These are documented channel defaults, not a claim about the user's account.
    # Unknown quotas ramp up and react to refusal. Old plans may explicitly opt down.
    known = {
        'dashscope': CloudPolicy(rps=20, max_bytes=2*1024*MiB),
        'siliconflow': CloudPolicy(max_bytes=50*MiB, max_seconds=3600),
        'volcengine': CloudPolicy(max_bytes=100*MiB, max_seconds=7200),
        'custom_openai': CloudPolicy(max_bytes=2*1024*MiB),
    }
    if provider not in known:
        raise ValueError('此服务尚未适配云端批次')
    if overrides is None:
        return known[provider]
    allowed = {'requests_per_second', 'requests_per_minute', 'requests_per_day', 'max_inflight','audio_seconds_per_hour','audio_seconds_per_day'}
    if type(overrides) is not dict or set(overrides)-allowed:
        raise ValueError('只接受请求频率、同时处理数和音频时长额度')
    values = {}
    for key, value in overrides.items():
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 1_000_000_000:
            raise ValueError('账号额度需要填写服务商确认的正数')
        if key == 'max_inflight' and type(value) is not int:
            raise ValueError('同时处理额度应为整数')
        values[{'requests_per_second':'rps', 'requests_per_minute':'rpm', 'max_inflight':'max_inflight'}.get(key,key)] = value
    from dataclasses import replace
    return replace(known[provider], **values)


def memory_available():
    import psutil
    value = psutil.virtual_memory()
    return value.available, value.total


class ResourceAdmission:
    """Reserve actual payload/SDK memory; never create a process per queued input."""
    def __init__(self):
        self.condition = threading.Condition()
        self.reserved = 0
        self.preparing = threading.BoundedSemaphore(2)

    def budget(self):
        available, total = memory_available()
        return max(0, min(total * 3 // 10, available // 2 + self.reserved))

    def capacity(self, count, requested=None):
        capacity = max(1, self.budget() // (96 * MiB))
        return max(1, min(count, capacity, requested if requested else count))

    @staticmethod
    def check_disk(folder=None):
        if shutil.disk_usage(folder or tempfile.gettempdir()).free < 256 * MiB:
            raise ValueError('临时磁盘空间不足，已停止准备新音频；原文件和已保存结果保留')

    @contextmanager
    def preparation(self, cancelled):
        while not self.preparing.acquire(timeout=.1):
            if cancelled():
                raise InterruptedError('已取消，尚未上传')
        try:
            self.check_disk()
            yield
        finally:
            self.preparing.release()

    @contextmanager
    def payload(self, size, provider, cancelled, progress):
        # SDK queues a complete WAV; JSON/multipart clients may make extra copies.
        need = 96*MiB + size * (4 if provider == 'volcengine' else 2)
        announced = False
        with self.condition:
            while True:
                available, _ = memory_available()
                if need > self.budget() and not self.reserved:
                    raise ValueError('此音频的上传内存不足，请关闭其他大程序或使用较短音频；尚未上传')
                if self.reserved + need <= self.budget() and available > need + 128*MiB:
                    self.reserved += need
                    break
                if cancelled():
                    raise InterruptedError('已取消，尚未上传')
                if not announced:
                    progress('等待本机上传资源，尚未发送…', -1)
                    announced = True
                self.condition.wait(.1)
        try:
            yield
        finally:
            with self.condition:
                self.reserved -= need
                self.condition.notify_all()


class RateGate:
    """All live batches for one provider/model share one frequency and in-flight pool."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.next_send = self.blocked_until = 0.
        self.active = 0
        self.ramp = 1.
        self.slowdown = 1.
        self.policies = {}
        self.audio = []
        self.account_refused = False

    def attach(self, policy):
        token = object()
        with self.lock:
            # A fresh explicit run may use repaired account configuration. Keep
            # concurrent existing runs stopped until all their clients close.
            if not self.policies:
                self.account_refused = False
            self.policies[token] = policy
        return token

    def detach(self, token):
        with self.lock:
            self.policies.pop(token, None)

    def delay(self):
        rates = [max(1/p.rps if p.rps else 0, 60/p.rpm if p.rpm else 0)
                 for p in self.policies.values()]
        return (max(rates, default=0) or 1/self.ramp) * self.slowdown

    def acquire(self, cancelled, progress):
        announced = False
        while True:
            if cancelled():
                raise InterruptedError('已取消，尚未发送')
            with self.lock:
                if self.account_refused:
                    from .cloud_http import CloudFailure
                    raise CloudFailure('account')
                caps = [p.max_inflight for p in self.policies.values() if p.max_inflight]
                wait = max(0., self.next_send-self.clock(), self.blocked_until-self.clock())
                if (not caps or self.active < min(caps)) and wait <= 0:
                    self.active += 1
                    self.next_send = self.clock() + self.delay()
                    return
            if not announced:
                progress('按服务额度等待提交，尚未发送…', -1)
                announced = True
            time.sleep(min(.1, max(.01, wait)))

    def release(self, *, success=False):
        with self.lock:
            self.active -= 1
            if success:
                self.ramp += 1
                self.slowdown = max(1.,self.slowdown*.95)

    def refused(self, delay):
        with self.lock:
            self.ramp = max(1/60, self.ramp/2)
            self.slowdown = min(1_000_000.,self.slowdown*2)
            self.blocked_until = max(self.blocked_until, self.clock()+delay)

    def refuse_account(self):
        with self.lock:
            self.account_refused = True

    @contextmanager
    def audio_budget(self, seconds, cancelled, progress):
        record = {'seconds':seconds,'sent':False,'rejected':False,'when':self.clock()}
        announced = False
        while True:
            if cancelled():
                raise InterruptedError('已取消，尚未上传')
            with self.lock:
                now = self.clock()
                self.audio = [r for r in self.audio if not r['sent'] or now-r['when'] < 86400]
                blocked = False
                for field,window in [('audio_seconds_per_hour',3600),('audio_seconds_per_day',86400),('requests_per_day',86400)]:
                    caps = [getattr(p,field) for p in self.policies.values() if getattr(p,field)]
                    if not caps:
                        continue
                    amount = 1 if field == 'requests_per_day' else seconds
                    if amount > min(caps):
                        raise ValueError('此音频超过账号单个额度窗口，请核对服务商音频时长额度；尚未上传')
                    used = sum(1 if field == 'requests_per_day' else r['seconds'] for r in self.audio if not r['sent'] or now-r['when'] < window)
                    blocked = blocked or used+amount > min(caps)
                if not blocked:
                    self.audio.append(record)
                    break
            if not announced:
                progress('等待账号音频时长额度恢复，尚未上传；可取消后稍后恢复…',-1)
                announced = True
            time.sleep(.1)
        try:
            yield record
        finally:
            with self.lock:
                if (not record['sent'] or record['rejected']) and record in self.audio:
                    self.audio.remove(record)

    def audio_sent(self, record):
        with self.lock:
            record.update(sent=True,when=self.clock())

    def seed_audio(self, attempts, wall_clock=time.time):
        """Rebuild this library's known billable usage, including uncertain calls."""
        with self.lock:
            now, wall = self.clock(), wall_clock()
            known = {r.get('attempt_id') for r in self.audio if r.get('attempt_id')}
            for attempt in attempts:
                seconds, sent = attempt.get('audio_seconds'), attempt.get('sent_at')
                aid = attempt.get('attempt_id')
                if (attempt.get('state') not in {'sending','received','unknown'} or not isinstance(aid,str)
                        or not aid or aid in known or type(seconds) not in {int,float}
                        or type(sent) not in {int,float} or not math.isfinite(seconds) or not math.isfinite(sent)
                        or seconds <= 0):
                    continue
                age = max(0,wall-sent)
                if age < 86400:
                    self.audio.append(dict(seconds=seconds,sent=True,rejected=False,when=now-age,attempt_id=aid))
                    known.add(aid)

    def commit_audio(self, record, attempt_id):
        with self.lock:
            # A second batch can seed the just-persisted attempt before this call.
            self.audio = [r for r in self.audio if r is record or r.get('attempt_id') != attempt_id]
            record.update(sent=True,when=self.clock(),attempt_id=attempt_id)


RESOURCES = ResourceAdmission()
_gates, _lock = {}, threading.RLock()


def shared_gate(provider, model, endpoint=''):
    # Conservative across keys: a second key does not prove a different account.
    key = (provider, model, endpoint if provider == 'custom_openai' else '')
    with _lock:
        return _gates.setdefault(key, RateGate())
