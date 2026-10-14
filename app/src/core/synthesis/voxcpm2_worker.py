"""Offline VoxCPM2 worker. Stdout is JSONL; model diagnostics use stderr."""
import argparse
import json
import random
import sys
import time
from pathlib import Path

_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr


def emit(event):
    _REAL_STDOUT.write(json.dumps(event, ensure_ascii=False) + '\n')
    _REAL_STDOUT.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request-json', required=True)
    req = json.loads(Path(parser.parse_args().request_json).read_text('utf-8'))
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from importlib.metadata import version
        from voxcpm import VoxCPM
        start = time.monotonic()
        emit({'type': 'progress', 'message': '加载 VoxCPM2 本地模型…', 'frac': .05})
        model = VoxCPM.from_pretrained(req['model_path'], device=req['device'],
                    load_denoiser=False, optimize=False, local_files_only=True)
        seed = req['seed']
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if req['device'] == 'mps':
            torch.mps.manual_seed(seed)
        emit({'type': 'progress', 'message': '正在生成声音…', 'frac': .15})
        audio = np.asarray(model.generate(**req['generate']), dtype='float32').reshape(-1)
        sr = int(model.tts_model.sample_rate)
        if not len(audio) or not np.all(np.isfinite(audio)):
            raise ValueError('模型未返回有效声音。')
        sf.write(req['raw_path'], audio, sr, subtype='FLOAT')
        sf.write(req['output_path'], audio, sr, subtype='PCM_16')
        emit({'type': 'result', 'wav_path': req['output_path'], 'raw_path': req['raw_path'],
              'sample_rate': sr, 'duration': len(audio)/sr, 'seconds': time.monotonic()-start,
              'package_version': version('voxcpm'),
              'dtype': str(next(model.tts_model.parameters()).dtype)})
    except Exception as exc:
        emit({'type': 'error', 'error_type': type(exc).__name__, 'detail': str(exc)})
        sys.exit(1)


if __name__ == '__main__':
    main()
