"""IndexTTS2 运行时包自检:关键依赖可导入 + vendor 源码可被 import(布好 sys.path 后)。

被 build_tts_runtime.sh 用运行时解释器 `-I -s` 跑;失败即不出包。
不加载权重(权重不在包内),只验「能 import 到推理类 IndexTTS2」。
"""
import os
import sys
from pathlib import Path

import torch  # noqa: E402
import torchaudio  # noqa: E402
import transformers  # noqa: E402
import librosa  # noqa: E402
import omegaconf  # noqa: E402
import safetensors  # noqa: E402

# vendor 源码:index-tts 是纯 Python 包,clone 根(含 indextts/)加到 sys.path 即可 import
stage = Path(os.environ["STAGE"]).resolve()
vendor = stage / "vendor" / "index-tts"
sys.path.insert(0, str(vendor))
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
from indextts.infer_v2_5 import IndexTTS2  # noqa: E402,F401
from indextts.utils.front import TextNormalizer  # noqa: E402,F401

print(
    f"  自检 OK · torch {torch.__version__} · torchaudio {torchaudio.__version__} · "
    f"mps {torch.backends.mps.is_available()} · transformers {transformers.__version__} · "
    f"index-tts IndexTTS2 import 成功"
)
