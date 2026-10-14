"""平台字幕直转:链接若自带字幕(人工/自动),直接取来当逐字稿,跳过本地模型。

更快、免费、且自带时间戳。优先人工字幕(更准),其次自动字幕。纯公开字幕文件,
匿名下载,无登录/无账号。
"""

from __future__ import annotations

import re
import urllib.request

from .models import Segment, Transcript

_CUE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG = re.compile(r"<[^>]+>")
_DEFAULT_LANGS = ("zh-Hans", "zh-CN", "zh-Hant", "zh", "en", "en-US")


def _sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(text: str) -> list[Segment]:
    """把 WebVTT/SRT 文本解析成带时间戳的 Segment 列表(去标签、去滚动重复)。"""
    segs: list[Segment] = []
    last = None
    for block in re.split(r"\r?\n\r?\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        m = _CUE.search(lines[ti])
        if not m:
            continue
        g = m.groups()
        start, end = _sec(*g[:4]), _sec(*g[4:])
        txt = " ".join(lines[ti + 1:])
        txt = _TAG.sub("", txt)               # 去掉 <c>/<00:00:01.360> 这类内联标签
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt or txt == last:            # 自动字幕常有滚动重复,跳过
            continue
        last = txt
        segs.append(Segment(text=txt, start=start, end=end))
    return segs


def _pick(table: dict, langs: list[str]):
    """从 {lang: [formats]} 里挑一个 vtt 字幕,优先 langs 顺序。返回 (lang, url) 或 None。"""
    for lang in langs:
        for f in table.get(lang, []):
            if f.get("ext") == "vtt" and f.get("url"):
                return lang, f["url"]
    for lang, fs in table.items():            # 兜底:任意 vtt
        for f in fs:
            if f.get("ext") == "vtt" and f.get("url"):
                return lang, f["url"]
    return None


def fetch_from_link(url: str, progress=None) -> Transcript | None:
    """链接若有字幕,取来构造 Transcript(带时间戳);没有则返回 None。"""
    import yt_dlp

    from .audio.extractor import _DESKTOP_UA, _fill_meta
    from .audio.download_runtime import public_download_options

    progress = progress or (lambda *_: None)
    headers = {"User-Agent": _DESKTOP_UA, "Accept-Encoding": "identity"}
    progress("查询平台字幕…", -1.0)
    try:
        with yt_dlp.YoutubeDL({
            **public_download_options(url),
            "quiet": True, "no_warnings": True, "skip_download": True,
            "http_headers": headers, "retries": 2, "socket_timeout": 15,
        }) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None

    langs = list(_DEFAULT_LANGS)
    if info.get("language"):
        langs.insert(0, info["language"])

    kind = "人工字幕"
    got = _pick(info.get("subtitles") or {}, langs)
    if not got:
        kind = "自动字幕"
        got = _pick(info.get("automatic_captions") or {}, langs)
    if not got:
        return None
    lang, sub_url = got

    progress(f"下载平台{kind}({lang})…", -1.0)
    try:
        vtt = urllib.request.urlopen(
            urllib.request.Request(sub_url, headers=headers), timeout=30
        ).read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    segs = parse_vtt(vtt)
    if not segs:
        return None

    meta: dict = {}
    _fill_meta(meta, info, url)
    progress(f"已取用平台{kind}", 1.0)
    return Transcript(
        text="\n".join(s.text for s in segs),
        segments=segs,
        language=lang,
        engine=f"平台{kind}",
        source=url,
        title=meta.get("title"),
        meta=meta,
    )
