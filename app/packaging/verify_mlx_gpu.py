#!/usr/bin/env python3
"""Exercise the assembled native MLX libraries, without models or user data."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

_PROBE = r'''
import ctypes, json, sys
from pathlib import Path
assert sys.dont_write_bytecode
root = Path(sys.argv[1]).resolve()
# Python-only helpers come from the locked build environment. The extension
# and its native dependencies must come from the finished app, checked below.
import mlx
mlx.__path__ = [str(root / "mlx"), *mlx.__path__]
import mlx.core as mx
assert Path(mx.__file__).resolve().is_relative_to(root), mx.__file__
lib = ctypes.CDLL(None)
lib._dyld_image_count.restype = ctypes.c_uint32
lib._dyld_get_image_name.argtypes = [ctypes.c_uint32]
lib._dyld_get_image_name.restype = ctypes.c_char_p
images = [Path(lib._dyld_get_image_name(i).decode()).resolve()
          for i in range(lib._dyld_image_count())]
loaded = [p for p in images if p.name == "libmlx.dylib"]
assert loaded and all(p.is_relative_to(root) for p in loaded), loaded
mx.set_default_device(mx.gpu)
x = mx.arange(16, dtype=mx.float32).reshape(4, 4)
y = x @ x.T
mx.eval(y)
assert y[0, 0].item() == 14.0 and y[3, 3].item() == 734.0
print(json.dumps({"ok": True, "device": "gpu", "shape": list(y.shape),
                  "native_libraries": [str(p.relative_to(root)) for p in loaded]}))
'''


def verify(app: Path) -> dict:
    root = app.resolve() / 'Contents/Frameworks'
    result = subprocess.run([sys.executable, '-I', '-B', '-c', _PROBE, str(root)],
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('打包后 MLX GPU 实际运算失败:\n' + result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.app), ensure_ascii=False))
