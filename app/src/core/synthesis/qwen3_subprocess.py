"""Persistent, cancellable official Qwen subprocess. One resident model, no RAM caps."""
import atexit
from collections import deque
from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from .base import SynthesisEngine,SynthesisError,SynthesisResult,_noop
from .errors import scrub_detail
from .qwen3_options import ENGINE_ID,validate_options,generation_inputs
from . import qwen3_runtime as runtime
from .voxcpm2 import _Cancelled
from .voxcpm2_subprocess import _terminate
from ..audio.output_level import level_pcm16_wav

class QwenProcess:
    def __init__(self):
        self.proc=None;self.identity=None;self.events=queue.Queue();self.errors=deque(maxlen=30)
        self.lock=threading.Lock();self.last_used=0
        self.watcher=None;self.deployment_lease=None

    def _stop(self):
        if self.proc is not None:
            _terminate(self.proc)
            for stream in (self.proc.stdin,self.proc.stdout,self.proc.stderr):
                if stream: stream.close()
        self.proc=None;self.identity=None
        if self.deployment_lease is not None:
            self.deployment_lease.__exit__(None,None,None);self.deployment_lease=None

    def release(self):
        if not self.lock.acquire(blocking=False): return False
        try: self._stop();return True
        finally:self.lock.release()

    def status(self):
        return {'alive':bool(self.proc and self.proc.poll() is None),'busy':self.lock.locked(),'pid':self.proc.pid if self.proc and self.proc.poll() is None else None}

    def _idle(self):
        while True:
            time.sleep(15)
            if self.proc and time.monotonic()-self.last_used>600: self.release()

    def run(self,payload,progress,cancel):
        if not self.lock.acquire(blocking=False): raise SynthesisError('Qwen 正在运行，请先等待或取消。')
        try:
            identity=(str(runtime.python_path()),payload['model_path'],payload['revision'])
            if self.proc and (self.proc.poll() is not None or self.identity!=identity): self._stop()
            if self.proc is None:
                from .qwen3_weights import deployment_lock
                lease=deployment_lock(runtime.root()/'models',wait=False)
                lease.__enter__();self.deployment_lease=lease
                # Readiness must be checked again after acquiring the lifetime lease.
                ready,message=runtime.readiness(payload['mode'])
                if not ready:
                    self._stop();raise SynthesisError(message)
                self.events=queue.Queue();self.errors=deque(maxlen=30)
                env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_IMPLICIT_TOKEN':'1','PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1'}
                work=runtime.root()/'work';work.mkdir(parents=True,exist_ok=True)
                env.update(HF_HOME=str(work/'hf'),MPLCONFIGDIR=str(work/'mpl'),NUMBA_CACHE_DIR=str(work/'numba'))
                self.proc=subprocess.Popen([str(runtime.python_path()),'-I','-s',str(Path(__file__).with_name('qwen3_worker.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1,env=env,start_new_session=True,cwd=work)
                self.identity=identity
                def stdout(stream,events):
                    try:
                        for line in stream: events.put(line)
                    except (OSError,ValueError):pass
                    finally:events.put(None)
                def stderr(stream,errors):
                    try:
                        for line in stream: errors.append(line[-2000:])
                    except (OSError,ValueError):pass
                threading.Thread(target=stdout,args=(self.proc.stdout,self.events),daemon=True).start()
                threading.Thread(target=stderr,args=(self.proc.stderr,self.errors),daemon=True).start()
                if self.watcher is None:
                    self.watcher=threading.Thread(target=self._idle,daemon=True);self.watcher.start()
            self.proc.stdin.write(json.dumps(payload,ensure_ascii=False)+'\n');self.proc.stdin.flush()
            from .indextts2_subprocess import _ProcessMemoryMonitor
            memory=_ProcessMemoryMonitor(self.proc,'mps')
            deadline=time.monotonic()+1800
            while True:
                memory.check()
                if cancel():self._stop();raise _Cancelled()
                if time.monotonic()>deadline:self._stop();raise SynthesisError('本次生成超过 30 分钟，已停止。原有音频保留。')
                try:line=self.events.get(timeout=.15)
                except queue.Empty:continue
                if line is None:
                    error=scrub_detail(''.join(self.errors)[-3000:]);self._stop();raise SynthesisError(error or 'Qwen 生成后台已退出，请重试。')
                try:event=json.loads(line)
                except (ValueError,TypeError):continue
                if event.get('type')=='progress':progress(str(event['message']),-1)
                elif event.get('type')=='result':return memory.annotate(event)
                elif event.get('type')=='error':
                    self._stop();raise SynthesisError(scrub_detail(str(event.get('detail'))) or 'Qwen 生成失败。')
        except (OSError,ValueError,RuntimeError) as exc:
            self._stop();raise SynthesisError('Qwen 本地后台无法运行：'+scrub_detail(str(exc))) from exc
        finally:self.last_used=time.monotonic();self.lock.release()

PROCESS=QwenProcess()
atexit.register(PROCESS.release)

def shutdown_persistent_worker(): return PROCESS.release()
def persistent_worker_status(): return PROCESS.status()

class Qwen3Engine(SynthesisEngine):
    name=ENGINE_ID
    display_name='Qwen3-TTS 本地声音工作台'
    requires_network=False

    def synthesize(self,request,progress=None,is_cancelled=None):
        progress=progress or _noop;cancel=is_cancelled or (lambda:False)
        options=validate_options(request.engine_options)
        inputs=generation_inputs(request.text,request.voice,options)
        ready,message=runtime.readiness(options['mode'])
        if not ready:raise SynthesisError(message+'，请先准备对应的本地模型。')
        if cancel():raise _Cancelled()
        # Never keep the Index worker resident alongside Qwen's large model.
        from .indextts2_subprocess import persistent_worker_status as index_status,shutdown_persistent_worker as release_index
        if index_status().get('busy'):raise SynthesisError('IndexTTS 正在运行，请等待完成。')
        release_index()
        model=runtime.model_path(options['mode'])
        revision=runtime.revision(options['mode'])
        with tempfile.TemporaryDirectory(prefix='lulu_qwen_') as tmp:
            folder=Path(tmp)
            payload={'mode':options['mode'],'model_path':str(model),'revision':revision,'generate':inputs,'seed':options['seed'],
                     'output_path':str(folder/'result.wav'),'raw_path':str(folder/'raw.wav'),'cache_dir':str(runtime.root()/'voice-features')}
            event=PROCESS.run(payload,progress,cancel)
            def check():
                if cancel():raise _Cancelled()
            check();progress('整理成品音量…',-1)
            level=level_pcm16_wav(Path(payload['output_path']),check_cancelled=check)
            check()
            wav=request.output_path.with_suffix('.wav');wav.parent.mkdir(parents=True,exist_ok=True)
            if wav.exists():raise SynthesisError('输出文件已存在，请使用新文件名。')
            raw=wav.with_name(wav.stem+'.raw.wav')
            if raw.exists():raise SynthesisError('原始输出文件已存在，请使用新文件名。')
            # Exclusive creation preserves any existing result, even if another writer races us.
            made=[]
            try:
                for source,dest in [(Path(payload['raw_path']),raw),(Path(payload['output_path']),wav)]:
                    with dest.open('xb') as out,source.open('rb') as inp: made.append(dest);shutil.copyfileobj(inp,out)
                check()
                output=wav
                if request.output_format=='mp3':
                    from ..audio.converter import wav_to_mp3
                    output=request.output_path.with_suffix('.mp3')
                    if output.exists():raise SynthesisError('导出文件已存在。')
                    wav_to_mp3(wav,output);made.append(output)
                check()
            except BaseException:
                for dest in made:dest.unlink(missing_ok=True)
                raise
        return SynthesisResult(output,wav,event['duration'],self.name,request.voice.id if request.voice and options['mode']=='clone' else '',request.text,
            meta={'mode':options['mode'],'options':deepcopy(options),'model_revision':revision,'device':'mps','dtype':'bfloat16','sample_rate':event['sample_rate'],
                  'raw_path':str(raw),'seconds':event['seconds'],'cache_hit':event['cache_hit'],'output_level':level,'memory':event.get('meta',{}),
                  'reference_text':inputs.get('ref_text',''),'reference_audio_path':inputs.get('ref_audio',''),'runtime':'official-qwen-tts-0.1.1'})
