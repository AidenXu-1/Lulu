"""把 Transcript 写成文件,落到用户指定位置。

支持格式:
- txt:纯逐字稿
- md :带简单标题的 Markdown
- srt:字幕(仅当 transcript 带时间戳时可用)
"""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

from ..models import Transcript
from .segments import speaker_text, valid_interval

SUPPORTED_FORMATS = ("txt", "md", "srt")


@contextmanager
def new_export_file(directory: Path, filename: str):
    """Reserve one distinct export destination, including across concurrent exports."""
    name = Path(filename).name
    base = Path(name)
    index = 1
    while True:
        target = directory / (name if index == 1 else f"{base.stem} ({index}){base.suffix}")
        try:
            stream = target.open("xb")
            break
        except FileExistsError:
            index += 1
    try:
        with stream:
            yield stream
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clock(seconds: float) -> str:
    """紧凑时间戳:不足一小时用 mm:ss,超过用 h:mm:ss。"""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render(transcript: Transcript, fmt: str) -> str:
    """把 transcript 渲染成指定格式的字符串。"""
    fmt = fmt.lower()
    if fmt == "txt":
        return transcript.text + "\n"
    if fmt == "md":
        lines = ["# 逐字稿", ""]
        if transcript.source:
            lines.append(f"> 来源:{transcript.source}")
        if transcript.engine:
            lines.append(f"> 引擎:{transcript.engine}")
        lines.append("")
        if transcript.has_timestamps:
            # 有时间戳:每段一行,前缀 `[mm:ss]`(加粗便于扫读)
            for seg in transcript.segments:
                if not (seg.text or "").strip():
                    continue
                text = speaker_text(seg)
                if seg.start is not None:
                    lines.append(f"**[{_clock(seg.start)}]** {text}")
                else:
                    lines.append(text)
                lines.append("")
        else:
            lines.append(transcript.text)
        return "\n".join(lines) + "\n"
    if fmt == "srt":
        if not transcript.has_timestamps:
            raise ValueError("当前转录结果没有时间戳,无法导出 srt。请改用 txt / md。")
        meaningful = [s for s in transcript.segments if (s.text or '').strip()]
        if not meaningful or not all(valid_interval(s) for s in meaningful):
            raise ValueError("部分文字缺少有效时间，无法完整导出字幕。请改用 txt / md。")
        blocks = []
        idx = 1
        for seg in meaningful:
            blocks.append(
                f"{idx}\n{_srt_time(seg.start)} --> {_srt_time(seg.end)}\n{speaker_text(seg)}\n"
            )
            idx += 1
        return "\n".join(blocks) + "\n"
    raise ValueError(f"不支持的格式:{fmt}(支持:{', '.join(SUPPORTED_FORMATS)})")


def write_transcript(transcript: Transcript, dst: Path, fmt: str | None = None) -> Path:
    """把 transcript 写到 dst。fmt 为空时按 dst 后缀推断,默认 txt。"""
    dst = Path(dst).expanduser()
    if fmt is None:
        suffix = dst.suffix.lower().lstrip(".")
        fmt = suffix if suffix in SUPPORTED_FORMATS else "txt"
    content = render(transcript, fmt)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8")
    return dst
