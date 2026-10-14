"""CLI model choices share the GUI engines and validated generation settings."""
import math

MODELS = {
    'local_indextts2': ('IndexTTS 2.5', 'local_indextts2', '2.5'),
    'local_indextts2_0': ('IndexTTS 2', 'local_indextts2', '2.0'),
    'local_voxcpm2': ('VoxCPM2', 'local_voxcpm2', None),
    'local_qwen3_tts': ('Qwen3-TTS', 'local_qwen3_tts', None),
}
INDEX_FIELDS = {'emotion_mode', 'emotion_preset', 'emotion_intensity', 'emotion_vector',
    'emotion_weight', 'emotion_text', 'emotion_audio_path', 'random_emotion',
    'language', 'speed', 'generation_options', 'do_sample'}


def validate(engine, raw=None):
    if engine not in MODELS or (raw is not None and not isinstance(raw, dict)):
        raise ValueError('不支持的模型或参数格式。')
    raw = dict(raw or {})
    if engine == 'local_voxcpm2':
        from .voxcpm2_options import validate_options
        return validate_options(raw)
    if engine == 'local_qwen3_tts':
        from .qwen3_options import validate_options, DEFAULTS
        if set(raw) - set(DEFAULTS) or ('instruction' in raw and not isinstance(raw['instruction'], str)):
            raise ValueError('不支持的千问设置。')
        return validate_options(raw)
    if set(raw) - (INDEX_FIELDS - ({'do_sample', 'language', 'speed'} if engine == 'local_indextts2_0' else set())):
        raise ValueError('不支持的 IndexTTS 设置。')
    from .indextts2_presets import EMOTION_PRESETS, EMOTION_INTENSITY_WEIGHTS, normalize_emotion_vector
    from .indextts2_worker import validate_index_generation
    options = {'emotion_mode': 'follow', 'language': 'ZH', 'speed': 1.0, **raw,
               'model_version': MODELS[engine][2]}
    mode = options['emotion_mode']
    if mode not in {'follow', 'template', 'vector', 'emotext', 'emoaudio'}:
        raise ValueError('不支持的情感模式。')
    if options.get('emotion_preset', 'natural') not in EMOTION_PRESETS:
        raise ValueError('未知情感模板。')
    if options.get('emotion_intensity', 'standard') not in EMOTION_INTENSITY_WEIGHTS:
        raise ValueError('未知情感强度。')
    if options['language'] not in {'ZH', 'EN', 'JA', 'ES', 'AR'}:
        raise ValueError('不支持的语言。')
    for key, low, high in [('speed', .5, 2), ('emotion_weight', 0, 1.6)]:
        value = options.get(key, 1)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('参数超出范围。')
    for key in ('random_emotion', 'do_sample'):
        if key in options and type(options[key]) is not bool:
            raise ValueError('开关参数应为布尔值。')
    if 'do_sample' in options and engine == 'local_indextts2_0':
        raise ValueError('此采样开关仅适用于 2.5。')
    if 'emotion_vector' in options or mode == 'vector':
        values = options.get('emotion_vector')
        if not isinstance(values, list) or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('情感向量无效。')
        options['emotion_vector'] = list(normalize_emotion_vector(values))
    for key in ('emotion_text', 'emotion_audio_path'):
        if key in options and not isinstance(options[key], str):
            raise ValueError('情感参考须为文字或路径。')
    if mode == 'emotext' and not options.get('emotion_text', '').strip():
        raise ValueError('请提供情感描述。')
    if mode == 'emoaudio' and not options.get('emotion_audio_path', '').strip():
        raise ValueError('请提供情感参考音频。')
    if 'generation_options' in options:
        options['generation_options'] = validate_index_generation(options['generation_options'], options['model_version'])
    return options


def needs_voice(engine, options):
    return not ((engine == 'local_voxcpm2' and options['mode'] == 'design') or
                (engine == 'local_qwen3_tts' and options['mode'] in {'official', 'design'}))


def readiness(engine, options):
    if engine.startswith('local_indextts2'):
        from .indextts2_readiness import check_runtime
        state = check_runtime(expected_version=MODELS[engine][2])
        return 'ready' if state.ready and state.model_version == MODELS[engine][2] else 'not_ready'
    if engine == 'local_qwen3_tts':
        from .qwen3_runtime import readiness as probe
        return 'ready' if probe(options['mode'])[0] else 'not_ready'
    from .runtime_packages import status
    return status(engine)


def catalog():
    from .voxcpm2_options import DEFAULTS as vox
    from .qwen3_options import DEFAULTS as qwen, LANGUAGES, SPEAKERS
    from .indextts2_presets import EMOTION_PRESETS, EMOTION_INTENSITY_WEIGHTS
    from .indextts2_worker import INDEX2_GENERATION_FIELDS, INDEX25_GENERATION_FIELDS
    rows = []
    for engine, (name, pipeline_engine, version) in MODELS.items():
        row = dict(engine=engine, name=name, pipeline_engine=pipeline_engine, model_version=version,
                   modes=(['follow', 'template', 'vector', 'emotext', 'emoaudio'] if version else
                          ['hifi', 'directed', 'design'] if engine == 'local_voxcpm2' else ['clone', 'official', 'design']))
        row['option_fields'] = sorted(INDEX_FIELDS - ({'do_sample', 'language', 'speed'} if version == '2.0' else set()) if version else vox if engine == 'local_voxcpm2' else qwen)
        if version:
            row.update(emotion_presets=list(EMOTION_PRESETS), emotion_intensities=list(EMOTION_INTENSITY_WEIGHTS),
                       generation_fields=INDEX25_GENERATION_FIELDS if version == '2.5' else INDEX2_GENERATION_FIELDS)
        if engine == 'local_qwen3_tts':
            row.update(languages=LANGUAGES, speakers=SPEAKERS)
        rows.append(row)
    return rows
