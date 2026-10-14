"""VoxCPM2 运行时包自检(被 build_tts_runtime.sh 用运行时解释器 -I -s 跑)。"""
import torch
import voxcpm  # noqa: F401
import soundfile  # noqa: F401

print(f"  自检 OK · torch {torch.__version__} · mps {torch.backends.mps.is_available()}")
