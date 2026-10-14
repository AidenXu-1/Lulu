"""Isolated official Qwen runtime. JSONL input/output, offline and one model per process."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import sys
import time

OUT=sys.stdout

def emit(kind,**kwargs):
    print(json.dumps({'type':kind,**kwargs},ensure_ascii=False),file=OUT,flush=True)

def deny_network(*args,**kwargs):
    raise RuntimeError('Qwen 本地生成禁止网络访问。')



def prompt_key(payload):
    inputs=payload['generate']
    digest=hashlib.sha256(Path(inputs['ref_audio']).read_bytes()).hexdigest()
    identity={'audio':digest,'text':inputs.get('ref_text',''),'x_vector_only':inputs['x_vector_only_mode'],
              'model_revision':payload['revision'],'package':'qwen-tts-0.1.1','preprocess':2}
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()


def clone_prompt(model,payload,torch,np):
    """Store arrays only; never unpickle user-supplied voice files."""
    from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem
    inputs=payload['generate'];key=prompt_key(payload)
    folder=Path(payload['cache_dir'])
    try:folder.mkdir(parents=True,exist_ok=True)
    except OSError:
        return model.create_voice_clone_prompt(ref_audio=inputs['ref_audio'],ref_text=inputs.get('ref_text'),x_vector_only_mode=inputs['x_vector_only_mode']),False
    cache=folder/(key+'.npz')
    if cache.is_file() and cache.stat().st_size < 64*1024*1024:
        try:
            with np.load(cache,allow_pickle=False) as data:
                code=data['code'];embedding=data['embedding']
                if embedding.ndim!=1 or not np.isfinite(embedding).all() or code.ndim not in (1,2): raise ValueError('cache shape')
                item=VoiceClonePromptItem(ref_code=None if inputs['x_vector_only_mode'] else torch.from_numpy(code.copy()).to(model.device),
                    ref_spk_embedding=torch.from_numpy(embedding.copy()).to(device=model.device,dtype=getattr(torch,str(data['embedding_dtype'].item()))),
                    x_vector_only_mode=inputs['x_vector_only_mode'],icl_mode=not inputs['x_vector_only_mode'],ref_text=inputs.get('ref_text'))
            return [item],True
        except (OSError,ValueError,KeyError,AttributeError):
            pass  # Rebuild corrupt/incompatible features from the preserved original recording.
    prompt=model.create_voice_clone_prompt(ref_audio=inputs['ref_audio'],ref_text=inputs.get('ref_text'),x_vector_only_mode=inputs['x_vector_only_mode'])
    item=prompt[0]
    code=item.ref_code.detach().cpu().numpy() if item.ref_code is not None else np.empty((0,),dtype=np.int64)
    embedding=item.ref_spk_embedding.detach().float().cpu().numpy()
    tmp=cache.with_suffix('.tmp')
    try:
        with tmp.open('wb') as f: np.savez(f,code=code,embedding=embedding,embedding_dtype=str(item.ref_spk_embedding.dtype).split(".")[-1])
        tmp.replace(cache)
    except OSError:
        tmp.unlink(missing_ok=True)  # Cache is optional; generation remains usable.
    return prompt,False


def main():
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',HF_HUB_DISABLE_IMPLICIT_TOKEN='1',TOKENIZERS_PARALLELISM='false')
    socket.socket.connect=deny_network
    socket.create_connection=deny_network
    
    model=None;identity=None
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        import numpy as np
        import soundfile as sf
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        from importlib.metadata import version
        for package,expected in [('qwen-tts','0.1.1'),('torch','2.10.0'),('transformers','4.57.3')]:
            if version(package)!=expected: raise RuntimeError(package+' 版本与当前已验证运行环境不一致。')
        from qwen_tts import Qwen3TTSModel
    for line in sys.stdin:
        started=time.monotonic()
        try:
            payload=json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                if not torch.backends.mps.is_available(): raise RuntimeError('当前已验证的 Qwen 路径需要 Apple Silicon MPS；未自动切换低精度或其他后端。')
                current=(payload['model_path'],payload['revision'])
                if identity is not None and current!=identity: raise RuntimeError('切换模型需要先释放上一模型。')
                if model is None:
                    emit('progress',message='加载 Qwen3 本地模型…',frac=-1)
                    model=Qwen3TTSModel.from_pretrained(payload['model_path'],device_map='mps',dtype=torch.bfloat16,attn_implementation='sdpa',local_files_only=True)
                    identity=current
                kwargs=dict(payload['generate']);cache_hit=False
                if payload['mode']=='clone':
                    emit('progress',message='准备参考音色…',frac=-1)
                    prompt,cache_hit=clone_prompt(model,payload,torch,np)
                    for key in ('ref_audio','ref_text','x_vector_only_mode'): kwargs.pop(key,None)
                    kwargs['voice_clone_prompt']=prompt
                seed=payload['seed'];random.seed(seed);np.random.seed(seed);torch.manual_seed(seed);torch.mps.manual_seed(seed)
                emit('progress',message='正在生成声音…',frac=-1)
                api={'clone':model.generate_voice_clone,'official':model.generate_custom_voice,'design':model.generate_voice_design}[payload['mode']]
                wavs,sr=api(**kwargs)
                torch.mps.synchronize()
                wave=np.asarray(wavs[0],dtype=np.float32).reshape(-1)
                if not len(wave) or not np.isfinite(wave).all() or not np.any(wave): raise RuntimeError('模型未生成有效音频，请重试。')
                # Each codec frame is 1/12 s. Reaching the model token ceiling is not a successful complete file.
                limit=int(model._merge_generate_kwargs().get('max_new_tokens',8192))
                if len(wave)/sr >= (limit-2)/12: raise RuntimeError('达到单次生成长度上限，未将可能截断的音频标为完成。请缩短文案后重试。')
                sf.write(payload['raw_path'],wave,sr,subtype='FLOAT')
                sf.write(payload['output_path'],wave,sr,subtype='PCM_16')
            emit('result',duration=len(wave)/sr,sample_rate=int(sr),seconds=time.monotonic()-started,cache_hit=cache_hit,dtype='bfloat16',device='mps')
        except Exception as exc:
            emit('error',detail=str(exc))

if __name__=='__main__': main()
