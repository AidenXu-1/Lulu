"""Supported VoxCPM 2.0.3 inputs; one unchanged text per invocation."""
import math
from pathlib import Path
import re
from .voxcpm2_pronunciation import validate_terms, apply_pronunciations

MODES = {'hifi': '完整克隆', 'directed': '调整表达', 'design': '设计声音'}
DEFAULTS = {'mode': 'hifi', 'inference_timesteps': 10, 'cfg_value': 2.0,
            'normalize': False, 'seed': 1234, 'instruction': '', 'pronunciations': []}


def validate_options(raw=None):
    raw = dict(raw or {})
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError('VoxCPM2 设置包含不支持的选项。')
    result = {**DEFAULTS, **raw}
    result['pronunciations'] = validate_terms(result['pronunciations'])
    if result['mode'] not in MODES:
        raise ValueError('请选择有效的 VoxCPM2 配音方式。')
    steps = result['inference_timesteps']
    if type(steps) is not int or not 4 <= steps <= 30:
        raise ValueError('生成步数须为 4–30 的整数。')
    cfg = result['cfg_value']
    if type(cfg) not in (int, float) or not math.isfinite(cfg) or not 1 <= cfg <= 3:
        raise ValueError('CFG 须在 1–3 之间。')
    if type(result['normalize']) is not bool:
        raise ValueError('数字整理设置须为开或关。')
    if type(result['seed']) is not int or not 0 <= result['seed'] < 2**32:
        raise ValueError('随机种子超出有效范围。')
    if not isinstance(result['instruction'], str):
        raise ValueError('声音描述须为文字。')
    result['instruction'] = result['instruction'].strip()
    if result['mode'] == 'hifi':
        result['instruction'] = ''
    if result['mode'] == 'design' and not result['instruction']:
        raise ValueError('请先描述想要的声音。')
    return result


def generation_inputs(text, voice, options):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('请输入要合成的文本。')
    opts = validate_options(options)
    mode = opts['mode']
    spoken = apply_pronunciations(text, opts['pronunciations'])
    has_phonemes = bool(re.search(r'\{[^{}]+\}', spoken))
    kwargs = {'text': spoken, 'inference_timesteps': opts['inference_timesteps'],
              'cfg_value': opts['cfg_value'], 'normalize': opts['normalize'] and not has_phonemes,
              'denoise': False, 'retry_badcase': False}
    if mode != 'design':
        if voice is None or not Path(voice.reference_audio_path).is_file():
            raise ValueError('请选择有效的参考音色。')
        kwargs['reference_wav_path'] = str(voice.reference_audio_path)
    if mode == 'hifi':
        if not voice.reference_text.strip():
            raise ValueError('完整克隆需要准确的参考文字，请先查看与校对。')
        kwargs.update(prompt_wav_path=str(voice.reference_audio_path), prompt_text=voice.reference_text)
    elif opts['instruction']:
        kwargs['text'] = f"({opts['instruction']}){spoken}"
    return kwargs
