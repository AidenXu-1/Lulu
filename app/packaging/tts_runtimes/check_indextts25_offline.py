"""Offline, weight-free runtime comparison probe; does not certify synthesis.

Run with the candidate's own Python: -I -B this_file runtime output.json.
All caches and HOME are redirected to a temporary directory before importing libs.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

runtime = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
sys.dont_write_bytecode = True
attempts = []


def deny(*args, **kwargs):
    attempts.append('network attempt')
    raise RuntimeError('Network is forbidden during offline runtime verification')


socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny

with tempfile.TemporaryDirectory(prefix='lulu-runtime-check-') as temp:
    root = Path(temp)
    for key in ('HOME', 'XDG_CACHE_HOME', 'MPLCONFIGDIR', 'NUMBA_CACHE_DIR', 'HF_HOME', 'TORCH_HOME'):
        directory = root / key
        directory.mkdir()
        os.environ[key] = str(directory)
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_IMPLICIT_TOKEN='1', PYTHONDONTWRITEBYTECODE='1')
    os.chdir(root)
    sys.path.insert(0, str(runtime / 'vendor/index-tts'))
    import nltk
    nltk.data.path = [str(runtime / 'python/nltk_data')]
    from indextts.infer_v2_5 import IndexTTS2
    from indextts.utils.front import TextNormalizer
    from g2p_en import G2p
    import fugashi
    import unidic_lite
    from whisper.tokenizer import get_tokenizer
    import numpy as np
    import torch
    import torchaudio
    import librosa
    import pandas as pd
    import sympy
    from sklearn.preprocessing import StandardScaler

    normalizer = TextNormalizer()
    normalizer.load()
    normalized = [normalizer.normalize(s) for s in (
        '今天是2026年9月20日，价格是12.5元。', 'I have 25 apples.',
        'Lulu支持GPT-5，也支持<行|XING2>走。')]
    phonemes = G2p()('Hello, welcome to Lulu.')
    tagger = fugashi.Tagger(f'-d "{unidic_lite.DICDIR}"')
    japanese = [(w.surface, list(w.feature)) for w in tagger('こんにちは、今日は良い天気です。')]
    tokens = {}
    for lang, phrase in [('zh', '你好，世界。'), ('en', 'Hello, world.'),
                         ('ja', 'こんにちは世界。'), ('es', 'Hola, mundo.'), ('ar', 'مرحبا بالعالم.')]:
        tokenizer = get_tokenizer(multilingual=True, language=lang, task='transcribe')
        ids = tokenizer.encode(phrase)
        assert tokenizer.decode(ids) == phrase
        tokens[lang] = ids
    wave = torch.sin(torch.arange(24000, dtype=torch.float32) * (2 * np.pi * 440 / 24000)).unsqueeze(0)
    resampled = torchaudio.functional.resample(wave, 24000, 16000)
    spectrum = librosa.feature.melspectrogram(y=resampled[0].numpy(), sr=16000, n_mels=40)
    assert resampled.shape == (1, 16000) and np.isfinite(spectrum).all()
    scaled = StandardScaler().fit_transform([[1., 2.], [3., 4.]])
    x = sympy.Symbol('x')
    report = {
        'synthesis_verified': False, 'inference_class': IndexTTS2.__name__,
        'normalization': normalized, 'english_phonemes': phonemes,
        'japanese': japanese, 'tokens': tokens,
        'audio_sha256': hashlib.sha256(resampled.numpy().tobytes()).hexdigest(),
        'mel_sha256': hashlib.sha256(spectrum.tobytes()).hexdigest(),
        'scaled': scaled.tolist(), 'pandas_sum': int(pd.Series([1, 2, 3]).sum()),
        'sympy': str(sympy.expand((x + 1) ** 2)), 'network_attempts': attempts,
    }
    assert normalized and all(normalized) and phonemes and japanese
    assert not attempts, 'A dependency tried to access the network'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('OFFLINE_RUNTIME_CHECK_PASSED (no model synthesis)')
