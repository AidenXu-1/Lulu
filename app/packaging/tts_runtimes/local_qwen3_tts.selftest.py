"""Offline import/version probe; no model loading or downloads."""
import importlib.metadata as metadata
import socket
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline selftest'))
for name, version in {'qwen-tts':'0.1.1', 'torch':'2.10.0', 'transformers':'4.57.3'}.items():
    assert metadata.version(name) == version, name
from qwen_tts import Qwen3TTSModel
import torch
assert torch.backends.mps.is_available(), 'Apple Silicon MPS unavailable'
print('Qwen runtime selftest passed')
