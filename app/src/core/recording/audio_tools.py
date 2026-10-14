"""Small audio helpers for recording post-processing."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path


class RecordingAudioError(RuntimeError):
    """Audio analysis or mixing failed."""


def _ffmpeg_exe() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RecordingAudioError("音频处理组件不可用") from exc


def _decode_to_wav(path: Path, wav_path: Path) -> None:
    completed = subprocess.run(
        [
            _ffmpeg_exe(),
            "-y",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "48000",
            str(wav_path),
        ],
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    if completed.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size <= 44:
        raise RecordingAudioError("音频文件无法读取")


def _wav_stats(path: Path) -> dict[str, float]:
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
        data = wav.readframes(frames)
    if not data:
        return {"sample_rate": float(sample_rate), "duration": 0.0, "rms": 0.0, "peak": 0.0}
    samples = struct.unpack("<" + "h" * (len(data) // 2), data)
    if not samples:
        return {"sample_rate": float(sample_rate), "duration": 0.0, "rms": 0.0, "peak": 0.0}
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    peak = max(abs(v) for v in samples)
    return {
        "sample_rate": float(sample_rate),
        "duration": float(frames) / float(sample_rate or 1),
        "rms": float(rms),
        "peak": float(peak),
    }


def analyze_recording_file(path: Path, *, silence_rms: float = 80.0, silence_peak: float = 200.0) -> dict[str, object]:
    """Return simple stats and a conservative silence flag for a local audio file."""

    if not path.exists() or path.stat().st_size == 0:
        raise RecordingAudioError("音频文件为空")
    with tempfile.TemporaryDirectory(prefix="lulu-audio-analysis-") as tmp:
        wav_path = Path(tmp) / "analysis.wav"
        _decode_to_wav(path, wav_path)
        stats = _wav_stats(wav_path)
    stats["is_silent"] = stats.get("rms", 0.0) < silence_rms or stats.get("peak", 0.0) < silence_peak
    return stats


def mix_recording_tracks(microphone_path: Path, system_path: Path, output_path: Path) -> dict[str, object]:
    """Mix microphone and system-audio tracks into one user-facing M4A file.

    ffmpeg aligns both inputs at their file start, resamples to 48 kHz and lets
    amix use the longest track. This keeps the first product slice simple:
    clock drift or sub-second manual stop skew is not corrected beyond ffmpeg's
    resampling, so long-form professional sync remains a later refinement.
    """

    if not microphone_path.exists() or microphone_path.stat().st_size == 0:
        raise RecordingAudioError("麦克风音轨为空")
    if not system_path.exists() or system_path.stat().st_size == 0:
        raise RecordingAudioError("系统声音音轨为空")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        "[0:a]aresample=48000,asetpts=PTS-STARTPTS[mic];"
        "[1:a]aresample=48000,asetpts=PTS-STARTPTS[sys];"
        "[mic][sys]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[mix]"
    )
    completed = subprocess.run(
        [
            _ffmpeg_exe(),
            "-y",
            "-v",
            "error",
            "-i",
            str(microphone_path),
            "-i",
            str(system_path),
            "-filter_complex",
            filter_graph,
            "-map",
            "[mix]",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(output_path),
        ],
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        try:
            output_path.unlink()
        except OSError:
            pass
        raise RecordingAudioError("混合录音保存失败")
    return analyze_recording_file(output_path)


__all__ = ["RecordingAudioError", "analyze_recording_file", "mix_recording_tracks"]
