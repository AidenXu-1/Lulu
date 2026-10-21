"""把任意音视频转码成 16k 单声道 wav,喂给转录引擎。

优先用 imageio-ffmpeg 自带的 ffmpeg 二进制,避免要求用户手动安装。
转码是纯本地操作,不联网。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

TARGET_SR = 16000
TARGET_CH = 1


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        # 退路:系统 PATH 里的 ffmpeg
        return "ffmpeg"


def ensure_ffmpeg_on_path() -> None:
    """让裸调用 `ffmpeg` 的库(如 mlx-whisper、yt-dlp)也能用上自带的 ffmpeg。

    从访达双击启动的 .app,PATH 很精简(没有 Homebrew 的 /opt/homebrew/bin),
    凡是写死 `["ffmpeg", ...]` 的依赖都会报 [Errno 2] No such file or directory: 'ffmpeg'。
    这里把自带二进制软链成名叫 `ffmpeg` 的文件、把所在目录塞进 PATH,一次解决。
    幂等:启动时调一次即可。
    """
    import os
    import shutil
    import tempfile

    exe = _ffmpeg_exe()
    if exe == "ffmpeg":
        return  # 没有自带二进制,只能指望系统里有
    bindir = Path(tempfile.gettempdir()) / "lulu_ffmpeg_bin"
    link = bindir / "ffmpeg"
    try:
        bindir.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            link.symlink_to(exe)
        except OSError:
            shutil.copy2(exe, link)
            link.chmod(0o755)
    except OSError:
        return  # 建链接失败就算了,转码仍走自带二进制(走的是绝对路径)
    if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")


def to_wav_mono(src: Path, dst: Path, sr: int = TARGET_SR, *, is_cancelled=None, timeout=None) -> Path:
    """把 src 转成单声道、指定采样率的 wav 写到 dst,返回 dst。
    用于把任意格式(mp3/m4a/stereo wav…)的参考音频统一成干净 wav。"""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg_exe(),
        "-y",
        "-i", str(src),
        "-vn",                 # 丢掉视频流
        "-ac", str(TARGET_CH),
        "-ar", str(sr),
        "-f", "wav",
        str(dst),
    ]
    if is_cancelled is None and timeout is None:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        returncode, stderr = proc.returncode, proc.stderr
    else:
        started = time.monotonic()
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            try:
                while True:
                    if is_cancelled is not None and is_cancelled():
                        raise InterruptedError("转码已取消")
                    if timeout is not None and time.monotonic() - started >= timeout:
                        raise TimeoutError("音频转码超时，可重试此任务")
                    try:
                        _, stderr = proc.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
            returncode = proc.returncode
    if returncode != 0:
        raise RuntimeError(
            f"ffmpeg 转码失败(returncode={returncode}):\n{stderr[-2000:]}"
        )
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg 转码后未生成有效 wav 文件。")
    return dst


def to_wav_16k_mono(src: Path, dst: Path, *, is_cancelled=None, timeout=None) -> Path:
    """把 src 转成 16k 单声道 wav 写到 dst,返回 dst。"""
    result = to_wav_mono(src, dst, sr=TARGET_SR, is_cancelled=is_cancelled, timeout=timeout)
    reject_zero_pcm(result, is_cancelled=is_cancelled)
    return result


def reject_zero_pcm(path: Path, *, is_cancelled=None) -> None:
    """Reject only exact digital silence, never a volume threshold or phrase."""
    import wave
    with wave.open(str(path), 'rb') as wav:
        # FFmpeg's WAV default here is signed 16-bit PCM.
        if wav.getsampwidth() != 2 or wav.getcomptype() != 'NONE':
            raise ValueError('音频准备结果无效，请重新导入。')
        while True:
            if is_cancelled and is_cancelled():
                raise InterruptedError('转录已取消')
            data = wav.readframes(65536)
            if not data:
                raise ValueError('音频为空或完全静音，未开始转录。')
            if any(data):
                return


def wav_to_mp3(src: Path, dst: Path, bitrate: str = "192k") -> Path:
    """把 wav 转成 mp3,用于本地配音导出。"""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg_exe(),
        "-y",
        "-i", str(src),
        "-vn",
        "-codec:a", "libmp3lame",
        "-b:a", bitrate,
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 导出 MP3 失败(returncode={proc.returncode}):\n{proc.stderr[-2000:]}"
        )
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg 导出 MP3 后未生成有效文件。")
    return dst
