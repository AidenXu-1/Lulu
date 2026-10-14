"""Validated inputs for the three distinct official Qwen3 TTS model APIs."""
from pathlib import Path
import math

ENGINE_ID = 'local_qwen3_tts'
MODES = {'clone': '克隆声音', 'official': '官方音色', 'design': '设计声音'}
LANGUAGES = {'Chinese':'中文','Auto':'自动识别','English':'英语','Japanese':'日语','Korean':'韩语','German':'德语','French':'法语','Russian':'俄语','Portuguese':'葡萄牙语','Spanish':'西班牙语','Italian':'意大利语'}
SPEAKERS = {'Vivian':'明亮利落 · 中文女声','Serena':'温暖温柔 · 中文女声','Uncle_Fu':'低沉醇厚 · 中文男声','Dylan':'年轻自然 · 北京男声','Eric':'活泼微哑 · 成都男声','Ryan':'有力明快 · 英文男声','Aiden':'阳光清朗 · 英文男声','Ono_Anna':'轻巧活泼 · 日语女声','Sohee':'温暖丰富 · 韩语女声'}
DEFAULTS = {'mode':'clone','language':'Chinese','speaker':'Serena','instruction':'','x_vector_only_mode':False,'temperature':0.9,'repetition_penalty':1.05,'seed':1234}
MODEL_NAMES = {'clone':'Base','official':'CustomVoice','design':'VoiceDesign'}

def validate_options(raw=None):
    values = {**DEFAULTS, **(raw or {})}
    if values['mode'] not in MODES: raise ValueError('请选择有效的配音方式。')
    if values['language'] not in LANGUAGES: raise ValueError('请选择支持的配音语言。')
    if values['speaker'] not in SPEAKERS: raise ValueError('请选择官方音色。')
    if type(values['x_vector_only_mode']) is not bool: raise ValueError('克隆选项无效。')
    for key, low, high in [('temperature', .1, 2), ('repetition_penalty', 1, 2)]:
        v=values[key]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError('生成设置超出可用范围。')
    if type(values['seed']) is not int or not 0 <= values['seed'] < 2**32: raise ValueError('随机种子无效。')
    values['instruction']=str(values['instruction']).strip()
    if values['mode']=='clone': values['instruction']=''
    if values['mode']=='design' and not values['instruction']: raise ValueError('请先描述想要的声音。')
    return {key: values[key] for key in DEFAULTS}

def generation_inputs(text, voice, raw=None):
    opts=validate_options(raw)
    if not isinstance(text,str) or not text.strip(): raise ValueError('请输入配音文案。')
    kwargs={'text':text,'language':opts['language'],'non_streaming_mode':opts['mode']!='clone',
            'temperature':opts['temperature'],'repetition_penalty':opts['repetition_penalty']}
    if opts['mode']=='clone':
        if not voice or not Path(voice.reference_audio_path).is_file(): raise ValueError('请先选择有效的参考音色。')
        if not opts['x_vector_only_mode'] and not voice.reference_text.strip(): raise ValueError('完整克隆需要校对参考文字。')
        kwargs.update(ref_audio=voice.reference_audio_path,ref_text=voice.reference_text,
                      x_vector_only_mode=opts['x_vector_only_mode'])
    elif opts['mode']=='official': kwargs.update(speaker=opts['speaker'],instruct=opts['instruction'])
    else: kwargs['instruct']=opts['instruction']
    return kwargs
