"""Local Qwen installation paths; reading state never downloads or imports models."""
import os
import json
from pathlib import Path
from . import runtime_packages
from .qwen3_options import MODEL_NAMES

REVISIONS={
    'clone':'fd4b254389122332181a7c3db7f27e918eec64e3',
    'official':'0c0e3051f131929182e2c023b9537f8b1c68adfe',
    'design':'5ecdb67327fd37bb2e042aab12ff7391903235d3',
}

def root():
    override=os.environ.get('LULU_QWEN3_HOME','').strip()
    return Path(override).expanduser() if override else runtime_packages.tts_root()/'local_qwen3_tts'

def python_path():
    override=os.environ.get('LULU_QWEN3_PYTHON','').strip()
    if override: return Path(override).expanduser()
    return runtime_packages._runtime_python_in(root()/'runtime')

def model_path(mode):
    return root()/'models'/MODEL_NAMES[mode]

def readiness(mode):
    if not os.environ.get('LULU_QWEN3_HOME') and not os.environ.get('LULU_QWEN3_PYTHON'):
        if not runtime_packages.is_runtime_installed('local_qwen3_tts'):return False,'Qwen3 运行环境尚未准备，请到 AI 大模型安装'
        from . import qwen3_weights
        if not qwen3_weights.is_installed(root()/'models', MODEL_NAMES[mode]):return False,'请到 AI 大模型安装或修复千问的「'+qwen3_weights.FEATURES[MODEL_NAMES[mode]]+'」'
    if not python_path().is_file(): return False,'Qwen3 运行环境尚未准备'
    model=model_path(mode)
    required=('config.json','generation_config.json','model.safetensors','tokenizer_config.json','vocab.json','merges.txt','speech_tokenizer/config.json','speech_tokenizer/model.safetensors')
    if any(not (model/name).is_file() for name in required): return False,MODEL_NAMES[mode]+' 模型尚未准备'
    try:
        revision(mode)
        config=json.loads((model/'config.json').read_text())
        kind={'clone':'base','official':'custom_voice','design':'voice_design'}[mode]
        if config.get('tts_model_size')!='1b7' or config.get('tts_model_type')!=kind:
            return False,MODEL_NAMES[mode]+' 模型类型或大小不匹配'
    except (OSError,ValueError,KeyError,TypeError,AttributeError): return False,MODEL_NAMES[mode]+' 模型版本尚未确认'
    # Full file digest verification is performed during deployment, not on every UI refresh.
    return True,MODEL_NAMES[mode]+' · 本地就绪'


def revision(mode):
    manifest=model_path(mode)/'.lulu-qwen-model.json'
    data=json.loads(manifest.read_text())
    value=data['revision']
    if data.get('model') != MODEL_NAMES[mode] or not isinstance(value,str) or len(value)!=40 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Qwen 模型版本记录无效，请重新准备模型。')
    if value!=REVISIONS[mode]:raise ValueError(MODEL_NAMES[mode]+' 模型与当前已核验版本不一致。')
    return value
