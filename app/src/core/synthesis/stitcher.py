"""合成音频拼接与导出。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf


def _trim_silence(data: np.ndarray, sample_rate: int, threshold: float = 0.008, pad_seconds: float = 0.04) -> np.ndarray:
    """去掉每段首尾明显静音,保留少量余量;不改变段内动态。"""
    if data.size == 0:
        return data
    mono = np.abs(data)
    idx = np.flatnonzero(mono > threshold)
    if idx.size == 0:
        return data
    pad = int(sample_rate * pad_seconds)
    start = max(0, int(idx[0]) - pad)
    end = min(len(data), int(idx[-1]) + pad + 1)
    return data[start:end]


def _peak_normalize(data: np.ndarray, target_peak: float = 0.89) -> np.ndarray:
    """整体峰值归一化,避免导出音量过小;不做动态压缩。"""
    if data.size == 0:
        return data
    peak = float(np.max(np.abs(data)))
    if peak <= 1e-6 or peak >= target_peak:
        return data
    return data * (target_peak / peak)


def stitch_wavs(parts: list[Path], dst: Path, gap_seconds: float = 0.16) -> tuple[Path, float]:
    if not parts:
        raise ValueError("没有可拼接的音频片段。")
    arrays = []
    sample_rate: int | None = None
    for i, p in enumerate(parts):
        data, sr = sf.read(str(p), dtype="float32")
        if sample_rate is None:
            sample_rate = sr
        elif sr != sample_rate:
            raise ValueError(f"采样率不一致:{p}={sr}, expected={sample_rate}")
        if data.ndim > 1:
            data = data.mean(axis=1)
        data = _trim_silence(data, sr)
        # 只在段与段之间插静音,最后一段后面不再追加,避免成品末尾多一截静音
        if gap_seconds > 0 and i > 0:
            arrays.append(np.zeros(int(sr * gap_seconds), dtype="float32"))
        arrays.append(data)
    merged = np.concatenate(arrays)
    merged = _peak_normalize(merged)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), merged, int(sample_rate or 48000))
    return dst, float(len(merged) / float(sample_rate or 48000))
