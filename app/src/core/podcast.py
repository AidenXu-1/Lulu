"""播客 RSS 解析:输入订阅(RSS)链接 → 列出每集 + 音频直链。

播客本质是开放订阅:RSS + enclosure 音频是公开静态文件(专为分发设计),
匿名 HTTP GET 即可,无登录/无 cookie/无账号 → 零封号风险、基本不碰风控。
拿到每集的 audio_url 后,交给现有链接下载管线(yt-dlp 实测可直读 enclosure)转写。
仅用标准库,不新增依赖。
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0/}"


@dataclass
class Episode:
    title: str
    audio_url: str
    date: str = ""        # 已格式化 YYYY-MM-DD
    duration: str = ""    # 已格式化 mm:ss / h:mm:ss


@dataclass
class Feed:
    title: str
    episodes: list[Episode] = field(default_factory=list)


def looks_like_feed(url: str) -> bool:
    """快速启发式:像不像 RSS 订阅链接(避免对普通视频链接也去抓一遍)。"""
    u = url.lower()
    return any(k in u for k in (".xml", "/rss", "rss.", "/feed", "feed.", "feeds.", "/podcast"))


def _fmt_date(pubdate: str) -> str:
    if not pubdate:
        return ""
    try:
        return parsedate_to_datetime(pubdate).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return pubdate[:16]


def _fmt_dur(item: ET.Element) -> str:
    raw = item.findtext(f"{_ITUNES}duration") or ""
    raw = raw.strip()
    if not raw:
        return ""
    if ":" in raw:          # 已是 hh:mm:ss / mm:ss
        return raw
    try:                    # 纯秒数
        sec = int(float(raw))
    except ValueError:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fetch_feed(url: str, *, timeout: int = 20, limit: int = 300) -> Feed | None:
    """抓取并解析 RSS;不是播客 RSS 则返回 None(让上层回退到普通链接处理)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:  # noqa: BLE001
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    chan = root.find("channel")
    if chan is None:        # 只认 RSS(播客几乎都是 RSS,不是 Atom)
        return None
    title = (chan.findtext("title") or "播客").strip()
    episodes: list[Episode] = []
    for it in chan.findall("item"):
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        typ = (enc.get("type") or "").lower()
        if typ and not typ.startswith("audio"):  # 跳过视频/图文条目
            continue
        episodes.append(Episode(
            title=(it.findtext("title") or "(无标题)").strip(),
            audio_url=enc.get("url").strip(),
            date=_fmt_date(it.findtext("pubDate") or ""),
            duration=_fmt_dur(it),
        ))
        if len(episodes) >= limit:
            break
    if not episodes:
        return None
    return Feed(title=title, episodes=episodes)
