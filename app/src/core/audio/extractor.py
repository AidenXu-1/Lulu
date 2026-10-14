"""音频来源解析:本地文件直通 + 链接下载。

链接下载是 CLAUDE.md 允许的唯一联网例外,且必须隔离、可关闭(见 ADR-0004):
- 不登录、不带任何浏览器/账号 cookie、只取公开作品。
- enable_link_download=False 时,任何链接输入都会被拒绝,转录链路完全不碰网络。

抖音链接走专用解析器(解析公开分享页内嵌的 _ROUTER_DATA 拿播放直链,无登录、无签名),
其它通用链接交给 yt-dlp。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from ..models import TranscriptCover, normalize_official_cover_url

ProgressCallback = Callable[[str, float], None]


def _progress_bytes(value: int) -> str:
    clean = max(0, int(value))
    if clean < 1024 * 1024:
        return f"{clean / 1024:.0f} KB"
    if clean < 1024**3:
        return f"{clean / 1024**2:.1f} MB"
    return f"{clean / 1024**3:.1f} GB"


def _debug(msg: str) -> None:
    """仅在设置 LULU_DEBUG 环境变量时往 stderr 打一行,便于排查解析为何取不到。

    纯本地 stderr,不联网、不落文件、非遥测(守住 CLAUDE.md 红线)。
    """
    if os.environ.get("LULU_DEBUG"):
        print(f"[extractor] {msg}", file=sys.stderr)

# 从任意文案中抠出第一个链接(抖音分享文案里 URL 前后都是中文/空格)
_URL_FIND_RE = re.compile(r"https?://[^\s'\"，。！？、）)】<>]+", re.IGNORECASE)
_DOUYIN_OFFICIAL_DOMAINS = frozenset({"douyin.com", "iesdouyin.com"})
_DOUYIN_VIDEO_CDN_DOMAINS = frozenset({"douyinvod.com"})
_DOUYIN_WORK_PATH_RE = re.compile(
    r"^/(?:share/)?video/([0-9]{1,64})/?$",
    re.IGNORECASE,
)
_DOUYIN_CANONICAL_RE = re.compile(
    r"<link\b(?=[^>]*\brel=[\"']canonical[\"'])"
    r"(?=[^>]*\bhref=[\"'](https://[^\"']+)[\"'])[^>]*>",
    re.IGNORECASE,
)

# 抖音页面把数据塞在某个 window.XXX = {...} 里。抖音改版会换变量名,
# 所以不只认 _ROUTER_DATA:先试已知的几个(按优先级),再兜底抓任意内嵌 JSON。
_ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\});?\s*</script>", re.S)
_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)
_ANY_WINDOW_JSON_RE = re.compile(r"window\.[A-Za-z0-9_$]+\s*=\s*(\{.*?\});?\s*</script>", re.S)

# 播放直链可能挂在不同字段(不同清晰度/版本字段名不一),多候选命中其一即可。
_DOUYIN_ADDR_KEYS = ("play_addr", "play_addr_h264", "play_addr_265", "download_addr")

# 互动数据字段(同一页里,零额外请求)。out_key → 候选源字段名(多候选,改名也不至于整组丢)。
_DOUYIN_STAT = {
    "like": ("digg_count",),
    "favorite": ("collect_count",),
    "share": ("share_count",),
    "comment": ("comment_count",),
    "view": ("play_count",),
}

# 兜底:直接从页面正则抓播放直链(JSON 全找不到时的最后一搏)。
_DOUYIN_PLAY_URL_RE = re.compile(r'https?:[^"\'\\]*?(?:aweme/v1/play|/play/)[^"\'\\]*', re.IGNORECASE)

# 抖音改版导致解析失效时的统一文案:明确告诉用户「不怪你,是 App 该更新了」。
_DOUYIN_CHANGED_MSG = (
    "没能从抖音页面解析出视频。\n"
    "你的链接大概率没问题 —— 很可能是抖音改版了,App 的解析方式需要更新,可把这条链接反馈给开发者。\n"
    "(也有可能该作品已被设为非公开或已删除。)"
)

# UA(浏览器自我介绍头)分两个池,按场景取,绝不混用:
#   - 抖音必须用「移动端」UA 才拿得到内嵌 _ROUTER_DATA(实测,见 ADR-0004),只能从 _MOBILE_UAS 取。
#   - B站/小红书/yt-dlp 用「桌面」UA 绕过 WAF(无浏览器 UA 会被回 412)。
# 池里放多个真实、较新的 UA:① UA 过期了改这一处即可;② 重试时换一个再试(见 _pick_ua)。
# 仅普通请求头,不含登录/cookie/账号/签名,守住 ADR-0004 边界。
_MOBILE_UAS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
)
_DESKTOP_UAS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
)

# 单次请求(无重试)的默认取池内第一个;别处直接引用这两个别名即可。
_MOBILE_UA = _MOBILE_UAS[0]
_DESKTOP_UA = _DESKTOP_UAS[0]


def _pick_ua(pool: tuple[str, ...], attempt: int = 0) -> str:
    """从 UA 池按次序取(非随机,便于复现):第 attempt 次重试换下一个。"""
    return pool[attempt % len(pool)]

_SSL_CTX = ssl.create_default_context()


def extract_url(text: str) -> str | None:
    """从一段文本里抠出第一个链接(支持直接粘贴抖音分享文案)。"""
    m = _URL_FIND_RE.search(text)
    return m.group(0) if m else None


def is_douyin(url: str) -> bool:
    """Only route authenticated HTTPS Douyin hosts to the specialized parser."""

    raw = str(url or "")
    if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return False
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return False

    hostname = (parsed.hostname or "").casefold()
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError:
        return False
    if (
        not hostname
        or hostname.startswith(".")
        or hostname.endswith(".")
        or ".." in hostname
        or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789.-"
            for char in hostname
        )
    ):
        return False
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in _DOUYIN_OFFICIAL_DOMAINS
    )


def is_douyin_video_cdn(url: str) -> bool:
    """只接受采集结果中的抖音官方 HTTPS 视频 CDN 直链。"""

    raw = str(url or "")
    if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return False
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        return False
    hostname = (parsed.hostname or "").casefold()
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError:
        return False
    if (
        not hostname
        or hostname.startswith(".")
        or hostname.endswith(".")
        or ".." in hostname
        or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789.-"
            for char in hostname
        )
    ):
        return False
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in _DOUYIN_VIDEO_CDN_DOMAINS
    )


class _ValidatedMediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    """在发起下一跳请求前验证媒体重定向，避免允许域跳向任意地址。"""

    def __init__(self, validator: Callable[[str], bool]) -> None:
        super().__init__()
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._validator(newurl):
            raise RuntimeError("媒体地址重定向到了非允许域名")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def resolve_source(
    source: str,
    work_dir: Path,
    enable_link_download: bool,
    progress: ProgressCallback | None = None,
    meta: dict | None = None,
    link_media_mode: str = "audio",
    is_cancelled=None,
) -> Path:
    """把用户输入(本地路径 / 链接 / 含链接的分享文案)解析成本地媒体文件。

    meta(可选):传入一个 dict,链接下载时会回填真实标题到 meta["title"]。
    """
    progress = progress or (lambda *_: None)
    source = source.strip()

    # 输入里只要含链接,就走下载(兼容整段分享文案,自动抠出 URL)
    url = extract_url(source)
    if url:
        if not enable_link_download:
            raise RuntimeError(
                "链接下载功能已关闭(离线模式)。如需按链接取音频,请在界面打开该开关。"
            )
        work_dir.mkdir(parents=True, exist_ok=True)
        if is_douyin(url):
            return _download_douyin(url, work_dir, progress, meta)
        if is_cancelled is not None:
            from .download_process import download_in_process
            return download_in_process(url, work_dir, progress, meta,
                                       link_media_mode=link_media_mode, is_cancelled=is_cancelled)
        return _download_via_ytdlp(url, work_dir, progress, meta, link_media_mode=link_media_mode)

    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"找不到文件:{path}")
    return path


# --------------------------------------------------------------------------- #
# 抖音专用:解析公开分享页 → 播放直链 → 下载(无登录、无签名)
# --------------------------------------------------------------------------- #
def _fetch_douyin_html(share_url: str, attempt: int = 0) -> tuple[str, str]:
    """匿名打开抖音分享页拿 HTML;第 attempt 次重试换一个移动端 UA。

    失败抛原始异常(不在此转人话),由上层 _douyin_play_url 在所有 UA 都试过后统一报错。
    """
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_SSL_CTX)
    )
    opener.addheaders = [("User-Agent", _pick_ua(_MOBILE_UAS, attempt)),
                         ("Accept-Encoding", "identity")]
    with opener.open(share_url, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore"), resp.geturl()


def _extract_embedded_json(html: str) -> list[dict]:
    """从页面里抠出内嵌的 window.X = {...} JSON,逐个解析成 dict。

    抖音改版常换全局变量名(_ROUTER_DATA / __INITIAL_STATE__ / …),
    与其死认一个,不如把所有内嵌 JSON 都拿来挨个试(按优先级去重)。
    """
    raw_blobs: list[str] = []
    for pat in (_ROUTER_DATA_RE, _INITIAL_STATE_RE, _ANY_WINDOW_JSON_RE):
        for m in pat.finditer(html):
            raw_blobs.append(m.group(1))

    out: list[dict] = []
    seen: set[str] = set()
    for b in raw_blobs:
        if b in seen:
            continue
        seen.add(b)
        try:
            data = json.loads(b)
        except Exception:  # noqa: BLE001 — 不是合法 JSON 就跳过,继续试下一个
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _douyin_page_work_id(share_url: str, html: str) -> str:
    """只从官方作品路径提取身份，不从任意数字或查询参数猜测。"""

    candidates = [str(share_url or "")]
    candidates.extend(
        match.group(1) for match in _DOUYIN_CANONICAL_RE.finditer(str(html or ""))
    )
    for candidate in candidates:
        if not is_douyin(candidate):
            continue
        try:
            path = urlsplit(candidate).path
        except ValueError:
            continue
        match = _DOUYIN_WORK_PATH_RE.fullmatch(path)
        if match:
            return match.group(1)
    return ""


def _douyin_public_detail_fallback(
    work_id: str,
    progress: ProgressCallback | None = None,
) -> tuple[str, str, dict, dict, str, str, dict, bool]:
    """复用主页采集的匿名公开详情链，返回单链接解析所需字段。"""

    from ..collect.douyin_fetch import fetch_item, fetch_item_with_page_fallback

    identity = _douyin_work_id(work_id)
    if not identity:
        raise RuntimeError("抖音作品身份无效")
    item = fetch_item_with_page_fallback(identity, progress) if progress is not None else fetch_item(identity)
    if item.work_id != identity:
        raise RuntimeError("抖音作品详情身份不一致")
    if item.is_image:
        return "", item.title, {}, {}, identity, item.cover_url, {}, True

    play_url = str(item.play_url or "").replace("playwm", "play")
    if not is_douyin_video_cdn(play_url):
        raise RuntimeError("抖音公开详情返回了非允许媒体地址")

    stats = {
        "like": item.like,
        "favorite": item.collect,
        "share": item.share,
        "comment": item.comment,
    }
    author = {"name": item.author_name}
    if item.author_id:
        author["sec_uid"] = item.author_id
    facts = {
        "work_id": identity,
        "create_time": item.create_time,
        "duration_ms": item.duration_ms,
    }
    cover_url = TranscriptCover(url=item.cover_url).url
    return (
        play_url,
        item.title,
        stats,
        author,
        identity,
        cover_url,
        facts,
        False,
    )


def _scan_douyin_blobs(blobs: list[dict]) -> tuple[str, str, dict, dict]:
    """在所有内嵌 JSON 里找:播放直链 / 文案 / 互动数据 / 作者。多字段候选,改个名也不至于全丢。"""
    desc = ""
    urls: list[str] = []
    stats_raw: dict = {}
    author: dict = {}
    for data in blobs:
        for path, value in _walk(data):
            if not desc and path.endswith("/desc") and isinstance(value, str) and value:
                desc = value
            # 作者昵称/sec_uid 在 item 的 author 对象里(/music/author 是字符串,不会误中)
            if path.endswith("/author/nickname") and isinstance(value, str) and value:
                author.setdefault("name", value)
            if path.endswith("/author/sec_uid") and isinstance(value, str) and value:
                author.setdefault("sec_uid", value)
            if (
                any(k in path for k in _DOUYIN_ADDR_KEYS)
                and "url_list" in path
                and isinstance(value, str)
                and value.startswith("http")
            ):
                urls.append(value)
            for out_k, src_keys in _DOUYIN_STAT.items():
                if isinstance(value, int) and any(
                    path.endswith(f"/statistics/{sk}") for sk in src_keys
                ):
                    stats_raw.setdefault(out_k, value)

    missing = [k for k in _DOUYIN_STAT if k not in stats_raw]
    if missing:
        _debug(f"抖音互动数据未命中字段:{missing}(候选名可能已变)")
    if "name" not in author:
        _debug("抖音作者昵称未命中(author/nickname 可能已改名)")
    # play_count 抖音常返回 0,丢掉避免误导
    stats = {k: v for k, v in stats_raw.items() if not (k == "view" and v == 0)}
    play = urls[0] if urls else ""
    return play, desc, stats, author


def _first_https_url(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    urls = value.get("url_list")
    if not isinstance(urls, list):
        return ""
    for candidate in urls:
        cover = TranscriptCover(url=candidate)
        if cover.url:
            return cover.url
    return ""


def _douyin_work_id(value: object) -> str:
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return ""
    work_id = str(value).strip()
    return work_id if work_id.isascii() and work_id.isdigit() else ""


def _douyin_media_referer(value: object) -> str:
    """只从严格数字作品身份构造官方来源页，拒绝调用方注入任意请求头。"""

    work_id = _douyin_work_id(value)
    return f"https://www.douyin.com/video/{work_id}" if work_id else ""


def _douyin_media_referer_from_url(value: object) -> str:
    """只接受 Lulu 自身可构造出的标准抖音作品页。"""

    raw = str(value or "")
    if not raw or any(ord(char) <= 32 or ord(char) == 127 for char in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "www.douyin.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    match = _DOUYIN_WORK_PATH_RE.fullmatch(parsed.path)
    return _douyin_media_referer(match.group(1)) if match else ""


def _douyin_cover_identity(blobs: list[dict]) -> tuple[str, str]:
    """从同一个作品对象中绑定 aweme_id 与封面，避免跨对象拼接身份。"""

    def walk(value: object) -> tuple[str, str]:
        if isinstance(value, dict):
            work_id = _douyin_work_id(value.get("aweme_id"))
            video = value.get("video")
            if work_id and isinstance(video, dict):
                from ..collect.models import _douyin_cover_url
                return work_id, TranscriptCover(url=_douyin_cover_url(video)).url
            for child in value.values():
                found = walk(child)
                if found[0]:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found[0]:
                    return found
        return "", ""

    for blob in blobs:
        found = walk(blob)
        if found[0]:
            return found
    return "", ""


def _douyin_work_facts(blobs: list[dict]) -> dict:
    """从同一个抖音作品对象提取长期归档需要的客观事实。"""

    def integer(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    def walk(value: object) -> dict:
        if isinstance(value, dict):
            work_id = _douyin_work_id(value.get("aweme_id"))
            if work_id:
                video = value.get("video")
                video = video if isinstance(video, dict) else {}
                return {
                    "work_id": work_id,
                    "create_time": integer(value.get("create_time")),
                    "duration_ms": integer(video.get("duration")),
                }
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return {}

    for blob in blobs:
        found = walk(blob)
        if found:
            return found
    return {}


def _apply_douyin_work_meta(
    meta: dict | None,
    *,
    share_url: str,
    play_url: str,
    facts: dict | None = None,
    fallback_work_id: str = "",
) -> None:
    """把抖音解析结果写成单篇转录、飞书和 Obsidian 共用的元数据。"""

    if meta is None or not is_douyin(share_url):
        return
    facts = facts if isinstance(facts, dict) else {}
    work_id = _douyin_work_id(facts.get("work_id") or fallback_work_id)
    if work_id:
        canonical = f"https://www.douyin.com/video/{work_id}"
        meta["platform"] = "douyin"
        meta["work_id"] = work_id
        meta["webpage_url"] = canonical
        meta["original_url"] = canonical
    if play_url:
        meta["media_url"] = play_url
        meta["play_url"] = play_url
    create_time = facts.get("create_time")
    if isinstance(create_time, int) and create_time > 0:
        meta["timestamp"] = create_time
        cst = timezone(timedelta(hours=8))
        meta["upload_date"] = datetime.fromtimestamp(
            create_time, cst
        ).strftime("%Y%m%d")
    duration_ms = facts.get("duration_ms")
    if isinstance(duration_ms, int) and duration_ms >= 0:
        meta["duration_ms"] = duration_ms
        meta["duration"] = duration_ms / 1000


def _douyin_author(sec_uid: str) -> dict:
    """匿名调旧版公开接口补作者粉丝量等(无登录/无签名,见 ADR-0009)。

    该旧接口随时可能下线 —— best-effort:失败记 debug 日志、返回空 dict。
    """
    api = f"https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid={sec_uid}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": _pick_ua(_MOBILE_UAS, 0)})
        with urllib.request.urlopen(req, timeout=12, context=_SSL_CTX) as resp:
            ui = (json.loads(resp.read().decode("utf-8", "ignore")) or {}).get("user_info") or {}
    except Exception as exc:  # noqa: BLE001
        _debug(f"抖音作者信息接口失败(旧接口可能已下线):{exc}")
        return {}
    out: dict = {}
    if isinstance(ui.get("mplatform_followers_count"), int) and ui["mplatform_followers_count"] > 0:
        out["fans"] = ui["mplatform_followers_count"]
    tf = str(ui.get("total_favorited") or "")
    if tf.isdigit() and int(tf) > 0:
        out["likes_total"] = int(tf)
    if isinstance(ui.get("aweme_count"), int) and ui["aweme_count"] > 0:
        out["works"] = ui["aweme_count"]
    if isinstance(ui.get("unique_id"), str) and ui["unique_id"]:
        out["handle"] = ui["unique_id"]
    if not out:
        _debug("抖音作者信息接口返回但字段未命中(结构可能已变)")
    return out


def _douyin_play_url(
    share_url: str, progress: ProgressCallback, meta: dict | None = None
) -> tuple[str, str, dict, dict, str, str]:
    """返回 (播放直链, 文案, 互动数据, 作者, 作品 ID, HTTPS 封面)。

    多策略回退:已知变量名 → 任意内嵌 JSON → 正则直抓播放链。
    全部失败才报「抖音改版,需更新」,并和「图文作品/非公开」区分开。
    """
    progress("解析抖音分享页…", -1.0)

    play = desc = ""
    stats: dict = {}
    author: dict = {}
    cover_work_id = ""
    cover_url = ""
    last_html = ""
    matched_facts: dict = {}
    last_exc: Exception | None = None
    detail_exc: Exception | None = None
    detail_attempted = False
    is_image = False
    # 每次重试换一个移动端 UA:某个 UA 拿到的是拦截页/空页时,换一个往往能解出来。
    for attempt in range(len(_MOBILE_UAS)):
        progress("解析抖音作品…", -1.0)
        try:
            html, resolved_url = _fetch_douyin_html(share_url, attempt=attempt)
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _debug(f"抖音分享页抓取失败(UA #{attempt}):{exc}")
            continue
        last_html = html

        blobs = _extract_embedded_json(html)
        play, desc, stats, author = _scan_douyin_blobs(blobs) if blobs else ("", "", {}, {})
        cover_work_id, cover_url = (
            _douyin_cover_identity(blobs) if blobs else ("", "")
        )
        matched_facts = _douyin_work_facts(blobs) if blobs else {}
        if not play:
            # 兜底:JSON 里没找到,直接从页面正则抓播放直链
            m = _DOUYIN_PLAY_URL_RE.search(html)
            if m:
                play = m.group(0)
        if play:
            break
        work_id = _douyin_page_work_id(share_url, html) or _douyin_page_work_id(resolved_url, "")
        if work_id and not detail_attempted:
            detail_attempted = True
            try:
                (
                    play,
                    desc,
                    stats,
                    author,
                    cover_work_id,
                    cover_url,
                    matched_facts,
                    is_image,
                ) = _douyin_public_detail_fallback(work_id, progress)
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001
                detail_exc = exc
                _debug(f"抖音匿名公开详情回退失败:{exc}")
            if play or is_image:
                break
        # 确定是图文帖 → 换 UA 也没用,立即停,别白跑请求
        if any(k in html for k in ("image_post", "aweme_type:68", '"images"')):
            is_image = True
            break
        _debug(f"抖音 UA #{attempt} 未解析出播放直链,换 UA 重试…")

    if not play and not detail_attempted:
        work_id = _douyin_page_work_id(share_url, last_html)
        if work_id:
            detail_attempted = True
            try:
                (
                    play,
                    desc,
                    stats,
                    author,
                    cover_work_id,
                    cover_url,
                    matched_facts,
                    is_image,
                ) = _douyin_public_detail_fallback(work_id, progress)
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001
                detail_exc = exc
                _debug(f"抖音匿名公开详情回退失败:{exc}")

    if not play:
        if not last_html and last_exc is not None:
            # 所有 UA 都没能打开页面 → 网络/链接问题
            raise RuntimeError(
                "打不开抖音分享页 —— 多半是网络不稳定,或链接已失效。\n"
                "请检查链接是否完整、网络是否正常,稍后再点一次重试。"
            ) from last_exc
        if is_image:
            raise RuntimeError(
                "这条抖音作品是图文 / 图片帖,本身没有可转录的声音。\n"
                "请换一条「视频」作品再试。"
            )
        if detail_attempted and detail_exc is not None:
            from ..collect.douyin_fetch import DouyinCollectError
            if isinstance(detail_exc, DouyinCollectError):
                raise detail_exc
            raise RuntimeError(
                "这条抖音链接有效，但公开作品详情暂时没有返回可用视频。\n"
                "请稍后重试；如果同一链接持续失败，请更新 Lulu 或把链接反馈给开发者。"
            ) from detail_exc
        raise RuntimeError(_DOUYIN_CHANGED_MSG)

    # playwm 是带水印版,play 为无水印版;音频一致,优先无水印
    play = play.replace("playwm", "play")
    _apply_douyin_work_meta(
        meta,
        share_url=share_url,
        play_url=play,
        facts=matched_facts,
        fallback_work_id=cover_work_id,
    )
    return play, desc, stats, author, cover_work_id, cover_url


def safe_filename(name: str, fallback: str = "media", maxlen: int = 60) -> str:
    """把任意文案清洗成合法文件名(去非法字符、换行、首尾点/空格,限长)。"""
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]", " ", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    name = name[:maxlen].strip()
    return name or fallback


def _download_douyin(share_url: str, work_dir: Path, progress: ProgressCallback,
                     meta: dict | None = None) -> Path:
    (
        play_url,
        desc,
        stats,
        author,
        cover_work_id,
        cover_url,
    ) = _douyin_play_url(share_url, progress, meta)
    if meta is not None:
        _apply_douyin_work_meta(
            meta,
            share_url=share_url,
            play_url=play_url,
            fallback_work_id=cover_work_id,
        )
        if desc:
            meta["title"] = desc
        if stats:
            meta["stats"] = stats
        if author.get("sec_uid"):
            author.update(_douyin_author(author.pop("sec_uid")))
        if author.get("name"):
            meta["author"] = author
            meta.setdefault("uploader", author["name"])  # 与 yt-dlp 路径的「作者」字段对齐
        trusted_source = is_douyin(share_url)
        trusted_work_id = (
            _douyin_work_id(cover_work_id) if trusted_source else ""
        )
        trusted_cover_url = (
            TranscriptCover(url=cover_url).url if trusted_source else ""
        )
        complete_identity = bool(trusted_work_id and trusted_cover_url)
        meta.update(
            TranscriptCover(
                platform="douyin" if complete_identity else "",
                work_id=trusted_work_id if complete_identity else "",
                url=trusted_cover_url,
                status="url_only" if trusted_cover_url else "not_requested",
            ).merge_meta(meta)
        )
    if desc:
        progress(f"作品:{desc[:30]}…", -1.0)
    # 用作品文案命名,保留时文件名更有意义
    dst = work_dir / f"{safe_filename(desc, 'douyin_media')}.mp4"
    last_error = ""
    for attempt in range(len(_MOBILE_UAS)):
        progress("准备下载抖音作品…", -1.0)
        if dst.exists():
            dst.unlink()
        try:
            validator = (
                is_douyin_video_cdn
                if is_douyin_video_cdn(play_url)
                else None
            )
            _stream_download(
                play_url,
                dst,
                progress,
                ua_offset=attempt,
                url_validator=validator,
                referer=_douyin_media_referer(cover_work_id),
            )
            _ensure_downloaded_media_looks_valid(dst)
            return dst
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            _debug(f"抖音媒体下载后校验失败(#{attempt}):{exc}")
    raise RuntimeError(
        "这条抖音视频下载下来的媒体文件不完整或不可解码。\n"
        "可能是抖音 CDN 返回了异常内容、作品受限,或临时网络中断。请稍后重试;如果同一链接持续失败,把链接反馈给开发者更新解析。"
        + (f"\n技术线索:{last_error[:160]}" if last_error else "")
    )


def download_douyin_video_cdn(
    url: str,
    work_dir: Path,
    progress: ProgressCallback | None = None,
    *,
    work_id: str,
) -> Path:
    """把采集得到的抖音视频直链保存为固定短文件名。"""

    if not is_douyin_video_cdn(url):
        raise RuntimeError("不是允许的抖音视频地址")
    referer = _douyin_media_referer(work_id)
    if not referer:
        raise RuntimeError("抖音作品身份无效，已停止下载")
    progress = progress or (lambda *_: None)
    work_dir.mkdir(parents=True, exist_ok=True)
    destination = work_dir / "douyin-video.mp4"
    if destination.exists():
        destination.unlink()
    try:
        _stream_download(
            url,
            destination,
            progress,
            max_retries=2,
            url_validator=is_douyin_video_cdn,
            referer=referer,
        )
        _ensure_downloaded_media_looks_valid(destination)
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def _stream_download(
    url: str,
    dst: Path,
    progress: ProgressCallback,
    max_retries: int = 6,
    ua_offset: int = 0,
    url_validator: Callable[[str], bool] | None = None,
    referer: str = "",
) -> Path:
    """分块下载,遇到中断用 Range 续传。抖音 CDN 支持 Range。"""
    if url_validator is not None and not url_validator(url):
        raise RuntimeError("媒体地址不在允许范围")
    if referer and not _douyin_media_referer_from_url(referer):
        raise RuntimeError("媒体来源页不在允许范围")
    opener = (
        urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_SSL_CTX),
            _ValidatedMediaRedirectHandler(url_validator),
        )
        if url_validator is not None
        else None
    )
    pos = 0
    total: int | None = None
    for attempt in range(max_retries):
        progress("连接媒体服务器…" if not pos else "重新连接媒体服务器…", -1.0)
        # 显式要求不压缩(identity):媒体流本就不该被 gzip,避免任何解压环节
        # 出现 "incorrect header check" 之类错误。重试时换一个移动端 UA。
        headers = {
            "User-Agent": _pick_ua(_MOBILE_UAS, ua_offset + attempt),
            "Accept-Encoding": "identity",
        }
        if referer:
            headers["Referer"] = referer
        if pos:
            headers["Range"] = f"bytes={pos}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            response = (
                opener.open(req, timeout=10)
                if opener is not None
                else urllib.request.urlopen(req, timeout=10, context=_SSL_CTX)
            )
            with response as resp:
                if (
                    url_validator is not None
                    and not url_validator(resp.geturl())
                ):
                    raise RuntimeError("媒体响应离开了允许范围")
                content_range = resp.headers.get("Content-Range", "")
                range_match = re.fullmatch(
                    r"bytes\s+(\d+)-(\d+)/(\d+)", content_range.strip()
                )
                # 有些 CDN/代理会忽略 Range 并重发整个文件。此时必须
                # 从头覆盖，否则把完整文件追加到 .part 后会静默损坏媒体。
                if pos and range_match:
                    if int(range_match.group(1)) != pos:
                        raise RuntimeError("续传服务器返回了错误的字节起点")
                elif pos and not range_match:
                    pos = 0
                    total = None

                cl = resp.headers.get("Content-Length")
                if range_match:
                    total = int(range_match.group(3))
                elif cl and cl.isdigit():
                    total = int(cl)
                mode = "ab" if pos else "wb"
                with open(dst, mode) as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        pos += len(chunk)
                        if total:
                            fraction = min(1.0, pos / total)
                            progress(
                                "下载音视频中… "
                                f"{int(fraction * 100)}% · "
                                f"{_progress_bytes(pos)} / {_progress_bytes(total)}",
                                fraction,
                            )
                        else:
                            progress(f"下载音视频中… {pos // 1024} KB", -1.0)
            # 读到自然 EOF 且大小达标 → 完成
            if total is None or pos >= total:
                break
        except InterruptedError:
            raise
        except IncompleteRead:
            pos = dst.stat().st_size if dst.exists() else 0
            continue
        except Exception as exc:  # noqa: BLE001
            if attempt == max_retries - 1:
                raise RuntimeError(f"下载抖音媒体失败:{exc}") from exc
            pos = dst.stat().st_size if dst.exists() else 0

    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("抖音媒体下载完成但文件为空。")
    if total is not None and dst.stat().st_size < total:
        raise RuntimeError(
            "抖音媒体下载文件不完整，请重试。"
        )
    progress("下载完成", 1.0)
    return dst


def _ensure_downloaded_media_looks_valid(path: Path) -> None:
    """抖音直链偶发返回异常内容/半截 mp4;先挡住,别把 ffmpeg 内部日志直接甩给用户。"""
    data = path.read_bytes()[:4096]
    if not data:
        raise RuntimeError("媒体文件为空")
    stripped = data.lstrip()[:80].lower()
    if stripped.startswith((b"<html", b"<!doctype", b"{", b"[")):
        raise RuntimeError("下载到的不是媒体文件")
    media_magic = data.startswith((b"ID3", b"OggS", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
    if b"ftyp" not in data[:256] and not media_magic:
        raise RuntimeError("媒体文件头异常")


def _walk(obj, path: str = ""):
    """深度遍历嵌套 dict/list,产出 (路径, 叶子值)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


# --------------------------------------------------------------------------- #
# 通用链接:交给 yt-dlp(不带 cookie、不登录)
# --------------------------------------------------------------------------- #
def _fill_meta(meta: dict, info: dict, src_url: str | None = None) -> None:
    """从 yt-dlp 的 info 提取元数据填入 meta。

    标题/简介/作者/日期/时长/封面/标签/章节/字幕可用性等客观信息;
    互动数据与作者公开信息按 ADR-0009(用户显式开启,匿名公开、快照、best-effort)。
    """
    def put(key, *names):
        for n in names:
            v = info.get(n)
            if v not in (None, "", [], {}):
                meta[key] = v
                return

    put("title", "title")
    put("description", "description")
    put("uploader", "uploader", "channel", "creator", "uploader_id")
    put("upload_date", "upload_date")            # YYYYMMDD
    put("duration", "duration")                  # 秒
    put("thumbnail", "thumbnail")
    put("webpage_url", "webpage_url", "original_url")
    put("language", "language")
    tags = info.get("tags") or info.get("categories")
    if tags:
        meta["tags"] = [str(t) for t in tags][:12]
    if info.get("chapters"):
        meta["chapters"] = [
            {"title": c.get("title"), "start": c.get("start_time")}
            for c in info["chapters"] if c.get("title")
        ]
    # 平台是否带字幕(只记可用性,不在此下载;字幕直转是另一个功能)
    subs = list((info.get("subtitles") or {}).keys())
    autos = list((info.get("automatic_captions") or {}).keys())
    if subs:
        meta["subtitles"] = subs[:8]
    if autos:
        meta["auto_captions"] = autos[:8]

    # 互动数据 + 作者公开信息(用户显式开启,见 ADR-0009)。匿名公开数据,转录那一刻的快照。
    stats: dict = {}
    author: dict = {}
    for out_k, in_k in (("like", "like_count"), ("comment", "comment_count"),
                        ("view", "view_count"), ("favorite", "save_count"),
                        ("share", "repost_count")):
        v = info.get(in_k)
        if type(v) is int and v >= 0:
            stats[out_k] = v
    # 通用路径(YouTube 等):作者昵称/粉丝就在已抓的 info 里,零额外请求
    if meta.get("uploader"):
        author["name"] = str(meta["uploader"])
    if isinstance(info.get("channel_follower_count"), int) and info["channel_follower_count"] > 0:
        author["fans"] = info["channel_follower_count"]
    wp = info.get("webpage_url") or ""
    if "bilibili.com" in wp:           # B站额外调公开 API 补 收藏/投币 + 作者粉丝/认证(yt-dlp 不给)
        b_stats, b_author = _bilibili_stats_author(wp)
        stats.update(b_stats)
        author.update(b_author)
    if stats:
        meta["stats"] = stats
    if author.get("name"):
        meta["author"] = author
        meta.setdefault("uploader", author["name"])

    platform, work_id = _trusted_ytdlp_identity(info, src_url)
    cover_url = normalize_official_cover_url(platform, info.get("thumbnail"))
    meta.update(
        TranscriptCover(
            platform=platform,
            work_id=work_id,
            url=cover_url,
            status="url_only" if cover_url else "not_requested",
        ).merge_meta(meta)
    )


def _trusted_ytdlp_identity(
    info: dict, src_url: str | None = None
) -> tuple[str, str]:
    """只接受已知 extractor 与官方作品域名同时吻合的稳定 ID。"""

    extractor = str(
        info.get("extractor_key") or info.get("extractor") or ""
    ).casefold()
    raw_url = str(
        info.get("webpage_url")
        or info.get("original_url")
        or src_url
        or ""
    )
    try:
        hostname = (urlsplit(raw_url).hostname or "").casefold()
    except ValueError:
        hostname = ""

    def official(*domains: str) -> bool:
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in domains
        )

    platform = ""
    if extractor.startswith("youtube") and official(
        "youtube.com", "youtu.be"
    ):
        platform = "youtube"
    elif "bilibili" in extractor and official("bilibili.com", "b23.tv"):
        platform = "bilibili"
    elif (
        "xiaohongshu" in extractor or "rednote" in extractor
    ) and official("xiaohongshu.com", "xhslink.com"):
        platform = "xiaohongshu"

    raw_id = info.get("id")
    work_id = (
        str(raw_id).strip()
        if platform
        and raw_id is not None
        and not isinstance(raw_id, (dict, list, tuple, set, bool))
        else ""
    )
    return (platform, work_id) if work_id else ("", "")


# B站 stat 字段:out_key → 候选源字段名(stat 对象字段稳定,候选为防御性冗余)。
_BILI_STAT = {
    "like": ("like",), "favorite": ("favorite",), "share": ("share",),
    "comment": ("reply", "comment"), "coin": ("coin",),
    "view": ("view",), "danmaku": ("danmaku",),
}


def _bili_api(api: str) -> dict | None:
    """匿名调一个 B站公开接口,带退避重试一次;失败返回 None(best-effort)。"""
    import json

    data: dict | None = None
    for attempt in range(2):
        try:
            # 重试时换一个桌面 UA(频控/风控有时认 UA)
            headers = {"User-Agent": _pick_ua(_DESKTOP_UAS, attempt),
                       "Referer": "https://www.bilibili.com/"}
            req = urllib.request.Request(api, headers=headers)
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        except Exception as exc:  # noqa: BLE001
            _debug(f"B站接口请求异常(第 {attempt + 1} 次):{exc}")
            data = None
        if isinstance(data, dict) and data.get("code") == 0:
            return data
        if attempt == 0:  # code 非 0(频控/风控)或请求失败 → 退避后再试一次
            _debug(f"B站接口未就绪(code={(data or {}).get('code')}),退避重试…")
            time.sleep(1.0)
    _debug(f"B站接口放弃(code={(data or {}).get('code')}),空着不影响转录")
    return None


def _bilibili_stats_author(url: str, want_author: bool = True) -> tuple[dict, dict]:
    """匿名取 B站互动数据 + 作者公开信息(昵称/粉丝/认证/总获赞/稿件数,见 ADR-0009)。

    互动数据来自已要调的 view 接口(作者昵称/mid 顺手就有,零额外);
    粉丝等再调一次匿名 card 接口。两段各自 best-effort,失败返回空不阻塞。
    """
    import re as _re

    m = _re.search(r"(BV[0-9A-Za-z]+)", url)
    if not m:
        return {}, {}
    data = _bili_api(f"https://api.bilibili.com/x/web-interface/view?bvid={m.group(1)}")
    if data is None:
        return {}, {}

    d = data.get("data") or {}
    s = d.get("stat") or {}
    out = {}
    for out_k, cands in _BILI_STAT.items():
        for k in cands:
            if isinstance(s.get(k), int):
                out[out_k] = s[k]
                break

    author: dict = {}
    owner = d.get("owner") or {}
    if isinstance(owner.get("name"), str) and owner["name"]:
        author["name"] = owner["name"]
    mid = owner.get("mid")
    if mid and want_author:
        card = _bili_api(f"https://api.bilibili.com/x/web-interface/card?mid={mid}")
        cd = (card or {}).get("data") or {}
        c = cd.get("card") or {}
        if isinstance(c.get("fans"), int) and c["fans"] > 0:
            author["fans"] = c["fans"]
        official = (c.get("Official") or {}).get("title")
        if isinstance(official, str) and official:
            author["verify"] = official
        if isinstance(cd.get("like_num"), int) and cd["like_num"] > 0:
            author["likes_total"] = cd["like_num"]
        if isinstance(cd.get("archive_count"), int) and cd["archive_count"] > 0:
            author["works"] = cd["archive_count"]
    return out, author


def _bilibili_stats(url: str) -> dict:
    """兼容旧调用:只要互动数据,不多调作者 card 接口。"""
    return _bilibili_stats_author(url, want_author=False)[0]


def _download_via_ytdlp(url: str, work_dir: Path, progress: ProgressCallback,
                        meta: dict | None = None, link_media_mode: str = "audio") -> Path:
    import yt_dlp

    mode = "video" if str(link_media_mode or "").lower() in {"video", "audio_video", "audiovideo"} else "audio"
    label = "音视频" if mode == "video" else "音频"
    progress(f"正在解析链接并下载{label}…", -1.0)
    captured: dict[str, str] = {}

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if (
                isinstance(downloaded, (int, float))
                and isinstance(total, (int, float))
                and total > 0
            ):
                fraction = max(0.0, min(1.0, float(downloaded) / float(total)))
                progress(
                    f"下载{label}中… {int(fraction * 100)}% · "
                    f"{_progress_bytes(int(downloaded))} / {_progress_bytes(int(total))}",
                    fraction,
                )
            else:
                downloaded_text = (
                    f" · 已完成 {_progress_bytes(int(downloaded))}"
                    if isinstance(downloaded, (int, float))
                    else ""
                )
                progress(f"下载{label}中…{downloaded_text}", -1.0)
        elif d.get("status") == "finished":
            captured["filename"] = d.get("filename", "")
            progress(f"{label}下载完成，正在整理文件…", 1.0)

    # 带桌面浏览器 UA + identity 不压缩(科学上网代理常把 gzip 响应弄坏 →
    # "incorrect header check",禁压缩即可避开)
    http_headers = {"User-Agent": _DESKTOP_UA, "Accept-Encoding": "identity"}
    # B站 WAF 收紧:仅 UA 已不够(回 412),需再带 Referer + Origin 指向 bilibili.com
    # 才放行(匿名公开,仍无登录/cookie)。只对 B站加,别污染 YouTube/抖音等请求。
    if "bilibili.com" in url:
        http_headers["Referer"] = "https://www.bilibili.com/"
        http_headers["Origin"] = "https://www.bilibili.com"

    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    is_xiaohongshu = hostname in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}
    fmt = "bestvideo*+bestaudio/best" if mode == "video" else "bestaudio/best"
    if is_xiaohongshu and mode == "audio":
        fmt = "bestaudio/best[format_id!=direct]/best"
    from .download_runtime import public_download_options, DownloadFailure
    ydl_opts = {
        **public_download_options(url),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "http_headers": http_headers,
        "retries": 2,
        "extractor_retries": 1,
        "fragment_retries": 2,
        "socket_timeout": 15,
        "continuedl": True,
        "format": fmt,
        "outtmpl": str(work_dir / "%(id)s.%(format_id)s.%(ext)s"),
    }
    from .converter import _ffmpeg_exe
    ydl_opts["ffmpeg_location"] = _ffmpeg_exe()
    if mode == "video":
        ydl_opts["merge_output_format"] = "mp4"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if is_xiaohongshu:
                from .xiaohongshu import LuluXiaoHongShuIE
                ydl.add_info_extractor(LuluXiaoHongShuIE())
            info = ydl.extract_info(url, download=True)
            filename = _ytdlp_downloaded_path(info, ydl, work_dir, captured.get("filename"))
            if meta is not None:
                _fill_meta(meta, info, url)
    except InterruptedError:
        raise
    except Exception as exc:
        raise DownloadFailure.from_exception(exc) from None

    path = Path(filename).resolve()
    root = work_dir.resolve()
    if (not path.is_relative_to(root) or not path.is_file()
            or path.suffix in {".part", ".ytdl", ".tmp"} or path.stat().st_size == 0):
        raise DownloadFailure("CORE_DOWNLOAD_INCOMPLETE", "下载未产生完整媒体文件，请重试。")
    progress(f"{label}下载完成", 1.0)
    return path


def _ytdlp_downloaded_path(info: dict, ydl, work_dir: Path, captured_filename: str | None = None) -> str:
    """尽量取 yt-dlp 最终产物路径。

    音视频模式可能先下载分离音轨/视频轨,再合并成 mp4。progress hook 的 finished
    filename 有时是合并前的分片,所以优先看 yt-dlp 写回 info 的最终路径,再兜底按 id 扫目录。
    """
    candidates: list[str] = []
    for key in ("filepath", "_filename", "filename", "requested_filename"):
        val = info.get(key) if isinstance(info, dict) else None
        if isinstance(val, str) and val:
            candidates.append(val)
    if captured_filename:
        candidates.append(captured_filename)
    try:
        candidates.append(ydl.prepare_filename(info))
    except Exception:  # noqa: BLE001
        pass
    for item in info.get("requested_downloads") or []:
        if isinstance(item, dict):
            for key in ("filepath", "_filename", "filename"):
                val = item.get(key)
                if isinstance(val, str) and val:
                    candidates.append(val)
    for cand in candidates:
        path = Path(cand)
        if path.exists():
            return str(path)
    media_id = str(info.get("id") or "")
    if media_id:
        guesses = sorted(
            work_dir.glob(f"{media_id}.*"),
            key=lambda p: (p.suffix.lower() not in {".mp4", ".mkv", ".webm", ".m4a", ".mp3"}, p.name),
        )
        if guesses:
            return str(guesses[0])
    return candidates[0] if candidates else str(work_dir / "downloaded_media")
