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
    count = square_sum = peak = 0
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
        # A long recording must not create a tuple for every sample at once.
        while data := wav.readframes(65536):
            samples = struct.unpack(f"<{len(data) // 2}h", data)
            count += len(samples)
            square_sum += sum(value * value for value in samples)
            peak = max(peak, max((abs(value) for value in samples), default=0))
    if not count:
        return {"sample_rate": float(sample_rate), "duration": 0.0, "rms": 0.0, "peak": 0.0}
    rms = math.sqrt(square_sum / count)
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


def mix_recording_tracks(microphone_path: Path, system_path: Path, output_path: Path, *,
                         microphone_started_at: float | None, system_started_at: float | None) -> dict[str, object]:
    """Place both media origins on their common monotonic capture timeline."""

    clocks = (microphone_started_at, system_started_at)
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in clocks):
        raise RecordingAudioError("无法确认两路录音时间，未保存可能错位的混录。请重新录制。")
    origin = min(clocks)
    mic_delay = round((microphone_started_at - origin) * 48000)
    sys_delay = round((system_started_at - origin) * 48000)
    if max(mic_delay, sys_delay) > 7200 * 48000:
        raise RecordingAudioError("两路录音时间异常，请重新录制。")

    if not microphone_path.exists() or microphone_path.stat().st_size == 0:
        raise RecordingAudioError("麦克风音轨为空")
    if not system_path.exists() or system_path.stat().st_size == 0:
        raise RecordingAudioError("系统声音音轨为空")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"[0:a]aresample=48000,asetpts=PTS-STARTPTS,adelay={mic_delay}S:all=1[mic];"
        f"[1:a]aresample=48000,asetpts=PTS-STARTPTS,adelay={sys_delay}S:all=1[sys];"
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
