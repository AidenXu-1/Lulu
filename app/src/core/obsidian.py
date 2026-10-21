"""一键把逐字稿写成 Obsidian 笔记(.md)落到指定文件夹。

Obsidian 仓库本质就是一堆 Markdown 文件,所以"存入 Obsidian" = 往用户选定的目录
写一个带 YAML frontmatter 的 .md(标题/日期/来源/引擎/标签 + 逐字稿,可带时间戳与摘要)。
纯本地文件写入,不联网。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .collect.export import resolve_managed_cover_file
from .feishu_storage import (
    transcript_duration_seconds,
    transcript_media_url,
    transcript_published_at_ms,
    transcript_work_url,
)
from .models import Transcript
from .output.writer import _clock

_BAD = set('/\\:*?"<>|')


def _safe_name(title: str, fallback: str = "转写") -> str:
    name = "".join(c for c in (title or "") if c not in _BAD).strip()
    name = name.replace("\n", " ").strip(". ")
    return (name[:80] or fallback)


def _fmt_date(yyyymmdd) -> str:
    s = str(yyyymmdd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else ""


def _yaml_str(v) -> str:
    return '"' + str(v).replace('"', "'").replace("\n", " ") + '"'


def _cover_reference(transcript: Transcript) -> str:
    cover = transcript.cover
    if cover.status == "downloaded" and cover.local_path:
        candidate = resolve_managed_cover_file(cover.local_path)
        if candidate is not None:
            return candidate.as_uri()
    return cover.url or str((transcript.meta or {}).get("thumbnail") or "")


def _frontmatter(transcript: Transcript, title: str) -> list[str]:
    m = transcript.meta or {}
    # 字段名用中文;唯独 tags 保持英文(Obsidian 原生标签属性,改了标签药丸会失效)
    lines = ["---", f"标题: {_yaml_str(title)}"]
    published_at_ms = transcript_published_at_ms(m)
    pub = _fmt_date(m.get("upload_date"))
    if not pub and published_at_ms is not None:
        cst = timezone(timedelta(hours=8))
        pub = datetime.fromtimestamp(
            published_at_ms / 1000, cst
        ).date().isoformat()
    lines.append(f"日期: {pub or date.today().isoformat()}")
    if m.get("uploader"):
        lines.append(f"作者: {_yaml_str(m['uploader'])}")
    work_url = transcript_work_url(m, transcript.source)
    source_value = work_url or transcript.source
    if source_value:
        lines.append(f"来源: {source_value}")
    if work_url:
        lines.append(f"原作品链接: {work_url}")
    media_url = transcript_media_url(m)
    if media_url:
        lines.append(f"音视频直链: {media_url}")
    duration = transcript_duration_seconds(m)
    if duration is not None:
        lines.append(f"时长: {_clock(duration)}")
    cover_reference = _cover_reference(transcript)
    if cover_reference:
        lines.append(f"封面: {cover_reference}")
    if transcript.engine:
        lines.append(f"引擎: {transcript.engine}")
    if transcript.language or m.get("language"):
        lines.append(f"语种: {transcript.language or m.get('language')}")
    # 标签:固定标签 + 平台里"无空格的单词型"标签(带空格的短语不适合做 Obsidian 标签,跳过)
    tags = ["转写", "Lulu"]
    for t in (m.get("tags") or []):
        tt = str(t).strip()
        if tt and " " not in tt and "," not in tt and len(tt) <= 20 and tt not in tags:
            tags.append(tt)
    lines.append("tags: [" + ", ".join(tags[:15]) + "]")
    # 作者公开信息保留身份类字段;互动/热度数据只放正文,避免 YAML 与正文重复。
    au = m.get("author") or {}
    for fld, key in (("认证", "verify"), ("平台号", "handle")):
        if isinstance(au.get(key), str) and au[key]:
            lines.append(f"{fld}: {_yaml_str(au[key])}")
    lines += ["---", ""]
    return lines


# 互动数据展示用的 emoji 与中文标签(顺序即显示顺序)
_STAT_PAIRS = [("👍", "点赞", "like"), ("⭐", "收藏", "favorite"), ("🔁", "分享", "share"),
               ("💬", "评论", "comment"), ("🪙", "投币", "coin"), ("💭", "弹幕", "danmaku"),
               ("▶️", "播放", "view")]


def _fmt_count(n: int) -> str:
    """紧凑数字:1 万以内原样(带千分位),否则转「万 / 亿」,避免窄卡片溢出。

    752 → 752;5320 → 5,320;195000 → 19.5万;2720000 → 272万;130000000 → 1.3亿
    """
    if n < 10_000:
        return f"{n:,}"
    if n < 100_000_000:
        return f"{n / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
    return f"{n / 100_000_000:.2f}".rstrip("0").rstrip(".") + "亿"


def _fans_text(fans) -> str:
    """粉丝量显示:int 走紧凑格式(2万),模糊串(「1千+」)原样。空返回 ''。"""
    if isinstance(fans, int):
        return _fmt_count(fans)
    if isinstance(fans, str):
        return fans
    return ""


def _author_bit(m: dict) -> str:
    """UI 紧凑作者段:`👤 陈广厦 · 2万粉`(无粉丝数时只有昵称,无作者返回空)。"""
    au = m.get("author") or {}
    name = au.get("name")
    if not name:
        return ""
    fans = _fans_text(au.get("fans"))
    return f"👤 {name} · {fans}粉" if fans else f"👤 {name}"


def _stats_line(m: dict) -> str:
    """UI 用紧凑一行:`👤 陈广厦 · 2万粉  👍 752  ⭐ 67`(纯 emoji,大数字转万)。"""
    st = m.get("stats") or {}
    bits = [f"{emoji} {_fmt_count(st[key])}"
            for emoji, _zh, key in _STAT_PAIRS if isinstance(st.get(key), int)]
    au = _author_bit(m)
    if au:
        bits.insert(0, au)
    return "  ".join(bits)


def stat_items(m: dict) -> list[tuple[str, str, str]]:
    """结构化互动数据:[(key, emoji, 紧凑数字), ...],按显示顺序、只含有值的。

    给 UI 用真图标渲染(emoji 作无图标指标的兜底)。Markdown 仍走 _stats_line*。
    """
    st = m.get("stats") or {}
    return [(key, emoji, _fmt_count(st[key]))
            for emoji, _zh, key in _STAT_PAIRS if isinstance(st.get(key), int)]


def _author_full(m: dict) -> str:
    """作者完整段:`作者 陈广厦(抖音号 GUANGX321) · 粉丝 20,000 · 获赞 204,009 · 作品 21`。"""
    au = m.get("author") or {}
    name = au.get("name")
    if not name:
        return ""
    head = str(name)
    if au.get("handle"):
        head += f"({au['handle']})"
    bits = [f"作者 {head}"]
    fans = au.get("fans")
    if isinstance(fans, int):
        bits.append(f"粉丝 {fans:,}")
    elif isinstance(fans, str) and fans:
        bits.append(f"粉丝 {fans}")
    if isinstance(au.get("likes_total"), int):
        bits.append(f"获赞 {au['likes_total']:,}")
    if isinstance(au.get("works"), int):
        bits.append(f"作品 {au['works']}")
    if au.get("verify"):
        bits.append(str(au["verify"]))
    return " · ".join(bits)


def _stats_tooltip(m: dict) -> str:
    """悬停提示:带中文标签 + 完整原始数字,弥补纯 emoji 的可读性。"""
    st = m.get("stats") or {}
    bits = [f"{zh} {st[key]:,}"
            for _emoji, zh, key in _STAT_PAIRS if isinstance(st.get(key), int)]
    au = _author_full(m)
    if au:
        bits.insert(0, au)
    return " · ".join(bits)


def _stats_line_full(m: dict) -> str:
    """Obsidian 笔记正文用:`👍 点赞 752 · ⭐ 收藏 67 …`(emoji+中文+完整数字,归档更清楚)。"""
    st = m.get("stats") or {}
    bits = [f"{emoji} {zh} {st[key]:,}"
            for emoji, zh, key in _STAT_PAIRS if isinstance(st.get(key), int)]
    return " · ".join(bits)


def _body(transcript: Transcript) -> list[str]:
    from .output.segments import speaker_text
    if transcript.has_timestamps:
        out = []
        for seg in transcript.segments:
            if not (seg.text or "").strip():
                continue
            txt = speaker_text(seg)
            if seg.start is not None:
                out.append(f"**[{_clock(seg.start)}]** {txt}")
            else:
                out.append(txt)
            out.append("")
        return out
    return [transcript.text]


def _existing_lulu_note(text: str, title: str, summary: str | None = None) -> str | None:
    """Recognize a complete note already stored in the manuscript library.

    Link transcription results are saved as a full Markdown note before they
    enter the library. Exporting that record must not wrap the title, cover,
    metadata and transcript a second time.
    """
    raw = str(text or "").lstrip("\ufeff")
    if raw.startswith("---\n") or raw.startswith("---\r\n"):
        return None
    lines = raw.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None or not lines[first].startswith("# "):
        return None
    transcript_heading = next(
        (index for index, line in enumerate(lines[first + 1:], first + 1)
         if line.strip() == "## 逐字稿"),
        None,
    )
    if transcript_heading is None:
        return None

    # Keep the saved document intact while honoring a title that was renamed in
    # the library after the original note body was created.
    lines[first] = "# " + title
    if summary and not any(line.strip() == "## 摘要" for line in lines[first + 1:transcript_heading]):
        lines[transcript_heading:transcript_heading] = [
            "## 摘要", "", *summary.strip().splitlines(), "", ""
        ]
    return "\n".join(lines).rstrip() + "\n"


def frontmatter_text(transcript: Transcript, title: str) -> str:
    """返回与 Obsidian 笔记一致的 YAML frontmatter。"""
    return "\n".join(_frontmatter(transcript, title)).rstrip() + "\n"


def note_body(transcript: Transcript, title: str, summary: str | None = None,
              text_override: str | None = None) -> str:
    """返回 Obsidian 笔记正文部分,不含 YAML frontmatter。

    `text_override` 用于右侧栏:用户已编辑过正文时,继续保留标题/封面/互动/简介等结构,
    但逐字稿内容取当前编辑结果。
    """
    existing = _existing_lulu_note(
        transcript.text if text_override is None else text_override,
        title,
        summary,
    )
    if existing is not None:
        return existing

    m = transcript.meta or {}
    parts = [f"# {title}"]
    parts.append("")
    cover_reference = _cover_reference(transcript)
    if cover_reference:
        parts += [f"![封面]({cover_reference})", ""]
    # 元信息一行(作者 · 日期 · 时长 · 来源);有作者详情段时昵称放那边,这里不重复
    author_line = _author_full(m)
    bits = []
    if m.get("uploader") and not author_line:
        bits.append(str(m["uploader"]))
    published_at_ms = transcript_published_at_ms(m)
    published = _fmt_date(m.get("upload_date"))
    if not published and published_at_ms is not None:
        cst = timezone(timedelta(hours=8))
        published = datetime.fromtimestamp(
            published_at_ms / 1000, cst
        ).date().isoformat()
    if published:
        bits.append(published)
    duration = transcript_duration_seconds(m)
    if duration is not None:
        bits.append(_clock(duration))
    if bits:
        parts += ["> " + " · ".join(bits), ""]
    if author_line:
        parts += ["> 👤 " + author_line, ""]
    stats_line = _stats_line_full(m)   # 笔记正文用「emoji + 中文 + 完整数字」,更清楚
    if stats_line:
        parts += ["> " + stats_line, ""]
    work_url = transcript_work_url(m, transcript.source)
    if work_url:
        parts += [f"> 原作品：{work_url}", ""]
    desc = str(m.get("description") or "").strip()
    if len(desc) >= 4:           # 跳过 "-" 这类空描述
        parts += ["## 简介", "", desc, ""]
    if m.get("chapters"):
        parts += ["## 章节", ""]
        for c in m["chapters"]:
            st = c.get("start")
            ts = f"[{_clock(float(st))}] " if isinstance(st, (int, float)) else ""
            parts.append(f"- {ts}{c.get('title', '')}")
        parts.append("")
    if summary:
        parts += ["## 摘要", "", summary.strip(), ""]
    parts += ["## 逐字稿", ""]
    if text_override is not None:
        parts.append(text_override)
    else:
        parts += _body(transcript)
    return "\n".join(parts).rstrip() + "\n"


def build_note(transcript: Transcript, title: str, summary: str | None = None,
               text_override: str | None = None) -> str:
    parts = _frontmatter(transcript, title)
    parts.append(note_body(transcript, title, summary, text_override).rstrip())
    return "\n".join(parts).rstrip() + "\n"


def save_note(transcript: Transcript, folder: str | Path, title: str,
              summary: str | None = None) -> Path:
    """把笔记写到 folder,返回写入的文件路径。重名自动加序号。"""
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    base = _safe_name(title)
    dst = folder / f"{base}.md"
    n = 2
    while dst.exists():
        dst = folder / f"{base} ({n}).md"
        n += 1
    dst.write_text(build_note(transcript, title, summary), encoding="utf-8")
    return dst


def save_markdown_text(text: str, folder: str | Path, title: str) -> Path:
    """把(可能被用户编辑过的)Markdown 原文写到 folder,返回路径。重名自动加序号。

    与 save_note 的区别:不重新生成,原样落盘用户在右栏改好的文本。
    """
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    base = _safe_name(title)
    dst = folder / f"{base}.md"
    n = 2
    while dst.exists():
        dst = folder / f"{base} ({n}).md"
        n += 1
    dst.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return dst
